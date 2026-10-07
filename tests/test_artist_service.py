import copy
import hashlib
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from netease_organizer.service import Organizer, OrganizerError


APPROVED = {'林俊杰 · 红心精选': 21, '蔡健雅 · 红心精选': 13,
            '王力宏 · 红心精选': 12, '蔡依林 · 红心精选': 11, 'Taylor Swift · 红心精选': 10}


class ArtistServiceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name).resolve()
        self.cli = Mock()
        self.cli.configured.return_value = True
        self.reader = Mock()
        self.reader.load.return_value = {'owner_id': '7'}
        self.jobs = []
        start = 1
        for name, count in APPROVED.items():
            self.jobs.append({'kind': 'create_artist_playlist', 'name': name,
                              'candidate_track_ids': [f'{i:032X}' for i in range(start, start + count)],
                              'intended_visibility': 'private', 'status': 'blocked'})
            start += count
        self.approval = {'kind': 'approved_artist_selection', 'account_original_id': '7',
                         'intended_visibility': 'provider_default', 'jobs': copy.deepcopy(self.jobs)}
        self.executor = Mock()
        self.executor.execute.return_value = {'status': 'completed', 'completed_count': 5,
                                              'applied_to_account': True, 'write_attempted': True,
                                              'outcome_known': True, 'items': []}
        self.controller = Organizer(self.root, cli=self.cli, reader=self.reader,
                                    data_dir=self.root / 'cloud-cache', artist_executor=self.executor)
        def preview():
            self.controller._online_plan = {'jobs': copy.deepcopy(self.jobs)}
            self.controller._online_snapshot = {'account': {'original_id': str(self.reader.load.return_value['owner_id'])}}
        self.controller.online_preview = Mock(side_effect=preview)
        self.controller._write_artifact('已批准歌手精选.json', json.dumps(self.approval, ensure_ascii=False))

    def tearDown(self):
        self.temp.cleanup()

    def execute(self):
        return self.controller.execute_artist_playlists(accept_default_visibility=True)

    def test_no_default_visibility_consent_prevents_reads_and_writes(self):
        result = self.controller.execute_artist_playlists()
        self.assertEqual(result['status'], 'blocked')
        self.assertFalse(result['applied_to_account'])
        self.controller.online_preview.assert_not_called()
        self.executor.execute.assert_not_called()

    def test_exact_approved_candidates_are_refreshed_and_only_artists_sent(self):
        self.controller._online_plan = {'jobs': []}  # cached state cannot authorize a write
        result = self.execute()
        self.controller.online_preview.assert_called_once_with()
        jobs = self.executor.execute.call_args.args[0]
        self.assertEqual({j['name']: len(j['candidate_track_ids']) for j in jobs}, APPROVED)
        self.assertTrue(all(j['intended_visibility'] == 'provider_default' for j in jobs))
        self.assertTrue(result['applied_to_account'])
        self.assertTrue((self.root / 'artifacts/歌手精选执行结果.json').is_file())
        self.assertIsNone(self.controller._online_plan)

    def test_changed_song_at_same_count_is_blocked(self):
        self.jobs[0]['candidate_track_ids'][0] = 'F' * 32
        result = self.execute()
        self.assertEqual(result['status'], 'blocked')
        self.executor.execute.assert_not_called()

    def test_changed_account_or_approval_scope_is_blocked(self):
        for change in ({'account_original_id': '8'}, {'intended_visibility': 'private'}):
            with self.subTest(change=change):
                altered = {**self.approval, **change}
                self.controller._write_artifact('已批准歌手精选.json', json.dumps(altered))
                result = self.execute()
                self.assertEqual(result['status'], 'blocked')
        self.executor.execute.assert_not_called()

    def test_existing_progress_prevents_duplicate_execution_after_restart(self):
        self.controller._write_artifact('歌手精选执行进度.json', json.dumps({'stage': 'create_attempted'}))
        result = self.execute()
        self.assertEqual(result['status'], 'blocked')
        self.executor.execute.assert_not_called()
        self.controller.online_preview.assert_not_called()

    def test_existing_cross_process_lock_prevents_network_and_mutation(self):
        lock = self.root / '.organizer/artist-execution.lock'
        lock.parent.mkdir(parents=True)
        lock.write_text('held', encoding='utf-8')
        result = self.execute()
        self.assertEqual(result['status'], 'blocked')
        self.executor.execute.assert_not_called()
        self.controller.online_preview.assert_not_called()

    def test_saved_completion_is_not_executed_again(self):
        self.execute()
        second = self.execute()
        self.assertEqual(second['status'], 'blocked')
        self.assertEqual(self.executor.execute.call_count, 1)

    def test_final_record_failure_preserves_confirmed_effect(self):
        original = self.controller._write_artifact
        def write(filename, text):
            if filename == '歌手精选执行结果.json':
                raise OSError('PRIVATE-INTERNAL')
            return original(filename, text)
        self.controller._write_artifact = write
        result = self.execute()
        self.assertEqual(result['completed_count'], 5)
        self.assertTrue(result['applied_to_account'])
        self.assertFalse(result['record_saved'])
        self.assertNotIn('PRIVATE-INTERNAL', json.dumps(result))

    def test_real_executor_checks_persisted_intent_before_every_simulated_write(self):
        from test_create import FakeCli
        cli = FakeCli()
        cli.configured = lambda: True
        cli.add_mode = 'reverse'  # exercises the complete-set reorder boundary too
        self.controller.cli = cli
        self.controller.artist_executor = None
        self.reader.load.return_value['owner_id'] = '42'
        self.approval['account_original_id'] = '42'
        self.controller._write_artifact('已批准歌手精选.json', json.dumps(self.approval))
        original = cli.run_json
        persisted = []
        def run(arguments):
            if arguments[:2] in (['playlist', 'create'], ['playlist', 'add'], ['playlist', 'reorder']):
                record = json.loads((self.root / 'artifacts/歌手精选执行进度.json').read_text(encoding='utf-8'))
                self.assertEqual(record['phase'], arguments[1] + '_attempted')
                self.assertEqual(record['account_original_id'], '42')
                self.assertEqual(record['approved_visibility'], 'provider_default')
                self.assertEqual(len(record['jobs']), 5)
                persisted.append(record['phase'])
            return original(arguments)
        cli.run_json = run
        result = self.execute()
        self.assertEqual(result['status'], 'completed', json.dumps(result, ensure_ascii=False))
        self.assertEqual(result['completed_count'], 5)
        self.assertEqual(persisted, ['create_attempted', 'add_attempted', 'reorder_attempted'] * 5)
        self.assertEqual(len(cli.created), 5)
        final = json.loads((self.root / 'artifacts/歌手精选执行进度.json').read_text(encoding='utf-8'))
        self.assertEqual(final['stage'], 'finished')
        self.assertEqual(final['status'], 'completed')
        self.assertEqual(final['phase'], 'completed')

    def _recovery_fixture(self, job_index=0):
        from test_create import FakeCli
        cli = FakeCli()
        cli.configured = lambda: True
        self.controller.cli = cli
        self.controller.artist_executor = None
        self.reader.load.return_value['owner_id'] = '42'
        self.approval['account_original_id'] = '42'
        path = self.controller._write_artifact('已批准歌手精选.json', json.dumps(self.approval))
        items = [{'name':j['name'], 'playlist_id':None, 'original_playlist_id':None,
                  'count':0, 'status':'blocked'} for j in self.jobs]
        for index in range(job_index + 1):
            tracks = self.jobs[index]['candidate_track_ids'] if index < job_index else []
            ident = cli.make_playlist(self.jobs[index]['name'], tracks)
            items[index].update(playlist_id=ident, original_playlist_id=str(cli.created[ident]['originalId']),
                                status='completed' if index < job_index else 'uncertain', count=len(tracks))
        previous = {'status':'uncertain','completed_count':job_index,'applied_to_account':True,'items':items}
        self.controller._write_artifact('歌手精选执行结果.json', json.dumps(previous))
        self.controller._write_artifact('歌手精选执行进度.json', json.dumps(previous))
        recovery = {k:items[job_index][k] for k in ('name','playlist_id','original_playlist_id')}
        recovery.update(job_index=job_index, create_only_verified=True)
        evidence = {'kind':'verified_create_only_recovery','account_original_id':'42',
                    'approval_sha256':hashlib.sha256(path.read_bytes()).hexdigest(),
                    'observed_mutations':['create'],'add_or_reorder_observed':False,'recovery':recovery}
        self.controller._write_artifact('歌手精选恢复证据.json', json.dumps(evidence))
        return cli, ident, evidence

    def test_recovery_only_adds_to_verified_created_first_playlist_and_is_one_shot(self):
        cli, ident, _ = self._recovery_fixture()
        result = self.controller.execute_artist_playlists(accept_default_visibility=True, resume_created=True)
        self.assertEqual(result['status'], 'completed', json.dumps(result, ensure_ascii=False))
        self.assertEqual(result['completed_count'], 5)
        self.assertEqual(cli.writes, ['add'] + ['create','add'] * 4)
        self.assertEqual(len(cli.created), 5)
        self.assertEqual(result['items'][0]['playlist_id'], ident)
        self.assertTrue((self.root / 'artifacts/歌手精选执行结果-首轮.json').is_file())
        again = self.controller.execute_artist_playlists(accept_default_visibility=True, resume_created=True)
        self.assertEqual(again['status'], 'blocked')
        self.assertEqual(len(cli.writes), 9)

    def test_recovery_with_possible_historical_add_is_blocked_before_network(self):
        cli, _, evidence = self._recovery_fixture()
        evidence['add_or_reorder_observed'] = True
        self.controller._write_artifact('歌手精选恢复证据.json', json.dumps(evidence))
        result = self.controller.execute_artist_playlists(accept_default_visibility=True, resume_created=True)
        self.assertEqual(result['status'], 'blocked')
        self.assertEqual(cli.calls, [])
        self.assertEqual(cli.writes, [])
        self.assertFalse((self.root / 'artifacts/歌手精选恢复开始.json').exists())

    def test_recovery_marker_created_before_lock_acquisition_is_rechecked(self):
        cli, _, _ = self._recovery_fixture()
        actual_open = os.open
        def acquire(path, flags, *args, **kwargs):
            fd = actual_open(path, flags, *args, **kwargs)
            if Path(path).name == 'artist-execution.lock':
                (self.root / 'artifacts/歌手精选恢复开始.json').write_text('{}', encoding='utf-8')
            return fd
        with patch('netease_organizer.service.os.open', side_effect=acquire):
            result = self.controller.execute_artist_playlists(accept_default_visibility=True, resume_created=True)
        self.assertEqual(result['status'], 'blocked')
        self.assertEqual(cli.calls, [])
        self.assertEqual(cli.writes, [])

    def test_later_create_checkpoint_preserves_completed_playlist_and_merges_five_receipts(self):
        cli, ident, _ = self._recovery_fixture(job_index=1)
        self.controller._write_artifact('歌手精选恢复开始.json', '{}')  # previous index0 recovery
        result = self.controller.execute_artist_playlists(accept_default_visibility=True,
                                                         resume_created=True, resume_job_index=1)
        self.assertEqual(result['status'], 'completed', json.dumps(result, ensure_ascii=False))
        self.assertEqual(result['completed_count'], 5)
        self.assertEqual(result['items'][1]['playlist_id'], ident)
        self.assertEqual(cli.writes, ['add'] + ['create','add'] * 3)
        self.assertEqual(len(cli.created), 5)
        again = self.controller.execute_artist_playlists(accept_default_visibility=True,
                                                        resume_created=True, resume_job_index=1)
        self.assertEqual(again['status'], 'blocked')
        self.assertEqual(len(cli.writes), 7)

    def test_changed_completed_playlist_prevents_later_checkpoint_writes(self):
        cli, _, _ = self._recovery_fixture(job_index=1)
        prior_id = next(iter(cli.created))
        cli.members[prior_id].append('F' * 32)
        cli.created[prior_id]['trackCount'] += 1
        cli.created[prior_id]['trackUpdateTime'] += 1
        result = self.controller.execute_artist_playlists(accept_default_visibility=True,
                                                         resume_created=True, resume_job_index=1)
        self.assertEqual(result['status'], 'blocked')
        self.assertEqual(cli.writes, [])
        self.assertFalse((self.root / 'artifacts/歌手精选恢复开始-1.json').exists())

    def test_pause_in_later_legacy_recovery_keeps_full_checkpoint_for_gui_resume(self):
        cli, _, _ = self._recovery_fixture(job_index=1)
        original = cli.run_json
        def paused_add(arguments):
            result = original(arguments)
            if arguments[:2] == ['playlist', 'add']:
                self.controller.request_pause()
            return result
        cli.run_json = paused_add
        result = self.controller.execute_artist_playlists(accept_default_visibility=True,
                                                         resume_created=True, resume_job_index=1)
        self.assertEqual(result['status'], 'paused')
        self.assertEqual(result['completed_count'], 2)
        self.assertTrue(result['last_result']['resumable'])
        checkpoint = json.loads((self.root / 'artifacts/歌手精选执行进度.json').read_text(encoding='utf-8'))
        self.assertEqual(len(checkpoint['jobs']), 5)
        self.assertEqual(checkpoint['items'], result['items'])
        cli.run_json = original
        self.controller.prepare_operation('继续上次任务')
        final = self.controller.resume_last_operation()
        self.assertEqual(final['status'], 'completed')
        self.assertEqual(final['completed_count'], 5)
        self.assertEqual(cli.writes, ['add'] + ['create', 'add'] * 3)

    def test_later_recovery_block_preserves_verified_historical_effects(self):
        cli, _, _ = self._recovery_fixture(job_index=1)
        original = cli.run_json
        checks = [0]
        def read(arguments):
            result = original(arguments)
            if arguments == ['user','info']:
                checks[0] += 1
                if checks[0] >= 3:
                    result['data']['originalId'] = 43
            return result
        cli.run_json = read
        result = self.controller.execute_artist_playlists(accept_default_visibility=True,
                                                         resume_created=True, resume_job_index=1)
        self.assertEqual(result['status'], 'partial')
        self.assertEqual(result['completed_count'], 1)
        self.assertTrue(result['applied_to_account'])
        self.assertEqual(cli.writes, [])

    def test_transient_windows_atomic_save_denial_finishes_before_returning(self):
        actual_replace = os.replace
        denied = PermissionError('PRIVATE-INTERNAL')
        denied.winerror = 5
        attempts = [0]
        def replace(source, destination):
            attempts[0] += 1
            if attempts[0] < 3:
                raise denied
            return actual_replace(source, destination)
        with patch('netease_organizer.service.os.replace', side_effect=replace):
            path = self.controller._write_artifact('保存测试.json', '{"confirmed":true}')
        self.assertEqual(json.loads(path.read_text(encoding='utf-8')), {'confirmed':True})
        self.assertEqual(attempts[0], 3)

    def test_no_write_preflight_failure_does_not_manufacture_pending_journal(self):
        from test_create import FakeCli
        for failure in ('manifest', 'existing_name'):
            with self.subTest(failure=failure), tempfile.TemporaryDirectory() as case_dir:
                self.controller.project = Path(case_dir).resolve()
                self.controller.data_dir = self.controller.project / 'cache'
                cli = FakeCli()
                cli.configured = lambda: True
                self.controller.cli = cli
                self.controller.artist_executor = None
                self.reader.load.return_value['owner_id'] = '42'
                self.approval['account_original_id'] = '42'
                self.controller._write_artifact('已批准歌手精选.json', json.dumps(self.approval))
                if failure == 'manifest':
                    cli.commands = []
                else:
                    cli.make_playlist(self.jobs[0]['name'], [])
                result = self.controller.execute_artist_playlists(accept_default_visibility=True)
                self.assertEqual(result['status'], 'blocked')
                self.assertFalse(result['write_attempted'])
                self.assertFalse(result['applied_to_account'])
                self.assertEqual(cli.writes, [])
                self.assertFalse((self.controller.project / 'artifacts/歌手精选执行进度.json').exists())
                self.assertEqual(self.controller.write_recovery()['status'], 'clear')


if __name__ == '__main__':
    unittest.main()
