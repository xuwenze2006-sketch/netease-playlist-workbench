import copy
import json
import os
import tempfile
import unittest
from pathlib import Path

from netease_organizer.online_planning import build_online_plan
from netease_organizer.web_preview import build_local_preview


def snapshot(*, names_only=False, track_count=8):
    tracks = [
        {'id': f'{index:032X}', 'original_id': str(900000 + index),
         'name': f'歌曲 {index} · 雨の音', 'metadata_available': True,
         'artists': [{'id': 'D' * 32, 'original_id': '87654321', 'name': '歌手 · 蔡健雅'}]}
        for index in range(1, track_count + 1)
    ]
    liked = {'id': 'C' * 32, 'original_id': '23456789', 'name': '我喜欢的音乐',
             'track_count': track_count, 'special_type': 5, 'track_update_time': 20,
             'creator_id': 'A' * 32, 'tracks': [] if names_only else tracks,
             'membership_complete': not names_only, 'metadata_complete': not names_only,
             'missing_record_count': track_count if names_only else 0,
             'missing_metadata_track_ids': []}
    return {
        'account': {'id': 'A' * 32, 'original_id': '12345678', 'nickname': '测试用户'},
        'playlists': [
            {'id': 'B' * 32, 'original_id': '34567890', 'name': 'funk',
             'track_count': 2, 'special_type': 0, 'track_update_time': 10},
            {key: value for key, value in liked.items()
             if key in ('id', 'original_id', 'name', 'track_count', 'special_type', 'track_update_time')},
        ],
        'liked': liked, 'complete': not names_only,
        'overview_complete': True, 'tracks_loaded': not names_only,
    }


class WebPreviewTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.project = Path(self.directory.name)
        (self.project / 'artifacts').mkdir()

    def preview(self, **kwargs):
        return build_local_preview(self.project, **kwargs)

    def write(self, name, data, timestamp):
        path = self.project / 'artifacts' / name
        path.write_text(json.dumps(data, ensure_ascii=False), encoding='utf-8')
        os.utime(path, (timestamp, timestamp))
        return path

    def pair(self, live=None, *, scope='full', timestamp=100, plan=None):
        live = live if live is not None else snapshot(names_only=scope == 'names')
        plan = build_online_plan(live) if plan is None else plan
        prefix = '在线名称整理' if scope == 'names' else '在线整理'
        self.write(prefix + '快照.json', live, timestamp)
        self.write(prefix + '清单.json', plan, timestamp + 1)
        return live, plan

    def test_full_preview_shows_exact_diff_candidates_and_unicode_without_ids(self):
        live, plan = self.pair()
        plan['raw_message'] = 'TOKEN_RAW_RESPONSE_DO_NOT_EXPOSE'
        self.write('在线整理清单.json', plan, 101)
        result = self.preview()
        self.assertEqual(result['source'], 'local_record')
        self.assertEqual(result['scope'], 'full')
        self.assertEqual(result['updated_at'], '1970-01-01T00:01:41+00:00')
        self.assertEqual(result['rename_count'], 1)
        self.assertEqual(result['renames'], [{'key': 'rename-1', 'old_name': 'funk', 'name': 'Funk'}])
        self.assertEqual(result['artists'], [{
            'name': '歌手 · 蔡健雅 · 红心精选', 'count': 8,
            'tracks': [{'name': track['name'], 'artists': ['歌手 · 蔡健雅']}
                       for track in live['liked']['tracks']],
        }])
        self.assertEqual(result['liked'], {'expected': 8, 'observed': 8, 'missing': 0})
        public = json.dumps(result, ensure_ascii=False)
        for secret in ('12345678', '87654321', '23456789', '34567890', 'A' * 32,
                       'D' * 32, '900001', 'TOKEN_RAW_RESPONSE_DO_NOT_EXPOSE'):
            self.assertNotIn(secret, public)
        self.assertIn('本地记录，仅供预览；执行前仍需重新核验。', result['limitations'])

    def test_names_preview_does_not_report_unread_songs_as_missing(self):
        self.pair(scope='names')
        result = self.preview()
        self.assertEqual(result['scope'], 'names')
        self.assertEqual(result['artists'], [])
        self.assertEqual(result['liked'], {'expected': 8, 'observed': None, 'missing': None})
        self.assertIn('本次未读取红心歌曲明细。', result['limitations'])

    def test_latest_reliable_pair_is_selected_and_bad_newer_pair_falls_back(self):
        self.pair(timestamp=100)
        live, plan = self.pair(scope='names', timestamp=200)
        self.assertEqual(self.preview()['scope'], 'names')
        plan['owner_id'] = '99999999'
        self.write('在线名称整理清单.json', plan, 201)
        self.assertEqual(self.preview()['scope'], 'full')

    def test_expected_owner_and_encrypted_account_filter_old_other_account_pairs(self):
        self.pair(timestamp=100)
        self.assertIsNone(self.preview(expected_owner='99999999', expected_account_id='E' * 32))
        self.assertIsNone(self.preview(expected_owner='12345678', expected_account_id='E' * 32))
        self.assertIsNone(self.preview(expected_owner='12345678'))
        self.assertEqual(self.preview(expected_owner='12345678', expected_account_id='a' * 32)['scope'], 'full')

    def test_snapshot_newer_than_plan_cannot_pair_with_stale_checklist(self):
        live, _ = self.pair()
        self.write('在线整理快照.json', live, 102)
        self.assertIsNone(self.preview())

    def test_owner_binding_requires_both_ids_and_verified_source(self):
        for field, value in [('owner_id', '99999999'), ('account_id', 'E' * 32),
                             ('online_account_verified', False), ('kind', 'offline_organizing_plan')]:
            with self.subTest(field=field):
                live = snapshot()
                plan = build_online_plan(live)
                plan[field] = value
                self.pair(live, plan=plan)
                self.assertIsNone(self.preview())

    def test_rename_target_old_name_missing_job_and_double_id_tampering_are_rejected(self):
        for mutation in ('target', 'old', 'missing', 'encrypted', 'original'):
            with self.subTest(mutation=mutation):
                live = snapshot()
                plan = build_online_plan(live)
                job = next(job for job in plan['jobs'] if job['kind'] == 'rename_playlist')
                if mutation == 'target':
                    job['name'] = '用户没有要求的新名称'
                elif mutation == 'old':
                    job['old_name'] = '已经变化的名称'
                elif mutation == 'missing':
                    plan['jobs'].remove(job)
                elif mutation == 'encrypted':
                    job['playlist_id'] = 'E' * 32
                else:
                    job['original_playlist_id'] = '99999999'
                self.pair(live, plan=plan)
                self.assertIsNone(self.preview())

    def test_artist_candidates_require_exact_double_mapping_membership_and_order(self):
        for mutation in ('encrypted', 'original', 'reversed', 'artist', 'source'):
            with self.subTest(mutation=mutation):
                live = snapshot()
                plan = build_online_plan(live)
                job = next(job for job in plan['jobs'] if job['kind'] == 'create_artist_playlist')
                if mutation == 'encrypted':
                    job['candidate_track_ids'][0] = 'E' * 32
                elif mutation == 'original':
                    job['original_candidate_track_ids'][0] = '99999999'
                elif mutation == 'reversed':
                    job['candidate_track_ids'].reverse()
                    job['original_candidate_track_ids'].reverse()
                elif mutation == 'artist':
                    job['artist']['id'] = '99999999'
                else:
                    job['source_playlist_id'] = 'E' * 32
                self.pair(live, plan=plan)
                self.assertIsNone(self.preview())

    def test_duplicate_playlist_track_and_conflicting_artist_ids_are_rejected(self):
        for mutation in ('playlist', 'track', 'artist'):
            with self.subTest(mutation=mutation):
                live = snapshot()
                if mutation == 'playlist':
                    live['playlists'][1]['id'] = live['playlists'][0]['id']
                elif mutation == 'track':
                    live['liked']['tracks'][1]['id'] = live['liked']['tracks'][0]['id']
                else:
                    live['liked']['tracks'][1]['artists'][0]['original_id'] = '99999999'
                self.pair(live)
                self.assertIsNone(self.preview())

    def test_liked_owner_and_directory_identity_conflicts_are_rejected(self):
        for mutation in ('creator', 'id', 'name', 'count'):
            with self.subTest(mutation=mutation):
                live = snapshot()
                if mutation == 'creator':
                    live['liked']['creator_id'] = 'E' * 32
                elif mutation == 'id':
                    live['liked']['id'] = 'E' * 32
                elif mutation == 'name':
                    live['liked']['name'] = '不一致'
                else:
                    live['liked']['track_count'] = 9
                self.pair(live)
                self.assertIsNone(self.preview())

    def test_partial_metadata_remains_honest_and_only_actual_candidates_are_shown(self):
        live = snapshot(track_count=9)
        live['liked']['tracks'].pop()
        live['liked']['tracks'][0]['metadata_available'] = False
        live['liked'].update(membership_complete=False, metadata_complete=False,
                             missing_record_count=1, missing_metadata_track_ids=['900001'])
        live['complete'] = False
        self.pair(live)
        result = self.preview()
        self.assertEqual(result['liked'], {'expected': 9, 'observed': 8, 'missing': 1})
        self.assertEqual(result['artists'][0]['count'], 8)
        self.assertIn('红心记录存在缺失，候选只包含已读取歌曲。', result['limitations'])
        self.assertIn('部分歌曲歌手资料不完整。', result['limitations'])

    def test_more_than_500_candidates_degrades_without_truncating_selected_artist(self):
        self.pair(snapshot(track_count=501))
        result = self.preview()
        self.assertEqual(result['scope'], 'names')
        self.assertEqual(result['artists'], [])
        self.assertEqual(result['rename_count'], 1)
        self.assertEqual(result['liked'], {'expected': 501, 'observed': 501, 'missing': 0})
        self.assertIn('精选候选超出展示上限，本次仅展示名称整理。', result['limitations'])

    def test_equal_timestamps_with_conflicting_accounts_or_double_ids_are_not_chosen_arbitrarily(self):
        for mutation in ('account', 'playlist'):
            with self.subTest(mutation=mutation):
                self.pair(timestamp=100)
                live = snapshot(names_only=True)
                if mutation == 'account':
                    live['account']['id'] = 'E' * 32
                    live['liked']['creator_id'] = 'E' * 32
                else:
                    live['playlists'][0]['id'] = 'E' * 32
                self.pair(live, scope='names', timestamp=100)
                self.assertIsNone(self.preview())

    def test_malformed_json_types_never_crash_or_return_invented_candidates(self):
        changes = [
            lambda live: live['account'].update(original_id=True),
            lambda live: live['liked'].update(track_count=8.0),
            lambda live: live['liked']['tracks'][0].update(name=[]),
            lambda live: live['liked']['tracks'][0].update(artists={}),
            lambda live: live['liked']['tracks'][0]['artists'][0].update(name=[]),
        ]
        good = snapshot()
        plan = build_online_plan(good)
        for change in changes:
            with self.subTest(change=change):
                live = copy.deepcopy(good)
                change(live)
                self.pair(live, plan=copy.deepcopy(plan))
                self.assertIsNone(self.preview())

    def test_missing_duplicate_key_invalid_json_and_oversized_plan_have_no_preview(self):
        self.assertIsNone(self.preview())
        live, plan = self.pair()
        path = self.project / 'artifacts' / '在线整理清单.json'
        valid_json = json.dumps(plan, ensure_ascii=False)
        duplicate = valid_json.replace('"account_id": "' + 'A' * 32 + '"',
                                       '"account_id": "' + 'E' * 32 + '", "account_id": "' + 'A' * 32 + '"')
        nan_json = valid_json[:-1] + ', "ignored": NaN}'
        for raw in ('{broken', duplicate, nan_json,
                    '[' * 6000 + '0' + ']' * 6000, ' ' * (2 * 1024 * 1024) + '{}'):
            with self.subTest(raw=raw[:20]):
                path.write_text(raw, encoding='utf-8')
                os.utime(path, (101, 101))
                self.assertIsNone(self.preview())
        self.write('在线整理清单.json', plan, 101)
        self.assertIsNotNone(self.preview())

    def test_duets_and_same_name_artists_keep_ordered_shared_candidates(self):
        live = snapshot()
        for track in live['liked']['tracks']:
            track['artists'][0]['name'] = ' 同名歌手 '
            track['artists'].append({'id': 'E' * 32, 'original_id': '87654322', 'name': '同名歌手'})
        # Endpoint timestamp values can differ without conflicting membership.
        live['liked']['track_update_time'] = 0
        live['liked']['tracks'][0]['id'] = live['liked']['tracks'][0]['id'].lower()
        self.pair(live)
        result = self.preview()
        self.assertEqual([artist['name'] for artist in result['artists']],
                         ['同名歌手 · 红心精选（ID 87654321）', '同名歌手 · 红心精选（ID 87654322）'])
        self.assertEqual(result['artists'][0]['tracks'], result['artists'][1]['tracks'])
        self.assertEqual(result['artists'][0]['tracks'][0]['artists'], ['同名歌手', '同名歌手'])

    def test_empty_artist_metadata_keeps_other_verified_candidates_without_inventing_a_credit(self):
        live = snapshot(track_count=9)
        track = live['liked']['tracks'][-1]
        track.update(artists=[], metadata_available=False)
        live['complete'] = False
        live['liked'].update(metadata_complete=False, missing_metadata_track_ids=[track['original_id']])
        self.pair(live)
        result = self.preview()
        self.assertEqual(result['artists'][0]['count'], 8)
        self.assertNotIn(track['name'], [item['name'] for item in result['artists'][0]['tracks']])
        self.assertEqual(result['liked'], {'expected': 9, 'observed': 9, 'missing': 0})

    def test_1000_renames_are_complete_but_1001_are_not_silently_truncated(self):
        live = snapshot(names_only=True)
        live['playlists'] = [
            {'id': f'{index + 1000:032X}', 'original_id': str(index + 1000), 'name': 'funk',
             'track_count': 0, 'special_type': 0, 'track_update_time': 0}
            for index in range(1000)
        ]
        self.pair(live, scope='names')
        self.assertEqual(self.preview()['rename_count'], 1000)
        self.assertEqual(len(self.preview()['renames']), 1000)
        live['playlists'].append({'id': f'{2000:032X}', 'original_id': '2000', 'name': 'funk',
                                  'track_count': 0, 'special_type': 0, 'track_update_time': 0})
        self.pair(live, scope='names')
        self.assertIsNone(self.preview())

    def test_extra_artist_duplicate_candidates_and_bool_summary_cannot_look_complete(self):
        for mutation in ('artist', 'candidate', 'summary'):
            with self.subTest(mutation=mutation):
                live = snapshot()
                plan = build_online_plan(live)
                job = next(job for job in plan['jobs'] if job['kind'] == 'create_artist_playlist')
                if mutation == 'artist':
                    plan['jobs'].extend(copy.deepcopy(job) for _ in range(5))
                elif mutation == 'candidate':
                    job['candidate_track_ids'].append(job['candidate_track_ids'][0])
                    job['original_candidate_track_ids'].append(job['original_candidate_track_ids'][0])
                else:
                    plan['summary']['rename_count'] = True
                self.pair(live, plan=plan)
                self.assertIsNone(self.preview())

    def test_outside_symlink_cannot_supply_local_preview(self):
        _, plan = self.pair()
        with tempfile.TemporaryDirectory() as external:
            outside = Path(external) / 'plan.json'
            outside.write_text(json.dumps(plan), encoding='utf-8')
            path = self.project / 'artifacts' / '在线整理清单.json'
            path.unlink()
            try:
                path.symlink_to(outside)
            except OSError:
                self.skipTest('Symlink creation is not enabled on this Windows host.')
            self.assertIsNone(self.preview())


if __name__ == '__main__':
    unittest.main()
