import hashlib
import http.client
import json
import threading
import unittest
from pathlib import Path

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
