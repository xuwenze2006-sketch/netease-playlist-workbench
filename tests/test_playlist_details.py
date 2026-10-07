"""Strict saved details and stable file reads, using disposable records only."""

import copy
import importlib
import importlib.util
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch


def record():
    artist = {'id': 'e'*32, 'original_id': 701, 'name': '已记录歌手'}
    return {
        'kind': 'playlist_details', 'version': 1, 'read_at': 1700000000000,
        'account': {'id': 'a'*32, 'original_id': 42}, 'complete': True,
        'playlist': {
            'id': 'b'*32, 'original_id': 9000, 'name': '英语 🎵', 'track_count': 2,
            'special_type': 50, 'track_update_time': 100, 'creator_id': 'a'*32,
            'tracks': [
                {'id': 'c'*32, 'original_id': 501, 'name': '歌曲一',
                 'artists': [copy.deepcopy(artist)], 'metadata_available': True},
                {'id': 'd'*32, 'original_id': '502', 'name': '歌曲二',
                 'artists': [copy.deepcopy(artist)], 'metadata_available': True},
            ],
            'membership_complete': True, 'metadata_complete': True,
            'missing_record_count': 0, 'missing_metadata_track_ids': [],
        },
    }


class SavedDetailsTests(unittest.TestCase):
    def module(self):
        self.assertIsNotNone(importlib.util.find_spec('netease_organizer.playlist_details'),
                             'The strict local details module must exist')
        return importlib.import_module('netease_organizer.playlist_details')

    def test_whitelist_normalizes_ids_without_mutating_input_or_leaking_unknown_fields(self):
        raw = record()
        raw['token'] = 'PRIVATE-NOT-ECHO'
        raw['account']['private_key'] = 'PRIVATE-NOT-ECHO'
        raw['playlist']['tracks'][0]['request_body'] = 'PRIVATE-NOT-ECHO'
        before = copy.deepcopy(raw)
        normalized = self.module().validate_record(raw)
        self.assertEqual(raw, before)
        self.assertEqual(normalized['account'], {'id': 'A'*32, 'original_id': '42'})
        self.assertEqual(normalized['playlist']['id'], 'B'*32)
        self.assertEqual(normalized['playlist']['tracks'][0]['original_id'], '501')
        self.assertEqual(normalized['playlist']['tracks'][0]['artists'][0]['id'], 'E'*32)
        self.assertNotIn('PRIVATE-NOT-ECHO', json.dumps(normalized))
        normalized['playlist']['tracks'].clear()
        self.assertEqual(len(raw['playlist']['tracks']), 2)

    def test_known_empty_and_partial_members_with_known_but_incomplete_artists_remain_distinct(self):
        raw = record()
        raw['playlist'].update(track_count=0, tracks=[])
        self.assertTrue(self.module().validate_record(raw)['complete'])
        partial = record()
        first = partial['playlist']['tracks'][0]
        first['metadata_available'] = False
        partial['playlist'].update(track_count=3, membership_complete=False, metadata_complete=False,
                                   missing_record_count=1, missing_metadata_track_ids=[501])
        partial['complete'] = False
        normalized = self.module().validate_record(partial)
        self.assertFalse(normalized['complete'])
        self.assertEqual(normalized['playlist']['missing_record_count'], 1)
        self.assertEqual(normalized['playlist']['missing_metadata_track_ids'], ['501'])
        self.assertEqual(normalized['playlist']['tracks'][0]['artists'][0]['name'], '已记录歌手')
        first['artists'] = []
        self.assertFalse(self.module().validate_record(partial)['playlist']['tracks'][0]['metadata_available'])

    def test_valid_upper_time_bounds_are_allowed_but_bools_floats_and_overflow_are_not(self):
        raw = record()
        raw['read_at'] = 253402300799999
        raw['playlist']['track_update_time'] = 10**18
        self.assertEqual(self.module().validate_record(raw)['read_at'], 253402300799999)
        for path, bad in [(('read_at',), 253402300800000), (('read_at',), True),
                          (('version',), True), (('complete',), 1),
                          (('playlist', 'track_count'), True), (('playlist', 'track_count'), 10001),
                          (('playlist', 'special_type'), 1000001),
                          (('playlist', 'track_update_time'), 10**18+1),
                          (('playlist', 'track_update_time'), 100.0)]:
            altered = record()
            parent = altered if len(path) == 1 else altered[path[0]]
            parent[path[-1]] = bad
            with self.subTest(path=path, bad=bad), self.assertRaises(ValueError):
                self.module().validate_record(altered)

    def test_owner_safe_unicode_canonical_decimal_and_exact_flags_are_required(self):
        changes = [
            lambda raw: raw['playlist'].update(creator_id='f'*32),
            lambda raw: raw['account'].update(original_id='042'),
            lambda raw: raw['account'].update(original_id=10**20),
            lambda raw: raw['playlist'].update(name='bad\ud800'),
            lambda raw: raw['playlist'].update(name='bad\x00'),
            lambda raw: raw['playlist'].update(name='bad\x85'),
            lambda raw: raw['playlist']['tracks'][0].update(name='x'*513),
            lambda raw: raw['playlist'].update(missing_record_count=1),
            lambda raw: raw['playlist'].update(membership_complete=1),
            lambda raw: raw['playlist'].update(metadata_complete=False),
            lambda raw: raw['playlist']['tracks'][0].update(metadata_available=True, artists=[]),
        ]
        for change in changes:
            raw = record(); change(raw)
            with self.subTest(change=change), self.assertRaises(ValueError) as error:
                self.module().validate_record(raw)
            self.assertNotIn('bad', str(error.exception))

    def test_track_and_artist_double_identity_conflicts_or_name_drift_are_rejected(self):
        changes = [
            lambda p: p['tracks'].__setitem__(1, copy.deepcopy(p['tracks'][0])),
            lambda p: p['tracks'][1].update(id='C'*32),
            lambda p: p['tracks'][1].update(original_id=501),
            lambda p: p['tracks'][1]['artists'][0].update(original_id=702),
            lambda p: p['tracks'][1]['artists'][0].update(id='f'*32),
            lambda p: p['tracks'][1]['artists'][0].update(name='不同名称'),
            lambda p: p['tracks'][0]['artists'].append(copy.deepcopy(p['tracks'][0]['artists'][0])),
        ]
        for change in changes:
            raw = record(); change(raw['playlist'])
            with self.subTest(change=change), self.assertRaises(ValueError):
                self.module().validate_record(raw)
        distinct = record()
        distinct['playlist']['tracks'][1]['artists'][0].update(id='f'*32, original_id=702)
        self.assertEqual(self.module().validate_record(distinct)['playlist']['tracks'][1]['artists'][0]['name'], '已记录歌手')

    def test_artist_limit_and_missing_member_metadata_flags_follow_saved_records(self):
        raw = record()
        raw['playlist']['tracks'] = raw['playlist']['tracks'][:1]
        raw['playlist'].update(track_count=2, membership_complete=False, missing_record_count=1)
        raw['complete'] = False
        artists = [{'id': f'{index:032x}', 'original_id': index, 'name': f'歌手{index}'}
                   for index in range(1, 101)]
        raw['playlist']['tracks'][0]['artists'] = artists
        normalized = self.module().validate_record(raw)
        self.assertFalse(normalized['playlist']['membership_complete'])
        self.assertTrue(normalized['playlist']['metadata_complete'])
        artists.append({'id': f'{101:032x}', 'original_id': 101, 'name': '歌手101'})
        with self.assertRaises(ValueError):
            self.module().validate_record(raw)
        raw['playlist']['tracks'][0]['artists'] = artists[:100]
        raw['playlist']['missing_record_count'] = True
        with self.assertRaises(ValueError):
            self.module().validate_record(raw)


class SavedDetailsFileTests(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(importlib.util.find_spec('netease_organizer.playlist_details'),
                             'The strict local details module must exist')
        self.module = importlib.import_module('netease_organizer.playlist_details')
        self.temp = tempfile.TemporaryDirectory(prefix='organizer-details-store-test-')
        self.addCleanup(self.temp.cleanup)
        self.project = Path(self.temp.name)
        self.folder = self.project/'artifacts/歌单明细'
        self.folder.mkdir(parents=True)
        self.path = self.folder/'9000.json'

    def write(self, value=None):
        self.path.write_text(json.dumps(record() if value is None else value, ensure_ascii=False), encoding='utf-8')

    def test_missing_and_bad_existing_return_none_good_record_is_bound_to_file_key(self):
        self.assertIsNone(self.module.load_record(self.project, '9000'))
        self.write()
        result, timestamp = self.module.load_record(self.project, '9000')
        self.assertEqual(result['playlist']['original_id'], '9000')
        self.assertEqual(timestamp, self.path.stat().st_mtime_ns)
        bad = record(); bad['playlist']['original_id'] = '9001'; self.write(bad)
        self.assertIsNone(self.module.load_record(self.project, '9000'))
        for key in ('../9000', '0', '09000', '1'*21, 9000, True):
            with self.subTest(key=key):
                self.assertIsNone(self.module.load_record(self.project, key))

    def test_duplicate_json_keys_nan_and_oversized_files_are_rejected(self):
        self.write()
        clean = self.path.read_text(encoding='utf-8')
        for raw in (clean.replace('"complete": true', '"complete": false, "complete": true'),
                    clean[:-1]+', "unknown": NaN}',
                    ' '* (16*1024*1024+1)):
            self.path.write_text(raw, encoding='utf-8')
            self.assertIsNone(self.module.load_record(self.project, '9000'))

    def test_replacing_path_between_stat_and_open_cannot_return_a_different_generation(self):
        self.write()
        original_open = Path.open
        original_stat = self.path.stat()
        replacement = self.folder/'replacement.json'
        changed = record(); changed['playlist']['name'] = '德语 🎵'
        replacement.write_text(json.dumps(changed, ensure_ascii=False), encoding='utf-8')
        os.utime(replacement, ns=(original_stat.st_atime_ns, original_stat.st_mtime_ns))
        replaced = False
        def open_replaced(path, *args, **kwargs):
            nonlocal replaced
            if path == self.path and args == ('rb',) and not replaced:
                replaced = True
                os.replace(replacement, self.path)
            return original_open(path, *args, **kwargs)
        with patch.object(Path, 'open', open_replaced):
            self.assertIsNone(self.module.load_record(self.project, '9000'))
        self.assertTrue(replaced)

    def test_growth_during_read_is_bounded_and_refused(self):
        self.write()
        original_open = Path.open
        observed_sizes = []
        payload = json.dumps(record(), ensure_ascii=False)[:-1]+', "unused":"'+('x'*(16*1024*1024))+'"}'
        class GrowingStream:
            def __init__(self, stream): self.stream = stream
            def __enter__(self): return self
            def __exit__(self, *args): self.stream.close()
            def fileno(self): return self.stream.fileno()
            def read(inner, size=-1):
                observed_sizes.append(size)
                with original_open(self.path, 'wb') as writer:
                    writer.write(payload.encode('utf-8'))
                return inner.stream.read(size)
        def changing_open(path, *args, **kwargs):
            stream = original_open(path, *args, **kwargs)
            return GrowingStream(stream) if path == self.path and args == ('rb',) else stream
        with patch.object(Path, 'open', changing_open):
            self.assertIsNone(self.module.load_record(self.project, '9000'))
        self.assertEqual(observed_sizes, [16*1024*1024+1])

    def test_file_and_parent_symlinks_never_supply_a_record(self):
        self.write()
        original = self.folder/'source.json'
        self.path.rename(original)
        try:
            self.path.symlink_to(original)
        except (OSError, NotImplementedError):
            self.skipTest('Creating symlinks is unavailable for this Windows user')
        self.assertIsNone(self.module.load_record(self.project, '9000'))
        self.path.unlink()
        original.rename(self.path)
        moved = self.project/'temporary-records'
        self.folder.rename(moved)
        try:
            self.folder.symlink_to(moved, target_is_directory=True)
            self.assertIsNone(self.module.load_record(self.project, '9000'))
        finally:
            if self.folder.is_symlink():
                self.folder.unlink()  # Unlink only the link, never recursively delete its target.


if __name__ == '__main__':
    unittest.main()
