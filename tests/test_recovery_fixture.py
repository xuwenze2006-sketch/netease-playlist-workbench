"""Recovery browser fixtures use real local HTTP/service and an owned fake account."""

import http.client
import json
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path

from scripts import serve_web_fixture as fixture
from netease_organizer.web_server import shutdown_server


class RecoveryFixtureTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.project = Path(self.directory.name)
        self.assets = self.project / 'frontend/dist'
        self.assets.mkdir(parents=True)
        (self.assets / 'index.html').write_text(
            '<meta name="organizer-session" content="__ORGANIZER_SESSION__">', encoding='utf-8')

    def start(self, **options):
        factory = getattr(fixture, 'create_fixture', None)
        self.assertTrue(callable(factory), 'Recovery fixture factory is not implemented')
        server = factory(self.project / 'owned-fake-account', assets=self.assets, **options)
        serving = threading.Thread(target=server.serve_forever)
        serving.start()

        def cleanup():
            shutdown_server(server)
            serving.join(3)
            self.assertFalse(serving.is_alive(), 'Owned fake HTTP service did not stop')

        self.addCleanup(cleanup)
        return server

    def request(self, server, method, path, value=None):
        connection = http.client.HTTPConnection('127.0.0.1', server.server_address[1], timeout=3)
        headers = {'X-Organizer-Session': server.application.session}
        if value is not None:
            headers['Content-Type'] = 'application/json'
        try:
            connection.request(method, path, json.dumps(value) if value is not None else None, headers)
            response = connection.getresponse()
            return response.status, json.loads(response.read())
        finally:
            connection.close()

    def action(self, server, action, payload=None):
        status, accepted = self.request(server, 'POST', '/api/actions',
                                        {'action': action, 'payload': payload or {}})
        self.assertEqual(status, 202)
        self.assertTrue(accepted['accepted'])
        self.assertTrue(server.application.wait_for_idle(3), 'Fake action did not finish')
        status, state = self.request(server, 'GET', '/api/state')
        self.assertEqual(status, 200)
        return state['job']['result']

    def writes(self, server):
        status, value = self.request(server, 'GET', '/fixture/state')
        self.assertEqual(status, 200)
        self.assertEqual(set(value), {'kind', 'fake_writes', 'count'})
        self.assertEqual(value['kind'], 'fake_account_state')
        return value

    def test_default_ready_fixture_keeps_confirmed_rename_and_no_startup_writes(self):
        server = self.start()
        self.assertEqual(self.writes(server)['count'], 0)
        status, state = self.request(server, 'GET', '/api/state')
        self.assertEqual(status, 200)
        self.assertTrue(state['connection']['installed'])
        self.assertTrue(state['connection']['configured'])
        self.assertEqual(len(state['data']['playlists']), 4)
        result = self.action(server, 'rename')
        self.assertEqual(result['status'], 'completed')
        self.assertEqual(result['completed_count'], 1)
        self.assertTrue(result['applied_to_account'])
        self.assertTrue(result['outcome_known'])
        self.assertEqual(self.writes(server),
                         {'kind': 'fake_account_state', 'fake_writes': ['updateName'], 'count': 1})
        self.assertNotIn('PRIVATE-TOKEN', json.dumps(state))

    def test_connection_choices_expose_only_simulated_local_install_and_config(self):
        for choice, installed, configured in (('missing-cli', False, False),
                                              ('missing-credentials', True, False)):
            with self.subTest(choice=choice):
                server = self.start(connection=choice)
                _, state = self.request(server, 'GET', '/api/state')
                self.assertIs(state['connection']['installed'], installed)
                self.assertIs(state['connection']['configured'], configured)
                self.assertIsNone(state['connection']['authorized'])
                self.assertEqual(self.writes(server)['count'], 0)
                self.assertFalse((server.application.controller.project / '.organizer/cli-home').exists())

    def test_cache_and_account_preview_failures_preserve_specific_public_next_step(self):
        for failure, code, step in (('cache', 'local_snapshot_unavailable', 'load_desktop_playlists'),
                                    ('account', 'account_mismatch', 'authorize_correct_account')):
            with self.subTest(failure=failure):
                server = self.start(failure=failure)
                for action in ('preview_names', 'preview_full'):
                    result = self.action(server, action)
                    self.assertEqual(result['status'], 'blocked')
                    self.assertEqual(result['error_code'], code)
                    self.assertEqual(result['next_step'], step)
                self.assertEqual(self.writes(server)['count'], 0)

    def test_unknown_write_changes_fake_name_once_and_never_claims_no_effect(self):
        server = self.start(failure='unknown-write')
        result = self.action(server, 'rename')
        self.assertEqual(result['status'], 'uncertain')
        self.assertFalse(result['outcome_known'])
        self.assertEqual(result['next_step'], 'inspect_records')
        self.assertNotEqual(result.get('applied_to_account'), False)
        self.assertEqual(self.writes(server)['fake_writes'], ['updateName'])
        names = {row['name'] for row in server.fake_cli.created.values()}
        self.assertIn('Funk', names)
        self.assertNotIn('funk', names)
        # Repeated state reads and a deliberate repeated action cannot create
        # a second fixture mutation after this injected unknown result.
        for _ in range(3):
            self.request(server, 'GET', '/api/state')
        status, reply = self.request(server, 'POST', '/api/actions', {'action': 'rename'})
        self.assertEqual(status, 409)
        self.assertFalse(reply['accepted'])
        self.assertEqual(self.writes(server)['count'], 1)

    def test_receipt_permission_failure_keeps_real_service_confirmed_rename_result(self):
        server = self.start(failure='record-save')
        result = self.action(server, 'rename')
        self.assertEqual(result['status'], 'completed')
        self.assertEqual(result['completed_count'], 1)
        self.assertTrue(result['applied_to_account'])
        self.assertTrue(result['write_attempted'])
        self.assertTrue(result['outcome_known'])
        self.assertFalse(result['record_saved'])
        self.assertEqual(result['next_step'], 'inspect_records')
        self.assertEqual(self.writes(server)['count'], 1)
        self.assertFalse((server.application.controller.project / 'artifacts/名称整理执行结果.json').exists())
        self.assertIn('Funk', {row['name'] for row in server.fake_cli.created.values()})

    def test_default_artist_gate_still_pauses_after_first_create_and_resumes_without_duplicate(self):
        server = self.start(gate=True)
        status, _ = self.request(server, 'POST', '/api/actions',
                                 {'action': 'artists', 'payload': {'accept_default_visibility': True}})
        self.assertEqual(status, 202)
        self.assertTrue(server.fake_cli.gate_entered.wait(2))
        self.request(server, 'POST', '/api/pause', {})
        self.assertTrue(server.application.wait_for_idle(3))
        _, paused = self.request(server, 'GET', '/api/state')
        self.assertEqual(paused['job']['result']['status'], 'paused')
        self.assertTrue(paused['data']['history']['resumable'])
        self.assertEqual(self.writes(server)['fake_writes'], ['create'])
        completed = self.action(server, 'resume')
        self.assertEqual(completed['status'], 'completed')
        self.assertEqual(completed['completed_count'], 5)
        self.assertEqual(self.writes(server)['fake_writes'], ['create', 'add'] * 5)

    def test_fixture_state_is_unavailable_on_the_normal_product_handler(self):
        server = self.start()
        from netease_organizer.web_server import _WorkbenchHandler
        server.RequestHandlerClass = _WorkbenchHandler
        status, _ = self.request(server, 'GET', '/fixture/state')
        self.assertEqual(status, 404)

    def test_invalid_fixture_options_are_rejected_before_any_fake_workspace_or_server(self):
        factory = getattr(fixture, 'create_fixture', None)
        self.assertTrue(callable(factory), 'Recovery fixture factory is not implemented')
        for options in ({'connection': 'user-account'}, {'failure': 'live'},
                        {'connection': 'https://music.163.com/'}):
            with self.subTest(options=options), self.assertRaises(ValueError):
                factory(self.project / 'invalid-input', assets=self.assets, **options)
        self.assertFalse((self.project / 'invalid-input').exists())

    def test_stdio_recovery_report_and_cleanup_do_not_overwrite_default_acceptance(self):
        artifacts = self.project / 'artifacts'
        artifacts.mkdir()
        original = artifacts / '现代前端-pause-resume-验收.json'
        original.write_text('DEFAULT-ACCEPTANCE-SENTINEL', encoding='utf-8')
        child = ('import functools; from pathlib import Path; from scripts import serve_web_fixture as f; '
                 'f.PROJECT=Path(__import__("sys").argv.pop(1)); '
                 'f.tempfile.TemporaryDirectory=functools.partial(f.tempfile.TemporaryDirectory, dir=f.PROJECT); '
                 'f.main()')
        result = subprocess.run([sys.executable, '-X', 'utf8', '-c', child, str(self.project),
                                 '--connection', 'missing-cli', '--stdio-control'],
                                cwd=fixture.PROJECT, input='\n', text=True, encoding='utf-8',
                                capture_output=True, timeout=8)
        self.assertEqual(result.returncode, 0, result.stderr)
        announcement = json.loads(result.stdout)
        self.assertEqual(announcement['kind'], 'fake_account_only')
        self.assertEqual(announcement['real_account_calls'], 0)
        self.assertTrue(announcement['url'].startswith('http://127.0.0.1:'))
        report = json.loads((artifacts / '现代前端-recovery-验收.json').read_text(encoding='utf-8'))
        self.assertEqual(report['scenario'], 'recovery')
        self.assertEqual(report['connection'], 'missing-cli')
        self.assertEqual(report['failure'], 'none')
        self.assertEqual(report['real_account_calls'], 0)
        self.assertEqual(report['fake_writes'], [])
        self.assertEqual(original.read_text(encoding='utf-8'), 'DEFAULT-ACCEPTANCE-SENTINEL')
        self.assertEqual(list(self.project.glob('organizer-web-fixture-*')), [])
        self.assertNotIn('PRIVATE-TOKEN', result.stdout + result.stderr)


if __name__ == '__main__':
    unittest.main()
