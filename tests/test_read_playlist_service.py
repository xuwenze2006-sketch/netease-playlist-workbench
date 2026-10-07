import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from netease_organizer.service import Organizer, OrganizerError
from netease_organizer.write_journal import write_lease
from tests.test_online_playlist import PlaylistCli
from tests.test_online import encrypted
from tests.test_write_journal import intent


class ReadPlaylistServiceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.project = Path(self.temp.name)
        self.artifacts = self.project/'artifacts'
        self.artifacts.mkdir()
        self.cli = PlaylistCli()
        self.cli.configured = lambda: True
        self.reader = Mock()
        self.reader.load.side_effect = AssertionError('A single playlist read must not load desktop cache')
        self.owner = Organizer(self.project, cli=self.cli, reader=self.reader, data_dir=self.project/'cache')
        self.live = {'account': {'id': encrypted(123), 'original_id': '123', 'nickname': '用户'},
            'playlists': [{'id': encrypted(1000), 'original_id': '1000', 'name': '旧目录名称',
                          'track_count': 1, 'special_type': 0}], 'liked': None}
        self.directory = self.artifacts/'在线名称整理快照.json'
        self.directory.write_text(json.dumps(self.live), encoding='utf-8')
        self.saved = self.artifacts/'歌单明细/1000.json'

    def read(self, key='1000'):
        return self.owner.read_playlist(key)

    def seed_old(self):
        self.saved.parent.mkdir(exist_ok=True)
        self.saved.write_bytes(b'OLD-RECORD-RETAINED')
        return self.saved.read_bytes(), self.saved.stat().st_mtime_ns

    def assert_old(self, previous):
        self.assertEqual((self.saved.read_bytes(), self.saved.stat().st_mtime_ns), previous)

    def test_explicit_read_saves_stable_owned_details_without_cache_or_account_mutation(self):
        before = self.directory.read_bytes()
        result = self.read()
        self.assertEqual(result['status'], 'completed')
        self.assertEqual(result['playlist_key'], '1000')
        self.assertTrue(result['record_saved'])
        self.assertEqual((result['expected_count'], result['count'], result['missing_count']), (2, 2, 0))
        self.assertFalse(result['applied_to_account'])
        self.assertFalse(result['write_attempted'])
        raw = json.loads(self.saved.read_text(encoding='utf-8'))
        self.assertEqual(raw['account'], {'id': encrypted(123).upper(), 'original_id': '123'})
        self.assertEqual(raw['playlist']['name'], self.cli.header['name'])
        self.assertEqual(raw['playlist']['creator_id'], raw['account']['id'])
        self.assertTrue(raw['complete'])
        self.assertGreater(raw['read_at'], 0)
        self.assertEqual(len(raw['playlist']['tracks']), 2)
        self.assertEqual(self.directory.read_bytes(), before)
        self.reader.load.assert_not_called()
        self.assertEqual(len(self.cli.calls), 5)

    def test_filtered_records_and_known_incomplete_metadata_save_as_partial(self):
        self.cli.tracks[0]['artists'].append({'id': None, 'originalId': 0, 'name': '未知歌手'})
        self.cli.tracks[1] = None
        result = self.read()
        self.assertEqual(result['status'], 'partial')
        self.assertTrue(result['record_saved'])
        self.assertEqual((result['expected_count'], result['count'], result['missing_count'],
                          result['missing_metadata_count']), (2, 1, 1, 1))
        raw = json.loads(self.saved.read_text(encoding='utf-8'))
        self.assertFalse(raw['complete'])
        self.assertFalse(raw['playlist']['tracks'][0]['metadata_available'])
        self.assertTrue(raw['playlist']['tracks'][0]['artists'])

    def test_invalid_or_unbound_key_rejects_before_any_account_request(self):
        for key in ('不存在', '../1000', '01000', '0', '9'*21, True, 1000, '9999'):
            with self.subTest(key=key):
                result = self.read(key)
                self.assertEqual(result['status'], 'blocked')
        self.assertEqual(self.cli.calls, [])
        self.assertFalse(self.saved.exists())

    def test_wrong_account_stops_after_one_userinfo_and_keeps_old_details(self):
        previous = self.seed_old()
        self.cli.account['originalId'] = 999
        with self.assertRaises(OrganizerError) as raised:
            self.read()
        self.assertEqual(raised.exception.code, 'account_mismatch')
        self.assertEqual(self.cli.calls, [('user', 'info')])
        self.assert_old(previous)

    def test_pause_after_page_returns_paused_without_publishing_partial_read(self):
        previous = self.seed_old()
        original = self.cli.overrides[self.cli.page_key(0)]
        def pause(occurrence):
            self.owner.request_pause()
            return original(occurrence)
        self.cli.overrides[self.cli.page_key(0)] = pause
        result = self.read()
        self.assertEqual(result['status'], 'paused')
        self.assertFalse(result['write_attempted'])
        self.assertEqual(len(self.cli.calls), 3)
        self.assert_old(previous)

    def test_source_binding_changes_during_read_preserve_previous_details(self):
        previous = self.seed_old()
        original = self.cli.run_json
        def change(arguments):
            result = original(arguments)
            if tuple(arguments) == ('user', 'info') and self.cli.calls.count(('user', 'info')) == 2:
                self.live['account']['id'] = encrypted(999)
                self.directory.write_text(json.dumps(self.live), encoding='utf-8')
            return result
        with patch.object(self.cli, 'run_json', side_effect=change):
            result = self.read()
        self.assertEqual(result['status'], 'blocked')
        self.assertFalse(result['record_saved'])
        self.assert_old(previous)

    def test_pause_when_save_progress_is_delivered_keeps_previous_record(self):
        previous = self.seed_old()
        def pause_at_save(event):
            if event.get('phase') == 'saving':
                self.owner.request_pause()
        self.owner.set_progress_listener(pause_at_save)
        result = self.read()
        self.assertEqual(result['status'], 'paused')
        self.assert_old(previous)
        self.assertEqual(len(self.cli.calls), 5)

    def test_directory_deleted_when_save_progress_is_delivered_keeps_previous_record(self):
        previous = self.seed_old()
        def delete_at_save(event):
            if event.get('phase') == 'saving':
                self.live['playlists'] = []
                self.directory.write_text(json.dumps(self.live), encoding='utf-8')
        self.owner.set_progress_listener(delete_at_save)
        result = self.read()
        self.assertEqual(result['status'], 'blocked')
        self.assertFalse(result['record_saved'])
        self.assert_old(previous)
        self.assertEqual(len(self.cli.calls), 5)

    def test_atomic_save_failure_retains_old_record_and_reports_confirmed_read(self):
        previous = self.seed_old()
        with patch.object(self.owner, '_write_artifact', side_effect=PermissionError('PRIVATE-SECRET')):
            result = self.read()
        self.assertEqual(result['status'], 'failed')
        self.assertFalse(result['record_saved'])
        self.assertTrue(result['outcome_known'])
        self.assertEqual(result['count'], 2)
        self.assertNotIn('PRIVATE-SECRET', json.dumps(result))
        self.assert_old(previous)

    def test_pending_write_intent_does_not_block_read_or_clear_original_recovery(self):
        state = intent()
        state.update(expected_owner_id='123', owner_id=encrypted(123))
        path = self.artifacts/'名称整理执行进度.json'
        path.write_text(json.dumps(state), encoding='utf-8')
        before = path.read_bytes()
        self.assertEqual(self.owner.write_recovery()['status'], 'review_required')
        self.assertTrue(self.read()['record_saved'])
        self.assertEqual(path.read_bytes(), before)
        self.assertEqual(self.owner.write_recovery()['status'], 'review_required')
        self.assertEqual(self.owner.execute_renames()['status'], 'blocked')
        self.assertEqual(len(self.cli.calls), 5)

    def test_other_process_lease_blocks_read_before_cli(self):
        path = self.project/'.organizer/account-write.lock'
        path.parent.mkdir()
        with write_lease(path):
            result = self.read()
        self.assertEqual(result['status'], 'blocked')
        self.assertEqual(self.cli.calls, [])

    def test_local_record_path_symlink_is_not_followed_even_within_project(self):
        elsewhere = self.project/'elsewhere'
        elsewhere.mkdir()
        self.saved.parent.parent.mkdir(exist_ok=True)
        try:
            self.saved.parent.symlink_to(elsewhere, target_is_directory=True)
        except OSError:
            self.skipTest('Windows symbolic links require privileges')
        result = self.read()
        self.assertEqual(result['status'], 'blocked')
        self.assertEqual(self.cli.calls, [])
        self.assertEqual(list(elsewhere.iterdir()), [])
