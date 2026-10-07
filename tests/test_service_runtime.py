import copy
import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

from netease_organizer.runtime import OperationPaused
from netease_organizer.service import Organizer
from test_create import FakeCli
from test_artist_service import APPROVED
from test_online_planning import live_snapshot


class RuntimeServiceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.cli = FakeCli()
        self.cli.configured = lambda: True
        self.cli.installed = lambda: True
        self.cli.version = lambda: 'fixture'
        self.reader = Mock()
        self.reader.load.return_value = {'owner_id': '42'}
        self.controller = Organizer(self.root, cli=self.cli, reader=self.reader,
                                    data_dir=self.root / 'fixture-cache')
        self.jobs = []
        start = 11
        for name, count in APPROVED.items():
            self.jobs.append({'kind': 'create_artist_playlist', 'name': name,
                             'candidate_track_ids': [f'{i:032X}' for i in range(start, start + count)],
                             'intended_visibility': 'provider_default', 'status': 'ready'})
            start += count
        self.controller._write_artifact('已批准歌手精选.json', json.dumps(
            {'kind': 'approved_artist_selection', 'account_original_id': '42',
             'intended_visibility': 'provider_default', 'jobs': self.jobs}, ensure_ascii=False))
        def preview(**kwargs):
            self.controller.control.checkpoint()
            self.controller._online_plan = {'jobs': copy.deepcopy(self.jobs)}
            self.controller._online_snapshot = {'account': {'original_id': '42'}}
            return {'status': 'online_plan_ready'}
        self.controller.online_preview = Mock(side_effect=preview)

    def tearDown(self):
        self.temp.cleanup()

    def pause_after(self, command):
        run = self.cli.run_json
        def wrapped(arguments):
            result = run(arguments)
            if arguments[:2] == ['playlist', command]:
                self.controller.request_pause()
            return result
        self.cli.run_json = wrapped
        result = self.controller.execute_artist_playlists(accept_default_visibility=True)
        self.cli.run_json = run
        return result

    def test_pause_after_create_survives_restart_and_resumes_without_duplicate_create(self):
        paused = self.pause_after('create')
        self.assertEqual(paused['status'], 'paused')
        self.assertTrue(paused['applied_to_account'])
        self.assertTrue(paused['record_saved'])
        self.assertTrue(paused['last_result']['resumable'])
        self.assertEqual(self.cli.writes, ['create'])
        # A new process/controller must consume the durable checkpoint, not memory.
        other = Organizer(self.root, cli=self.cli, reader=self.reader, data_dir=self.root / 'fixture-cache')
        def preview():
            other._online_plan = {'jobs': copy.deepcopy(self.jobs)}
            other._online_snapshot = {'account': {'original_id': '42'}}
            return {'status': 'online_plan_ready'}
        other.online_preview = Mock(side_effect=preview)
        other.prepare_operation('继续上次任务')
        result = other.resume_last_operation()
        self.assertEqual(result['status'], 'completed')
        self.assertEqual(result['completed_count'], 5)
        self.assertEqual(self.cli.writes.count('create'), 5)
        self.assertEqual(self.cli.writes.count('add'), 5)
        self.assertFalse(result['last_result']['resumable'])
        for item, job in zip(result['items'], self.jobs):
            self.assertEqual(self.cli.members[item['playlist_id']], job['candidate_track_ids'])
        calls = len(self.cli.calls)
        self.assertEqual(other.resume_last_operation()['status'], 'blocked')
        self.assertEqual(len(self.cli.calls), calls)

    def test_pause_after_add_preserves_confirmed_first_and_resumes_only_four(self):
        paused = self.pause_after('add')
        self.assertEqual(paused['completed_count'], 1)
        self.assertEqual(paused['items'][0]['phase'], 'completed')
        self.controller.prepare_operation('继续上次任务')
        result = self.controller.resume_last_operation()
        self.assertEqual(result['completed_count'], 5)
        self.assertEqual(self.cli.writes.count('create'), 5)
        self.assertEqual(self.cli.writes.count('add'), 5)

    def test_modified_completed_member_blocks_entire_resume_before_any_new_write(self):
        paused = self.pause_after('add')
        ident = paused['items'][0]['playlist_id']
        self.cli.members[ident].reverse()
        before = list(self.cli.writes)
        self.controller.prepare_operation('继续上次任务')
        result = self.controller.resume_last_operation()
        self.assertIn(result['status'], ('partial', 'blocked'))
        self.assertTrue(result['applied_to_account'])
        self.assertEqual(self.cli.writes, before)
        self.assertFalse(result['last_result']['resumable'])

    def test_consumed_checkpoint_and_process_lock_block_resume_without_calls(self):
        self.pause_after('create')
        self.controller.prepare_operation('继续上次任务')
        lock = self.root / '.organizer/artist-execution.lock'
        lock.write_text('fixture', encoding='utf-8')
        calls = len(self.cli.calls)
        self.assertEqual(self.controller.resume_last_operation()['status'], 'blocked')
        self.assertEqual(len(self.cli.calls), calls)
        lock.unlink()
        progress = self.root / 'artifacts/歌手精选执行进度.json'
        digest = hashlib.sha256(progress.read_bytes()).hexdigest()
        self.controller._write_artifact('暂停续做-' + digest + '.json', '{}')
        self.assertFalse(self.controller.doctor()['last_result']['resumable'])
        self.assertEqual(self.controller.resume_last_operation()['status'], 'blocked')
        self.assertEqual(len(self.cli.calls), calls)

    def test_fresh_candidate_change_prevents_resume(self):
        self.pause_after('create')
        before = list(self.cli.writes)
        self.jobs[0]['candidate_track_ids'][0] = 'F' * 32
        self.controller.prepare_operation('继续上次任务')
        result = self.controller.resume_last_operation()
        self.assertEqual(result['status'], 'blocked')
        self.assertEqual(self.cli.writes, before)

    def test_pause_during_resume_refresh_retains_historical_effects(self):
        self.pause_after('add')
        before = list(self.cli.writes)
        self.controller.prepare_operation('继续上次任务')
        self.controller.online_preview = Mock(return_value={
            'status': 'paused', 'completed_count': 0, 'applied_to_account': False,
            'write_attempted': False, 'outcome_known': True})
        result = self.controller.resume_last_operation()
        self.assertEqual(result['status'], 'paused')
        self.assertEqual(result['completed_count'], 1)
        self.assertTrue(result['applied_to_account'])
        self.assertFalse(result['write_attempted'])
        self.assertEqual(self.cli.writes, before)
        self.assertTrue(result['last_result']['resumable'])

    def test_startup_history_is_local_only_and_pause_is_not_cleared(self):
        self.pause_after('create')
        before = len(self.cli.calls)
        result = self.controller.doctor()
        self.assertTrue(self.controller.control.pause_requested)
        self.assertEqual(result['last_result']['source'], 'local_record')
        self.assertEqual(len(self.cli.calls), before)
        public = json.dumps(result)
        self.assertNotIn(self.cli.secret, public)
        self.assertNotIn('playlist_id', public)

    def test_nested_read_pause_returns_paused_in_both_write_flows(self):
        reader = Mock()
        reader.read_snapshot.side_effect = OperationPaused()
        controller = Organizer(self.root, cli=self.cli, reader=self.reader,
                               online_reader=reader, data_dir=self.root / 'fixture-cache')
        self.assertEqual(controller.execute_renames()['status'], 'paused')
        self.assertEqual(controller.execute_artist_playlists(accept_default_visibility=True)['status'], 'paused')
        self.assertEqual(self.cli.calls, [])
        self.assertEqual(self.cli.writes, [])

    def test_progress_allowlist_and_listener_error_cannot_change_operation(self):
        events = []
        self.controller.set_progress_listener(events.append)
        self.controller._progress({'stage': self.cli.secret, 'token': self.cli.secret,
                                   'completed_count': 1, 'job_count': 5})
        self.assertEqual(events[0]['completed_count'], 1)
        self.assertNotIn(self.cli.secret, json.dumps(events))
        self.controller.set_progress_listener(lambda event: (_ for _ in ()).throw(RuntimeError('SECRET')))
        result = self.controller.execute_artist_playlists(accept_default_visibility=True)
        self.assertEqual(result['status'], 'completed')

    def test_cli_progress_keeps_current_stage_and_completed_count(self):
        events = []
        self.controller.set_progress_listener(events.append)
        self.controller._progress({'stage': 'adding', 'completed_count': 1, 'job_count': 5})
        self.controller._progress({'kind': 'cli', 'command': 'playlist get', 'phase': 'started'})
        self.assertEqual(events[-1]['completed_count'], 1)
        self.assertEqual(events[-1]['total_count'], 5)
        self.assertIn('添加歌曲', events[-1]['label'])
        self.assertIn('歌单', events[-1]['label'])
        self.controller.prepare_operation('新的预览')
        self.controller._progress({'kind': 'cli', 'command': 'user info', 'phase': 'started'})
        self.assertNotIn('completed_count', events[-1])

    def test_performance_receipt_persists_only_safe_finite_summary(self):
        self.cli.performance_summary = lambda: {
            'call_count': len(self.cli.calls), 'elapsed_seconds': 1.25, 'failure_count': 0,
            'commands': [{'arguments': self.cli.secret}], 'private_key': self.cli.secret}
        result = self.pause_after('create')
        saved = json.loads((self.root / 'artifacts/歌手精选执行结果.json').read_text(encoding='utf-8'))
        self.assertEqual(saved['performance']['call_count'], len(self.cli.calls))
        self.assertEqual(saved['performance']['elapsed_seconds'], 1.25)
        self.assertNotIn(self.cli.secret, json.dumps(result))
        self.assertNotIn('commands', saved['performance'])

    def test_bad_metrics_do_not_leak_or_hide_confirmed_outcome(self):
        self.cli.performance_summary = lambda: {'call_count': 3, 'elapsed_seconds': float('nan'),
                                                'failure_count': self.cli.secret}
        result = self.pause_after('create')
        self.assertTrue(result['applied_to_account'])
        self.assertNotIn(self.cli.secret, json.dumps(result))
        self.assertNotIn('NaN', json.dumps(result))


if __name__ == '__main__':
    unittest.main()
