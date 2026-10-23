"""Real-file tests for positional source identity and generation snapshots."""

import copy
import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from netease_organizer.classification_sources import GenerationInputs


BASELINE = '全库分类-账号基线.json'
HISTORY = '网易云本地歌单快照.json'
REVIEW = '分类语言核验01.json'
INPUTS = [f'分类输入-分块{i:02}.jsonl' for i in range(1, 4)]


class GenerationInputsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.folder = Path(self.temp.name)
        self.tracks = [
            {'original_id': 11, 'name': '第一曲', 'artists': [{'name': '甲'}],
             'metadata_available': True},
            {'original_id': 22, 'name': '第二曲', 'artists': [{'name': '乙'}],
             'metadata_available': True},
        ]
        self.rows = [[1, '第一曲', ['甲'], '', None, True],
                     [2, '第二曲', ['乙'], '', None, True]]
        self.historical = {}
        self.write_inputs()

    def write_json(self, name, data):
        (self.folder / name).write_text(json.dumps(data, ensure_ascii=False), encoding='utf-8')

    def write_inputs(self, rows=None):
        rows = self.rows if rows is None else rows
        for name, chunk in zip(INPUTS, [rows[:1], rows[1:], []]):
            (self.folder / name).write_text(
                ''.join(json.dumps(row, ensure_ascii=False) + '\n' for row in chunk), encoding='utf-8')

    def inputs(self):
        return GenerationInputs(self.folder)

    def assert_bad_tracks(self, rows=None, historical=None):
        if rows is not None:
            self.write_inputs(rows)
        with self.assertRaises(ValueError):
            self.inputs().validate_tracks(self.tracks, self.historical if historical is None else historical)

    def assert_bad_review(self, data):
        with self.assertRaises(ValueError):
            self.inputs().validate_review(data, self.tracks, self.historical)

    def test_aligned_tracks_accept_empty_third_chunk(self):
        self.assertIsNone(self.inputs().validate_tracks(self.tracks, self.historical))

    def test_missing_each_original_input_refuses(self):
        for name in INPUTS:
            with self.subTest(name=name):
                original = (self.folder / name).read_bytes()
                (self.folder / name).unlink()
                self.assert_bad_tracks()
                (self.folder / name).write_bytes(original)

    def test_positions_must_be_complete_unique_positive_exact_ints(self):
        for rows in ([self.rows[0]], [self.rows[0], self.rows[0]],
                     [[0, *self.rows[0][1:]], self.rows[1]],
                     [[True, *self.rows[0][1:]], self.rows[1]],
                     [[1.0, *self.rows[0][1:]], self.rows[1]],
                     [self.rows[0], [3, *self.rows[1][1:]]]):
            with self.subTest(rows=rows):
                self.assert_bad_tracks(rows)

    def test_swapped_track_order_refuses_legacy_positional_identity(self):
        self.tracks.reverse()
        self.assert_bad_tracks()

    def test_misaligned_title_artists_and_metadata_refuse(self):
        for field, value in ((1, '旧曲名'), (2, ['错误艺人']), (5, False)):
            with self.subTest(field=field):
                rows = copy.deepcopy(self.rows)
                rows[0][field] = value
                self.assert_bad_tracks(rows)

    def test_tuple_shape_and_field_types_refuse(self):
        bad_rows = [self.rows[0][:-1], self.rows[0] + ['extra'], {'position': 1}]
        for field, value in ((1, None), (2, '甲'), (2, [1]), (3, None),
                             (4, True), (4, -1), (4, 1.5), (4, '1'), (5, 1)):
            row = copy.deepcopy(self.rows[0])
            row[field] = value
            bad_rows.append(row)
        for row in bad_rows:
            with self.subTest(row=row):
                self.assert_bad_tracks([row, self.rows[1]])

    def test_malformed_jsonl_refuses_without_exposing_paths(self):
        (self.folder / INPUTS[0]).write_text('{bad json}', encoding='utf-8')
        with self.assertRaises(ValueError) as error:
            self.inputs().validate_tracks(self.tracks, self.historical)
        self.assertNotIn(str(self.folder), str(error.exception))

    def test_historical_fallback_and_name_deduplication_preserve_order(self):
        self.tracks[0]['metadata_available'] = False
        self.historical['11'] = {'name': '历史歌名', 'artists': [
            {'id': 1, 'name': '甲'}, {'id': 2, 'name': '甲'}, {'id': 3, 'name': '乙'}],
            'album': '历史专辑', 'duration_ms': 321}
        self.write_inputs([[1, '历史歌名', ['甲', '乙'], '历史专辑', 321, False], self.rows[1]])
        self.assertIsNone(self.inputs().validate_tracks(self.tracks, self.historical))
        self.assertIsNone(self.inputs().validate_review(
            [{'position': 1, 'name': '历史歌名'}], self.tracks, self.historical))

    def test_available_metadata_ignores_historical_artist_and_title(self):
        self.historical['11'] = {'name': '旧名', 'artists': [{'name': '旧艺人'}]}
        self.assertIsNone(self.inputs().validate_tracks(self.tracks, self.historical))

    def test_missing_historical_falls_back_to_current_metadata(self):
        self.tracks[0]['metadata_available'] = False
        self.rows[0][5] = False
        self.write_inputs()
        self.assertIsNone(self.inputs().validate_tracks(self.tracks, self.historical))

    def test_album_duration_not_in_baseline_are_not_forced_to_none(self):
        self.rows[0][3:5] = ['保留专辑', 999]
        self.write_inputs()
        self.assertIsNone(self.inputs().validate_tracks(self.tracks, self.historical))

    def test_historical_album_duration_mismatch_refuses(self):
        for history in ({'album': '历史专辑'}, {'duration_ms': 123}, {'duration_ms': None}):
            with self.subTest(history=history):
                rows = copy.deepcopy(self.rows)
                rows[0][4] = 999
                self.assert_bad_tracks(rows, {'11': history})

    def test_historical_duration_rejects_bool_float_and_negative_values(self):
        for value in (True, 0.0, -1):
            with self.subTest(value=value):
                rows = copy.deepcopy(self.rows)
                rows[0][4] = 1 if value is True else 0
                self.assert_bad_tracks(rows, {'11': {'duration_ms': value}})

    def test_historical_album_dict_requires_text_name(self):
        for value in (None, 1, [], {}):
            with self.subTest(value=value):
                self.assert_bad_tracks(historical={'11': {'album': {'name': value}}})
        self.assertIsNone(self.inputs().validate_tracks(
            self.tracks, {'11': {'album': {'id': 3, 'name': ''}}}))

    def test_historical_album_dict_matches_preserved_input(self):
        self.rows[0][3] = '历史专辑'
        self.write_inputs()
        self.assertIsNone(self.inputs().validate_tracks(
            self.tracks, {'11': {'album': {'id': 3, 'name': '历史专辑'}}}))

    def test_legacy_and_wrapped_reviews_allow_absent_declarations(self):
        for data in ([], [{'position': 1}], {'recommendations': [{'position': 2}]},
                     {'conflicts': [{'position': 1}]}, {}):
            with self.subTest(data=data):
                self.assertIsNone(self.inputs().validate_review(data, self.tracks, self.historical))

    def test_review_does_not_transform_or_validate_label_policy(self):
        data = {'recommendations': [{'position': 1, 'language': 'non-policy-label',
                                    'confidence': 'ignore', 'flags': 'policy checks elsewhere'}]}
        original = copy.deepcopy(data)
        self.assertIsNone(self.inputs().validate_review(data, self.tracks, self.historical))
        self.assertEqual(data, original)

    def test_review_names_in_both_sections_bind_to_effective_title(self):
        for section in ('recommendations', 'conflicts'):
            for value in ('错误曲名', '', '  ', None, 123):
                with self.subTest(section=section, value=value):
                    self.assert_bad_review({section: [{'position': 1, 'name': value}]})
        self.assert_bad_review([{'position': 1, 'name': '错误曲名'}])

    def test_review_positions_in_both_sections_require_exact_valid_int(self):
        for section in ('recommendations', 'conflicts'):
            for position in (True, 1.0, 0, 3, None, '1'):
                with self.subTest(section=section, position=position):
                    self.assert_bad_review({section: [{'position': position}]})

    def test_scope_constrains_recommendations_and_conflicts(self):
        for section in ('recommendations', 'conflicts'):
            self.assert_bad_review({'scope': {'start': 1, 'end': 1, 'count': 1},
                                    section: [{'position': 2}]})
        self.assertIsNone(self.inputs().validate_review(
            {'scope': {'start': 2, 'end': 2, 'count': 1}, 'recommendations': [{'position': 2}]},
            self.tracks, self.historical))

    def test_scope_requires_valid_integer_bounds_and_count(self):
        for scope in (None, [], {}, {'start': 0, 'end': 1, 'count': 2},
                      {'start': 1, 'end': 3, 'count': 3}, {'start': 2, 'end': 1, 'count': 0},
                      {'start': True, 'end': 2, 'count': 2},
                      {'start': 1, 'end': 2.0, 'count': 2},
                      {'start': 1, 'end': 2, 'count': True},
                      {'start': 1, 'end': 2, 'count': 1}):
            with self.subTest(scope=scope):
                self.assert_bad_review({'scope': scope})

    def test_sources_require_nonempty_allowed_basename_digest_mapping(self):
        for sources in ({}, [], None, {BASELINE: 'abc'}, {BASELINE: 'g' * 64},
                        {'../' + BASELINE: 'a' * 64}, {'unknown.json': 'a' * 64},
                        {BASELINE: 7}):
            with self.subTest(sources=sources):
                self.assert_bad_review({'sources': sources})

    def test_declared_source_digest_matches_cached_original_bytes(self):
        self.write_json(BASELINE, {'snapshot': 'old'})
        original = (self.folder / BASELINE).read_bytes()
        inputs = self.inputs()
        self.assertEqual(inputs.json(BASELINE), {'snapshot': 'old'})
        self.write_json(BASELINE, {'snapshot': 'new'})
        old_digest = hashlib.sha256(original).hexdigest()
        self.assertIsNone(inputs.validate_review({'sources': {BASELINE: old_digest.upper()}},
                                                self.tracks, self.historical))
        with self.assertRaises(ValueError):
            inputs.validate_review({'sources': {BASELINE: '0' * 64}}, self.tracks, self.historical)
        with self.assertRaises(ValueError):
            inputs.verify_unchanged()

    def test_declared_source_is_consumed_and_checked_again(self):
        self.write_json(HISTORY, {})
        digest = hashlib.sha256((self.folder / HISTORY).read_bytes()).hexdigest()
        inputs = self.inputs()
        inputs.validate_review({'sources': {HISTORY: digest}}, self.tracks, self.historical)
        (self.folder / HISTORY).unlink()
        with self.assertRaises(ValueError):
            inputs.verify_unchanged()

    def test_json_cache_returns_first_raw_bytes_despite_valid_json_change(self):
        self.write_json(BASELINE, {'a': 1})
        inputs = self.inputs()
        self.assertEqual(inputs.json(BASELINE), {'a': 1})
        self.write_json(BASELINE, {'a': 2})
        self.assertEqual(inputs.json(BASELINE), {'a': 1})
        with self.assertRaises(ValueError):
            inputs.verify_unchanged()

    def test_semantically_identical_byte_change_is_detected(self):
        self.write_json(BASELINE, {'a': 1})
        inputs = self.inputs()
        inputs.json(BASELINE)
        (self.folder / BASELINE).write_bytes(b'{ "a": 1 }')
        with self.assertRaises(ValueError):
            inputs.verify_unchanged()

    def test_jsonl_change_after_validation_is_detected(self):
        inputs = self.inputs()
        inputs.validate_tracks(self.tracks, self.historical)
        (self.folder / INPUTS[2]).write_bytes(b'\n')
        with self.assertRaises(ValueError):
            inputs.verify_unchanged()

    def test_same_bytes_and_unconsumed_file_changes_are_allowed(self):
        self.write_json(BASELINE, {'a': 1})
        inputs = self.inputs()
        inputs.json(BASELINE)
        raw = (self.folder / BASELINE).read_bytes()
        (self.folder / BASELINE).write_bytes(raw)
        self.write_json(REVIEW, [])
        self.assertIsNone(inputs.verify_unchanged())

    def test_optional_absence_is_cached_and_appearance_refuses(self):
        inputs = self.inputs()
        self.assertIsNone(inputs.json(REVIEW, optional=True))
        self.write_json(REVIEW, [])
        self.assertIsNone(inputs.json(REVIEW, optional=True))
        with self.assertRaises(ValueError):
            inputs.verify_unchanged()

    def test_optional_malformed_file_is_not_treated_as_missing(self):
        (self.folder / REVIEW).write_bytes(b'invalid')
        with self.assertRaises(ValueError):
            self.inputs().json(REVIEW, optional=True)

    def test_optional_directory_is_not_treated_as_missing(self):
        (self.folder / REVIEW).mkdir()
        with self.assertRaises(ValueError):
            self.inputs().json(REVIEW, optional=True)

    def test_existing_json_null_is_rejected_instead_of_optional_absence(self):
        self.write_json(REVIEW, None)
        for optional in (False, True):
            with self.subTest(optional=optional):
                with self.assertRaises(ValueError):
                    self.inputs().json(REVIEW, optional=optional)
    def test_required_missing_and_unapproved_names_refuse(self):
        for name in (BASELINE, '../' + BASELINE, str(self.folder / BASELINE), 'other.json'):
            with self.subTest(name=name):
                with self.assertRaises(ValueError):
                    self.inputs().json(name)

    def test_review_shapes_refuse_before_policy_merge(self):
        for data in (None, 'bad', 1, {'recommendations': {}}, {'conflicts': None},
                     {'recommendations': [None]}, [{'name': '第一曲'}]):
            with self.subTest(data=data):
                self.assert_bad_review(data)


if __name__ == '__main__':
    unittest.main()
