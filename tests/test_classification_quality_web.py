import hashlib
import http.client
import json
import threading
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.parse import urlencode

from netease_organizer.classification_drafts import DraftConflict
from netease_organizer import web_classification
from netease_organizer.web_server import create_server, shutdown_server
import test_web_classification as fixtures
from test_web_server import FakeController


class QualityProjectionTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.WebClassificationTests(methodName='runTest')
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)

    def test_conflict_and_low_score_are_visible_without_changing_old_pending(self):
        f = self.fixture
        f.records[0]['review_note'] = True
        f.records[1]['style_judgment_score'] = .65
        f.write('全库分类-逐曲结果.json', f.report, 101)
        page = f.view()
        self.assertEqual(page['quality']['review_count'], 4)
        self.assertTrue(page['records'][0]['needs_review'])
        self.assertIn('录音或语言证据存在冲突', page['records'][0]['review_reasons'])
        self.assertEqual([r['position'] for r in f.view(review='pending')['records']], [3, 4])
        self.assertEqual([r['position'] for r in f.view(review='conflict')['records']], [1])
        self.assertEqual([r['position'] for r in f.view(review='low_confidence')['records']], [2])

    def test_generic_explanation_has_separate_review_filter(self):
        f = self.fixture
        f.records[0]['evidence_note'] = '具体曲目、艺人与专辑的宽风格模型判断'
        f.write('全库分类-逐曲结果.json', f.report, 101)
        self.assertEqual([r['position'] for r in f.view(review='weak_evidence')['records']], [1])

    def test_score_is_validated_and_old_records_without_score_still_load(self):
        f = self.fixture
        for bad in (True, '0.9', -1, 1.01, float('nan')):
            with self.subTest(score=bad):
                f.records[0]['style_judgment_score'] = bad
                f.write('全库分类-逐曲结果.json', f.report, 101)
                self.assertEqual(f.view()['status'], 'unavailable')
        f.records[0].pop('style_judgment_score')
        f.write('全库分类-逐曲结果.json', f.report, 101)
        self.assertIsNone(f.view()['records'][0]['style_judgment_score'])

    def test_pilot_keeps_source_order_and_keys_are_opaque_stable(self):
        f = self.fixture
        first = f.view()
        self.assertEqual(first['quality']['pilot_positions'], [1, 2, 3, 4])
        self.assertEqual([r['position'] for r in f.view(review='pilot')['records']], [1, 2, 3, 4])
        keys = [r['record_key'] for r in first['records']]
        self.assertEqual(len(set(keys)), 4)
        for key in keys:
            self.assertRegex(key, r'^[a-f0-9]{32}$')
        f.records[0]['evidence_note'] += '，补充录音依据'
        f.write('全库分类-逐曲结果.json', f.report, 101)
        second = f.view()
        self.assertEqual(keys, [r['record_key'] for r in second['records']])
        self.assertNotEqual(first['quality']['source_version'], second['quality']['source_version'])

    def save(self, position=1, **labels):
        page = self.fixture.view()
        row = page['records'][position - 1]
        request = {'action': 'save', 'source_version': page['quality']['source_version'],
                   'revision': page['quality']['revision'], 'record_key': row['record_key'],
                   'styles': row['styles'], 'scenes': row['scenes'], 'language': row['language'],
                   'reason': '核对具体录音后保留本地修正建议', 'recording_note': '离线测试录音定位'}
        request.update(labels)
        return web_classification.save_local_classification_draft(self.fixture.project, request)

    def test_draft_basis_filters_merged_labels_and_options_without_rewriting_source(self):
        f = self.fixture
        original = f.view()
        self.save(scenes=['放松睡前'], styles=['电子舞曲'])
        default = f.view(dimension='scene', tag='通勤散步')
        self.assertEqual(default['filters']['basis'], 'original')
        self.assertEqual([r['position'] for r in default['records']], [1, 2])
        draft = f.view(basis='draft', dimension='scene', tag='放松睡前')
        self.assertEqual([r['position'] for r in draft['records']], [1])
        self.assertEqual(draft['records'][0]['scenes'], ['通勤散步'])
        self.assertEqual(draft['records'][0]['draft']['scenes'], ['放松睡前'])
        self.assertIn('电子舞曲', draft['options']['style'])
        self.assertNotIn('电子舞曲', default['options']['style'])
        self.assertEqual(draft['summary'], original['summary'])
        self.assertEqual(draft['playlists'], original['playlists'])
        self.assertEqual([r['position'] for r in f.view(basis='draft', tag='通勤散步')['records']], [2])

    def test_draft_pending_is_derived_while_original_review_diagnostics_remain(self):
        f = self.fixture
        self.save(3, styles=['流行抒情'])
        draft = f.view(basis='draft', review='pending')
        self.assertEqual([r['position'] for r in draft['records']], [4])
        self.assertEqual(draft['quality']['draft_summary'], {
            'pending_count': 1, 'unknown_style_count': 0, 'unknown_language_count': 1})
        self.assertEqual([r['position'] for r in f.view(review='pending')['records']], [3, 4])
        self.assertEqual([r['position'] for r in f.view(basis='draft', review='needs_review')['records']], [3, 4])
        self.save(1, styles=['待辨识'], language='待辨识')
        draft = f.view(basis='draft', review='pending')
        self.assertEqual([r['position'] for r in draft['records']], [1, 4])
        self.assertFalse(draft['records'][0]['needs_review'])
        self.assertEqual(draft['records'][0]['pending_reasons'], [])
        self.assertEqual(draft['quality']['draft_summary'], {
            'pending_count': 2, 'unknown_style_count': 1, 'unknown_language_count': 2})
        self.assertEqual(draft['quality']['review_count'], 2)

    def test_stale_or_corrupt_draft_basis_does_not_apply_edits(self):
        f = self.fixture
        self.save(scenes=['放松睡前'])
        f.records[0]['evidence_note'] += '，来源更新'
        f.write('全库分类-逐曲结果.json', f.report, 101)
        stale = f.view(basis='draft')
        self.assertEqual(stale['quality']['draft_status'], 'stale')
        self.assertEqual(stale['quality']['draft_summary']['pending_count'], 2)
        self.assertTrue(all(r['draft'] is None for r in stale['records']))
        self.assertNotIn('放松睡前', stale['options']['scene'])
        (f.project / '.organizer' / 'classification-draft.json').write_text('{}', encoding='utf-8')
        corrupt = f.view(basis='draft')
        self.assertEqual(corrupt['status'], 'available')
        self.assertEqual(corrupt['quality']['draft_status'], 'unavailable')
        self.assertEqual(corrupt['quality']['draft_summary']['pending_count'], 2)
        self.assertTrue(all(r['draft'] is None for r in corrupt['records']))

    def test_version_hints_are_separate_from_review_and_do_not_infer_labels(self):
        f = self.fixture
        f.records[0]['name'] = 'Rain (Live)'
        f.write('全库分类-逐曲结果.json', f.report, 101)
        version = f.view(review='version')
        self.assertEqual([r['position'] for r in version['records']], [1])
        self.assertEqual(version['records'][0]['recording_hints'], ['现场录音'])
        self.assertEqual(version['records'][0]['styles'], ['流行抒情'])
        self.assertFalse(version['records'][0]['needs_review'])
        self.assertEqual(version['quality']['review_count'], 2)

    def test_basis_parameter_is_strict(self):
        for basis in ('bad', '', None, True, []):
            with self.subTest(basis=basis), self.assertRaises(ValueError):
                self.fixture.view(basis=basis)

    def test_response_mutation_cannot_change_cached_original_or_draft_labels(self):
        self.save(scenes=['放松睡前'])
        first = self.fixture.view(basis='draft')
        first['records'][0]['styles'].clear()
        first['records'][0]['draft']['scenes'].clear()
        first['quality']['rules'].clear()
        first['quality']['draft_summary']['pending_count'] = 99
        second = self.fixture.view(basis='draft')
        self.assertEqual(second['records'][0]['styles'], ['流行抒情'])
        self.assertEqual(second['records'][0]['draft']['scenes'], ['放松睡前'])
        self.assertTrue(second['quality']['rules'])
        self.assertEqual(second['quality']['draft_summary']['pending_count'], 2)


class QualityChangesTests(unittest.TestCase):
    setUp = QualityProjectionTests.setUp
    save = QualityProjectionTests.save

    def changes(self, playlist='场景 · 放松睡前', **kwargs):
        page = self.fixture.view()
        arguments = {'playlist': playlist, 'source_version': page['quality']['source_version'],
                     'revision': page['quality']['revision']}
        arguments.update(kwargs)
        return web_classification.build_local_classification_changes(self.fixture.project, **arguments)

    def test_changes_show_each_added_removed_song_in_source_order_and_paginate(self):
        f = self.fixture
        before = {p.name: hashlib.sha256(p.read_bytes()).hexdigest()
                  for p in (f.project / 'artifacts').iterdir()}
        self.save(1, scenes=['放松睡前'])
        self.save(2, scenes=['放松睡前'])
        first = self.changes(limit=1)
        self.assertEqual(first['source'], 'local_correction_draft')
        self.assertEqual(first['draft_status'], 'ready')
        self.assertEqual(first['revision'], 2)
        self.assertEqual(first['counts'], {'added': 2, 'removed': 0})
        self.assertEqual(first['pagination'], {'offset': 0, 'limit': 1, 'total': 2, 'next_offset': 1})
        self.assertEqual([r['position'] for r in first['records']], [1])
        self.assertEqual(self.changes(offset=1, limit=1)['records'][0]['position'], 2)
        row = first['records'][0]
        self.assertEqual(set(row), {'position', 'name', 'artists', 'record_key', 'change',
                                    'before', 'after', 'reason', 'recording_note'})
        self.assertEqual(row['before']['scenes'], ['通勤散步'])
        self.assertEqual(row['after']['scenes'], ['放松睡前'])
        removed = self.changes('场景 · 通勤散步', change='removed')
        self.assertEqual(removed['counts'], {'added': 0, 'removed': 2})
        self.assertEqual([r['position'] for r in removed['records']], [1, 2])
        self.assertTrue(all(r['change'] == 'removed' for r in removed['records']))
        self.assertEqual(self.changes(change='removed')['records'], [])
        public = json.dumps(first, ensure_ascii=False)
        for secret in ('RAW-SECRET', '12345678', '87654321', 'A' * 32, f.ids[0], 'raw_response'):
            self.assertNotIn(secret, public)
        self.assertEqual(before, {p.name: hashlib.sha256(p.read_bytes()).hexdigest()
                                 for p in (f.project / 'artifacts').iterdir()})

    def test_pending_membership_is_a_union_and_same_labels_only_add_evidence(self):
        self.save(3, styles=['流行抒情'], language='待辨识')
        self.assertEqual(self.changes('分类 · 待辨识')['counts'], {'added': 0, 'removed': 0})
        self.save(3, styles=['流行抒情'], language='英语')
        self.assertEqual(self.changes('分类 · 待辨识')['records'][0]['position'], 3)
        self.save(1)
        self.assertEqual(self.changes('风格 · 流行抒情')['counts'], {'added': 1, 'removed': 0})
        self.assertEqual(self.changes('场景 · 通勤散步')['records'], [])

    def test_direction_filter_retains_counts_for_both_directions(self):
        self.save(1, scenes=['放松睡前'])
        self.save(3, scenes=['通勤散步'])
        added = self.changes('场景 · 通勤散步', change='added')
        self.assertEqual(added['counts'], {'added': 1, 'removed': 1})
        self.assertEqual(added['pagination']['total'], 1)
        self.assertEqual([r['position'] for r in added['records']], [3])
        self.assertEqual([r['position'] for r in self.changes('场景 · 通勤散步')['records']], [1, 3])

    def test_missing_empty_stale_corrupt_and_unknown_playlist(self):
        self.assertEqual(self.changes()['status'], 'available')
        self.assertEqual(self.changes('场景 · 运动提神')['records'], [])
        with self.assertRaises(ValueError):
            self.changes('场景 · 不存在')
        self.save(scenes=['放松睡前'])
        self.fixture.records[0]['evidence_note'] += '，来源更新'
        self.fixture.write('全库分类-逐曲结果.json', self.fixture.report, 101)
        stale = self.changes()
        self.assertEqual(stale['status'], 'unavailable')
        self.assertEqual(stale['draft_status'], 'stale')
        self.assertEqual(stale['counts'], {'added': 0, 'removed': 0})
        (self.fixture.project / '.organizer' / 'classification-draft.json').write_text('{}', encoding='utf-8')
        corrupt = self.changes()
        self.assertEqual(corrupt['status'], 'unavailable')
        self.assertEqual(corrupt['draft_status'], 'unavailable')
        (self.fixture.project / 'artifacts' / fixtures.REPORT).unlink()
        missing = web_classification.build_local_classification_changes(
            self.fixture.project, playlist='场景 · 放松睡前', source_version='0' * 64, revision=0)
        self.assertEqual(missing['status'], 'not_loaded')
        self.assertIsNone(missing['source_version'])

    def test_mismatched_identity_or_sources_changed_mid_read_is_conflict(self):
        with self.assertRaises(DraftConflict):
            self.changes(source_version='0' * 64)
        with self.assertRaises(DraftConflict):
            self.changes(revision=1)
        real = web_classification.load_draft
        def changed(*args, **kwargs):
            result = real(*args, **kwargs)
            self.fixture.records[0]['evidence_note'] += '，读取过程中变化'
            self.fixture.write('全库分类-逐曲结果.json', self.fixture.report, 101)
            return result
        page = self.fixture.view()
        with patch('netease_organizer.web_classification.load_draft', side_effect=changed):
            with self.assertRaises(DraftConflict):
                web_classification.build_local_classification_changes(
                    self.fixture.project, playlist='场景 · 放松睡前',
                    source_version=page['quality']['source_version'], revision=page['quality']['revision'])

    def test_change_query_rejects_invalid_values_before_source_access(self):
        cases = [{'playlist': '../secret'}, {'source_version': 'bad'}, {'revision': True},
                 {'revision': -1}, {'revision': 2**53}, {'offset': 10001}, {'limit': 101},
                 {'limit': 0}, {'change': 'bad'}]
        for values in cases:
            with self.subTest(values=values), self.assertRaises(ValueError):
                self.changes(**values)


class QualityDraftHttpTests(unittest.TestCase):
    # HTTP tests reuse the same real local records; no account operation is requested.
    def setUp(self):
        QualityProjectionTests.setUp(self)
        f = self.fixture
        assets = f.project / 'dist'
        assets.mkdir()
        (assets / 'index.html').write_text('__ORGANIZER_SESSION__', encoding='utf-8')
        self.controller = FakeController(f.project)
        self.server = create_server(self.controller, assets=assets)
        self.thread = threading.Thread(target=self.server.serve_forever,
                                       kwargs={'poll_interval': .01}, daemon=True)
        self.thread.start()
        self.token = self.server.application.attach_page()
        self.addCleanup(self.stop)
        self.origin = f'http://127.0.0.1:{self.server.server_address[1]}'

    def stop(self):
        shutdown_server(self.server)
        self.thread.join(2)

    def post(self, body, headers=None):
        conn = http.client.HTTPConnection('127.0.0.1', self.server.server_address[1], timeout=3)
        actual = {'Origin': self.origin, 'X-Organizer-Session': self.token,
                  'Content-Type': 'application/json'}
        actual.update(headers or {})
        conn.request('POST', '/api/classification/draft', json.dumps(body), actual)
        response = conn.getresponse()
        result = response.status, json.loads(response.read())
        conn.close()
        return result

    def request(self, page):
        return {'action': 'save', 'source_version': page['quality']['source_version'],
                'revision': page['quality']['revision'], 'record_key': page['records'][0]['record_key'],
                'styles': ['流行抒情'], 'scenes': ['放松睡前'], 'language': '英语',
                'reason': '试听后认为更适合低强度场景', 'recording_note': '核对专辑原版'}

    def get(self, path, headers=None):
        conn = http.client.HTTPConnection('127.0.0.1', self.server.server_address[1], timeout=3)
        actual = {'Origin': self.origin, 'X-Organizer-Session': self.token}
        actual.update(headers or {})
        conn.request('GET', path, headers=actual)
        response = conn.getresponse()
        result = response.status, json.loads(response.read())
        conn.close()
        return result

    def changes_url(self, **kwargs):
        page = self.fixture.view()
        parameters = {'playlist': '场景 · 放松睡前', 'source_version': page['quality']['source_version'],
                      'revision': page['quality']['revision']}
        parameters.update(kwargs)
        return '/api/classification/changes?' + urlencode(parameters)

    def test_http_basis_version_refresh_and_expanded_query_are_supported(self):
        self.assertEqual(self.post(self.request(self.fixture.view()))[0], 200)
        status, page = self.get('/api/classification?' + urlencode({
            'basis': 'draft', 'dimension': 'scene', 'tag': '放松睡前', 'review': 'draft',
            'offset': 0, 'limit': 1, 'q': 'Taylor', 'refresh': 'local'}))
        self.assertEqual(status, 200)
        self.assertEqual(page['filters']['basis'], 'draft')
        self.assertEqual([r['position'] for r in page['records']], [1])
        self.assertEqual(self.get('/api/classification?basis=wrong')[0], 400)
        self.assertEqual(self.get('/api/classification?basis=draft&basis=original')[0], 400)
        self.assertEqual(self.get('/api/classification?review=version')[0], 200)

    def test_changes_http_guards_bounded_validation_identity_and_payload(self):
        self.assertEqual(self.post(self.request(self.fixture.view()))[0], 200)
        path = self.changes_url()
        status, data = self.get(path)
        self.assertEqual(status, 200)
        self.assertEqual(data['records'][0]['change'], 'added')
        for headers in ({'Origin': 'https://foreign.example'}, {'X-Organizer-Session': 'expired'},
                        {'Host': 'foreign.example'}, {'Sec-Fetch-Site': 'cross-site'}):
            with self.subTest(headers=headers):
                self.assertEqual(self.get(path, headers)[0], 403)
        for query in (path + '&revision=1', path + '&secret=value', '/api/classification/changes',
                      self.changes_url(limit=101), self.changes_url(playlist='未知歌单'),
                      self.changes_url(revision='01'), self.changes_url(revision=str(2**53)),
                      path + '&' + 'q=' + 'x' * 4096):
            with self.subTest(query=query[:90]):
                self.assertEqual(self.get(query)[0], 400)
        self.assertEqual(self.get(self.changes_url(revision=0))[0], 409)
        self.assertEqual(self.get(self.changes_url(source_version='0' * 64))[0], 409)
        self.assertEqual(self.controller.calls, [('doctor', {})])
        self.assertEqual(self.controller.prepared, [])

    def test_save_preview_reload_and_remove_preserve_original_files(self):
        f = self.fixture
        before = {p.name: hashlib.sha256(p.read_bytes()).hexdigest()
                  for p in (f.project / 'artifacts').iterdir()}
        page = f.view()
        status, result = self.post(self.request(page))
        self.assertEqual(status, 200)
        self.assertTrue(result['accepted'])
        draft = f.view(review='draft')
        self.assertEqual(draft['pagination']['total'], 1)
        self.assertEqual(draft['records'][0]['scenes'], ['通勤散步'])
        self.assertEqual(draft['records'][0]['draft']['scenes'], ['放松睡前'])
        changes = {r['name']: r for r in draft['quality']['playlist_changes']}
        self.assertEqual(changes['场景 · 通勤散步']['removed_count'], 1)
        self.assertEqual(changes['场景 · 放松睡前']['added_count'], 1)
        # Fresh GET sees edits even when the immutable source projection is cached.
        self.assertEqual(f.view()['quality']['changed_count'], 1)
        status, result = self.post({'action': 'remove', 'source_version': page['quality']['source_version'],
                                    'revision': 1, 'record_key': page['records'][0]['record_key']})
        self.assertEqual(status, 200)
        self.assertEqual(result['changed_count'], 0)
        self.assertEqual(before, {p.name: hashlib.sha256(p.read_bytes()).hexdigest()
                                 for p in (f.project / 'artifacts').iterdir()})
        self.assertEqual(self.controller.calls, [('doctor', {})])
        self.assertEqual(self.controller.prepared, [])

    def test_conflicting_revision_or_source_returns_409(self):
        body = self.request(self.fixture.view())
        self.assertEqual(self.post(body)[0], 200)
        self.assertEqual(self.post(body)[0], 409)
        body['revision'] = 1
        body['source_version'] = '0' * 64
        self.assertEqual(self.post(body)[0], 409)

    def test_session_origin_and_payload_rejected(self):
        body = self.request(self.fixture.view())
        self.assertEqual(self.post(body, {'Origin': 'https://foreign.example'})[0], 403)
        self.assertEqual(self.post(body, {'X-Organizer-Session': 'expired'})[0], 403)
        body['unexpected'] = 'secret'
        status, result = self.post(body)
        self.assertEqual(status, 400)
        self.assertNotIn('secret', json.dumps(result))
