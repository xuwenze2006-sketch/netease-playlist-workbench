"""Recording reviews preserve uncertainty and never authorize account writes."""

import copy
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch


MODULE = 'netease_organizer.recording_reviews'
AVAILABLE = importlib.util.find_spec(MODULE) is not None
if AVAILABLE:
    from netease_organizer import recording_reviews as reviews


def source_bytes():
    return json.dumps({'kind': 'classification_report', 'records': [
        {'position': 1, 'name': '同名歌', 'artists': '甲', 'scenes': ['放松睡前', '通勤散步']},
        {'position': 2, 'name': '同名歌 (Live)', 'artists': '甲', 'scenes': ['学习专注']},
        {'position': 3, 'name': '无场景', 'artists': '乙', 'scenes': []},
    ]}, ensure_ascii=False).encode('utf-8')


def audio_verified(row):
    row['recording'].update({
        'status': 'audio_verified', 'version': '专辑录音', 'note': '核对具体录音并试听标注区间。',
        'evidence_sources': [{'url': 'https://example.org/audio/1', 'title': '录音来源',
                              'kind': 'audio', 'note': '具体录音入口'}],
        'listened_segments': [{'start_seconds': 0, 'end_seconds': 30,
                              'source_url': 'https://example.org/audio/1', 'note': '人声及鼓点'}],
    })


class AvailabilityTests(unittest.TestCase):
    def test_core_api_exists(self):
        self.assertTrue(AVAILABLE, 'recording review implementation is missing')


@unittest.skipUnless(AVAILABLE, 'core API not implemented yet')
class RecordingReviewTests(unittest.TestCase):
    def setUp(self):
        self.source = source_bytes()
        self.review = reviews.create_review(self.source, [1, 2])

    def test_create_binds_original_rows_and_keeps_uncertain_scenes(self):
        self.assertEqual(self.review['source_sha256'], hashlib.sha256(self.source).hexdigest())
        self.assertEqual(self.review['source_count'], 3)
        self.assertEqual(self.review['records'][0]['original_scenes'], ['放松睡前', '通勤散步'])
        preview = reviews.build_preview(self.review, self.source)
        self.assertEqual(preview['records'][0]['after_scenes'], ['放松睡前', '通勤散步'])
        self.assertEqual(preview['summary']['uncertain_records'], 2)
        self.assertEqual(preview['summary']['scene_verified_records'], 0)
        self.assertEqual(preview['summary']['added_count'], 0)
        self.assertEqual(preview['summary']['removed_count'], 0)

    def test_selection_rejects_bool_duplicates_unknown_and_empty(self):
        for selected in ([], [True], [1, 1], [4], [0], list(range(1, 52))):
            with self.subTest(selected=selected), self.assertRaises(ValueError):
                reviews.create_review(self.source, selected)

    def test_unmentioned_original_scene_remains_in_uncertain_and_partial_counts(self):
        row = self.review['records'][0]
        audio_verified(row)
        row['scene_decisions'] = [
            {'scene': '放松睡前', 'decision': 'unsuitable', 'reason': '已核验的突发鼓点'},
        ]
        preview = reviews.build_preview(self.review, self.source)
        self.assertEqual(preview['records'][0]['after_scenes'], ['通勤散步'])
        self.assertEqual(preview['summary']['removed_count'], 1)
        self.assertEqual(preview['summary']['scene_verified_records'], 0)
        self.assertEqual(preview['summary']['uncertain_records'], 2)
        self.assertEqual(preview['summary']['partial_scene_records'], 1)

    def test_source_change_or_record_identity_change_is_rejected(self):
        for field, value in [('name', '另一首'), ('artists', '另一人'), ('original_scenes', [])]:
            altered = copy.deepcopy(self.review)
            altered['records'][0][field] = value
            with self.subTest(field=field), self.assertRaises(ValueError):
                reviews.validate_review(altered, self.source)
        with self.assertRaises(ValueError):
            reviews.validate_review(self.review, self.source + b' ')

    def test_schema_rejects_unknown_fields_bool_version_and_duplicate_rows(self):
        for mutate in (
            lambda r: r.update(version=True), lambda r: r.update(source_count=True),
            lambda r: r.update(extra=1), lambda r: r['records'].append(copy.deepcopy(r['records'][0])),
            lambda r: r['records'][0].update(position=True),
            lambda r: r.update(created_at='yesterday'),
            lambda r: r['records'][0]['recording'].update(status='done'),
        ):
            altered = copy.deepcopy(self.review)
            mutate(altered)
            with self.subTest(mutate=mutate), self.assertRaises(ValueError):
                reviews.validate_review(altered, self.source)

    def test_metadata_evidence_cannot_authorize_scene_change(self):
        row = self.review['records'][0]
        row['recording'].update(status='metadata_matched', version='专辑录音', note='目录匹配',
                                evidence_sources=[{'url': 'https://example.org/song', 'title': '目录',
                                                   'kind': 'catalog', 'note': '标题与艺人一致'}])
        self.assertEqual(reviews.build_preview(self.review, self.source)['summary']['metadata_matched_records'], 1)
        row['scene_decisions'][0].update(decision='unsuitable', reason='歌词强烈')
        with self.assertRaises(ValueError):
            reviews.build_preview(self.review, self.source)

    def test_confirmed_changes_leave_unknown_and_unmentioned_membership_intact(self):
        row = self.review['records'][0]
        audio_verified(row)
        row['scene_decisions'] = [
            {'scene': '放松睡前', 'decision': 'unsuitable', 'reason': '突发强鼓点'},
            {'scene': '学习专注', 'decision': 'suitable', 'reason': '已试听稳定片段'},
            {'scene': '运动提神', 'decision': 'uncertain', 'reason': '尚未核验'},
        ]
        preview = reviews.build_preview(self.review, self.source)
        result = preview['records'][0]
        self.assertEqual(result['after_scenes'], ['通勤散步', '学习专注'])
        self.assertEqual(result['added_scenes'], ['学习专注'])
        self.assertEqual(result['removed_scenes'], ['放松睡前'])
        self.assertEqual(preview['summary']['added_count'], 1)
        self.assertEqual(preview['summary']['removed_count'], 1)
        self.assertEqual(preview['summary']['partial_scene_records'], 1)
        self.assertEqual(preview['summary']['scene_verified_records'], 0)

    def test_confirmed_no_change_counts_as_scene_verification(self):
        row = self.review['records'][0]
        audio_verified(row)
        for decision in row['scene_decisions']:
            decision.update(decision='suitable', reason='具体试听区间支持保留')
        summary = reviews.build_preview(self.review, self.source)['summary']
        self.assertEqual(summary['scene_verified_records'], 1)
        self.assertEqual(summary['added_count'], 0)
        self.assertEqual(summary['removed_count'], 0)
        self.assertEqual(summary['audio_verified_records'], 1)

    def test_audio_status_rejects_missing_mismatched_or_invalid_segments(self):
        audio_verified(self.review['records'][0])
        for mutate in (
            lambda r: r.update(listened_segments=[]),
            lambda r: r.update(evidence_sources=[]),
            lambda r: r['listened_segments'][0].update(source_url='https://example.org/other'),
            lambda r: r['listened_segments'][0].update(start_seconds=True),
            lambda r: r['listened_segments'][0].update(start_seconds=-1),
            lambda r: r['listened_segments'][0].update(end_seconds=0),
            lambda r: r['listened_segments'][0].update(end_seconds=float('inf')),
        ):
            altered = copy.deepcopy(self.review)
            mutate(altered['records'][0]['recording'])
            with self.subTest(mutate=mutate), self.assertRaises(ValueError):
                reviews.validate_review(altered, self.source)

    def test_huge_segment_number_is_a_validation_error_not_an_overflow(self):
        row = self.review['records'][0]
        audio_verified(row)
        row['recording']['listened_segments'][0]['end_seconds'] = 10 ** 1000
        with self.assertRaises(ValueError):
            reviews.validate_review(self.review, self.source)

    def test_empty_decisions_are_not_completed(self):
        empty = reviews.create_review(self.source, [3])
        summary = reviews.build_preview(empty, self.source)['summary']
        self.assertEqual(summary['scene_verified_records'], 0)
        self.assertEqual(summary['uncertain_records'], 1)

    def test_decisions_reject_unknown_duplicate_or_too_many_resulting_scenes(self):
        row = self.review['records'][0]
        audio_verified(row)
        for decisions in (
            [{'scene': '不存在', 'decision': 'suitable', 'reason': '试听'}],
            [{'scene': '放松睡前', 'decision': 'suitable', 'reason': '试听'}] * 2,
            [{'scene': '学习专注', 'decision': 'suitable', 'reason': '试听'},
             {'scene': '运动提神', 'decision': 'suitable', 'reason': '试听'}],
        ):
            row['scene_decisions'] = decisions
            with self.subTest(decisions=decisions), self.assertRaises(ValueError):
                reviews.build_preview(self.review, self.source)

    def test_validation_does_not_mutate_inputs_and_returns_detached_data(self):
        original = copy.deepcopy(self.review)
        validated = reviews.validate_review(self.review, self.source)
        validated['records'][0]['recording']['note'] = 'changed'
        self.assertEqual(self.review, original)
        reviews.build_preview(self.review, self.source)
        self.assertEqual(self.review, original)

    def test_decode_rejects_duplicate_keys_nonfinite_and_oversize(self):
        for data in (b'{"a":1,"a":2}', b'{"a":NaN}', b'{"a":1e999}', b'[]', b'\xff',
                     b' ' * (16 * 1024 * 1024 + 1)):
            with self.subTest(data=data[:30]), self.assertRaises(ValueError):
                reviews.decode_review(data)

    def test_evidence_urls_reject_active_schemes_credentials_and_controls(self):
        for url in ('javascript:alert(1)', 'file:///secret', 'https://user:pass@example.org/a',
                    'https://example.org/\npath', 'https://'):
            altered = copy.deepcopy(self.review)
            altered['records'][0]['recording']['evidence_sources'] = [
                {'url': url, 'title': '来源', 'kind': 'catalog', 'note': '核对'}]
            with self.subTest(url=url), self.assertRaises(ValueError):
                reviews.validate_review(altered, self.source)


@unittest.skipUnless(AVAILABLE, 'core API not implemented yet')
class RecordingStorageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.project = Path(self.temp.name)
        (self.project / 'artifacts').mkdir()
        self.source_path = self.project / 'artifacts' / '全库分类-逐曲结果.json'
        self.source_path.write_bytes(source_bytes())
        self.review = reviews.create_review(source_bytes(), [1, 2])

    def test_save_reload_is_immutable_and_source_is_unchanged(self):
        saved = reviews.save_review(self.project, self.review, 'sample-10')
        self.assertEqual(saved, self.project / 'artifacts/recording-reviews/sample-10/review.json')
        self.assertEqual(reviews.read_review(self.project, 'sample-10'), self.review)
        before = saved.read_bytes()
        with self.assertRaises(ValueError):
            reviews.save_review(self.project, self.review, 'sample-10')
        self.assertEqual(saved.read_bytes(), before)
        self.assertEqual(self.source_path.read_bytes(), source_bytes())

    def test_bundle_names_reject_traversal_absolute_reserved_and_streams(self):
        for name in ('../escape', 'a/b', 'a\\b', 'C:\\escape', 'sample:stream', '.', '', 'CON', 'nul.txt'):
            with self.subTest(name=name), self.assertRaises(ValueError):
                reviews.save_review(self.project, self.review, name)
        self.assertFalse((self.project / 'artifacts/recording-reviews').exists())

    def test_source_change_blocks_read_and_save_without_overwriting(self):
        saved = reviews.save_review(self.project, self.review, 'old')
        previous = saved.read_bytes()
        self.source_path.write_bytes(source_bytes() + b' ')
        with self.assertRaises(ValueError):
            reviews.read_review(self.project, 'old')
        with self.assertRaises(ValueError):
            reviews.save_review(self.project, self.review, 'new')
        self.assertEqual(saved.read_bytes(), previous)
        self.assertFalse((saved.parent.parent / 'new').exists())

    def test_corrupt_oversized_and_directory_reviews_fail_closed(self):
        saved = reviews.save_review(self.project, self.review, 'bad')
        for data in (b'{"kind":1,"kind":2}', b' ' * (16 * 1024 * 1024 + 1)):
            saved.write_bytes(data)
            with self.assertRaises(ValueError):
                reviews.read_review(self.project, 'bad')
        saved.unlink()
        saved.mkdir()
        with self.assertRaises(ValueError):
            reviews.read_review(self.project, 'bad')

    def test_source_change_during_save_is_reported_and_prior_bundle_survives(self):
        saved = reviews.save_review(self.project, self.review, 'prior')
        original = saved.read_bytes()
        real_fsync = os.fsync
        def change_source(descriptor):
            real_fsync(descriptor)
            self.source_path.write_bytes(source_bytes() + b' ')
        with patch.object(reviews.os, 'fsync', side_effect=change_source):
            with self.assertRaises(ValueError):
                reviews.save_review(self.project, self.review, 'racing')
        self.assertEqual(saved.read_bytes(), original)
        with self.assertRaises(ValueError):
            reviews.read_review(self.project, 'racing')

    def test_source_change_during_read_is_rejected(self):
        saved = reviews.save_review(self.project, self.review, 'racing-read')
        previous = saved.read_bytes()
        validate = reviews.validate_review
        def change_source(review, source):
            result = validate(review, source)
            self.source_path.write_bytes(source_bytes() + b' ')
            return result
        with patch.object(reviews, 'validate_review', side_effect=change_source):
            with self.assertRaises(ValueError):
                reviews.read_review(self.project, 'racing-read')
        self.assertEqual(saved.read_bytes(), previous)

    def test_unrelated_sibling_write_does_not_invalidate_source_identity(self):
        fsync = os.fsync
        def write_sibling(descriptor):
            fsync(descriptor)
            (self.source_path.parent / 'unrelated.txt').write_text('unrelated', encoding='utf-8')
        with patch.object(reviews.os, 'fsync', side_effect=write_sibling):
            saved = reviews.save_review(self.project, self.review, 'sibling-write')
        self.assertTrue(saved.is_file())
        self.assertEqual(reviews.read_review(self.project, 'sibling-write'), self.review)

    def test_windows_reparse_attribute_is_refused_without_symlink_privilege(self):
        lstat = Path.lstat
        def marked(path, *args, **kwargs):
            value = lstat(path, *args, **kwargs)
            if path == self.source_path:
                return SimpleNamespace(st_mode=value.st_mode, st_file_attributes=0x400)
            return value
        with patch.object(Path, 'lstat', marked):
            with self.assertRaises(ValueError):
                reviews.load_source(self.project)

    def test_source_rejects_oversize_and_malformed_json(self):
        for data in (b' ' * (16 * 1024 * 1024 + 1), b'{"records":[],"records":[]}'):
            self.source_path.write_bytes(data)
            with self.subTest(data=data[:30]), self.assertRaises(ValueError):
                reviews.load_source(self.project)

    def test_source_symlink_is_refused_without_reading_target(self):
        target = self.project / 'other.json'
        self.source_path.rename(target)
        try:
            self.source_path.symlink_to(target)
        except OSError:
            self.skipTest('symlink creation unavailable in this environment')
        with self.assertRaises(ValueError):
            reviews.load_source(self.project)

    def test_review_directory_symlink_is_refused(self):
        outside = self.project / 'outside'
        outside.mkdir()
        link = self.project / 'artifacts/recording-reviews'
        try:
            link.symlink_to(outside, target_is_directory=True)
        except OSError:
            self.skipTest('symlink creation unavailable in this environment')
        with self.assertRaises(ValueError):
            reviews.save_review(self.project, self.review, 'escape')
        self.assertEqual(list(outside.iterdir()), [])


if __name__ == '__main__':
    unittest.main()
