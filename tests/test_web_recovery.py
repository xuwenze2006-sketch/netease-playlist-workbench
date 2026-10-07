"""Durable review is local; reconciling is an explicit read-only controller action."""

import copy
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

from netease_organizer.web_server import WorkbenchApplication, _valid_action
from test_web_server import FakeController

CLEAR = {'status': 'clear', 'operation': 'unknown', 'can_reconcile': False}
PENDING = {'status': 'review_required', 'operation': 'renames', 'can_reconcile': True}


class RecoveryController(FakeController):
    def __init__(self, project):
        super().__init__(project)
        self.recovery = copy.deepcopy(PENDING)
        self.recovery_reads = 0
        self.reconcile_clears = True

    def write_recovery(self):
        self.recovery_reads += 1
        return copy.deepcopy(self.recovery)

    def reconcile_renames(self):
        result = self._call('reconcile')
        if self.reconcile_clears:
            self.recovery = copy.deepcopy(CLEAR)
        return result


class WebRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.project = Path(self.temporary.name)
        self.controller = RecoveryController(self.project)

    def tearDown(self):
        self.temporary.cleanup()

    def application(self, controller=None):
        return WorkbenchApplication(controller or self.controller, revision='a' * 64,
            state_provider=lambda: {'source': 'empty', 'account': None, 'playlists': [],
                                    'history': None, 'artists_completed': False})

    def submit_and_wait(self, app, action):
        status, result = app.submit(action, {})
        self.assertEqual(status, 202)
        self.assertTrue(result['accepted'])
        self.assertTrue(app.wait_for_idle(2))

    def test_fresh_process_restores_review_and_rejects_writes_without_touching_job_or_pause(self):
        app = self.application()
        state = app.state()
        self.assertIsNone(state['job'])
        self.assertEqual(state['recovery'], PENDING)
        self.assertEqual(state['update']['status'], 'review_required')
        self.assertEqual([name for name, _ in self.controller.calls], ['doctor'])
        app.pause_requested = True
        for action in ('rename', 'artists', 'resume'):
            self.assertEqual(app.submit(action, {})[0], 409)
            self.assertIsNone(app.job)
            self.assertTrue(app.pause_requested)
        self.assertEqual(self.controller.prepared, [])

    def test_pending_is_reloaded_before_writes_and_update_even_if_it_appears_after_startup(self):
        self.controller.recovery = copy.deepcopy(CLEAR)
        app = self.application()
        self.controller.recovery = copy.deepcopy(PENDING)
        before = self.controller.recovery_reads
        self.assertEqual(app.submit('rename', {})[0], 409)
        self.assertGreater(self.controller.recovery_reads, before)
        self.assertEqual(app.request_update(app.instance_id, 'b' * 64),
                         (409, {'accepted': False, 'reason': 'review_required'}))
        self.assertEqual(self.controller.prepared, [])
        self.assertFalse(app.stop_requested)

    def test_check_preview_and_new_document_never_clear_local_pending(self):
        app = self.application()
        self.submit_and_wait(app, 'check')
        self.submit_and_wait(app, 'preview_names')
        app.attach_page()
        self.assertEqual(app.state()['recovery'], PENDING)
        self.assertEqual(app.state()['update']['status'], 'review_required')
        self.assertEqual(app.submit('artists', {})[0], 409)
        self.assertEqual([name for name, _ in self.controller.calls], ['doctor', 'doctor', 'preview'])

    def test_explicit_successful_read_only_reconciliation_clears_only_confirmed_rename_review(self):
        app = self.application()
        self.controller.result = {'status': 'completed', 'outcome_known': True, 'record_saved': True}
        self.submit_and_wait(app, 'reconcile_renames')
        state = app.state()
        self.assertEqual(state['job']['action'], 'reconcile_renames')
        self.assertEqual(state['recovery'], CLEAR)
        self.assertEqual(state['update']['status'], 'none')
        self.assertEqual(state['job']['result']['message'],
                         '上次改名结果已只读核对；后续整理仍暂停。')
        self.assertEqual([name for name, _ in self.controller.calls], ['doctor', 'reconcile'])
        self.assertEqual(app.request_update(app.instance_id, 'b' * 64)[0], 202)

    def test_uncertain_or_unsaved_reconciliation_never_unlocks_even_if_controller_reports_clear(self):
        for result in ({'status': 'uncertain', 'outcome_known': False, 'record_saved': True},
                       {'status': 'completed', 'outcome_known': True, 'record_saved': False},
                       {'status': 'blocked', 'outcome_known': True, 'record_saved': True}):
            with self.subTest(result=result):
                self.controller.recovery = copy.deepcopy(PENDING)
                app = self.application()
                self.controller.result = result
                self.submit_and_wait(app, 'reconcile_renames')
                self.assertEqual(app.submit('rename', {})[0], 409)
                self.assertEqual(app.state()['update']['status'], 'review_required')

    def test_a_rename_reconciliation_cannot_clear_an_earlier_artist_unknown_in_memory(self):
        self.controller.recovery = copy.deepcopy(CLEAR)
        app = self.application()
        self.controller.result = {'status': 'uncertain', 'write_attempted': True, 'outcome_known': False}
        self.submit_and_wait(app, 'artists')
        self.controller.recovery = copy.deepcopy(PENDING)
        self.controller.result = {'status': 'completed', 'outcome_known': True, 'record_saved': True}
        self.submit_and_wait(app, 'reconcile_renames')
        self.assertEqual(app.state()['recovery'], CLEAR)
        self.assertEqual(app.state()['update']['status'], 'review_required')
        self.assertEqual(app.submit('rename', {})[0], 409)

    def test_restored_other_review_reason_survives_later_rename_reconciliation(self):
        for operation in ('artists', 'unknown'):
            with self.subTest(operation=operation):
                self.controller.recovery = {'status': 'review_required', 'operation': operation,
                                            'can_reconcile': False}
                app = self.application()
                self.controller.recovery = copy.deepcopy(PENDING)
                self.controller.result = {'status': 'completed', 'outcome_known': True,
                                          'record_saved': True}
                self.submit_and_wait(app, 'reconcile_renames')
                self.assertEqual(app.state()['update']['status'], 'review_required')
                self.assertEqual(app.submit('artists', {})[0], 409)

    def test_invalid_or_failing_local_recovery_is_fail_closed_and_never_exposes_raw_fields(self):
        for value in ({'status': 'clear', 'can_reconcile': 'yes'},
                      {'status': 'review_required', 'operation': 'artists', 'can_reconcile': True},
                      {'status': 'review_required', 'operation': 'renames', 'can_reconcile': True,
                       'owner': 'SECRET', 'batch_id': 'SECRET', 'digest': 'SECRET'}):
            with self.subTest(value=value):
                self.controller.recovery = value
                app = self.application()
                public = app.state()['recovery']
                self.assertEqual(set(public), {'status', 'operation', 'can_reconcile'})
                self.assertNotIn('SECRET', repr(public))
                self.assertEqual(app.submit('rename', {})[0], 409)
                if value.get('operation') != 'renames':
                    self.assertFalse(public['can_reconcile'])
        self.controller.write_recovery = Mock(side_effect=RuntimeError('SECRET'))
        app = self.application()
        self.assertEqual(app.state()['recovery'], {'status': 'review_required', 'operation': 'unknown', 'can_reconcile': False})

    def test_legacy_controller_and_unconfigured_mock_dynamic_attributes_remain_compatible(self):
        old = FakeController(self.project)
        app = self.application(old)
        self.assertEqual(app.state()['recovery'], CLEAR)
        self.submit_and_wait(app, 'check')
        dynamic = Mock()
        dynamic.doctor.return_value = {'installed': True, 'configured': True, 'status': 'credentials_saved'}
        mocked = self.application(dynamic)
        self.assertEqual(mocked.state()['recovery'], CLEAR)
        dynamic.write_recovery.assert_not_called()

    def test_artist_or_unknown_recovery_never_exposes_a_rename_reconcile_action(self):
        for operation in ('artists', 'unknown'):
            self.controller.recovery = {'status': 'review_required', 'operation': operation, 'can_reconcile': False}
            app = self.application()
            self.assertEqual(app.submit('reconcile_renames', {})[0], 409)
            self.assertEqual(self.controller.prepared, [])

    def test_reconciliation_protocol_accepts_no_write_arguments(self):
        self.assertEqual(_valid_action({'action': 'reconcile_renames', 'payload': {}}),
                         ('reconcile_renames', {}))
        self.assertIsNone(_valid_action({'action': 'reconcile_renames',
                                       'payload': {'accept_default_visibility': True}}))


if __name__ == '__main__':
    unittest.main()
