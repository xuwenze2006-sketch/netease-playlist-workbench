import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from netease_organizer.web_launcher import (InstanceBusy, browser_command, instance_lock,
                                           read_running_instance, write_instance)


class WebLauncherTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.project = Path(self.temp.name).resolve()

    def tearDown(self):
        self.temp.cleanup()

    def test_app_browser_uses_only_loopback_url_and_project_profile(self):
        browser = self.project / 'edge/msedge.exe'
        browser.parent.mkdir()
        browser.touch()
        command = browser_command(self.project, 'http://127.0.0.1:12345/', executable=browser)
        self.assertEqual(command[0], str(browser))
        self.assertIn('--app=http://127.0.0.1:12345/', command)
        profile = next(value for value in command if value.startswith('--user-data-dir='))
        self.assertTrue(Path(profile.split('=', 1)[1]).is_relative_to(self.project))
        self.assertNotIn('--remote-debugging-port', ' '.join(command))

    def test_external_urls_credentials_and_bad_ports_rejected_before_opening(self):
        for url in ('https://music.163.com/', 'http://evil.test/', 'http://127.0.0.1.evil.test/',
                    'http://user@127.0.0.1:1234/', 'http://127.0.0.1:0/', 'http://127.0.0.1:70000/',
                    'http://127.0.0.1:1234/?key=SECRET'):
            with self.subTest(url=url), self.assertRaises(ValueError):
                browser_command(self.project, url)

    def test_instance_record_contains_no_session_or_account_credentials(self):
        write_instance(self.project, port=12345, instance='b'*32, pid=42)
        text = (self.project / '.organizer/web-instance.json').read_text(encoding='utf-8')
        self.assertEqual(set(json.loads(text)), {'port', 'instance', 'pid'})
        self.assertNotIn('session', text)

    def test_reuse_requires_exact_health_identity_and_never_calls_account(self):
        write_instance(self.project, port=12345, instance='b'*32, pid=42)
        check = Mock(return_value={'kind': 'netease-organizer-local', 'instance': 'b'*32})
        existing = read_running_instance(self.project, health_reader=check)
        self.assertEqual(existing, 'http://127.0.0.1:12345/')
        check.assert_called_once_with('http://127.0.0.1:12345/health')
        check.return_value = {'kind': 'another-local-server', 'instance': 'b'*32}
        self.assertIsNone(read_running_instance(self.project, health_reader=check))

    def test_broken_or_foreign_instance_record_is_not_opened(self):
        path = self.project / '.organizer/web-instance.json'
        path.parent.mkdir()
        checker = Mock()
        for data in ({'port': 12345, 'instance': 'SECRET', 'pid': 42},
                     {'port': True, 'instance': 'b'*32, 'pid': 42},
                     {'port': 12345, 'instance': 'b'*32, 'pid': -1}):
            path.write_text(json.dumps(data), encoding='utf-8')
            self.assertIsNone(read_running_instance(self.project, health_reader=checker))
        checker.assert_not_called()

    def test_only_one_local_service_can_hold_instance_lock_and_close_releases_it(self):
        with instance_lock(self.project):
            with self.assertRaises(InstanceBusy):
                with instance_lock(self.project):
                    self.fail('Concurrent server acquired the same project lock')
        # The lock file stays, while the OS lease is released. No manual cleanup.
        with instance_lock(self.project):
            pass

    def test_instance_lock_releases_even_when_startup_fails(self):
        with self.assertRaisesRegex(RuntimeError, 'fixture startup failure'):
            with instance_lock(self.project):
                raise RuntimeError('fixture startup failure')
        with instance_lock(self.project):
            pass
