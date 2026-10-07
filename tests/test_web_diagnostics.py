import json
from pathlib import Path
import tempfile
import unittest

from netease_organizer.official_cli import CliError
from netease_organizer.service import OrganizerError
from netease_organizer.web_server import WorkbenchApplication, _safe_result
from tests.test_web_server import FakeController, SECRET


class WebDiagnosticsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.project = Path(self.temp.name)
        self.controller = FakeController(self.project)
        self.app = WorkbenchApplication(self.controller)

    def tearDown(self):
        self.controller.release.set()
        self.app.wait_for_idle(2)
        self.temp.cleanup()

    def run_error(self, action, error, *, payload=None):
        def reject(*args, **kwargs):
            self.controller.calls.append((action, {}))
            raise error
        name = {
            'preview_names': 'online_preview', 'rename': 'execute_renames',
            'artists': 'execute_artist_playlists', 'resume': 'resume_last_operation',
            'save_credentials': 'save_credentials',
        }[action]
        setattr(self.controller, name, reject)
        self.assertEqual(self.app.submit(action, payload or {})[0], 202)
        self.assertTrue(self.app.wait_for_idle(2))
        state = self.app.state()
        self.assertNotIn(SECRET, json.dumps(state, ensure_ascii=False))
        return state['job']

    def test_unknown_write_exceptions_remain_uncertain_without_retry(self):
        for action in ('rename', 'artists', 'resume'):
            with self.subTest(action=action):
                # Classify each action in an independent instance. A prior
                # uncertain write now correctly blocks all subsequent writes.
                self.controller = FakeController(self.project)
                self.app = WorkbenchApplication(self.controller)
                job = self.run_error(action, TimeoutError(SECRET))
                self.assertEqual(job['status'], 'uncertain')
                self.assertFalse(job['result']['outcome_known'])
                self.assertEqual(job['result']['next_step'], 'inspect_records')
                self.assertIn('请勿重复提交', job['result']['message'])
                self.assertNotIn('write_attempted', job['result'])
                before = list(self.controller.calls)
                self.app.state()
                self.app.state()
                self.assertEqual(self.controller.calls, before)

    def test_typed_install_error_does_not_prove_write_was_not_attempted(self):
        job = self.run_error('rename', CliError(SECRET, code='cli_missing'))
        self.assertEqual(job['status'], 'uncertain')
        self.assertEqual(job['result']['error_code'], 'cli_missing')
        self.assertEqual(job['result']['next_step'], 'inspect_records')
        self.assertNotIn('write_attempted', job['result'])

    def test_read_preflight_diagnostics_have_specific_fixed_guidance(self):
        cases = (
            ('node_missing', 'install_cli', 'Node.js'),
            ('cli_missing', 'install_cli', 'CLI'),
            ('credentials_required', 'configure_credentials', '凭证'),
            ('local_snapshot_unavailable', 'load_desktop_playlists', '桌面'),
            ('account_mismatch', 'authorize_correct_account', '账号'),
        )
        for code, step, word in cases:
            with self.subTest(code=code):
                job = self.run_error('preview_names', OrganizerError(SECRET, code=code))
                self.assertEqual(job['status'], 'blocked')
                self.assertEqual(job['result']['error_code'], code)
                self.assertEqual(job['result']['next_step'], step)
                self.assertIn(word, job['result']['message'])

    def test_cli_timeout_guidance_requires_record_check_even_for_read_action(self):
        job = self.run_error('preview_names', CliError(SECRET, code='cli_timeout'))
        self.assertEqual(job['status'], 'failed')
        self.assertFalse(job['result']['outcome_known'])
        self.assertEqual(job['result']['next_step'], 'inspect_records')
        self.assertIn('超时', job['result']['message'])

    def test_credential_input_error_points_to_correction_and_clears_payload(self):
        payload = {'app_id': SECRET, 'private_key': SECRET}
        job = self.run_error('save_credentials', CliError(SECRET, code='invalid_app_id'), payload=payload)
        self.assertEqual(job['status'], 'blocked')
        self.assertEqual(job['result']['next_step'], 'correct_credentials')
        self.assertIn('App ID', job['result']['message'])
        self.assertEqual(payload, {})

    def test_untrusted_exception_code_attribute_cannot_forge_diagnostic(self):
        error = RuntimeError(SECRET)
        error.code = 'account_mismatch'
        job = self.run_error('preview_names', error)
        self.assertEqual(job['result']['error_code'], 'operation_failed')
        self.assertEqual(job['result']['next_step'], 'inspect_records')

    def test_doctor_result_uses_code_not_raw_message_and_ignores_forged_step(self):
        raw = {'status': 'first_setup_required', 'installed': False, 'configured': False,
               'error_code': 'cli_missing', 'message': SECRET, 'next_step': SECRET}
        result = _safe_result(raw, 'check', self.project)
        self.assertEqual(result['status'], 'first_setup_required')
        self.assertEqual(result['error_code'], 'cli_missing')
        self.assertEqual(result['next_step'], 'install_cli')
        self.assertIn('CLI', result['message'])
        self.assertNotIn(SECRET, json.dumps(result))

    def test_invalid_codes_and_raw_messages_are_not_exposed(self):
        result = _safe_result({'status': 'failed', 'error_code': SECRET, 'message': SECRET}, 'check', self.project)
        self.assertNotIn('error_code', result)
        self.assertNotIn(SECRET, json.dumps(result))

    def test_malformed_write_results_are_uncertain(self):
        for raw in (None, {}, {'status': 'failed'}, {'status': 'failed', 'outcome_known': False}):
            with self.subTest(raw=raw):
                result = _safe_result(raw, 'artists', self.project)
                self.assertEqual(result['status'], 'uncertain')
                self.assertFalse(result['outcome_known'])
                self.assertEqual(result['next_step'], 'inspect_records')

    def test_verified_service_results_preserve_known_outcomes(self):
        for status in ('paused', 'partial', 'blocked', 'completed'):
            with self.subTest(status=status):
                result = _safe_result({'status': status, 'outcome_known': True,
                                       'write_attempted': False, 'completed_count': 2}, 'artists', self.project)
                self.assertEqual(result['status'], status)
                self.assertTrue(result['outcome_known'])
                self.assertFalse(result['write_attempted'])
                self.assertEqual(result['completed_count'], 2)

    def test_record_save_failure_keeps_confirmed_account_effect_and_warns_against_replay(self):
        result = _safe_result({'status': 'completed', 'completed_count': 2,
                               'applied_to_account': True, 'outcome_known': True,
                               'record_saved': False, 'message': SECRET}, 'rename', self.project)
        self.assertEqual(result['status'], 'completed')
        self.assertTrue(result['applied_to_account'])
        self.assertTrue(result['outcome_known'])
        self.assertFalse(result['record_saved'])
        self.assertEqual(result['completed_count'], 2)
        self.assertIn('记录未能保存', result['message'])
        self.assertIn('请勿重复提交', result['message'])
        self.assertEqual(result['next_step'], 'inspect_records')

    def test_unknown_account_effect_and_known_uncertain_status_are_preserved(self):
        result = _safe_result({'status': 'uncertain', 'outcome_known': True,
                               'applied_to_account': None, 'write_attempted': True}, 'rename', self.project)
        self.assertEqual(result['status'], 'uncertain')
        self.assertTrue(result['outcome_known'])
        self.assertIsNone(result['applied_to_account'])
        self.assertTrue(result['write_attempted'])
        self.assertEqual(result['next_step'], 'inspect_records')


if __name__ == '__main__':
    unittest.main()
