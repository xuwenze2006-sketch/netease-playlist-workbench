import copy
import io
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts import verify_web


def browser_observation(scenario='pause-resume'):
    assertions = {
        'narrow_no_horizontal_overflow': True, 'create_gate_confirmed': True,
        'narrow_pause_reachable': True, 'pause_preserves_progress': True,
        'checkpoint_blocks_new_creation': True, 'resume_enabled_when_paused': True,
        'resume_completed': True, 'completed_artist_disabled': True,
        'completed_resume_disabled': True, 'reload_has_no_replay': True,
    } if scenario == 'pause-resume' else {
        'narrow_no_horizontal_overflow': True, 'create_gate_confirmed': True,
        'page_closed_during_create': True, 'closed_job_paused_before_shutdown': True,
    }
    return {'kind': 'modern_web_browser_observation', 'scenario': scenario, 'verified': True,
            'viewport': {'width': 390, 'height': 844}, 'assertions': assertions,
            'metrics': {'paused_completed_count': 0, 'completed_count': 5 if scenario == 'pause-resume' else 0}}


def fixture_report(scenario='pause-resume'):
    completed = scenario == 'pause-resume'
    names = ['林俊杰 · 红心精选', '蔡健雅 · 红心精选', '王力宏 · 红心精选',
             '蔡依林 · 红心精选', 'Taylor Swift · 红心精选']
    counts = [21, 13, 12, 11, 10]
    return {'kind': 'modern_frontend_fake_account_report', 'scenario': scenario, 'real_account_calls': 0,
            'fake_writes': ['create', 'add'] * 5 if completed else ['create'],
            'fake_playlist_count': 8 if completed else 4, 'fake_call_count': 93,
            'before_shutdown': {'page_closing': True, 'pause_requested': True,
                                'job_status': 'completed' if completed else 'paused'},
            'last_result': {'operation': 'artists', 'status': 'completed' if completed else 'paused',
                            'completed_count': 5 if completed else 0, 'resumable': not completed,
                            'items': [{'name': name, 'count': count if completed else 0,
                                       'status': 'completed' if completed else ('paused' if index == 0 else 'not_attempted')}
                                      for index, (name, count) in enumerate(zip(names, counts))]}}


class VerificationTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.project = Path(self.directory.name).resolve()

    def package(self, *, bin_value='playwright-cli.js', version='0.1.22'):
        root = self.project / 'local tools with spaces' / 'node_modules' / '@playwright' / 'cli'
        root.mkdir(parents=True, exist_ok=True)
        (root / 'playwright-cli.js').write_text('// local test bin', encoding='utf-8')
        (root / 'package.json').write_text(json.dumps({
            'name': '@playwright/cli', 'version': version,
            'bin': {'playwright-cli': bin_value},
            'dependencies': {'playwright-core': '1.64.0-alpha-1790635538000'},
        }), encoding='utf-8')
        return root

    def test_cli_location_uses_package_bin_and_explicit_path_with_spaces(self):
        root = self.package()
        for value in (root, root / 'package.json', root / 'playwright-cli.js'):
            with self.subTest(value=value):
                path, versions = verify_web.locate_cli(value)
                self.assertEqual(path, (root / 'playwright-cli.js').resolve())
                self.assertEqual(versions['playwright_cli'], '0.1.22')

    def test_cli_location_never_installs_when_missing_and_rejects_escaped_bin(self):
        with patch('subprocess.run') as run:
            with self.assertRaises(verify_web.VerificationError):
                verify_web.locate_cli(self.project / 'missing')
            run.assert_not_called()
        for version, value in [('0.1.22', '../../other.js'), ('9.9.9', 'playwright-cli.js'),
                               ('0.1.22', ['playwright-cli.js'])]:
            with self.subTest(version=version, value=value):
                root = self.package(version=version, bin_value=value)
                with self.assertRaises(verify_web.VerificationError):
                    verify_web.locate_cli(root)

    def test_helper_announcement_only_accepts_owned_fake_loopback_url(self):
        announcement = {'kind': 'fake_account_only', 'url': 'http://127.0.0.1:51333/',
                        'pid': 42, 'real_account_calls': 0}
        self.assertEqual(verify_web.validate_helper_announcement(json.dumps(announcement), 42), announcement['url'])
        cases = [('kind', 'real_account'), ('pid', 43), ('pid', True), ('real_account_calls', False),
                 ('url', 'http://localhost:51333/'), ('url', 'https://127.0.0.1:51333/'),
                 ('url', 'http://127.0.0.1:51333/?secret=1'), ('url', 'http://127.0.0.1:99999/'),
                 ('url', 'http://127.0.0.1:51333@evil.example/')]
        for field, value in cases:
            with self.subTest(field=field, value=value):
                bad = dict(announcement, **{field: value})
                with self.assertRaises(verify_web.VerificationError):
                    verify_web.validate_helper_announcement(json.dumps(bad), 42)

    def test_actual_cli_json_result_string_is_parsed_without_copying_raw_fields(self):
        observed = browser_observation()
        envelope = {'result': json.dumps(observed), 'snapshot': 'SECRET_SHOULD_NOT_BE_COPIED'}
        parsed = verify_web.parse_browser_observation(json.dumps(envelope), 'pause-resume')
        self.assertEqual(parsed, observed)
        self.assertNotIn('SECRET', json.dumps(parsed))

    def test_cli_error_missing_assertion_and_failed_browser_are_never_passed(self):
        good = browser_observation()
        cases = [{'isError': True, 'error': 'PRIVATE_RAW_ERROR'}, {'result': '{}'}, {'result': 'not json'}]
        bad = copy.deepcopy(good)
        bad['assertions']['completed_resume_disabled'] = False
        cases.append({'result': json.dumps(bad)})
        bad = dict(good, verified=False, failed_stage='resume_completed')
        cases.append({'result': json.dumps(bad)})
        for envelope in cases:
            with self.subTest(envelope=envelope):
                with self.assertRaises(verify_web.VerificationError) as caught:
                    verify_web.parse_browser_observation(json.dumps(envelope), 'pause-resume')
                self.assertNotIn('PRIVATE_RAW_ERROR', str(caught.exception))

    def test_malformed_browser_failure_stage_cannot_raise_unhashable_type_error(self):
        for value in ([], {}, False):
            with self.subTest(value=value):
                bad = {'kind': 'modern_web_browser_observation', 'scenario': 'close',
                       'verified': False, 'failed_stage': value}
                with self.assertRaises(verify_web.VerificationError):
                    verify_web.parse_browser_observation(json.dumps({'result': json.dumps(bad)}), 'close')

    def test_pause_resume_requires_exact_five_create_and_add_with_eight_total_lists(self):
        result = verify_web.check_scenario(browser_observation(), fixture_report(), 'pause-resume')
        self.assertTrue(result['verified'])
        self.assertEqual(result['metrics']['fake_create_count'], 5)
        self.assertEqual(result['metrics']['fake_add_count'], 5)
        self.assertEqual(result['metrics']['fake_playlist_count'], 8)
        for mutation in ('extra', 'missing', 'count', 'status', 'item'):
            with self.subTest(mutation=mutation):
                bad = fixture_report()
                if mutation == 'extra':
                    bad['fake_writes'].append('create')
                elif mutation == 'missing':
                    bad['fake_writes'].pop()
                elif mutation == 'count':
                    bad['fake_playlist_count'] = 5
                elif mutation == 'status':
                    bad['last_result']['status'] = 'uncertain'
                else:
                    bad['last_result']['items'][0]['count'] = 20
                with self.assertRaises(verify_web.VerificationError):
                    verify_web.check_scenario(browser_observation(), bad, 'pause-resume')

    def test_close_requires_page_pause_before_shutdown_not_cleanup_induced_pause(self):
        result = verify_web.check_scenario(browser_observation('close'), fixture_report('close'), 'close')
        self.assertTrue(result['assertions']['page_closing_before_shutdown'])
        self.assertTrue(result['assertions']['single_create_no_add'])
        for field, value in [('page_closing', False), ('pause_requested', False), ('job_status', 'running')]:
            with self.subTest(field=field):
                bad = fixture_report('close')
                bad['before_shutdown'][field] = value
                with self.assertRaises(verify_web.VerificationError):
                    verify_web.check_scenario(browser_observation('close'), bad, 'close')

    def test_fake_report_identity_and_real_account_count_cannot_be_forged_by_bool(self):
        for field, value in [('kind', 'other'), ('scenario', 'pause-resume'), ('real_account_calls', 1),
                             ('real_account_calls', False), ('fake_call_count', [])]:
            with self.subTest(field=field, value=value):
                bad = fixture_report('close')
                bad[field] = value
                with self.assertRaises(verify_web.VerificationError):
                    verify_web.check_scenario(browser_observation('close'), bad, 'close')

    def test_stale_or_oversized_fixture_reports_are_not_reused(self):
        path = self.project / 'report.json'
        path.write_text(json.dumps(fixture_report()), encoding='utf-8')
        signature = verify_web.file_signature(path)
        with self.assertRaises(verify_web.VerificationError):
            verify_web.load_fresh_report(path, signature, 0)
        self.assertEqual(verify_web.load_fresh_report(path, None, 0)['kind'], 'modern_frontend_fake_account_report')
        path.write_text(' ' * (1024 * 1024 + 1), encoding='utf-8')
        with self.assertRaises(verify_web.VerificationError):
            verify_web.load_fresh_report(path, None, 0)

    def test_cli_commands_are_direct_node_no_shell_no_update_and_named_only(self):
        root = self.package()
        cli_path, versions = verify_web.locate_cli(root)
        cli = verify_web.Cli('node.exe', cli_path, 'organizer-verification-test', self.project, 9999999999)
        with patch('subprocess.run', return_value=subprocess.CompletedProcess([], 0, '{}')) as run:
            cli.command('close', timeout=3)
        args, kwargs = run.call_args
        self.assertEqual(args[0][:2], ['node.exe', str(cli_path)])
        self.assertIn('--session=organizer-verification-test', args[0])
        self.assertFalse(kwargs['shell'])
        self.assertEqual(kwargs['env']['NO_UPDATE_NOTIFIER'], '1')
        self.assertEqual(kwargs['env']['CI'], '1')
        self.assertNotIn('close-all', args[0])
        self.assertNotIn('npx', args[0])

    def test_cli_timeout_is_safe_and_has_bounded_timeout(self):
        root = self.package()
        cli_path, _ = verify_web.locate_cli(root)
        cli = verify_web.Cli('node.exe', cli_path, 'organizer-verification-test', self.project, 9999999999)
        with patch('subprocess.run', side_effect=subprocess.TimeoutExpired(['SECRET'], 2)) as run:
            with self.assertRaises(verify_web.VerificationError) as caught:
                cli.command('run-code', '--filename=temporary.js', timeout=2)
        self.assertNotIn('SECRET', str(caught.exception))
        self.assertLessEqual(run.call_args.kwargs['timeout'], 2)

    def test_generated_close_callback_waits_for_paused_before_returning_and_hides_nonce(self):
        code = verify_web.browser_script('close', 'http://127.0.0.1:51333/')
        self.assertIn('page.close({ runBeforeUnload: true })', code)
        self.assertLess(code.index("page.waitForEvent('close'"), code.index('page.close('))
        self.assertLess(code.index('await closed;'), code.index('assertions.page_closed_during_create = true'))
        self.assertIn("state.job?.status === 'paused'", code)
        self.assertIn("meta[name=\"organizer-session\"]", code)
        self.assertNotIn("fetch(base + '/api/close'", code)
        self.assertNotIn('return { nonce', code)

    def test_narrow_pause_flow_visits_playlist_strip_then_returns_to_home_for_resume(self):
        code = verify_web.browser_script('pause-resume', 'http://127.0.0.1:51333/')
        self.assertIn('name: /我的歌单/', code)
        self.assertIn("name: '概览'", code)
        self.assertLess(code.index('name: /我的歌单/'), code.index("page.locator('.task-strip')"))
        self.assertLess(code.index("name: '概览'"), code.index("name: '已有歌手精选任务'"))

    def test_unexpected_browser_cleanup_failure_still_stops_owned_helper(self):
        process = type('Process', (), {'pid': 42, 'stdin': io.StringIO(), 'stdout': io.StringIO()})()
        cli = unittest.mock.Mock()
        cli.command.side_effect = ['{}', json.dumps({'result': json.dumps(browser_observation())})]
        cli.cleanup.side_effect = RuntimeError('DO_NOT_EXPOSE_RAW')
        with patch.object(verify_web, 'PROJECT', self.project), \
                patch.object(verify_web, 'Cli', return_value=cli), \
                patch.object(verify_web, '_announcement', return_value='http://127.0.0.1:51333/'), \
                patch.object(verify_web.subprocess, 'Popen', return_value=process), \
                patch.object(verify_web, 'stop_helper', return_value=True) as stop:
            with self.assertRaises(verify_web.VerificationError) as caught:
                verify_web._run_scenario('node.exe', self.project / 'bin.js', 'pause-resume',
                                         self.project, 9999999999, 'msedge', False)
        stop.assert_called_once_with(process)
        self.assertNotIn('DO_NOT_EXPOSE_RAW', str(caught.exception))

    def test_complete_orchestration_waits_callback_then_cleans_browser_then_stops_and_reads_fresh_fixture(self):
        events = []
        announcement = {'kind': 'fake_account_only', 'url': 'http://127.0.0.1:51333/', 'pid': 42,
                        'real_account_calls': 0}
        process = type('Process', (), {'pid': 42, 'stdin': io.StringIO(),
                                       'stdout': io.StringIO(json.dumps(announcement) + '\n')})()
        cli = unittest.mock.Mock()

        def command(name, *args, **kwargs):
            events.append(name)
            return '{}' if name == 'open' else json.dumps({'result': json.dumps(browser_observation())})

        cli.command.side_effect = command
        cli.cleanup.side_effect = lambda: events.append('browser-cleanup') or True

        def stop(owned):
            self.assertIs(owned, process)
            events.append('helper-stop')
            path = self.project / 'artifacts/现代前端-pause-resume-验收.json'
            path.parent.mkdir(parents=True)
            path.write_text(json.dumps(fixture_report()), encoding='utf-8')
            return True

        with patch.object(verify_web, 'PROJECT', self.project), \
                patch.object(verify_web, 'Cli', return_value=cli), \
                patch.object(verify_web.subprocess, 'Popen', return_value=process), \
                patch.object(verify_web, 'stop_helper', side_effect=stop):
            result = verify_web._run_scenario('node.exe', self.project / 'bin.js', 'pause-resume',
                                              self.project, 9999999999, 'msedge', False)
        self.assertEqual(events, ['open', 'run-code', 'browser-cleanup', 'helper-stop'])
        self.assertTrue(result['assertions']['own_session_and_helper_cleaned'])
        self.assertEqual(result['metrics']['fake_create_count'], 5)

    def test_final_failure_preserves_completed_first_scenario_and_exact_failed_stage(self):
        index = self.project / 'frontend/dist/index.html'
        index.parent.mkdir(parents=True)
        index.write_text('<html>local fake verification build</html>', encoding='utf-8')
        first = verify_web.check_scenario(browser_observation(), fixture_report(), 'pause-resume')
        with patch.object(verify_web, 'PROJECT', self.project), \
                patch.object(verify_web.shutil, 'which', return_value='node.exe'), \
                patch.object(verify_web, 'locate_cli', return_value=(self.project / 'bin.js', {'playwright_cli': '0.1.22'})), \
                patch.object(verify_web.subprocess, 'run', return_value=subprocess.CompletedProcess([], 0, 'v24.19.0\n')), \
                patch.object(verify_web, '_run_scenario', side_effect=[first, verify_web.VerificationError('browser_callback')]):
            result = verify_web.run_verification()
        self.assertFalse(result['verified'])
        self.assertEqual(result['failed_stage'], 'close:browser_callback')
        self.assertEqual(result['metrics']['verified_scenario_count'], 1)
        self.assertEqual(result['scenarios'], [first])
        self.assertEqual(result['real_account_calls'], 0)

    def test_helper_stop_gracefully_signals_after_callback_and_kills_only_own_process_on_timeout(self):
        process = type('Process', (), {})()
        process.stdin = io.StringIO()
        process.wait = unittest.mock.Mock(return_value=0)
        process.terminate = unittest.mock.Mock()
        process.kill = unittest.mock.Mock()
        self.assertTrue(verify_web.stop_helper(process, timeout=2))
        self.assertEqual(process.stdin.getvalue(), '\n')
        process.terminate.assert_not_called()
        process.wait.side_effect = [subprocess.TimeoutExpired('owned helper', 2), 0]
        self.assertFalse(verify_web.stop_helper(process, timeout=2))
        process.terminate.assert_called_once()
        process.kill.assert_not_called()

    def test_failed_report_replaces_prior_pass_and_preserves_failed_stage(self):
        path = self.project / 'modern-web-verification.json'
        verify_web.write_report(path, {'verified': True})
        failure = {'kind': 'modern_web_fake_browser_verification', 'verified': False,
                   'real_account_calls': 0, 'failed_stage': 'close:browser_callback'}
        verify_web.write_report(path, failure)
        self.assertEqual(json.loads(path.read_text(encoding='utf-8')), failure)


if __name__ == '__main__':
    unittest.main()
