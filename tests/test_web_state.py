import json
import os
import tempfile
import unittest
from pathlib import Path

from netease_organizer.web_state import build_local_state


class WebStateTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.project = Path(self.temp.name)
        self.artifacts = self.project / 'artifacts'
        self.artifacts.mkdir()
        self.account = {'id': 'A' * 32, 'original_id': '42', 'nickname': '用户甲', 'token': 'SECRET'}

    def tearDown(self):
        self.temp.cleanup()

    def write(self, name, data, *, timestamp=None):
        path = self.artifacts / name
        path.write_text(json.dumps(data, ensure_ascii=False), encoding='utf-8')
        if timestamp is not None:
            os.utime(path, (timestamp, timestamp))
        return path

    def snapshot(self, rows=None):
        return {'account': self.account, 'playlists': rows if rows is not None else [
            {'id': 'B' * 32, 'original_id': '7', 'name': 'Funk', 'track_count': 10,
             'special_type': 0, 'private_key': 'SECRET'}],
            'liked': {'id': 'C' * 32, 'original_id': '8', 'name': '我喜欢的音乐',
                      'track_count': 2090, 'tracks': [], 'missing_record_count': 5}}

    def artist_records(self):
        from netease_organizer.service import _APPROVED_ARTIST_COUNTS
        items = [{'name': name, 'playlist_id': f'{1000+i:032X}', 'original_playlist_id': str(1000+i),
                  'status': 'completed', 'count': count, 'expected_count': count, 'token': 'SECRET'}
                 for i, (name, count) in enumerate(_APPROVED_ARTIST_COUNTS.items())]
        self.write('歌手精选执行结果.json', {'status': 'completed', 'completed_count': 5,
                                            'outcome_known': True, 'applied_to_account': True, 'items': items}, timestamp=200)
        self.write('歌手精选执行进度.json', {'kind': 'artist_execution_journal', 'account_original_id': '42'})
        return items

    def test_empty_first_use_has_no_invented_account_or_playlists(self):
        state = build_local_state(self.project)
        self.assertEqual(state['source'], 'empty')
        self.assertIsNone(state['account'])
        self.assertEqual(state['playlists'], [])
        self.assertIsNone(state['history'])
        self.assertFalse(state['artists_completed'])

    def test_workbench_exposes_public_preview_for_its_selected_account_only(self):
        from test_web_preview import snapshot
        from netease_organizer.online_planning import build_online_plan
        live = snapshot(names_only=True)
        self.write('在线名称整理快照.json', live, timestamp=100)
        self.write('在线名称整理清单.json', build_online_plan(live), timestamp=101)
        state = build_local_state(self.project)
        self.assertEqual(state['preview']['renames'][0]['old_name'], 'funk')
        self.assertEqual(state['preview']['renames'][0]['name'], 'Funk')
        self.assertIsNone(state['preview']['liked']['observed'])
        self.assertNotIn(live['account']['id'], json.dumps(state['preview']))
        # A newer directory from another account cannot display the earlier
        # account's checklist just because that old pair is internally valid.
        other = snapshot(names_only=True)
        other['account'].update(id='E' * 32, original_id='99999')
        self.write('在线整理快照.json', other, timestamp=200)
        self.assertIsNone(build_local_state(self.project)['preview'])

    def test_snapshot_is_historical_and_only_public_fields_leave_mapper(self):
        self.write('在线整理快照.json', self.snapshot())
        state = build_local_state(self.project)
        self.assertEqual(state['source'], 'local_record')
        self.assertEqual(state['account']['nickname'], '用户甲')
        self.assertEqual({p['name'] for p in state['playlists']}, {'Funk', '我喜欢的音乐'})
        self.assertTrue(all(p['source'] == 'local_record' for p in state['playlists']))
        self.assertNotIn('SECRET', json.dumps(state))
        self.assertNotIn('online_account_verified', state)

    def test_complete_artist_receipt_merges_exact_ids_and_updates_older_empty_count(self):
        items = self.artist_records()
        row = {'id': items[0]['playlist_id'], 'original_id': items[0]['original_playlist_id'],
               'name': items[0]['name'], 'track_count': 0, 'special_type': 0}
        self.write('在线整理快照.json', self.snapshot([row]), timestamp=100)
        state = build_local_state(self.project)
        self.assertTrue(state['artists_completed'])
        self.assertEqual(len([p for p in state['playlists'] if p['category'] == 'artist']), 5)
        self.assertEqual(next(p['track_count'] for p in state['playlists'] if p['name'] == items[0]['name']), 21)
        self.assertFalse(state['history']['resumable'])

    def test_newer_snapshot_count_is_not_overwritten_by_older_receipt(self):
        items = self.artist_records()
        row = {'id': items[0]['playlist_id'], 'original_id': items[0]['original_playlist_id'],
               'name': items[0]['name'], 'track_count': 9, 'special_type': 0}
        self.write('在线整理快照.json', self.snapshot([row]), timestamp=300)
        state = build_local_state(self.project)
        self.assertEqual(next(p['track_count'] for p in state['playlists'] if p['name'] == items[0]['name']), 9)

    def test_newer_complete_directory_does_not_resurrect_removed_artist_playlists(self):
        items = self.artist_records()
        for timestamp in (200, 300):
            with self.subTest(timestamp=timestamp):
                live = self.snapshot()
                live['overview_complete'] = True
                self.write('在线整理快照.json', live, timestamp=timestamp)
                state = build_local_state(self.project)
                self.assertEqual({p['name'] for p in state['playlists']}, {'Funk', '我喜欢的音乐'})
                self.assertTrue(state['artists_completed'])
                self.assertEqual(state['history']['completed_count'], 5)
                self.assertFalse(state['history']['resumable'])
                self.assertFalse(any(p['key'] == items[0]['original_playlist_id'] for p in state['playlists']))

    def test_receipt_from_different_account_does_not_merge_or_claim_completion(self):
        self.artist_records()
        self.write('歌手精选执行进度.json', {'kind': 'artist_execution_journal', 'account_original_id': '99'})
        self.write('在线整理快照.json', self.snapshot())
        state = build_local_state(self.project)
        self.assertFalse(state['artists_completed'])
        self.assertFalse(any(p['category'] == 'artist' for p in state['playlists']))

    def test_corrupt_partial_files_and_duplicate_ids_do_not_invent_complete_directory(self):
        self.write('在线整理快照.json', self.snapshot() )
        self.write('在线名称整理快照.json', self.snapshot([
            {'id': 'B'*32, 'original_id': '7', 'name': '冲突1', 'track_count': 1, 'special_type': 0},
            {'id': 'B'*32, 'original_id': '9', 'name': '冲突2', 'track_count': 2, 'special_type': 0}]))
        state = build_local_state(self.project)
        self.assertEqual({p['name'] for p in state['playlists']}, {'Funk', '我喜欢的音乐'})
        (self.artifacts / '在线整理快照.json').write_text('{incomplete', encoding='utf-8')
        state = build_local_state(self.project)
        self.assertEqual(state['playlists'], [])
        self.assertEqual(state['source'], 'empty')

    def test_newer_valid_names_snapshot_wins_over_full_snapshot(self):
        self.write('在线整理快照.json', self.snapshot(), timestamp=100)
        newer = self.snapshot()
        newer['playlists'][0]['name'] = '新的名称'
        self.write('在线名称整理快照.json', newer, timestamp=300)
        state = build_local_state(self.project)
        self.assertIn('新的名称', [p['name'] for p in state['playlists']])
        self.assertNotIn('Funk', [p['name'] for p in state['playlists']])

    def test_unhashable_receipt_names_degrade_without_losing_valid_snapshot(self):
        self.write('在线整理快照.json', self.snapshot(), timestamp=100)
        for bad_name in ([], {}, ['林俊杰 · 红心精选']):
            with self.subTest(bad_name=bad_name):
                items = self.artist_records()
                items[0]['name'] = bad_name
                receipt = json.loads((self.artifacts / '歌手精选执行结果.json').read_text(encoding='utf-8'))
                receipt['items'] = items
                self.write('歌手精选执行结果.json', receipt, timestamp=200)
                state = build_local_state(self.project)
                self.assertFalse(state['artists_completed'])
                self.assertEqual({p['name'] for p in state['playlists']}, {'Funk', '我喜欢的音乐'})
                self.assertEqual(state['account']['nickname'], '用户甲')

    def test_noninteger_expected_artist_count_cannot_claim_completion(self):
        self.write('在线整理快照.json', self.snapshot(), timestamp=100)
        items = self.artist_records()
        items[0]['expected_count'] = float(items[0]['count'])
        receipt = json.loads((self.artifacts / '歌手精选执行结果.json').read_text(encoding='utf-8'))
        receipt['items'] = items
        self.write('歌手精选执行结果.json', receipt, timestamp=200)
        state = build_local_state(self.project)
        self.assertFalse(state['artists_completed'])
        self.assertFalse(any(p['category'] == 'artist' for p in state['playlists']))

    def test_same_time_receipt_identity_conflict_preserves_snapshot_without_completion(self):
        for collision in ('original_id', 'encrypted_id'):
            with self.subTest(collision=collision):
                items = self.artist_records()
                row = {'id': items[0]['playlist_id'] if collision == 'encrypted_id' else 'D' * 32,
                       'original_id': items[0]['original_playlist_id'] if collision == 'original_id' else '9999',
                       'name': '可靠旧歌单', 'track_count': 17, 'special_type': 0}
                self.write('在线整理快照.json', self.snapshot([row]), timestamp=200)
                state = build_local_state(self.project)
                self.assertFalse(state['artists_completed'])
                self.assertEqual({p['name'] for p in state['playlists']}, {'可靠旧歌单', '我喜欢的音乐'})
                self.assertEqual(next(p['track_count'] for p in state['playlists'] if p['name'] == '可靠旧歌单'), 17)

    def test_deeply_nested_json_degrades_to_other_valid_snapshot(self):
        self.write('在线名称整理快照.json', self.snapshot(), timestamp=100)
        (self.artifacts / '在线整理快照.json').write_text('{"child":' * 6000 + '0' + '}' * 6000, encoding='utf-8')
        state = build_local_state(self.project)
        self.assertEqual(state['source'], 'local_record')
        self.assertEqual({p['name'] for p in state['playlists']}, {'Funk', '我喜欢的音乐'})

    def test_malformed_or_failing_local_history_hook_falls_back_to_valid_receipt(self):
        self.write('在线整理快照.json', self.snapshot(), timestamp=100)
        valid = {'status': 'completed', 'completed_count': 1,
                 'items': [{'name': 'Funk', 'status': 'completed', 'count': 10}]}
        self.write('名称整理执行结果.json', valid, timestamp=150)
        self.write('歌手精选执行结果.json', {**valid, 'status': []}, timestamp=200)

        class LocalHistory:
            def __init__(self, value):
                self.value = value

            def _last_result(self):
                if self.value == 'raise':
                    raise ValueError('malformed local data')
                return self.value

        for bad in ({**valid, 'operation': 'artists', 'status': []},
                    {**valid, 'operation': []}, {**valid, 'operation': {'token': 'SECRET'}}, 'raise'):
            with self.subTest(bad=bad):
                state = build_local_state(self.project, organizer=LocalHistory(bad))
                self.assertEqual(state['history']['operation'], 'renames')
                self.assertEqual(state['history']['completed_count'], 1)
                self.assertFalse(state['history']['resumable'])
                self.assertFalse(state['artists_completed'])
                self.assertEqual(state['account']['nickname'], '用户甲')
                self.assertNotIn('SECRET', json.dumps(state))

    def test_same_timestamp_conflicting_snapshot_identity_cannot_select_arbitrary_directory(self):
        for collision in ('owner', 'owner_encrypted', 'original_id', 'encrypted_id'):
            with self.subTest(collision=collision):
                self.artist_records()
                first, second = self.snapshot(), self.snapshot()
                if collision in ('owner', 'owner_encrypted'):
                    second['account'] = {**self.account, 'id': 'D' * 32}
                    if collision == 'owner':
                        second['account']['original_id'] = '99'
                elif collision == 'original_id':
                    second['playlists'][0]['id'] = 'D' * 32
                else:
                    second['playlists'][0]['original_id'] = '9999'
                self.write('在线整理快照.json', first, timestamp=200)
                self.write('在线名称整理快照.json', second, timestamp=200)
                state = build_local_state(self.project)
                self.assertEqual(state['playlists'], [])
                self.assertIsNone(state['account'])
                self.assertFalse(state['artists_completed'])
                self.assertEqual(state['source'], 'empty')

    def test_same_timestamp_names_or_counts_do_not_invalidate_same_identity(self):
        first, second = self.snapshot(), self.snapshot()
        second['playlists'][0].update(name='Funk改名', track_count=9)
        self.write('在线整理快照.json', first, timestamp=200)
        self.write('在线名称整理快照.json', second, timestamp=200)
        state = build_local_state(self.project)
        self.assertEqual(state['source'], 'local_record')
        self.assertEqual(state['account']['nickname'], '用户甲')
        self.assertEqual(len(state['playlists']), 2)

    def test_history_resumable_only_for_paused_status(self):
        class LocalHistory:
            def __init__(self, status):
                self.status = status

            def _last_result(self):
                return {'operation': 'renames', 'status': self.status, 'completed_count': 0,
                        'items': [], 'resumable': True}

        for status, expected in (('completed', False), ('paused', True)):
            with self.subTest(status=status):
                state = build_local_state(self.project, organizer=LocalHistory(status))
                self.assertEqual(state['history']['resumable'], expected)


if __name__ == '__main__':
    unittest.main()
