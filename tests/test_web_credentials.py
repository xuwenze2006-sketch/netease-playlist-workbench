"""Credential replacement uses isolated files and fake CLI processes only."""

import json
from pathlib import Path
import subprocess
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from netease_organizer.official_cli import OfficialCli
from netease_organizer.service import Organizer
from netease_organizer.web_server import WorkbenchApplication


SECRET = 'PRIVATE-CREDENTIAL-NEVER-DISPLAY'


class WebCredentialsTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.project = Path(self.temporary.name)
        self.calls = []
        self.reject_config = None
        self.config_started = threading.Event()
        self.config_release = threading.Event()
        self.block_config = False
        node = self.project / 'fake-node.exe'
        node.write_bytes(b'not an executable')
        self.cli = OfficialCli(self.project, node=node, run_process=self.fake_process)
        self.cli.script.parent.mkdir(parents=True)
        self.cli.script.write_text('// never executed', encoding='utf-8')
        self.cli.config_dir.mkdir(parents=True)
        self.cli.marker.write_text('{"configured": true}', encoding='utf-8')
        (self.cli.config_dir / 'credentials.enc.json').write_text('fake encrypted credential', encoding='utf-8')
        self.controller = Organizer(self.project, cli=self.cli, data_dir=self.project / 'fake-client')
        self.app = WorkbenchApplication(self.controller, state_provider=lambda: {
            'source': 'empty', 'account': None, 'playlists': [], 'history': None, 'artists_completed': False})
        self.addCleanup(self.stop_worker)
        self.run_action('login_status')
        self.assertEqual(self.app.state()['connection'],
                         {'installed': True, 'configured': True, 'authorized': True})
        self.calls.clear()

    def stop_worker(self):
        self.config_release.set()
        self.app.wait_for_idle(3)

    def fake_process(self, args, **settings):
        command = args[2:]
        self.calls.append(command[:3])
        if command == ['--version']:
            return subprocess.CompletedProcess(args, 0, '1.2.3', '')
        if command == ['login', '--check', '--output', 'json']:
            return subprocess.CompletedProcess(args, 0, '{"success":true}', '')
        if command[:2] != ['config', 'set']:
            raise AssertionError('Unexpected fake CLI operation')
        self.config_started.set()
        if self.block_config and not self.config_release.wait(3):
            raise AssertionError('Fake config was not released')
        if command[2] == self.reject_config:
            return subprocess.CompletedProcess(args, 1, SECRET, SECRET)
        if command[2] == 'privateKey':
            self.assertEqual(Path(command[3]).read_text(encoding='utf-8'), SECRET)
            (self.cli.config_dir / 'credentials.enc.json').write_text('new fake encrypted credential', encoding='utf-8')
        return subprocess.CompletedProcess(args, 0, 'configuration saved', '')

    def run_action(self, action, payload=None):
        self.assertEqual(self.app.submit(action, payload or {})[0], 202)
        self.assertTrue(self.app.wait_for_idle(3))
        return self.app.state()

    def save(self, *, app_id='replacement-app'):
        payload = {'app_id': app_id, 'private_key': SECRET}
        state = self.run_action('save_credentials', payload)
        self.assertEqual(payload, {})
        self.assertNotIn(SECRET, json.dumps(state, ensure_ascii=False))
        return state

    def assert_only_config_processes(self):
        self.assertTrue(all(command[:2] == ['config', 'set'] for command in self.calls), self.calls)

    def test_failed_first_config_updates_actual_marker_and_revokes_previous_authorization(self):
        self.reject_config = 'appId'
        self.app.login_started = time.monotonic()
        state = self.save()
        self.assertFalse(self.cli.configured())
        self.assertEqual(state['connection'], {'installed': True, 'configured': False, 'authorized': None})
        self.assertIsNone(self.app.login_started)
        self.assertEqual(state['job']['status'], 'failed')
        self.assertEqual(state['job']['result']['error_code'], 'operation_failed')
        self.assert_only_config_processes()

    def test_failed_private_key_config_updates_actual_marker_without_exposing_private_input(self):
        self.reject_config = 'privateKey'
        state = self.save()
        self.assertFalse(self.cli.configured())
        self.assertFalse(state['connection']['configured'])
        self.assertIsNone(state['connection']['authorized'])
        self.assertFalse(list(self.cli.home.glob('private-key-*.tmp')))
        self.assertEqual(state['job']['result']['error_code'], 'operation_failed')
        self.assert_only_config_processes()

    def test_marker_write_failures_use_remaining_local_config_instead_of_blanket_false(self):
        original_write = Path.write_text
        for rejected_text, expected_configured in (
                ('{"configured": false}', True), ('{"configured": true}', False)):
            with self.subTest(marker=rejected_text):
                original_write(self.cli.marker, '{"configured": true}', encoding='utf-8')
                self.app.connection['authorized'] = True
                def guarded_write(path, text, *args, **kwargs):
                    if path == self.cli.marker and text == rejected_text:
                        raise PermissionError(SECRET)
                    return original_write(path, text, *args, **kwargs)
                with patch.object(Path, 'write_text', guarded_write):
                    state = self.save()
                self.assertEqual(self.cli.configured(), expected_configured)
                self.assertEqual(state['connection']['configured'], expected_configured)
                self.assertIsNone(state['connection']['authorized'])
                self.assertEqual(state['job']['result']['error_code'], 'local_permission_denied')
        self.assert_only_config_processes()

    def test_successful_replacement_requires_new_authorization_without_auto_probe(self):
        state = self.save()
        self.assertTrue(self.cli.configured())
        self.assertEqual(state['connection'], {'installed': True, 'configured': True, 'authorized': None})
        self.assertEqual(state['job']['result']['status'], 'credentials_saved')
        self.assert_only_config_processes()
        self.assertEqual(len(self.calls), 2)

    def test_early_validation_rejection_preserves_valid_local_config_but_revokes_authorization(self):
        state = self.save(app_id='invalid app id')
        self.assertTrue(self.cli.configured())
        self.assertTrue(state['connection']['configured'])
        self.assertIsNone(state['connection']['authorized'])
        self.assertEqual(state['job']['result']['error_code'], 'invalid_app_id')
        self.assertEqual(self.calls, [])

    def test_unavailable_local_state_cannot_retain_ready_or_verified_claim(self):
        with patch.object(self.controller, 'local_connection_state', create=True, side_effect=PermissionError(SECRET)):
            state = self.save()
        self.assertFalse(state['connection']['configured'])
        self.assertIsNone(state['connection']['authorized'])
        self.assertEqual(state['job']['result']['status'], 'credentials_saved')
        self.assert_only_config_processes()

    def test_malformed_local_state_is_not_truthy_ready_and_does_not_leak_extra_fields(self):
        for raw in (None, {}, {'installed': True, 'configured': SECRET},
                    {'installed': 1, 'configured': True}, {'configured': True},
                    {'installed': True}):
            with self.subTest(raw=raw), patch.object(self.controller, 'local_connection_state', create=True, return_value=raw):
                self.app.connection.update(configured=True, authorized=True)
                state = self.save()
                self.assertFalse(state['connection']['configured'])
                self.assertIsNone(state['connection']['authorized'])
                self.assertEqual(state['job']['result']['status'], 'credentials_saved')
        self.assert_only_config_processes()

    def test_save_result_cannot_forge_authorization_or_override_actual_configuration(self):
        self.cli.marker.write_text('{"configured": false}', encoding='utf-8')
        with patch.object(self.controller, 'save_credentials', return_value={
                'status': 'credentials_saved', 'authorized': True, 'installed': False, 'configured': True}):
            state = self.save()
        self.assertEqual(state['connection'], {'installed': True, 'configured': False, 'authorized': None})
        self.assertEqual(self.calls, [])

    def test_queued_pause_never_invokes_save_or_revokes_existing_authorization(self):
        worker_entered, worker_release = threading.Event(), threading.Event()
        original_work = self.app._work
        def delayed_work(*args):
            worker_entered.set()
            if worker_release.wait(3):
                original_work(*args)
        payload = {'app_id': 'replacement-app', 'private_key': SECRET}
        local_reads = []
        def local_state():
            local_reads.append(True)
            return {'installed': True, 'configured': True}
        with patch.object(self.app, '_work', delayed_work), \
                patch.object(self.controller, 'local_connection_state', create=True, side_effect=local_state):
            try:
                self.assertEqual(self.app.submit('save_credentials', payload)[0], 202)
                self.assertTrue(worker_entered.wait(1))
                self.app.request_pause()
            finally:
                worker_release.set()
            self.assertTrue(self.app.wait_for_idle(3))
        state = self.app.state()
        self.assertEqual(state['job']['status'], 'paused')
        self.assertEqual(state['connection'], {'installed': True, 'configured': True, 'authorized': True})
        self.assertEqual(payload, {})
        self.assertEqual(self.calls, [])
        self.assertEqual(local_reads, [])

    def assert_state_readable(self):
        states, read_finished = [], threading.Event()
        def read_state():
            states.append(self.app.state())
            read_finished.set()
        reader = threading.Thread(target=read_state, daemon=True)
        reader.start()
        try:
            self.assertTrue(read_finished.wait(0.5), 'local save/state read held the workbench state lock')
            self.assertIsNone(states[0]['connection']['authorized'])
            self.assertIsNone(self.app.login_started)
            self.assertEqual(states[0]['job']['status'], 'running')
        finally:
            self.config_release.set()
            reader.join(2)

    def test_slow_config_keeps_state_responsive_and_hides_previous_authorization(self):
        self.block_config = True
        self.app.login_started = time.monotonic()
        payload = {'app_id': 'replacement-app', 'private_key': SECRET}
        self.assertEqual(self.app.submit('save_credentials', payload)[0], 202)
        self.assertTrue(self.config_started.wait(1))
        self.assertEqual(self.app.submit('login_status', {})[0], 409)
        self.assert_state_readable()
        self.assertTrue(self.app.wait_for_idle(3))
        self.assertEqual(payload, {})
        self.assertNotIn(SECRET, json.dumps(self.app.state()))

    def test_slow_local_connection_read_runs_outside_state_lock(self):
        reading = threading.Event()
        def local_state():
            reading.set()
            if not self.config_release.wait(3):
                raise AssertionError('local state not released')
            return {'installed': True, 'configured': True}
        self.app.login_started = time.monotonic()
        with patch.object(self.controller, 'local_connection_state', create=True, side_effect=local_state):
            self.assertEqual(self.app.submit('save_credentials', {'app_id': 'replacement-app', 'private_key': SECRET})[0], 202)
            try:
                self.assertTrue(reading.wait(1), 'save did not read actual local connection state')
                self.assert_state_readable()
            finally:
                self.config_release.set()
            self.assertTrue(self.app.wait_for_idle(3))

    def test_credential_save_does_not_resolve_unknown_account_write(self):
        with patch.object(self.controller, 'execute_renames', side_effect=RuntimeError(SECRET)):
            state = self.run_action('rename')
        self.assertEqual(state['job']['status'], 'uncertain')
        state = self.save()
        self.assertEqual(state['update']['status'], 'review_required')
        self.assertEqual(self.app.submit('rename', {})[0], 409)
        self.assertIsNone(state['connection']['authorized'])
        self.assert_only_config_processes()

    def test_local_connection_helper_never_runs_doctor_version_or_login_probe(self):
        with patch.object(self.cli, 'run_process', side_effect=AssertionError('local helper started a process')):
            local = self.controller.local_connection_state()
        self.assertEqual(local, {'installed': True, 'configured': True})
        self.assertEqual(self.calls, [])


if __name__ == '__main__':
    unittest.main()
