"""Saved recording evidence is readable without changing source or account state."""

import copy
import http.client
import importlib.util
import json
import os
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from netease_organizer import recording_reviews as reviews
from netease_organizer.web_server import create_server, shutdown_server
from tests.test_web_server import FakeController


MODULE = 'netease_organizer.web_recording_reviews'
AVAILABLE = importlib.util.find_spec(MODULE) is not None
if AVAILABLE:
    from netease_organizer import web_recording_reviews as web


def fixture(project):
    artifacts = project / 'artifacts'
    artifacts.mkdir(exist_ok=True)
    source = json.dumps({'kind': 'classification_report', 'records': [
        {'position': 1, 'name': '同名歌 (Live)', 'artists': '甲', 'scenes': ['放松睡前', '通勤散步']},
        {'position': 2, 'name': '同名歌', 'artists': '乙', 'scenes': []},
    ]}, ensure_ascii=False).encode('utf-8')
    source_path = artifacts / '全库分类-逐曲结果.json'
    source_path.write_bytes(source)
    review = reviews.create_review(source, [1, 2])
    row = review['records'][0]
    row['recording'].update(status='audio_verified', version='明确版本', note='合成测试声明，非实际试听',
        evidence_sources=[{'url': 'https://example.org/recording', 'title': '测试来源',
                           'kind': 'audio', 'note': '仅用于测试'}],
        listened_segments=[{'start_seconds': 0, 'end_seconds': 30,
                            'source_url': 'https://example.org/recording', 'note': '测试区间'}])
    row['scene_decisions'] = [
        {'scene': '放松睡前', 'decision': 'unsuitable', 'reason': '测试移除'},
        {'scene': '学习专注', 'decision': 'suitable', 'reason': '测试新增'},
    ]  # Omitted original commute remains unresolved.
    target = reviews.save_review(project, review, 'saved-01')
    return source_path, target, review


class AvailabilityTests(unittest.TestCase):
    def test_read_only_web_projection_exists(self):
        self.assertTrue(AVAILABLE, 'saved recording review web projection is missing')


@unittest.skipUnless(AVAILABLE, 'web projection not implemented yet')
class RecordingReviewProjectionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.project = Path(self.temp.name).resolve()
        self.source, self.target, self.review = fixture(self.project)
        self.catalog = self.target.parent.parent

    def test_list_reads_names_only_including_incomplete_bundles(self):
        (self.catalog / 'a-incomplete').mkdir()
        self.target.write_bytes(b'not JSON')
        with patch.object(reviews, '_read_file', side_effect=AssertionError('list must not open reviews')):
            result = web.list_recording_reviews(self.project)
        self.assertEqual(result, {'status': 'available', 'bundles': [{'name': 'a-incomplete'}, {'name': 'saved-01'}],
                                  'limit': 100, 'truncated': False})

    def test_missing_artifacts_or_catalog_is_empty_and_does_not_create_files(self):
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory).resolve()
            self.assertEqual(web.list_recording_reviews(project),
                             {'status': 'empty', 'bundles': [], 'limit': 100, 'truncated': False})
            self.assertEqual(list(project.iterdir()), [])
            (project / 'artifacts').mkdir()
            self.assertEqual(web.list_recording_reviews(project)['status'], 'empty')
            self.assertEqual(list((project / 'artifacts').iterdir()), [])

    def test_empty_existing_catalog_is_empty(self):
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory).resolve()
            (project / 'artifacts/recording-reviews').mkdir(parents=True)
            self.assertEqual(web.list_recording_reviews(project)['status'], 'empty')

    def test_catalog_limit_returns_no_partial_success(self):
        for index in range(99):
            (self.catalog / f'item-{index:03}').mkdir()
        self.assertEqual(len(web.list_recording_reviews(self.project)['bundles']), 100)
        (self.catalog / 'one-too-many').mkdir()
        with self.assertRaises(web.RecordingReviewReadError) as caught:
            web.list_recording_reviews(self.project)
        self.assertEqual(caught.exception.code, 'recording_review_catalog_limit')

    def test_catalog_rejects_files_and_unsafe_directory_names(self):
        for name, directory in [('unexpected.json', False), ('unsafe space', True)]:
            target = self.catalog / name
            target.mkdir() if directory else target.write_text('private', encoding='utf-8')
            try:
                with self.subTest(name=name), self.assertRaises(web.RecordingReviewReadError):
                    web.list_recording_reviews(self.project)
            finally:
                target.rmdir() if directory else target.unlink()

    def test_catalog_permission_failure_is_not_empty(self):
        with patch.object(web.os, 'scandir', side_effect=PermissionError('PRIVATE-PATH')):
            with self.assertRaises(web.RecordingReviewReadError) as caught:
                web.list_recording_reviews(self.project)
        self.assertNotIn('PRIVATE-PATH', str(caught.exception))

    def test_catalog_change_during_scan_is_rejected(self):
        original = os.scandir
        def changed(path):
            iterator = original(path)
            (self.catalog / 'added-during-read').mkdir()
            return iterator
        with patch.object(web.os, 'scandir', side_effect=changed):
            with self.assertRaises(web.RecordingReviewReadError):
                web.list_recording_reviews(self.project)

    def test_detail_preserves_omitted_scene_uncertainty_and_does_not_write(self):
        before = self.source.read_bytes(), self.target.read_bytes()
        result = web.read_recording_review(self.project, 'saved-01')
        self.assertEqual(result['status'], 'available')
        self.assertEqual(result['bundle'], 'saved-01')
        self.assertEqual(result['created_at'], self.review['created_at'])
        self.assertEqual(result['preview']['summary'], {
            'reviewed_records': 2, 'metadata_matched_records': 0, 'audio_verified_records': 1,
            'scene_verified_records': 0, 'uncertain_records': 2, 'partial_scene_records': 1,
            'added_count': 1, 'removed_count': 1})
        self.assertEqual(result['preview']['records'][0]['after_scenes'], ['通勤散步', '学习专注'])
        self.assertEqual((self.source.read_bytes(), self.target.read_bytes()), before)

    def test_detail_rejects_corrupt_missing_and_stale_records(self):
        original = self.target.read_bytes()
        self.target.write_bytes(b'{"kind": "broken"}')
        with self.assertRaises(web.RecordingReviewReadError):
            web.read_recording_review(self.project, 'saved-01')
        self.target.write_bytes(original)
        with self.assertRaises(web.RecordingReviewReadError):
            web.read_recording_review(self.project, 'missing')
        self.source.write_bytes(self.source.read_bytes() + b' ')
        with self.assertRaises(web.RecordingReviewReadError):
            web.read_recording_review(self.project, 'saved-01')

    def test_detail_rejects_generation_pending(self):
        (self.project / 'artifacts/分类生成未完成.json').write_text('{}', encoding='utf-8')
        with self.assertRaises(web.RecordingReviewReadError):
            web.read_recording_review(self.project, 'saved-01')

    def test_detail_rejects_source_or_review_changes_during_preview(self):
        original = reviews.build_preview
        for target in (self.source, self.target):
            before = target.read_bytes()
            def changed(*args):
                result = original(*args)
                target.write_bytes(before + b' ')
                return result
            try:
                with self.subTest(target=target.name), patch.object(reviews, 'build_preview', side_effect=changed):
                    with self.assertRaises(web.RecordingReviewReadError):
                        web.read_recording_review(self.project, 'saved-01')
            finally:
                target.write_bytes(before)

    def test_detail_rejects_bundle_replaced_during_preview(self):
        original = reviews.build_preview
        def changed(*args):
            result = original(*args)
            previous = self.target.read_bytes()
            self.target.parent.rename(self.catalog / 'old-bundle')
            self.target.parent.mkdir()
            self.target.write_bytes(previous)
            return result
        with patch.object(reviews, 'build_preview', side_effect=changed):
            with self.assertRaises(web.RecordingReviewReadError):
                web.read_recording_review(self.project, 'saved-01')

    def test_symlink_catalog_entries_and_review_files_are_rejected(self):
        link = self.catalog / 'linked-bundle'
        try:
            link.symlink_to(self.target.parent, target_is_directory=True)
        except OSError:
            self.skipTest('symbolic link creation is not available')
        with self.assertRaises(web.RecordingReviewReadError):
            web.list_recording_reviews(self.project)
        with self.assertRaises(web.RecordingReviewReadError):
            web.read_recording_review(self.project, 'linked-bundle')


class RecordingReviewHttpTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.project = Path(self.temp.name).resolve()
        self.source, self.target, self.review = fixture(self.project)
        assets = self.project / 'assets'
        assets.mkdir()
        (assets / 'index.html').write_text('<meta name="organizer-session" content="__ORGANIZER_SESSION__">', encoding='utf-8')
        self.controller = FakeController(self.project)
        self.server = create_server(self.controller, assets=assets)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.port = self.server.server_address[1]
        self.token = self.server.application.session
        self.controller.calls.clear()

    def tearDown(self):
        self.controller.release.set()
        shutdown_server(self.server)
        self.thread.join(2)
        self.temp.cleanup()

    def request(self, path, *, method='GET', token=True, headers=None):
        connection = http.client.HTTPConnection('127.0.0.1', self.port, timeout=3)
        outgoing = {'Origin': f'http://127.0.0.1:{self.port}'}
        if token:
            outgoing['X-Organizer-Session'] = self.token
        outgoing.update(headers or {})
        connection.request(method, path, headers=outgoing)
        response = connection.getresponse()
        status, result = response.status, json.loads(response.read())
        connection.close()
        return status, result

    def test_list_and_detail_are_local_only_and_do_not_change_records(self):
        before = self.source.read_bytes(), self.target.read_bytes()
        application = self.server.application
        prior = copy.deepcopy((application.job, application.recovery, application.local_records))
        status, catalog = self.request('/api/recording-reviews')
        self.assertEqual(status, 200)
        self.assertEqual(catalog['bundles'], [{'name': 'saved-01'}])
        status, detail = self.request('/api/recording-reviews/detail?bundle=saved-01')
        self.assertEqual(status, 200)
        self.assertEqual(detail['preview']['summary']['uncertain_records'], 2)
        self.assertEqual(self.controller.calls, [])
        self.assertEqual((application.job, application.recovery, application.local_records), prior)
        self.assertEqual((self.source.read_bytes(), self.target.read_bytes()), before)

    def test_unknown_duplicate_missing_and_unsafe_parameters_are_rejected(self):
        for path in ('/api/recording-reviews?q=x', '/api/recording-reviews/detail',
                     '/api/recording-reviews/detail?bundle=', '/api/recording-reviews/detail?bundle=saved-01&bundle=saved-01',
                     '/api/recording-reviews/detail?bundle=saved-01&extra=1', '/api/recording-reviews/detail?bundle=../escape',
                     '/api/recording-reviews/detail?bundle=CON', '/api/recording-reviews/detail?bundle=%FF',
                     '/api/recording-reviews/detail?bundle=' + 'a' * 2048):
            with self.subTest(path=path):
                self.assertEqual(self.request(path)[0], 400)

    def test_host_origin_and_session_guards_apply_to_both_routes(self):
        for path in ('/api/recording-reviews', '/api/recording-reviews/detail?bundle=saved-01'):
            with self.subTest(path=path):
                self.assertEqual(self.request(path, token=False)[0], 403)
                self.assertEqual(self.request(path, headers={'Origin': 'https://example.org'})[0], 403)
                self.assertEqual(self.request(path, headers={'Host': 'localhost'})[0], 403)
                self.assertEqual(self.request(path, method='POST')[0], 404)

    def test_corrupt_detail_is_explicit_failure_without_private_exception_text(self):
        self.target.write_bytes(b'PRIVATE-MALFORMED-CONTENT')
        status, result = self.request('/api/recording-reviews/detail?bundle=saved-01')
        self.assertEqual(status, 503)
        self.assertFalse(result['accepted'])
        self.assertNotIn('PRIVATE', json.dumps(result))
        self.assertNotIn('preview', result)

    @unittest.skipUnless(AVAILABLE, 'web projection not implemented yet')
    def test_session_is_rechecked_after_each_disk_read_without_holding_state_lock(self):
        for function, path in (
            ('list_recording_reviews', '/api/recording-reviews'),
            ('read_recording_review', '/api/recording-reviews/detail?bundle=saved-01')):
            with self.subTest(function=function):
                entered, release = threading.Event(), threading.Event()
                original = getattr(web, function)
                def slow(*args):
                    entered.set()
                    if not release.wait(2):
                        raise RuntimeError('read not released')
                    return original(*args)
                result = []
                with patch('netease_organizer.web_server.' + function, side_effect=slow):
                    thread = threading.Thread(target=lambda: result.append(self.request(path)))
                    thread.start()
                    try:
                        self.assertTrue(entered.wait(1))
                        acquired = self.server.application.lock.acquire(timeout=.2)
                        try:
                            self.assertTrue(acquired, 'disk reads must not block state polling')
                            if acquired:
                                self.server.application.session = 'a' * 64 if self.token != 'a' * 64 else 'b' * 64
                        finally:
                            if acquired:
                                self.server.application.lock.release()
                    finally:
                        release.set()
                        thread.join(3)
                self.assertEqual(result[0][0], 403)
                self.assertNotIn('preview', result[0][1])
                self.token = self.server.application.session


if __name__ == '__main__':
    unittest.main()
