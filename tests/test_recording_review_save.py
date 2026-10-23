"""Synthetic-only, immutable recording-review editor save contracts."""

import copy
import hashlib
import http.client
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from netease_organizer import recording_reviews as reviews
from netease_organizer import web_recording_reviews as web
from netease_organizer.web_server import create_server, shutdown_server
from tests.test_web_recording_reviews import fixture
from tests.test_web_server import FakeController


class SaveFixture(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.project = Path(self.temp.name).resolve()
        self.source, self.base, self.review = fixture(self.project)
        self.catalog = self.base.parent.parent
        self.raw = {
            'base_bundle': 'saved-01',
            'base_sha256': hashlib.sha256(self.base.read_bytes()).hexdigest(),
            'target_bundle': 'review-' + 'a' * 32,
            'created_at': '2026-10-23T08:00:00.123Z',
            'position': 1,
            'recording': copy.deepcopy(self.review['records'][0]['recording']),
            'scene_decisions': copy.deepcopy(self.review['records'][0]['scene_decisions']),
        }
        self.raw['recording']['note'] = '另存的合成记录；不代表实际试听'

    @property
    def target(self):
        return self.catalog / self.raw['target_bundle'] / 'review.json'

    def expected(self):
        result = copy.deepcopy(self.review)
        result['created_at'] = self.raw['created_at']
        row = next(row for row in result['records'] if row['position'] == self.raw['position'])
        row['recording'] = copy.deepcopy(self.raw['recording'])
        row['scene_decisions'] = copy.deepcopy(self.raw['scene_decisions'])
        return result

    def save(self, raw=None, **kwargs):
        return web.save_recording_review(self.project, self.raw if raw is None else raw, **kwargs)

    def assert_error(self, status, raw=None, **kwargs):
        with self.assertRaises(web.RecordingReviewSaveError) as caught:
            self.save(raw, **kwargs)
        self.assertEqual(caught.exception.status, status)
        self.assertNotIn('PRIVATE', str(caught.exception))
        return caught.exception


class RecordingReviewSaveTests(SaveFixture):
    def test_detail_hash_covers_original_bytes(self):
        detail = web.read_recording_review(self.project, 'saved-01')
        self.assertEqual(detail['review_sha256'], self.raw['base_sha256'])
        self.base.write_bytes(self.base.read_bytes() + b' ')
        self.assertNotEqual(web.read_recording_review(self.project, 'saved-01')['review_sha256'],
                            self.raw['base_sha256'])

    def test_save_one_song_preserves_source_base_identity_and_other_rows(self):
        before = self.source.read_bytes(), self.base.read_bytes()
        request_before = copy.deepcopy(self.raw)
        result = self.save()
        self.assertEqual(result['status'], 'saved')
        self.assertEqual(result['bundle'], self.raw['target_bundle'])
        self.assertIs(result['replayed'], False)
        self.assertEqual(reviews.read_review(self.project, self.raw['target_bundle']), self.expected())
        self.assertEqual(result['detail'], web.read_recording_review(self.project, self.raw['target_bundle']))
        self.assertEqual(result['detail']['preview']['records'][0]['after_scenes'], ['通勤散步', '学习专注'])
        self.assertEqual((self.source.read_bytes(), self.base.read_bytes()), before)
        self.assertEqual(self.raw, request_before)

    def test_same_intent_is_replayed_without_writing_another_bundle(self):
        first = self.save()
        original = self.target.read_bytes(), self.target.stat().st_mtime_ns
        with patch.object(reviews, 'save_review', side_effect=AssertionError('must not rewrite')):
            second = self.save()
        self.assertIs(second['replayed'], True)
        self.assertEqual(second['detail'], first['detail'])
        self.assertEqual((self.target.read_bytes(), self.target.stat().st_mtime_ns), original)
        self.assertEqual(len(list(self.catalog.iterdir())), 2)

    def test_base_cannot_be_its_own_save_target(self):
        reviews.save_review(self.project, self.review, self.raw['target_bundle'])
        original = self.target.read_bytes()
        self.assert_error(400, {**self.raw, 'base_bundle': self.raw['target_bundle'],
                               'base_sha256': hashlib.sha256(original).hexdigest()})
        self.assertEqual(self.target.read_bytes(), original)

    def test_replay_precedes_capacity_and_other_bad_catalog_entries(self):
        self.save()
        for index in range(99):
            (self.catalog / f'other-{index}').mkdir()
        (self.catalog / 'bad file').write_bytes(b'PRIVATE')
        self.assertTrue(self.save()['replayed'])

    def test_new_target_rejects_full_catalog_without_creating_directory(self):
        for index in range(99):
            (self.catalog / f'other-{index}').mkdir()
        self.assert_error(409)
        self.assertFalse(self.target.parent.exists())

    def test_new_target_rejects_bad_catalog_without_creating_directory(self):
        (self.catalog / 'bad file').write_bytes(b'PRIVATE')
        self.assert_error(503)
        self.assertFalse(self.target.parent.exists())

    def test_existing_complete_different_target_is_conflict_and_preserved(self):
        reviews.save_review(self.project, self.review, self.raw['target_bundle'])
        before = self.target.read_bytes()
        self.assert_error(409)
        self.assertEqual(self.target.read_bytes(), before)

    def test_existing_partial_and_corrupt_target_are_unknown_and_preserved(self):
        self.target.parent.mkdir()
        self.assert_error(503)
        self.assertEqual(list(self.target.parent.iterdir()), [])
        self.target.write_bytes(b'PRIVATE-MALFORMED')
        self.assert_error(503)
        self.assertEqual(self.target.read_bytes(), b'PRIVATE-MALFORMED')

    def test_payload_is_exact_and_song_identity_is_not_editable(self):
        invalid = [None, [], {}, {**self.raw, 'name': '改名'},
                   {key: value for key, value in self.raw.items() if key != 'base_sha256'}]
        for raw in invalid:
            with self.subTest(raw=type(raw).__name__):
                # None is deliberately passed through, rather than the convenience helper.
                with self.assertRaises(web.RecordingReviewSaveError) as caught:
                    web.save_recording_review(self.project, raw)
                self.assertEqual(caught.exception.status, 400)
        self.assertFalse(self.target.parent.exists())

    def test_invalid_names_hashes_position_timestamp_and_evidence_are_rejected(self):
        changes = [
            {'base_bundle': '../escape'}, {'base_bundle': 'CON'},
            {'target_bundle': 'saved-02'}, {'target_bundle': '../review-' + 'a' * 32},
            {'target_bundle': 'review-' + 'a' * 31}, {'target_bundle': 'review-' + 'z' * 32},
            {'target_bundle': 'review-' + 'a' * 32 + ':stream'},
            {'base_sha256': 'a' * 63}, {'base_sha256': 'x' * 64},
            {'position': True}, {'position': 3}, {'position': 1.0},
            {'created_at': '2026-10-23'}, {'created_at': 'invalid'},
            {'recording': {**self.raw['recording'], 'version': 'x' * 2001}},
            {'recording': {**self.raw['recording'], 'note': 'x' * 8001}},
            {'recording': {**self.raw['recording'], 'listened_segments': []}},
            {'scene_decisions': [{'scene': '放松睡前', 'decision': 'uncertain', 'reason': ''}]},
        ]
        for change in changes:
            with self.subTest(change=list(change)):
                self.assert_error(400, {**self.raw, **change})
        unsafe = copy.deepcopy(self.raw)
        unsafe['recording']['evidence_sources'][0]['url'] = 'https://user:password@example.org/'
        self.assert_error(400, unsafe)
        self.assertFalse(self.target.parent.exists())

    def test_base_whitespace_changes_conflict_even_when_semantically_equal(self):
        self.base.write_bytes(self.base.read_bytes() + b' ')
        self.assert_error(409)
        self.assertFalse(self.target.parent.exists())

    def test_changed_source_and_pending_generation_conflict_before_write(self):
        original = self.source.read_bytes()
        self.source.write_bytes(original + b' ')
        self.assert_error(409)
        self.source.write_bytes(original)
        (self.project / 'artifacts/分类生成未完成.json').write_text('{}', encoding='utf-8')
        self.assert_error(409)
        self.assertFalse(self.target.parent.exists())

    def test_replay_still_requires_original_base_and_source(self):
        self.save()
        self.base.write_bytes(self.base.read_bytes() + b' ')
        self.assert_error(409)

    def test_base_and_source_changes_during_candidate_preview_prevent_write(self):
        original = reviews.build_preview
        for path in (self.base, self.source):
            before = path.read_bytes()
            def changed(*args):
                result = original(*args)
                path.write_bytes(before + b' ')
                return result
            try:
                with self.subTest(path=path.name), patch.object(reviews, 'build_preview', side_effect=changed):
                    self.assert_error(409)
                self.assertFalse(self.target.parent.exists())
            finally:
                path.write_bytes(before)

    def test_base_replacement_with_equal_bytes_before_commit_is_conflict(self):
        original = web.list_recording_reviews
        def replaced(*args):
            result = original(*args)
            content = self.base.read_bytes()
            self.base.parent.rename(self.catalog / 'old-base')
            self.base.parent.mkdir()
            self.base.write_bytes(content)
            return result
        with patch.object(web, 'list_recording_reviews', side_effect=replaced):
            self.assert_error(409)
        self.assertFalse(self.target.parent.exists())

    def test_base_change_during_writer_is_unknown_even_if_target_complete(self):
        original = reviews.save_review
        def changed(*args):
            result = original(*args)
            self.base.write_bytes(self.base.read_bytes() + b' ')
            return result
        with patch.object(reviews, 'save_review', side_effect=changed):
            self.assert_error(503)
        self.assertTrue(self.target.exists())

    def test_source_change_after_complete_write_is_unknown(self):
        original = reviews.save_review
        def changed(*args):
            result = original(*args)
            self.source.write_bytes(self.source.read_bytes() + b' ')
            return result
        with patch.object(reviews, 'save_review', side_effect=changed):
            self.assert_error(503)
        self.assertTrue(self.target.exists())

    def test_before_commit_callback_cannot_rebind_base_before_writer(self):
        def changed():
            self.base.write_bytes(self.base.read_bytes() + b' ')
        self.assert_error(409, before_commit=changed)
        self.assertFalse(self.target.parent.exists())

    def test_complete_write_followed_by_exception_is_confirmed_by_full_readback(self):
        original = reviews.save_review
        def wrote_then_failed(*args):
            original(*args)
            raise OSError('PRIVATE-WRITER-ERROR')
        with patch.object(reviews, 'save_review', side_effect=wrote_then_failed):
            result = self.save()
        self.assertEqual(result['status'], 'saved')
        self.assertEqual(reviews.read_review(self.project, self.raw['target_bundle']), self.expected())

    def test_writer_failure_without_readable_target_is_unknown_no_automatic_retry(self):
        for partial in (False, True):
            def failed(*args):
                if partial:
                    self.target.parent.mkdir()
                    self.target.write_bytes(b'{')
                raise OSError('PRIVATE-WRITER-ERROR')
            with self.subTest(partial=partial), patch.object(reviews, 'save_review', side_effect=failed) as writer:
                self.assert_error(503)
                self.assertEqual(writer.call_count, 1)
        self.assertEqual(self.target.read_bytes(), b'{')

    def test_readback_compares_all_rows_not_only_edited_record(self):
        original = reviews.save_review
        def wrong_other_row(project, review, name):
            changed = copy.deepcopy(review)
            changed['records'][1]['recording']['note'] = 'unintended unrelated change'
            return original(project, changed, name)
        with patch.object(reviews, 'save_review', side_effect=wrong_other_row):
            self.assert_error(503)
        self.assertTrue(self.target.exists())

    def test_target_change_during_detail_projection_is_unknown(self):
        original = web.read_recording_review
        def changed(*args):
            detail = original(*args)
            self.target.write_bytes(self.target.read_bytes() + b' ')
            return detail
        with patch.object(web, 'read_recording_review', side_effect=changed):
            self.assert_error(503)

    def test_target_created_between_check_and_writer_recovers_same_content(self):
        original = reviews.save_review
        def raced(project, review, name):
            original(project, review, name)
            return original(project, review, name)
        with patch.object(reviews, 'save_review', side_effect=raced):
            self.assertEqual(self.save()['status'], 'saved')
        self.assertEqual(len(list(self.catalog.iterdir())), 2)

    def test_reparse_target_is_refused_without_reading_or_overwriting_it(self):
        self.target.parent.mkdir()
        original = web.Path.lstat
        def reparse(path, *args, **kwargs):
            result = original(path, *args, **kwargs)
            if path == self.target.parent:
                class Info:
                    st_file_attributes = reviews._REPARSE
                    def __getattr__(self, name):
                        return getattr(result, name)
                return Info()
            return result
        with patch.object(web.Path, 'lstat', reparse):
            self.assert_error(503)
        self.assertFalse(self.target.exists())

    def test_symlink_base_and_target_are_rejected(self):
        link = self.catalog / ('review-' + 'b' * 32)
        try:
            link.symlink_to(self.base.parent, target_is_directory=True)
        except OSError:
            self.skipTest('symbolic link creation is not available')
        self.assert_error(503, {**self.raw, 'target_bundle': link.name})
        self.assert_error(409, {**self.raw, 'base_bundle': link.name})


class RecordingReviewSaveHttpTests(SaveFixture):
    def setUp(self):
        super().setUp()
        assets = self.project / 'assets'
        assets.mkdir()
        (assets / 'index.html').write_text('<meta name="organizer-session" content="__ORGANIZER_SESSION__">', encoding='utf-8')
        self.controller = FakeController(self.project)
        self.server = create_server(self.controller, assets=assets)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.port = self.server.server_address[1]
        self.app = self.server.application
        self.token = self.app.session
        self.controller.calls.clear()
        self.addCleanup(self.stop_server)

    def stop_server(self):
        self.controller.release.set()
        shutdown_server(self.server)
        self.thread.join(2)

    def request(self, raw=None, *, body=None, headers=None, path='/api/recording-reviews/save'):
        if body is None:
            body = json.dumps(self.raw if raw is None else raw, ensure_ascii=False).encode('utf-8')
        outgoing = {'Origin': f'http://127.0.0.1:{self.port}', 'X-Organizer-Session': self.token,
                    'Content-Type': 'application/json'}
        outgoing.update(headers or {})
        connection = http.client.HTTPConnection('127.0.0.1', self.port, timeout=5)
        connection.request('POST', path, body=body, headers=outgoing)
        response = connection.getresponse()
        result = response.status, json.loads(response.read())
        connection.close()
        return result

    def test_http_save_replay_and_no_account_dispatch(self):
        prior = copy.deepcopy((self.app.job, self.app.recovery, self.app.local_records))
        status, result = self.request()
        self.assertEqual(status, 200)
        self.assertFalse(result['replayed'])
        self.assertEqual(self.request()[1]['replayed'], True)
        self.assertEqual(self.controller.calls, [])
        self.assertEqual((self.app.job, self.app.recovery, self.app.local_records), prior)

    def test_http_guards_strict_json_and_64k_limit(self):
        for headers in ({'X-Organizer-Session': ''}, {'Origin': 'https://example.org'}, {'Host': 'localhost'}):
            with self.subTest(headers=list(headers)):
                self.assertEqual(self.request(headers=headers)[0], 403)
        for body in (b'{"position":1,"position":2}', b'{"x":NaN}', b'{"x":Infinity}', b'[]', b'\xff'):
            with self.subTest(body=body):
                self.assertEqual(self.request(body=body)[0], 400)
        self.assertEqual(self.request(body=b' ' * 65537)[0], 413)
        self.assertEqual(self.request(path='/api/recording-reviews/save?extra=1')[0], 404)
        self.assertFalse(self.target.parent.exists())

    def test_http_accepts_exact_64k_utf8_body(self):
        body = json.dumps(self.raw, ensure_ascii=False).encode('utf-8')
        body += b' ' * (65536 - len(body))
        self.assertEqual(len(body), 65536)
        self.assertEqual(self.request(body=body)[0], 200)

    def test_busy_stopping_and_closing_are_checked_before_disk_work(self):
        for field in ('busy', 'stopping', 'stop_requested', 'page_closing'):
            with self.subTest(field=field):
                setattr(self.app, field, True)
                try:
                    with patch.object(web.reviews, '_source', side_effect=AssertionError('disk work started')):
                        self.assertEqual(self.request()[0], 409)
                finally:
                    setattr(self.app, field, False)
        self.assertFalse(self.target.parent.exists())

    def test_session_and_shutdown_changes_during_read_prevent_commit(self):
        original = reviews.build_preview
        for field in ('session', 'page_closing', 'stop_requested'):
            def changed(*args):
                result = original(*args)
                setattr(self.app, field, 'b' * 64 if field == 'session' else True)
                return result
            try:
                with self.subTest(field=field), patch.object(reviews, 'build_preview', side_effect=changed):
                    self.assertEqual(self.request()[0], 403 if field == 'session' else 409)
                self.assertFalse(self.target.parent.exists())
            finally:
                setattr(self.app, field, self.token if field == 'session' else False)

    def test_session_change_after_write_is_unknown_and_explicit_same_intent_can_recover(self):
        original = reviews.save_review
        def changed(*args):
            result = original(*args)
            self.app.session = 'b' * 64
            return result
        with patch.object(reviews, 'save_review', side_effect=changed):
            status, result = self.request()
        self.assertEqual(status, 503)
        self.assertNotIn('detail', result)
        self.assertTrue(self.target.exists())
        self.token = self.app.session
        self.assertTrue(self.request()[1]['replayed'])

    def test_closing_after_write_is_unknown(self):
        original = reviews.save_review
        def changed(*args):
            result = original(*args)
            self.app.page_closing = True
            return result
        with patch.object(reviews, 'save_review', side_effect=changed):
            status, result = self.request()
        self.assertEqual(status, 503)
        self.assertNotIn('detail', result)
        self.assertTrue(self.target.exists())

    def test_status_lock_stays_available_during_disk_work(self):
        entered, release = threading.Event(), threading.Event()
        original = reviews.build_preview
        def slow(*args):
            entered.set()
            if not release.wait(2):
                raise RuntimeError('test timed out')
            return original(*args)
        result = []
        with patch.object(reviews, 'build_preview', side_effect=slow):
            caller = threading.Thread(target=lambda: result.append(self.request()))
            caller.start()
            try:
                self.assertTrue(entered.wait(1))
                acquired = self.app.lock.acquire(timeout=.2)
                try:
                    self.assertTrue(acquired)
                finally:
                    if acquired:
                        self.app.lock.release()
            finally:
                release.set()
                caller.join(4)
        self.assertEqual(result[0][0], 200)

    def test_same_intent_concurrent_http_requests_create_one_bundle(self):
        result = []
        callers = [threading.Thread(target=lambda: result.append(self.request())) for _ in range(2)]
        for caller in callers:
            caller.start()
        for caller in callers:
            caller.join(4)
        self.assertEqual(sorted(status for status, _ in result), [200, 200])
        self.assertEqual(sorted(data['replayed'] for _, data in result), [False, True])
        self.assertEqual(len(list(self.catalog.iterdir())), 2)

    def test_records_gate_serializes_job_submission_with_local_save(self):
        entered, release = threading.Event(), threading.Event()
        original = reviews.save_review
        def slow(*args):
            entered.set()
            if not release.wait(2):
                raise RuntimeError('test timed out')
            return original(*args)
        save_results, job_results = [], []
        with patch.object(reviews, 'save_review', side_effect=slow):
            saving = threading.Thread(target=lambda: save_results.append(self.request()))
            saving.start()
            self.assertTrue(entered.wait(1))
            submitting = threading.Thread(target=lambda: job_results.append(self.app.submit('check', {}, session=self.token)))
            submitting.start()
            try:
                self.assertFalse(self.controller.started.wait(.1))
                self.assertEqual(job_results, [])
            finally:
                release.set()
                saving.join(4)
                submitting.join(4)
        self.assertEqual(save_results[0][0], 200)
        self.assertEqual(job_results[0][0], 202)


if __name__ == '__main__':
    unittest.main()
