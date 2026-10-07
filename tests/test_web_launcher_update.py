import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch
import io
import urllib.error

from netease_organizer.web_build import LaunchError, load_build
from netease_organizer.web_launcher import _request_update, launch_web, read_running_instance, write_instance


class LauncherUpdateTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.project = Path(self.temp.name)
        assets = self.project / 'frontend/dist'
        assets.mkdir(parents=True)
        (assets / 'index.html').write_text('<main>modern</main>', encoding='utf-8')
        self.build = load_build(self.project)
        self.controller = Mock(project=self.project)
        self.instance = 'a' * 32
        self.control = 'c' * 64

    def record(self, revision):
        write_instance(self.project, port=12345, pid=42, instance=self.instance,
                       revision=revision, control_key=self.control)

    def health(self, revision):
        return {'kind': 'netease-organizer-local', 'instance': self.instance,
                'revision': revision, 'update_status': 'none'}

    def test_same_version_reuses_without_account_or_update_requests(self):
        self.record(self.build.revision)
        with patch('netease_organizer.web_launcher._health_reader', return_value=self.health(self.build.revision)), \
                patch('netease_organizer.web_launcher._request_update') as update, \
                patch('netease_organizer.web_launcher._open_window') as window:
            self.assertEqual(launch_web(self.controller, build=self.build), 'http://127.0.0.1:12345/')
        update.assert_not_called()
        window.assert_called_once_with(self.project, 'http://127.0.0.1:12345/')
        self.assertEqual(self.controller.method_calls, [])

    def test_busy_and_review_required_preserve_existing_service_and_open_its_page(self):
        self.record('b' * 64)
        for reason in ('busy', 'review_required'):
            with self.subTest(reason=reason), \
                    patch('netease_organizer.web_launcher._health_reader', return_value=self.health('b' * 64)), \
                    patch('netease_organizer.web_launcher._request_update', return_value={'accepted': False, 'reason': reason}), \
                    patch('netease_organizer.web_launcher._serve_window') as serve, \
                    patch('netease_organizer.web_launcher._open_window') as window:
                self.assertEqual(launch_web(self.controller, build=self.build), 'http://127.0.0.1:12345/')
            serve.assert_not_called()
            window.assert_called_once()
        self.assertTrue((self.project / '.organizer/web-instance.json').is_file())
        self.assertEqual(self.controller.method_calls, [])

    def test_idle_update_waits_for_original_lease_then_starts_with_frozen_build(self):
        self.record('b' * 64)
        with patch('netease_organizer.web_launcher._health_reader', side_effect=[self.health('b' * 64), OSError()]), \
                patch('netease_organizer.web_launcher._request_update', return_value={'accepted': True, 'reason': 'stopping'}) as update, \
                patch('netease_organizer.web_launcher._serve_window', return_value='http://127.0.0.1:54321/') as serve:
            self.assertEqual(launch_web(self.controller, build=self.build, open_window=False), 'http://127.0.0.1:54321/')
        update.assert_called_once()
        self.assertIs(serve.call_args.kwargs['build'], self.build)

    def test_legacy_running_server_is_not_claimed_as_current_or_killed(self):
        write_instance(self.project, port=12345, pid=42, instance=self.instance)
        with patch('netease_organizer.web_launcher._health_reader', return_value={'kind': 'netease-organizer-local', 'instance': self.instance}), \
                patch('netease_organizer.web_launcher._request_update') as update, \
                self.assertRaises(LaunchError) as error:
            launch_web(self.controller, build=self.build, open_window=False)
        self.assertEqual(error.exception.code, 'legacy_instance')
        update.assert_not_called()

    def test_record_revision_must_match_health_before_any_control_request(self):
        self.record('b' * 64)
        with patch('netease_organizer.web_launcher._health_reader', return_value=self.health('d' * 64)):
            self.assertIsNone(read_running_instance(self.project))

    def test_mid_import_source_change_does_not_register_or_start_a_service(self):
        def rebuild():
            (self.project / 'frontend/dist/index.html').write_bytes(b'<main>changed</main>')
        with patch('netease_organizer.web_launcher.preload_runtime', side_effect=rebuild), \
                patch('netease_organizer.web_launcher._serve_window') as serve, \
                self.assertRaises(LaunchError) as error:
            launch_web(self.controller, build=self.build, open_window=False)
        self.assertEqual(error.exception.code, 'build_changed')
        serve.assert_not_called()

    def test_private_client_sends_only_exact_instance_and_revision_to_verified_loopback(self):
        self.record('b' * 64)
        record = {'url': 'http://127.0.0.1:12345/', 'instance': self.instance, 'control_key': self.control}
        opener = Mock()
        response = io.BytesIO(b'{"accepted":true,"reason":"stopping"}')
        response.code = 202
        opener.open.return_value = response
        with patch('netease_organizer.web_launcher.urllib.request.build_opener', return_value=opener):
            self.assertEqual(_request_update(record, self.build.revision), {'accepted': True, 'reason': 'stopping'})
        request = opener.open.call_args.args[0]
        self.assertEqual(request.full_url, record['url'] + 'api/update')
        self.assertEqual(request.get_header('X-organizer-control'), self.control)
        self.assertEqual(json.loads(request.data), {'instance': self.instance, 'revision': self.build.revision})
        self.assertNotIn(self.control.encode(), request.data)

    def test_unexpected_private_responses_are_fixed_errors_and_never_retried(self):
        record = {'url': 'http://127.0.0.1:12345/', 'instance': self.instance, 'control_key': self.control}
        cases = ((200, b'{"accepted":true,"reason":"stopping"}'),
                 (202, b'{"accepted":false,"reason":"stopping"}'),
                 (409, b'{"accepted":false,"reason":"instance_mismatch","detail":"SECRET"}'),
                 (202, b'SECRET' * 500), (202, b'not json SECRET'))
        for status, body in cases:
            opener = Mock()
            response = io.BytesIO(body)
            response.code = status
            opener.open.return_value = response
            with self.subTest(status=status, length=len(body)), \
                    patch('netease_organizer.web_launcher.urllib.request.build_opener', return_value=opener), \
                    self.assertRaises(LaunchError) as error:
                _request_update(record, self.build.revision)
            self.assertEqual(error.exception.code, 'instance_busy')
            self.assertNotIn('SECRET', str(error.exception))
            opener.open.assert_called_once()

    def test_http_conflict_preserves_the_existing_instance(self):
        record = {'url': 'http://127.0.0.1:12345/', 'instance': self.instance, 'control_key': self.control}
        opener = Mock()
        opener.open.side_effect = urllib.error.HTTPError(record['url'], 409, 'conflict', {},
            io.BytesIO(b'{"accepted":false,"reason":"review_required"}'))
        with patch('netease_organizer.web_launcher.urllib.request.build_opener', return_value=opener):
            self.assertEqual(_request_update(record, self.build.revision),
                             {'accepted': False, 'reason': 'review_required'})

    def test_local_metadata_permission_failure_has_specific_recovery_reason(self):
        with patch('netease_organizer.web_launcher.instance_lock', side_effect=PermissionError('SECRET')), \
                self.assertRaises(LaunchError) as error:
            launch_web(self.controller, build=self.build, open_window=False)
        self.assertEqual(error.exception.code, 'local_permission_denied')
        self.assertNotIn('SECRET', str(error.exception))


if __name__ == '__main__':
    unittest.main()
