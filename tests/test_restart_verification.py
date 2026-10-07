"""Only temporary fake accounts; no browser or official CLI is started here."""

import copy
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import ProxyHandler, build_opener
from urllib.request import Request

from scripts import verify_restart as verification
from scripts import serve_restart_fixture as fixture
from scripts.verify_web import Cli, VerificationError, _NO_WINDOW, load_fresh_report, stop_helper


class RestartEvidenceTests(unittest.TestCase):
    def evidence(self, case='recovery'):
        observation = {'kind': 'modern_restart_browser_observation', 'case': case,
                       'verified': True, 'viewport': {'width': 390, 'height': 844},
                       'frontend_assets_sha256': 'c'*64,
                       'assertions': dict.fromkeys(verification.ASSERTIONS[case], True)}
        initial = {'status': 'review_required', 'operation': 'renames', 'can_reconcile': True}
        cleared = {'status': 'clear', 'operation': 'unknown', 'can_reconcile': False}
        report = {'kind': 'modern_restart_fake_account_report', 'version': 1,
                  'run_id': 'a'*32, 'case': case, 'process_id': 102, 'seed_pid': 101,
                  'index_sha256': 'b'*64, 'frontend_assets_sha256': 'c'*64, 'real_account_calls': 0,
                  'fake_update_name_count': 1, 'fake_writes': ['updateName'],
                  'remaining_jobs_not_executed': True,
                  'startup': {'job_null': True, 'fake_read_count': 0,
                              'recovery': initial if case == 'recovery' else cleared},
                  'end': {'recovery': cleared, 'reconcile_known_saved': case == 'recovery',
                          'remaining_resumable': True}}
        return json.dumps({'result': observation}), report

    def check(self, stdout, report, case='recovery'):
        return verification.check_evidence(stdout, report, case, run_id='a'*32,
                                           index_sha256='b'*64, frontend_assets_sha256='c'*64,
                                           expected_pid=102, seed_pid=101)

    def test_complete_evidence_only_returns_allowlisted_public_fields(self):
        for case in verification.ASSERTIONS:
            stdout, report = self.evidence(case)
            report['token'] = 'PRIVATE-DO-NOT-REPORT'
            checked = self.check(stdout, report, case)
            self.assertTrue(checked['verified'])
            self.assertEqual(checked['fake_update_name_count'], 1)
            self.assertNotIn('PRIVATE-DO-NOT-REPORT', json.dumps(checked))

    def test_missing_assertion_or_false_write_count_never_passes(self):
        stdout, report = self.evidence()
        outer = json.loads(stdout)
        del outer['result']['assertions']['refresh_keeps_freeze']
        with self.assertRaises(VerificationError):
            self.check(json.dumps(outer), report)
        for field, value in [('fake_update_name_count', True), ('fake_update_name_count', 2),
                             ('fake_writes', []), ('remaining_jobs_not_executed', False),
                             ('real_account_calls', 1)]:
            changed = {**report, field: value}
            with self.subTest(field=field, value=value), self.assertRaises(VerificationError):
                self.check(stdout, changed)

    def test_resume_unfreeze_proof_is_required_without_executing_remaining_work(self):
        stdout, report = self.evidence()
        outer = json.loads(stdout)
        outer['result']['assertions'].pop('resume_available_without_replay', None)
        with self.assertRaises(VerificationError):
            self.check(json.dumps(outer), report)
        with self.assertRaises(VerificationError):
            self.check(stdout, {**report, 'end': {**report['end'], 'remaining_resumable': False}})

    def test_whole_asset_fingerprint_is_required_in_both_independent_observations(self):
        stdout, report = self.evidence()
        for value in (None, 'd'*64):
            with self.subTest(value=value), self.assertRaises(VerificationError):
                self.check(stdout, {**report, 'frontend_assets_sha256': value})
            outer = json.loads(stdout)
            outer['result']['frontend_assets_sha256'] = value
            with self.subTest(browser=value), self.assertRaises(VerificationError):
                self.check(json.dumps(outer), report)

    def test_old_report_wrong_case_and_false_restart_are_rejected(self):
        stdout, report = self.evidence()
        for field, value in [('run_id', 'c'*32), ('index_sha256', 'd'*64),
                             ('case', 'clear-restart'), ('process_id', 101), ('seed_pid', 102)]:
            with self.subTest(field=field), self.assertRaises(VerificationError):
                self.check(stdout, {**report, field: value})
        for key, value in [('job_null', False), ('fake_read_count', 1), ('recovery', {'status': 'clear'})]:
            changed = copy.deepcopy(report)
            changed['startup'][key] = value
            with self.subTest(key=key), self.assertRaises(VerificationError):
                self.check(stdout, changed)
        with self.assertRaises(VerificationError):
            self.check(stdout, report, 'not-a-case')

    def test_unchanged_old_disk_report_cannot_be_used(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'report.json'
            path.write_text(self.evidence()[0], encoding='utf-8')
            signature = (path.stat().st_mtime_ns, path.stat().st_size)
            with self.assertRaises(VerificationError):
                load_fresh_report(path, signature, time.time_ns())

    def test_browser_failure_stages_cannot_smuggle_arbitrary_text(self):
        _, report = self.evidence()
        for stage in (['token'], {'token': 'PRIVATE'}, 'PRIVATE-DO-NOT-ECHO'):
            bad = {'result': {'kind': 'modern_restart_browser_observation', 'case': 'recovery',
                              'verified': False, 'failed_stage': stage}}
            with self.subTest(stage=stage), self.assertRaises(VerificationError) as failure:
                self.check(json.dumps(bad), report)
            self.assertEqual(failure.exception.stage, 'browser_callback')

    def test_preflight_failure_overwrites_an_old_pass_without_launching_anything(self):
        with tempfile.TemporaryDirectory() as folder:
            project = Path(folder)
            destination = project / 'artifacts/modern-restart-verification.json'
            verification.write_report(destination, {'verified': True, 'old_pass': True})
            with patch.object(verification, 'PROJECT', project), patch.object(sys, 'argv', ['verify_restart']), \
                    patch.object(verification.shutil, 'which', return_value=None), \
                    patch.object(verification.subprocess, 'Popen') as spawn, patch('builtins.print'):
                self.assertEqual(verification.main(), 1)
                spawn.assert_not_called()
            fresh = json.loads(destination.read_text(encoding='utf-8'))
            self.assertFalse(fresh['verified'])
            self.assertEqual(fresh['failed_stage'], 'node_location')
            self.assertNotIn('old_pass', fresh)

    def test_failed_browser_cleanup_rejects_pass_and_still_stops_own_helper(self):
        stdout, _ = self.evidence()
        with tempfile.TemporaryDirectory() as folder:
            process = unittest.mock.Mock(pid=102)
            fake_cli = Cli('node', Path(folder) / 'cli.js', 'own-fake-session', folder, time.monotonic()+60)
            def command(name, *args, **kwargs):
                if name == 'close':
                    raise RuntimeError('PRIVATE-DO-NOT-ECHO')
                return stdout
            fake_cli.command = unittest.mock.Mock(side_effect=command)
            with patch.object(verification, 'Cli', return_value=fake_cli), \
                    patch.object(verification.subprocess, 'Popen', return_value=process), \
                    patch.object(verification, '_announcement', return_value='http://127.0.0.1:12345/'), \
                    patch.object(verification, 'stop_helper', return_value=True) as stop, \
                    self.assertRaises(VerificationError) as failure:
                verification.run_case('recovery', 'node', Path(folder) / 'cli.js', folder,
                                      'a'*64, 'a'*32, 'b'*64, 101, time.monotonic() + 60,
                                      assets_sha256='c'*64)
            self.assertEqual(failure.exception.stage, 'cleanup')
            stop.assert_called_once_with(process, timeout=12)
            process.stdin.close.assert_called_once()
            process.stdout.close.assert_called_once()
            self.assertIn('delete-data', [call.args[0] for call in fake_cli.command.call_args_list])

    def test_failed_callback_still_closes_deletes_and_stops_own_helper(self):
        with tempfile.TemporaryDirectory() as folder:
            process = unittest.mock.Mock(pid=102)
            fake_cli = Cli('node', Path(folder) / 'cli.js', 'own-fake-session', folder, time.monotonic()+60)
            def command(name, *args, **kwargs):
                if name == 'run-code':
                    raise VerificationError('browser_callback')
                return '{}'
            fake_cli.command = unittest.mock.Mock(side_effect=command)
            with patch.object(verification, 'Cli', return_value=fake_cli), \
                    patch.object(verification.subprocess, 'Popen', return_value=process), \
                    patch.object(verification, '_announcement', return_value='http://127.0.0.1:12345/'), \
                    patch.object(verification, 'stop_helper', return_value=True) as stop, \
                    self.assertRaises(VerificationError) as failure:
                verification.run_case('recovery', 'node', Path(folder) / 'cli.js', folder,
                                      'a'*64, 'a'*32, 'b'*64, 101, time.monotonic()+60,
                                      assets_sha256='c'*64)
            self.assertEqual(failure.exception.stage, 'browser_callback')
            self.assertEqual([call.args[0] for call in fake_cli.command.call_args_list],
                             ['open', 'run-code', 'close', 'delete-data'])
            stop.assert_called_once_with(process, timeout=12)

    def test_close_interrupt_retries_close_and_deletes_but_never_accepts_pass(self):
        stdout, _ = self.evidence()
        with tempfile.TemporaryDirectory() as folder:
            process = unittest.mock.Mock(pid=102)
            fake_cli = Cli('node', Path(folder) / 'cli.js', 'own-fake-session', folder, time.monotonic()+60)
            closed = 0
            def command(name, *args, **kwargs):
                nonlocal closed
                if name == 'close':
                    closed += 1
                    if closed == 1:
                        raise KeyboardInterrupt()
                return stdout
            fake_cli.command = unittest.mock.Mock(side_effect=command)
            with patch.object(verification, 'Cli', return_value=fake_cli), \
                    patch.object(verification.subprocess, 'Popen', return_value=process), \
                    patch.object(verification, '_announcement', return_value='http://127.0.0.1:12345/'), \
                    patch.object(verification, 'stop_helper', return_value=True) as stop, \
                    patch.object(verification, 'load_fresh_report') as load, \
                    self.assertRaises(VerificationError) as failure:
                verification.run_case('recovery', 'node', Path(folder) / 'cli.js', folder,
                                      'a'*64, 'a'*32, 'b'*64, 101, time.monotonic()+60,
                                      assets_sha256='c'*64)
            self.assertEqual(failure.exception.stage, 'cleanup')
            self.assertEqual([call.args[0] for call in fake_cli.command.call_args_list],
                             ['open', 'run-code', 'close', 'close', 'delete-data'])
            stop.assert_called_once_with(process, timeout=12)
            load.assert_not_called()
            for call in fake_cli.command.call_args_list[2:]:
                self.assertEqual(call.kwargs, {'timeout': 8, 'cleanup': True})

    @unittest.skipUnless(shutil.which('node'), 'A local Node executable is required for JS syntax checks')
    def test_callbacks_parse_in_local_node_without_launching_a_browser(self):
        with tempfile.TemporaryDirectory() as folder:
            for case in verification.ASSERTIONS:
                script = Path(folder) / (case + '.js')
                script.write_text('(' + verification.browser_script(case, 'http://127.0.0.1:12345/',
                                  assets_sha256='c'*64) + ');',
                                  encoding='utf-8')
                checked = subprocess.run([shutil.which('node'), '--check', str(script)], shell=False,
                                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                         timeout=5, creationflags=_NO_WINDOW)
                self.assertEqual(checked.returncode, 0)

    def test_direct_script_entry_can_load_project_assets_without_pythonpath(self):
        with tempfile.TemporaryDirectory() as folder:
            assets = Path(folder) / 'dist'
            assets.mkdir()
            (assets / 'index.html').write_text('<meta content="__ORGANIZER_SESSION__">', encoding='utf-8')
            script = verification.PROJECT / 'scripts/verify_restart.py'
            code = ('import runpy,sys; '
                    f'sys.path.insert(0,{str(script.parent)!r}); '
                    f'entry=runpy.run_path({str(script)!r},run_name="verification_module"); '
                    f'entry["frontend_snapshot"]({str(assets)!r}); '
                    'print("snapshot-ready")')
            result = subprocess.run([sys.executable, '-I', '-X', 'utf8', '-c', code],
                                    cwd=folder, shell=False, capture_output=True, text=True,
                                    encoding='utf-8', timeout=5, creationflags=_NO_WINDOW)
            self.assertEqual(result.returncode, 0, 'Direct script imports must not depend on PYTHONPATH')
            self.assertEqual(result.stdout.strip(), 'snapshot-ready')


class RestartFixtureTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='organizer-restart-test-')
        self.workspace = Path(self.temp.name)
        self.token, self.run_id = secrets.token_hex(32), secrets.token_hex(16)
        verification.mark_workspace(self.workspace, self.token, self.run_id)
        self.assets = self.workspace / 'dist'
        self.assets.mkdir()
        (self.assets / 'index.html').write_text(
            '<meta name="organizer-session" content="__ORGANIZER_SESSION__">fake', encoding='utf-8')
        self.helpers = []
        self.open = build_opener(ProxyHandler({})).open

    def tearDown(self):
        for process in self.helpers:
            if process.poll() is None:
                self.assertTrue(stop_helper(process, timeout=5), 'Own fake helper must exit cooperatively')
            for stream in (process.stdin, process.stdout):
                if stream:
                    stream.close()
        self.temp.cleanup()

    def arguments(self, mode, case='recovery'):
        return [sys.executable, '-X', 'utf8', str(verification.PROJECT / 'scripts/serve_restart_fixture.py'),
                '--mode', mode, '--workspace', str(self.workspace), '--owner-token', self.token,
                '--case', case, '--assets', str(self.assets)]

    def seed(self):
        result = subprocess.run(self.arguments('seed'), shell=False, creationflags=_NO_WINDOW,
                                capture_output=True, text=True, encoding='utf-8', timeout=15)
        self.assertEqual(result.returncode, 0, 'Seed failed; raw output is intentionally not echoed')
        report = json.loads((self.workspace / 'seed-report.json').read_text(encoding='utf-8'))
        self.assertTrue(report['intent_before_write'])
        self.assertTrue(report['result_uncertain'])
        self.assertTrue(report['delayed_effect_not_replay'])
        self.assertEqual(report['fake_update_name_count'], 1)
        return report

    def serve(self, case='recovery'):
        process = subprocess.Popen(self.arguments('serve', case), shell=False, creationflags=_NO_WINDOW,
                                   stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                   text=True, encoding='utf-8')
        self.helpers.append(process)
        url = verification._announcement(process, time.monotonic() + 12)
        with self.open(url, timeout=3) as response:
            html = response.read(65536).decode('utf-8')
        nonce = re.search(r'content="([a-f0-9]{64})"', html)[1]
        return process, url, nonce

    def state(self, url, nonce):
        with self.open(Request(url + 'api/state', headers={'X-Organizer-Session': nonce}), timeout=3) as response:
            return json.load(response)

    def test_real_three_python_processes_restore_then_read_only_resolve_without_replay(self):
        seed = self.seed()
        process, url, nonce = self.serve()
        self.assertNotEqual(seed['process_id'], process.pid)
        state = self.state(url, nonce)
        self.assertIsNone(state['job'])
        self.assertEqual(state['recovery'], {'status': 'review_required', 'operation': 'renames', 'can_reconcile': True})
        for action in ('rename', 'artists', 'resume'):
            payload = {'accept_default_visibility': True} if action == 'artists' else {}
            request = Request(url + 'api/actions', json.dumps({'action': action, 'payload': payload}).encode(),
                              {'X-Organizer-Session': nonce, 'Content-Type': 'application/json'}, method='POST')
            with self.assertRaises(HTTPError) as rejected:
                self.open(request, timeout=3)
            self.assertEqual(rejected.exception.code, 409)
        request = Request(url + 'api/actions', b'{"action":"reconcile_renames","payload":{}}',
                          {'X-Organizer-Session': nonce, 'Content-Type': 'application/json'}, method='POST')
        with self.open(request, timeout=3) as response:
            self.assertEqual(response.status, 202)
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            state = self.state(url, nonce)
            if state['job']['status'] not in ('queued', 'running', 'pause_requested'):
                break
        self.assertEqual(state['job']['status'], 'completed')
        self.assertTrue(state['job']['result']['record_saved'])
        self.assertTrue(state['job']['result']['outcome_known'])
        self.assertTrue(state['data']['history']['resumable'])
        self.assertTrue(stop_helper(process, timeout=5))
        report = json.loads((self.workspace / 'recovery-report.json').read_text(encoding='utf-8'))
        self.assertEqual(report['fake_writes'], ['updateName'])
        self.assertTrue(report['remaining_jobs_not_executed'])
        receipt = json.loads((self.workspace / 'artifacts/名称整理执行结果.json').read_text(encoding='utf-8'))
        self.assertEqual(receipt['status'], 'paused')
        self.assertEqual([item['status'] for item in receipt['items']], ['completed', 'pending'])
        restarted, new_url, new_nonce = self.serve('clear-restart')
        self.assertNotIn(restarted.pid, (seed['process_id'], process.pid))
        state = self.state(new_url, new_nonce)
        self.assertIsNone(state['job'])
        self.assertEqual(state['recovery']['status'], 'clear')
        self.assertTrue(state['data']['history']['resumable'])
        self.assertTrue(stop_helper(restarted, timeout=5))
        clear = json.loads((self.workspace / 'clear-restart-report.json').read_text(encoding='utf-8'))
        self.assertEqual(clear['startup']['fake_read_count'], 0)
        self.assertEqual(clear['fake_update_name_count'], 1)

    def test_marker_is_required_and_real_project_is_always_rejected(self):
        with self.assertRaises(VerificationError):
            fixture.validate_workspace(verification.PROJECT, self.token)
        (self.workspace / fixture.MARKER).unlink()
        with self.assertRaises(VerificationError):
            fixture.validate_workspace(self.workspace, self.token)

    def test_unchanged_index_does_not_hide_changed_added_or_removed_assets(self):
        scripts = self.assets / 'assets'
        scripts.mkdir()
        script = scripts / 'app.js'
        script.write_bytes(b'window.fixture = "A";')
        extra = scripts / 'extra.css'
        extra.write_bytes(b'body {color:red}')
        (self.assets / 'index.html').write_text(
            '<meta name="organizer-session" content="__ORGANIZER_SESSION__">'
            '<script src="/assets/app.js"></script>', encoding='utf-8')
        original, index_hash, asset_hash = verification.frontend_snapshot(self.assets)
        self.assertEqual(original['assets/app.js'], b'window.fixture = "A";')
        for mutate, restore in (
                (lambda: script.write_bytes(b'window.fixture = "B";'),
                 lambda: script.write_bytes(b'window.fixture = "A";')),
                (lambda: (scripts / 'added.svg').write_bytes(b'<svg/>'),
                 lambda: (scripts / 'added.svg').unlink()),
                (lambda: extra.unlink(), lambda: extra.write_bytes(b'body {color:red}'))):
            mutate()
            self.assertEqual(verification.frontend_snapshot(self.assets)[1], index_hash)
            with self.assertRaises(VerificationError):
                verification.require_frontend_fresh(self.assets, index_hash, asset_hash)
            restore()
            verification.require_frontend_fresh(self.assets, index_hash, asset_hash)

    def test_helper_serves_fixed_full_assets_and_reports_birth_fingerprint(self):
        scripts = self.assets / 'assets'
        scripts.mkdir()
        script = scripts / 'app.js'
        script.write_bytes(b'window.fixture = "A";')
        (self.assets / 'index.html').write_text(
            '<meta name="organizer-session" content="__ORGANIZER_SESSION__">'
            '<script src="/assets/app.js"></script>', encoding='utf-8')
        self.seed()
        _, index_hash, asset_hash = verification.frontend_snapshot(self.assets)
        process, url, _ = self.serve()
        script.write_bytes(b'window.fixture = "B";')
        with self.open(url + 'assets/app.js', timeout=3) as response:
            self.assertEqual(response.read(4096), b'window.fixture = "A";')
        with self.open(url + 'fixture/restart', timeout=3) as response:
            self.assertEqual(json.load(response)['frontend_assets_sha256'], asset_hash)
        with self.assertRaises(VerificationError):
            verification.require_frontend_fresh(self.assets, index_hash, asset_hash)
        self.assertTrue(stop_helper(process, timeout=5))
        report = json.loads((self.workspace / 'recovery-report.json').read_text(encoding='utf-8'))
        self.assertEqual(report['frontend_assets_sha256'], asset_hash)


if __name__ == '__main__':
    unittest.main()
