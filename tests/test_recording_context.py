"""Optional historical metadata uses disposable, fully bound local sources."""

import copy
import importlib
import importlib.util
import json
import os
from pathlib import Path
import stat
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from tests import test_web_classification as fixtures
from tests.test_web_classification import REPORT


BASELINE = '全库分类-账号基线.json'
HISTORY = '网易云本地歌单快照.json'
STAMP = '2026-10-03T21:17:36+08:00'


def optional_sources(fixture):
    """A complete member list may legitimately have incomplete artist metadata."""
    tracks, historical = [], {}
    for index, row in enumerate(fixture.report['records']):
        original = str((index + 1) * 11)
        artists = [{'id': f'{1000 + index * 10 + j:032X}',
                    'original_id': str(1000 + index * 10 + j), 'name': name}
                   for j, name in enumerate(row['artists'].split(' / ') if row['artists'] else [])]
        available = bool(artists)
        row['live_metadata_available'] = available
        tracks.append({'id': fixture.ids[index], 'original_id': original,
                       'name': row['name'], 'artists': artists, 'metadata_available': available})
        historical[original] = {'id': original, 'name': row['name'],
                               'artists': [{'id': artist['original_id'], 'name': artist['name']}
                                           for artist in artists],
                               'album': {'id': str(500 + index), 'name': f'历史专辑 {index + 1}'},
                               'duration_ms': 90001 + index * 1000}
    missing = [track['original_id'] for track in tracks if not track['metadata_available']]
    liked = {**copy.deepcopy(fixture.directory['liked']), 'creator_id': 'A' * 32,
             'tracks': tracks, 'membership_complete': True, 'metadata_complete': not missing,
             'missing_record_count': 0, 'missing_metadata_track_ids': missing}
    baseline = {'kind': 'classification_source_snapshot', 'created_at': STAMP, 'snapshot': {
        'account': copy.deepcopy(fixture.directory['account']),
        'playlists': [copy.deepcopy(fixture.directory['liked'])], 'liked': liked,
        'overview_complete': True, 'tracks_loaded': True, 'complete': not missing}}
    history = {'format': 'netease-offline-playlist-snapshot-v1', 'generated_at': STAMP,
               'source_kind': 'local_offline_cache', 'live_account_verified': False,
               'tracks': historical, 'private': 'HISTORY-SECRET'}
    return baseline, history


class RecordingContextTests(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(importlib.util.find_spec('netease_organizer.recording_context'),
                             'optional recording context module is not implemented')
        self.context = importlib.import_module('netease_organizer.recording_context')
        self.fixture = self.make_fixture()

    def make_fixture(self):
        fixture = fixtures.WebClassificationTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        fixture.baseline, fixture.history = optional_sources(fixture)
        fixture.write(REPORT, fixture.report, 101)
        fixture.write(BASELINE, fixture.baseline, 102)
        fixture.write(HISTORY, fixture.history, 103)
        page = fixture.view()
        fixture.version = page['quality']['source_version']
        fixture.keys = [row['record_key'] for row in page['records']]
        return fixture

    def contexts(self, fixture=None, **kwargs):
        fixture = fixture or self.fixture
        return self.context.recording_contexts(fixture.project,
                                               kwargs.get('version', fixture.version),
                                               kwargs.get('keys', fixture.keys))

    def test_bound_historical_metadata_has_source_timestamp_and_public_fields_only(self):
        values = self.contexts()
        self.assertEqual(values[self.fixture.keys[0]], {
            'source': 'historical_snapshot', 'generated_at': STAMP,
            'album': '历史专辑 1', 'duration_ms': 90001})
        self.assertEqual(len(values), 4)
        public = json.dumps(values, ensure_ascii=False)
        for private in ('HISTORY-SECRET', 'account_original_id', 'original_id', '12345678', 'A' * 32):
            self.assertNotIn(private, public)
        self.assertEqual(len(self.contexts(keys=self.fixture.keys[:1])), 1)

    def test_incomplete_live_metadata_uses_historical_fallback_without_deduplicating(self):
        baseline, history = self.fixture.baseline, self.fixture.history
        track = baseline['snapshot']['liked']['tracks'][2]
        track.update(name='在线占位标题', artists=[])
        history['tracks']['33']['artists'] = [{'name': '重复艺人'}, {'name': '重复艺人'}]
        self.fixture.report['records'][2]['artists'] = '重复艺人 / 重复艺人'
        self.fixture.write(BASELINE, baseline, 104)
        self.fixture.write(HISTORY, history, 104)
        self.fixture.write(REPORT, self.fixture.report, 104)
        page = self.fixture.view()
        self.fixture.version = page['quality']['source_version']
        self.assertIn(self.fixture.keys[2], self.contexts())

    def test_baseline_global_identity_mismatches_discard_all_contexts(self):
        mutations = [
            lambda b: b['snapshot']['account'].update(id='F' * 32),
            lambda b: b['snapshot']['account'].update(original_id='99'),
            lambda b: b['snapshot']['liked'].update(id='F' * 32),
            lambda b: b['snapshot']['liked'].update(original_id='99'),
            lambda b: b['snapshot']['liked'].update(creator_id='F' * 32),
            lambda b: b['snapshot']['liked'].update(track_count=5),
            lambda b: b['snapshot']['liked'].update(membership_complete=False),
            lambda b: b['snapshot']['liked']['tracks'].reverse(),
            lambda b: b['snapshot']['liked']['tracks'][1].update(id=self.fixture.ids[0]),
            lambda b: b['snapshot']['liked']['tracks'][1].update(original_id='11'),
        ]
        for mutate in mutations:
            with self.subTest(mutate=mutate):
                self.assertTrue(self.contexts())
                changed = copy.deepcopy(self.fixture.baseline)
                mutate(changed)
                self.fixture.write(BASELINE, changed, 104)
                self.assertEqual(self.contexts(), {})
                self.fixture.write(BASELINE, self.fixture.baseline, 102)

    def test_missing_or_conflicting_historical_song_suppresses_only_that_song(self):
        mutations = [lambda h: h['tracks'].pop('11'),
                     lambda h: h['tracks']['11'].update(id='999'),
                     lambda h: h['tracks']['11'].update(name='其他录音'),
                     lambda h: h['tracks']['11'].update(artists=[{'name': '其他艺人'}])]
        for mutate in mutations:
            with self.subTest(mutate=mutate):
                changed = copy.deepcopy(self.fixture.history)
                mutate(changed)
                self.fixture.write(HISTORY, changed, 104)
                values = self.contexts()
                self.assertNotIn(self.fixture.keys[0], values)
                self.assertEqual(len(values), 3)

    def test_album_and_duration_invalidity_is_field_local(self):
        cases = [(None, None, None, None), ('', 0, None, None),
                 ('专辑', 86400000, '专辑', 86400000),
                 ('x' * 513, True, None, None), ('坏\n专辑', 1.5, None, None),
                 ('🛰' * 512, 86400001, '🛰' * 512, None),
                 ({'name': '对象专辑'}, -1, '对象专辑', None)]
        for album, duration, want_album, want_duration in cases:
            with self.subTest(album=str(album)[:20], duration=duration):
                changed = copy.deepcopy(self.fixture.history)
                changed['tracks']['11'].update(album=album, duration_ms=duration)
                self.fixture.write(HISTORY, changed, 104)
                value = self.contexts()[self.fixture.keys[0]]
                self.assertEqual(value['album'], want_album)
                self.assertEqual(value['duration_ms'], want_duration)

    def test_report_metadata_flag_is_optional_but_when_present_must_match_baseline(self):
        self.fixture.report['records'][0].pop('live_metadata_available')
        self.fixture.write(REPORT, self.fixture.report, 104)
        self.fixture.version = self.fixture.view()['quality']['source_version']
        self.assertIn(self.fixture.keys[0], self.contexts())
        for flag in (False, 1, 'true'):
            self.fixture.report['records'][0]['live_metadata_available'] = flag
            self.fixture.write(REPORT, self.fixture.report, 105)
            self.fixture.version = self.fixture.view()['quality']['source_version']
            self.assertNotIn(self.fixture.keys[0], self.contexts())

    def test_bad_optional_json_provenance_or_schema_discards_context_not_core(self):
        path = self.fixture.project / 'artifacts' / HISTORY
        valid = path.read_bytes()
        malformed = [b'', b'{', b'[]', b'{"tracks":{},"tracks":{}}',
                     b'{"tracks":{},"generated_at":NaN}']
        for field, value in [('format', 'unknown'), ('generated_at', '2026-10-03'),
                             ('generated_at', STAMP + '\x00'), ('tracks', [])]:
            changed = copy.deepcopy(self.fixture.history)
            changed[field] = value
            malformed.append(json.dumps(changed).encode())
        for raw in malformed:
            with self.subTest(raw=raw[:40]):
                path.write_bytes(raw)
                self.assertEqual(self.contexts(), {})
                self.assertEqual(self.fixture.view()['status'], 'available')
        path.write_bytes(valid)
        self.assertTrue(self.contexts())
        path.unlink()
        self.assertEqual(self.contexts(), {})

    def test_nonobject_snapshot_and_nested_shapes_never_escape_optional_boundary(self):
        for value in (None, [], 'invalid', 1, True):
            with self.subTest(snapshot=value):
                changed = copy.deepcopy(self.fixture.baseline)
                changed['snapshot'] = value
                self.fixture.write(BASELINE, changed, 104)
                self.assertEqual(self.contexts(), {})
                self.assertEqual(self.fixture.view()['status'], 'available')
        for field in ('account', 'liked', 'playlists'):
            changed = copy.deepcopy(self.fixture.baseline)
            changed['snapshot'][field] = None
            self.fixture.write(BASELINE, changed, 104)
            self.assertEqual(self.contexts(), {})
            self.assertEqual(self.fixture.view()['status'], 'available')
        for field in ('tracks',):
            changed = copy.deepcopy(self.fixture.baseline)
            changed['snapshot']['liked'][field] = None
            self.fixture.write(BASELINE, changed, 104)
            self.assertEqual(self.contexts(), {})
        changed = copy.deepcopy(self.fixture.baseline)
        changed['snapshot']['liked']['tracks'][0]['artists'] = None
        self.fixture.write(BASELINE, changed, 104)
        self.assertEqual(self.contexts(), {})

    def test_warm_cache_rehashes_without_parsing_and_returns_independent_values(self):
        first = self.contexts()
        first[self.fixture.keys[0]]['album'] = 'caller mutation'
        with patch.object(self.context.json, 'loads', side_effect=AssertionError('warm parse')):
            second = self.contexts()
        self.assertEqual(second[self.fixture.keys[0]]['album'], '历史专辑 1')
        serialized = json.dumps(list(self.context._CACHE.values()), ensure_ascii=False, default=str)
        for private in ('HISTORY-SECRET', 'account_original_id', 'candidate_track_ids', 'original_id'):
            self.assertNotIn(private, serialized)

    def test_same_size_restored_mtime_change_invalidates_content_cache(self):
        signature = self.context._signature
        def stable_ctime(info):
            value = list(signature(info))
            value[5] = 0  # Model Windows' unchanged path creation-time field.
            return tuple(value)
        with patch.object(self.context, '_signature', side_effect=stable_ctime):
            self.assertTrue(self.contexts())
            path = self.fixture.project / 'artifacts' / HISTORY
            before = path.stat()
            raw = path.read_bytes().replace('历史专辑 1'.encode(), '历史专辑 X'.encode())
            path.write_bytes(raw)
            os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns))
            self.assertEqual(self.contexts()[self.fixture.keys[0]]['album'], '历史专辑 X')

    def test_oversize_and_reparse_ancestor_invalidate_only_optional_projection(self):
        self.assertTrue(self.contexts())
        with patch.object(self.context, 'MAX_HISTORY_BYTES', 16):
            self.assertEqual(self.contexts(), {})
        self.assertTrue(self.contexts())
        original = Path.lstat
        def lstat(candidate, *args, **kwargs):
            result = original(candidate, *args, **kwargs)
            if candidate == self.fixture.project:
                return SimpleNamespace(**{name: getattr(result, name) for name in
                    ('st_dev', 'st_ino', 'st_mode', 'st_size', 'st_mtime_ns', 'st_ctime_ns')},
                    st_file_attributes=getattr(stat, 'FILE_ATTRIBUTE_REPARSE_POINT', 0x400))
            return result
        with patch.object(Path, 'lstat', lstat):
            self.assertEqual(self.contexts(), {})
        self.assertTrue(self.contexts())

    def test_descriptor_changes_during_bounded_read_discard_context(self):
        original = os.fstat
        calls = 0
        def fstat(descriptor):
            nonlocal calls
            calls += 1
            result = original(descriptor)
            if calls == 2:
                values = {name: getattr(result, name) for name in
                          ('st_dev', 'st_ino', 'st_mode', 'st_size', 'st_mtime_ns', 'st_ctime_ns')}
                values['st_ctime_ns'] += 1
                return SimpleNamespace(**values)
            return result
        with patch.object(os, 'fstat', fstat):
            self.assertEqual(self.contexts(), {})
        self.assertTrue(self.contexts())

    def test_two_project_lru_evicts_old_public_projection(self):
        first = self.fixture
        second, third = self.make_fixture(), self.make_fixture()
        self.contexts(first)
        self.contexts(second)
        self.contexts(third)
        with patch.object(self.context.json, 'loads', wraps=self.context.json.loads) as parse:
            self.assertTrue(self.contexts(first))
        self.assertGreater(parse.call_count, 0)
        self.assertLessEqual(len(self.context._CACHE), 2)

    def test_bad_request_or_wrong_expected_source_version_returns_no_context(self):
        for keys in ('not-a-list', ['x' * 32], [self.fixture.keys[0]] * 101):
            self.assertEqual(self.contexts(keys=keys), {})
        for version in ('x', 'f' * 64):
            self.assertEqual(self.contexts(version=version), {})

    def test_nonregular_unreadable_and_reparse_optional_sources_discard_warm_context(self):
        path = self.fixture.project / 'artifacts' / HISTORY
        self.assertTrue(self.contexts())
        raw = path.read_bytes()
        path.unlink()
        path.mkdir()
        self.assertEqual(self.contexts(), {})
        path.rmdir()
        path.write_bytes(raw)
        original = Path.lstat
        for denied in (True, False):
            def lstat(candidate, *args, **kwargs):
                result = original(candidate, *args, **kwargs)
                if candidate == path:
                    if denied:
                        raise PermissionError('PRIVATE optional path')
                    return SimpleNamespace(**{name: getattr(result, name) for name in
                        ('st_dev', 'st_ino', 'st_mode', 'st_size', 'st_mtime_ns', 'st_ctime_ns')},
                        st_file_attributes=getattr(stat, 'FILE_ATTRIBUTE_REPARSE_POINT', 0x400))
                return result
            with patch.object(Path, 'lstat', lstat):
                self.assertEqual(self.contexts(), {})

    def test_optional_change_during_cold_context_build_discards_result(self):
        build = self.context._build_contexts
        path = self.fixture.project / 'artifacts' / HISTORY
        def changed(*args, **kwargs):
            result = build(*args, **kwargs)
            path.write_bytes(path.read_bytes() + b' ')
            return result
        self.context._CACHE.clear()
        with patch.object(self.context, '_build_contexts', side_effect=changed):
            self.assertEqual(self.contexts(), {})
        self.assertTrue(self.contexts())

    def test_unrelated_directory_entry_change_does_not_invalidate_bound_context(self):
        build = self.context._build_contexts
        def changed(*args, **kwargs):
            result = build(*args, **kwargs)
            (self.fixture.project / 'artifacts' / 'unrelated.txt').write_text('unrelated')
            return result
        self.context._CACHE.clear()
        with patch.object(self.context, '_build_contexts', side_effect=changed):
            self.assertEqual(len(self.contexts()), 4)


class RecordingContextIntegrationTests(unittest.TestCase):
    def test_optional_context_overlays_response_without_entering_core_cache(self):
        fixture = fixtures.WebClassificationTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        first = fixture.view()
        self.assertIn('recording_context', first['records'][0])
        self.assertIsNone(first['records'][0]['recording_context'])
        baseline, history = optional_sources(fixture)
        fixture.write(REPORT, fixture.report, 101)
        fixture.write(BASELINE, baseline, 102)
        fixture.write(HISTORY, history, 103)
        original = fixture.view()
        fixture.write(HISTORY, {'broken': True}, 104)
        damaged = fixture.view()
        self.assertEqual(damaged['status'], 'available')
        self.assertEqual(damaged['quality'], original['quality'])
        self.assertTrue(all(row['recording_context'] is None for row in damaged['records']))
        for old, new in zip(original['records'], damaged['records']):
            self.assertEqual({k: v for k, v in old.items() if k != 'recording_context'},
                             {k: v for k, v in new.items() if k != 'recording_context'})
        from netease_organizer import web_classification
        for entry in web_classification._PROJECTION_CACHE.values():
            self.assertNotIn('recording_context', entry[1]['records'][0])

    def test_optional_damage_does_not_change_saved_draft_or_prevent_next_save(self):
        from netease_organizer import web_classification
        fixture = fixtures.WebClassificationTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        baseline, history = optional_sources(fixture)
        fixture.write(REPORT, fixture.report, 101)
        fixture.write(BASELINE, baseline, 102)
        fixture.write(HISTORY, history, 103)
        page = fixture.view()
        row = page['records'][0]
        request = {'action': 'save', 'source_version': page['quality']['source_version'],
                   'revision': 0, 'record_key': row['record_key'], 'styles': row['styles'],
                   'scenes': ['放松睡前'], 'language': row['language'],
                   'reason': '用户试听后的本地修正', 'recording_note': '保留用户的录音备注'}
        web_classification.save_local_classification_draft(fixture.project, request)
        before = fixture.view()
        draft_path = fixture.project / '.organizer' / 'classification-draft.json'
        saved = draft_path.read_bytes()
        fixture.write(BASELINE, {'snapshot': None}, 104)
        after = fixture.view()
        self.assertEqual(after['quality'], before['quality'])
        self.assertEqual(after['records'][0]['draft'], before['records'][0]['draft'])
        self.assertEqual(draft_path.read_bytes(), saved)
        request.update(revision=1, reason='再次试听后更新本地依据')
        self.assertEqual(web_classification.save_local_classification_draft(fixture.project, request)['revision'], 2)
