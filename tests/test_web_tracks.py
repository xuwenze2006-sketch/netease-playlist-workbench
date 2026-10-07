import copy
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from netease_organizer.web_tracks import build_local_tracks
from test_web_preview import snapshot
from tests.test_playlist_details import record as saved_record


class LocalTracksTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.project = Path(self.temp.name)
        (self.project / 'artifacts').mkdir()

    def write(self, name, value, timestamp=100):
        path = self.project / 'artifacts' / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value, ensure_ascii=False), encoding='utf-8')
        os.utime(path, (timestamp, timestamp))
        return path

    def read(self, key='23456789', **kwargs):
        return build_local_tracks(self.project, key, **kwargs)

    def save_detail(self, key='34567890', *, read_at=1700000000000):
        record = saved_record()
        record['account'].update(id='A'*32, original_id='12345678')
        record['playlist'].update(id='B'*32 if key == '34567890' else 'C'*32,
                                  original_id=key, creator_id='A'*32,
                                  special_type=0 if key == '34567890' else 5)
        record['read_at'] = read_at
        self.write(f'歌单明细/{key}.json', record)
        return record

    def test_independent_normal_details_are_local_partial_records_with_their_own_time(self):
        self.write('在线整理快照.json', snapshot())
        record = self.save_detail()
        record['playlist']['track_count'] = 3
        record['playlist'].update(membership_complete=False, missing_record_count=1)
        record['complete'] = False
        self.write('歌单明细/34567890.json', record)
        with patch('netease_organizer.official_cli.OfficialCli.run_json', side_effect=AssertionError('No account calls')) as cli:
            result = self.read('34567890', query='歌曲二')
        self.assertEqual(result['status'], 'available')
        self.assertEqual(result['counts'], {'expected': 3, 'observed': 2, 'missing': 1, 'metadata_missing': 0})
        self.assertEqual(result['playlist']['track_count'], 2)
        self.assertEqual(result['tracks'][0]['position'], 2)
        self.assertEqual(result['updated_at'], '2023-11-14T22:13:20+00:00')
        self.assertNotIn('B'*32, json.dumps(result))
        cli.assert_not_called()

    def test_independent_details_from_other_account_or_playlist_identity_are_not_used(self):
        self.write('在线整理快照.json', snapshot())
        for field in ('account', 'playlist'):
            record = self.save_detail()
            if field == 'account':
                record['account'].update(id='E'*32, original_id='999')
                record['playlist']['creator_id'] = 'E'*32
            else:
                record['playlist']['id'] = 'E'*32
            self.write('歌单明细/34567890.json', record)
            with self.subTest(field=field):
                self.assertEqual(self.read('34567890')['status'], 'not_loaded')

    def test_bad_independent_record_is_unavailable_and_does_not_fall_back_to_older_liked_tracks(self):
        self.write('在线整理快照.json', snapshot())
        self.save_detail('23456789')
        (self.project/'artifacts/歌单明细/23456789.json').write_text('{broken', encoding='utf-8')
        result = self.read()
        self.assertEqual(result['status'], 'unavailable')
        self.assertEqual(result['tracks'], [])

    def test_saved_details_cannot_resurrect_a_playlist_removed_from_newer_directory(self):
        self.write('在线整理快照.json', snapshot())
        self.save_detail()
        names = snapshot(names_only=True)
        names['playlists'] = names['playlists'][1:]
        self.write('在线名称整理快照.json', names, 300)
        self.assertEqual(self.read('34567890')['status'], 'missing_playlist')

    def test_liked_source_selection_compares_read_time_to_full_snapshot_time(self):
        self.write('在线整理快照.json', snapshot(track_count=8), 200)
        self.save_detail('23456789', read_at=100000)
        self.assertEqual(self.read()['counts']['observed'], 8)
        self.save_detail('23456789', read_at=300000)
        self.assertEqual(self.read()['counts']['observed'], 2)
        self.assertEqual(self.read()['updated_at'], '1970-01-01T00:05:00+00:00')

    def test_independent_source_replaced_during_read_discards_its_songs(self):
        from netease_organizer.playlist_details import load_record
        self.write('在线整理快照.json', snapshot())
        self.save_detail()
        def replace(project, key):
            loaded = load_record(project, key)
            # Product saves replace atomically, rather than restoring an
            # in-place file's original metadata after changing its bytes.
            replacement = self.project/'artifacts/歌单明细/replacement.json'
            changed = copy.deepcopy(loaded[0])
            changed['read_at'] += 1
            replacement.write_text(json.dumps(changed), encoding='utf-8')
            replacement.replace(self.project/'artifacts/歌单明细'/f'{key}.json')
            return loaded
        with patch('netease_organizer.web_tracks.load_record', side_effect=replace):
            result = self.read('34567890')
        self.assertEqual(result['status'], 'unavailable')
        self.assertEqual(result['tracks'], [])
        self.assertIsNone(result['counts']['observed'])

    def test_saved_tracks_are_paginated_in_original_order_and_only_public_fields_leave_mapper(self):
        live = snapshot(track_count=123)
        live['liked']['tracks'][0]['token'] = 'PRIVATE-NO-ECHO'
        self.write('在线整理快照.json', live)
        result = self.read(offset=50, limit=50)
        self.assertEqual(result['status'], 'available')
        self.assertEqual(result['counts'], {'expected': 123, 'observed': 123, 'missing': 0, 'metadata_missing': 0})
        self.assertEqual(result['pagination'], {'offset': 50, 'limit': 50, 'total': 123, 'next_offset': 100})
        self.assertEqual(len(result['tracks']), 50)
        self.assertEqual(result['tracks'][0]['position'], 51)
        self.assertEqual(result['tracks'][0]['name'], live['liked']['tracks'][50]['name'])
        self.assertEqual(self.read(offset=100)['pagination']['next_offset'], None)
        text = json.dumps(result, ensure_ascii=False)
        for private in ('PRIVATE-NO-ECHO', 'A'*32, 'C'*32, 'D'*32, '900051', '87654321', '12345678'):
            self.assertNotIn(private, text)

    def test_search_finds_song_or_artist_without_changing_original_positions(self):
        live = snapshot(track_count=12)
        live['liked']['tracks'][6]['name'] = 'ＳＵＭＭＥＲ · 限定歌曲'
        self.write('在线整理快照.json', live)
        found = self.read(query=' summer ')
        self.assertEqual(found['pagination']['total'], 1)
        self.assertEqual(found['tracks'][0]['position'], 7)
        self.assertEqual(self.read(query='蔡健雅')['pagination']['total'], 12)
        self.assertEqual(self.read(query='不存在')['tracks'], [])
        self.assertEqual(self.read(query='不存在')['counts']['observed'], 12)

    def test_missing_members_and_missing_artist_metadata_remain_distinct(self):
        live = snapshot(track_count=8)
        live['liked']['tracks'].pop()
        first = live['liked']['tracks'][0]
        first.update(artists=[], metadata_available=False)
        live['liked'].update(membership_complete=False, metadata_complete=False,
                             missing_record_count=1, missing_metadata_track_ids=[first['original_id']])
        live['complete'] = False
        self.write('在线整理快照.json', live)
        result = self.read()
        self.assertEqual(result['counts'], {'expected': 8, 'observed': 7, 'missing': 1, 'metadata_missing': 1})
        self.assertFalse(result['tracks'][0]['metadata_available'])
        self.assertEqual(result['tracks'][0]['artists'], [])
        self.assertEqual(result['tracks'][0]['artist_count'], 0)

    def incomplete_snapshot(self):
        live = snapshot(track_count=67)
        tracks = live['liked']['tracks'][:62]
        tracks[0].update(artists=[], metadata_available=False)
        tracks[54].update(name='ＬＡＴＥ · 资料不完整', metadata_available=False)
        live['liked'].update(tracks=tracks, membership_complete=False, metadata_complete=False,
            missing_record_count=5, missing_metadata_track_ids=[tracks[0]['original_id'], tracks[54]['original_id']])
        live['complete'] = False
        return live

    def test_metadata_filter_keeps_global_counts_time_and_cross_page_positions(self):
        self.write('在线整理快照.json', self.incomplete_snapshot())
        with patch('netease_organizer.official_cli.OfficialCli.run_json', side_effect=AssertionError('No account reads')) as cli:
            result = self.read(metadata='incomplete')
        self.assertEqual(result['metadata_filter'], 'incomplete')
        self.assertEqual(result['counts'], {'expected': 67, 'observed': 62, 'missing': 5, 'metadata_missing': 2})
        self.assertEqual(result['pagination'], {'offset': 0, 'limit': 50, 'total': 2, 'next_offset': None})
        self.assertEqual([track['position'] for track in result['tracks']], [1, 55])
        self.assertTrue(all(track['metadata_available'] is False for track in result['tracks']))
        self.assertEqual(result['tracks'][1]['artists'], ['歌手 · 蔡健雅'])
        self.assertEqual(result['updated_at'], '1970-01-01T00:01:40+00:00')
        cli.assert_not_called()

    def test_metadata_filter_intersects_search_before_pagination(self):
        self.write('在线整理快照.json', self.incomplete_snapshot())
        found = self.read(metadata='incomplete', query=' late ')
        self.assertEqual([row['position'] for row in found['tracks']], [55])
        self.assertEqual(found['pagination']['total'], 1)
        self.assertEqual(self.read(metadata='incomplete', query='蔡健雅')['pagination']['total'], 1)
        page = self.read(metadata='incomplete', offset=1, limit=1)
        self.assertEqual([row['position'] for row in page['tracks']], [55])
        self.assertEqual(page['pagination']['total'], 2)
        self.assertIsNone(page['pagination']['next_offset'])
        absent = self.read(metadata='incomplete', query='不存在')
        self.assertEqual(absent['tracks'], [])
        self.assertEqual(absent['counts']['observed'], 62)

    def test_zero_incomplete_empty_and_unread_records_have_distinct_status_and_counts(self):
        self.write('在线整理快照.json', snapshot())
        full = self.read(metadata='incomplete')
        self.assertEqual(full['status'], 'available')
        self.assertEqual(full['counts']['observed'], 8)
        self.assertEqual(full['counts']['metadata_missing'], 0)
        self.assertEqual(full['tracks'], [])
        self.write('在线整理快照.json', snapshot(track_count=0))
        empty = self.read(metadata='incomplete')
        self.assertEqual(empty['status'], 'available')
        self.assertEqual(empty['counts']['observed'], 0)
        self.write('在线整理快照.json', snapshot(names_only=True))
        unread = self.read(metadata='incomplete')
        self.assertEqual(unread['status'], 'not_loaded')
        self.assertIsNone(unread['counts']['observed'])
        self.assertEqual(unread['metadata_filter'], 'incomplete')

    def test_metadata_filter_uses_independent_normal_record_without_loading_account(self):
        self.write('在线整理快照.json', snapshot())
        record = self.save_detail()
        record['playlist']['tracks'][1]['metadata_available'] = False
        record['playlist']['metadata_complete'] = False
        record['playlist']['missing_metadata_track_ids'] = [record['playlist']['tracks'][1]['original_id']]
        record['complete'] = False
        self.write('歌单明细/34567890.json', record)
        result = self.read('34567890', metadata='incomplete')
        self.assertEqual(result['status'], 'available')
        self.assertEqual([row['position'] for row in result['tracks']], [2])
        self.assertEqual(result['counts']['metadata_missing'], 1)
        self.assertEqual(result['counts']['observed'], 2)

    def test_metadata_filter_is_strict_and_default_all_keeps_the_same_result(self):
        self.write('在线整理快照.json', self.incomplete_snapshot())
        self.assertEqual(self.read(), self.read(metadata='all'))
        for invalid in (None, True, 0, [], {}, '', 'INCOMPLETE', ' incomplete', 'false', 'all '):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                self.read(metadata=invalid)

    def test_missing_and_unavailable_echo_filter_without_inventing_tracks(self):
        missing = self.read('99999', metadata='incomplete')
        self.assertEqual(missing['status'], 'missing_playlist')
        self.assertEqual(missing['metadata_filter'], 'incomplete')
        self.write('在线整理快照.json', snapshot())
        self.write('歌单明细/23456789.json', {'bad': True})
        unavailable = self.read(metadata='incomplete')
        self.assertEqual(unavailable['status'], 'unavailable')
        self.assertEqual(unavailable['metadata_filter'], 'incomplete')
        self.assertEqual(unavailable['tracks'], [])
        self.assertIsNone(unavailable['counts']['observed'])

    def test_unread_normal_playlist_is_not_reported_as_an_empty_playlist(self):
        self.write('在线整理快照.json', snapshot())
        result = self.read('34567890')
        self.assertEqual(result['status'], 'not_loaded')
        self.assertEqual(result['counts'], {'expected': 2, 'observed': None, 'missing': None, 'metadata_missing': None})
        self.assertEqual(result['tracks'], [])
        self.assertEqual(self.read('99999')['status'], 'missing_playlist')

    def test_loaded_empty_liked_playlist_is_distinct_from_no_saved_details(self):
        self.write('在线整理快照.json', snapshot(track_count=0))
        self.assertEqual(self.read()['status'], 'available')
        self.assertEqual(self.read()['counts']['observed'], 0)
        self.write('在线整理快照.json', snapshot(names_only=True))
        self.assertEqual(self.read()['status'], 'not_loaded')
        self.assertIsNone(self.read()['counts']['observed'])

    def test_newer_names_directory_can_show_older_details_with_their_own_record_time(self):
        self.write('在线整理快照.json', snapshot(track_count=8), 100)
        names = snapshot(names_only=True, track_count=9)
        self.write('在线名称整理快照.json', names, 200)
        result = self.read()
        self.assertEqual(result['status'], 'available')
        self.assertEqual(result['playlist']['track_count'], 9)
        self.assertEqual(result['counts']['expected'], 8)
        self.assertEqual(result['updated_at'], '1970-01-01T00:01:40+00:00')

    def test_old_other_account_or_changed_liked_identity_cannot_supply_tracks(self):
        self.write('在线整理快照.json', snapshot(), 100)
        for field, value in (('owner', '999'), ('account_id', 'E'*32), ('playlist_id', 'E'*32)):
            names = snapshot(names_only=True)
            if field == 'owner':
                names['account']['original_id'] = value
            elif field == 'account_id':
                names['account']['id'] = value
            else:
                names['liked']['id'] = value
                names['playlists'][1]['id'] = value
            self.write('在线名称整理快照.json', names, 200)
            with self.subTest(field=field):
                self.assertEqual(self.read()['status'], 'not_loaded')
                self.assertEqual(self.read()['tracks'], [])

    def test_conflicting_equal_time_directories_do_not_publish_any_details(self):
        self.write('在线整理快照.json', snapshot(), 100)
        other = snapshot(names_only=True)
        other['account']['original_id'] = '999'
        self.write('在线名称整理快照.json', other, 100)
        self.assertEqual(self.read()['status'], 'missing_playlist')

    def test_invalid_detail_record_preserves_directory_and_fixed_unavailable_status(self):
        live = snapshot()
        for change in ('duplicate_track', 'bad_count', 'surrogate'):
            bad = copy.deepcopy(live)
            if change == 'duplicate_track':
                bad['liked']['tracks'][1] = bad['liked']['tracks'][0]
            elif change == 'bad_count':
                bad['liked']['missing_record_count'] = 1
            else:
                bad['liked']['tracks'][0]['name'] = '\ud800'
            path = self.project / 'artifacts/在线整理快照.json'
            path.write_text(json.dumps(bad, ensure_ascii=True), encoding='utf-8')
            with self.subTest(change=change):
                result = self.read()
                self.assertEqual(result['status'], 'unavailable')
                self.assertEqual(result['playlist']['name'], '我喜欢的音乐')
                self.assertEqual(result['tracks'], [])

    def test_request_ranges_and_query_are_bounded_without_coercion(self):
        for kwargs in ({'limit': 0}, {'limit': 101}, {'limit': True}, {'offset': -1},
                       {'offset': 10001}, {'offset': 1.0}, {'query': 'a'*161},
                       {'query': '\x00'}, {'query': '\ud800'}, {'query': []}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                self.read(**kwargs)
        for key in ('0', '01', '../secret', '1'*21, True):
            with self.subTest(key=key), self.assertRaises(ValueError):
                self.read(key)

    def test_long_artist_lists_are_bounded_without_using_hidden_ids_as_names(self):
        live = snapshot(track_count=1)
        track = live['liked']['tracks'][0]
        track['artists'] = [{'id': f'{i+500:032X}', 'original_id': str(i+500), 'name': '歌手'+str(i)+'甲'*200}
                            for i in range(20)]
        self.write('在线整理快照.json', live)
        result = self.read()
        self.assertEqual(len(result['tracks'][0]['artists']), 8)
        self.assertEqual(result['tracks'][0]['artist_count'], 20)
        self.assertTrue(all(len(name) <= 160 for name in result['tracks'][0]['artists']))
        self.assertEqual(self.read(query='歌手19')['pagination']['total'], 1)

    def test_account_switch_between_directory_and_detail_reads_cannot_mix_account_titles_and_songs(self):
        from netease_organizer.web_state import _project_directory
        live = snapshot(track_count=1)
        live['liked']['tracks'][0]['name'] = 'ACCOUNT-A-SONG'
        self.write('在线整理快照.json', live, 100)
        other = snapshot(names_only=True, track_count=3)
        other['account'].update(original_id='999', id='E'*32)
        other['liked'].update(name='ACCOUNT-B-PLAYLIST', creator_id='E'*32)
        other['playlists'][1]['name'] = 'ACCOUNT-B-PLAYLIST'
        def switch_account(project, timestamp, directory):
            self.write('在线名称整理快照.json', other, 200)
            return _project_directory(project, timestamp, directory)
        with patch('netease_organizer.web_tracks._project_directory', side_effect=switch_account):
            result = self.read()
        self.assertNotEqual(result['status'], 'available')
        self.assertEqual(result['tracks'], [])

    def test_surrogate_playlist_titles_never_escape_a_public_dto(self):
        live = snapshot()
        live['playlists'][0]['name'] = '\ud800'
        (self.project / 'artifacts/在线整理快照.json').write_text(json.dumps(live, ensure_ascii=True), encoding='utf-8')
        result = self.read('34567890')
        self.assertNotEqual(result['status'], 'available')
        json.dumps(result, ensure_ascii=False).encode('utf-8')

    def test_source_switch_during_detail_read_discards_already_projected_songs(self):
        from netease_organizer.web_preview import _load
        self.write('在线整理快照.json', snapshot(), 100)
        def replace_after_read(project, name, maximum):
            read = _load(project, name, maximum)
            changed = snapshot(names_only=True)
            changed['account']['original_id'] = '999'
            self.write('在线名称整理快照.json', changed, 200)
            return read
        with patch('netease_organizer.web_tracks._load', side_effect=replace_after_read):
            result = self.read()
        self.assertEqual(result['status'], 'unavailable')
        self.assertEqual(result['tracks'], [])
        self.assertIsNone(result['counts']['observed'])
        self.assertEqual(result['pagination']['total'], 0)

    def test_directory_reader_rejects_growth_after_initial_size_check_and_duplicate_keys(self):
        from netease_organizer.web_state import _load
        path = self.write('local.json', {'kind': 'record'})
        original_open = Path.open
        changed = False
        def grow_before_open(target, *args, **kwargs):
            nonlocal changed
            if target == path and not changed:
                changed = True
                with original_open(target, 'wb') as stream:
                    stream.write(b'{"kind":"record"}' + b' '*1024)
            return original_open(target, *args, **kwargs)
        with patch.object(Path, 'open', grow_before_open):
            self.assertEqual(_load(self.project, 'local.json', maximum=256), (None, 0))
        path.write_text('{"kind":"first","kind":"second"}', encoding='utf-8')
        self.assertEqual(_load(self.project, 'local.json'), (None, 0))
