"""Offline plan generation preserves accepted manual language review state."""

import contextlib
import csv
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts import build_classification_plan as builder


class LocalArtifactWriter:
    """Replace Organizer construction, including its real CLI/cache dependencies."""

    def __init__(self, root):
        self.project = Path(root).resolve()
        self.artifacts = self.project / 'artifacts'

    def _workspace_target(self, relative):
        return self.project / relative

    def _write_artifact(self, filename, text, *, newline=None):
        (self.artifacts / filename).write_text(text, encoding='utf-8', newline=newline)


class BuildClassificationPlanTests(unittest.TestCase):
    def fixture(self, entry, *, wrapped=False, model=None):
        directory = tempfile.TemporaryDirectory(prefix='classification-plan-fixture-')
        self.addCleanup(directory.cleanup)
        root = Path(directory.name)
        artifacts = root / 'artifacts'
        artifacts.mkdir()
        track_id = 'A' * 32
        inputs = {
            '全库分类-账号基线.json': {'snapshot': {
                'account': {'original_id': '1', 'id': 'B' * 32},
                'liked': {'id': 'C' * 32, 'original_id': '2', 'track_count': 1,
                          'membership_complete': True, 'tracks': [{
                              'id': track_id, 'original_id': '3', 'name': '模拟录音',
                              'artists': [{'name': '模拟艺人'}], 'metadata_available': True}]}}},
            '分类判断-分块01.json': [[1, ['pop'], ['commute'], model, .9, '模拟录音风格依据']],
            '分类判断-分块02.json': [],
            '分类判断-分块03.json': [],
            '全库分类-歌词特征.json': {'tracks': {'3': {
                'status': 'ok', 'has_original_lyric_text': True, 'original_line_count': 54,
                'script_counts': {'han': 391}, 'language_hints': ['zh_or_yue']}}},
            '网易云本地歌单快照.json': {'tracks': {}},
            '分类语言核验01.json': {'recommendations': [entry]} if wrapped else [entry],
        }
        for name, value in inputs.items():
            (artifacts / name).write_text(json.dumps(value, ensure_ascii=False), encoding='utf-8')
        for i in range(1, 4):
            (artifacts / f'分类输入-分块{i:02}.jsonl').write_text(
                json.dumps([1, '模拟录音', ['模拟艺人'], '', None, True], ensure_ascii=False) + '\n'
                if i == 1 else '', encoding='utf-8')
        return root

    def invoke(self, root):
        with patch.object(builder, '__file__', str(root / 'scripts/build_classification_plan.py')), \
                patch.object(builder, 'Organizer', LocalArtifactWriter), \
                contextlib.redirect_stdout(io.StringIO()):
            builder.main()

    def assert_result(self, root, *, language, assigned, review, reason='人工保留的版本依据'):
        artifacts = root / 'artifacts'
        report = json.loads((artifacts / '全库分类-逐曲结果.json').read_text(encoding='utf-8'))
        reviews = json.loads((artifacts / '全库分类-需复核录音.json').read_text(encoding='utf-8'))
        record = report['records'][0]
        self.assertEqual(record['language'], language)
        self.assertEqual(record['language_evidence_note'], '单曲录音复核：' + reason)
        self.assertIs(record['review_note'], review)
        self.assertEqual(report['summary']['review_note_count'], int(review))
        self.assertEqual([item['position'] for item in reviews], [1] if review else [])
        if review:
            self.assertEqual(reviews[0]['assigned'], assigned)
            self.assertEqual(reviews[0]['note'], record['language_evidence_note'])
        plan = json.loads((artifacts / '分类整理计划.json').read_text(encoding='utf-8'))
        self.assertEqual(plan['source_track_ids'], ['A' * 32])
        self.assertEqual([job['name'] for job in plan['jobs'] if job['dimension'] == 'language'],
                         ['语言 · 英语'] if assigned == 'en' else [])

    def test_explicit_unresolved_review_survives_list_and_recommendations_inputs(self):
        for wrapped, label, public in [(False, 'instrumental', '器乐或配乐录音'), (True, 'en', '英语')]:
            with self.subTest(wrapped=wrapped):
                entry = {'position': 1, 'language': label, 'confidence': 'high',
                         'notes': '人工保留的版本依据', 'review_note': True}
                root = self.fixture(entry, wrapped=wrapped)
                self.invoke(root)
                self.assert_result(root, language=public, assigned=label, review=True)

    def test_explicit_resolution_overrides_legacy_wrong_lyric_and_reason_text(self):
        entry = {'position': 1, 'suggested_language': 'instrumental', 'review_note': False,
                 'flags': ['possible_wrong_lyric'], 'reason': '已核对旧提示“仍须复核”'}
        root = self.fixture(entry)
        self.invoke(root)
        self.assert_result(root, language='器乐或配乐录音', assigned='instrumental', review=False,
                           reason='已核对旧提示“仍须复核”')

    def test_unknown_language_always_requires_review_even_when_explicitly_cleared(self):
        entry = {'position': 1, 'language': None, 'review_note': False, 'notes': '人工保留的版本依据'}
        root = self.fixture(entry)
        self.invoke(root)
        self.assert_result(root, language='待辨识', assigned=None, review=True)

    def test_only_exact_legacy_wrong_lyric_flag_reopens_nonnull_manual_label(self):
        cases = [({}, False), ({'flags': []}, False),
                 ({'flags': ['version_marker', 'future_flag', 'possible_wrong_lyric_extra']}, False),
                 ({'flags': ['version_marker', 'possible_wrong_lyric']}, True)]
        for changes, review in cases:
            with self.subTest(changes=changes):
                entry = {'position': 1, 'language': 'instrumental', 'notes': '人工保留的版本依据', **changes}
                root = self.fixture(entry)
                self.invoke(root)
                self.assert_result(root, language='器乐或配乐录音', assigned='instrumental', review=review)

    def test_invalid_accepted_review_fields_fail_before_any_output_is_written(self):
        cases = [('review_note', value) for value in (None, 0, 1, 'true', [], {})]
        cases += [('flags', value) for value in (None, 'possible_wrong_lyric', {}, [False], [1], ['valid', None])]
        for key, value in cases:
            with self.subTest(field=key, value=value):
                entry = {'position': 1, 'language': 'instrumental', key: value}
                root = self.fixture(entry)
                artifacts = root / 'artifacts'
                before = {path.name: path.read_bytes() for path in artifacts.iterdir()}
                with self.assertRaises(ValueError):
                    self.invoke(root)
                self.assertEqual({path.name: path.read_bytes() for path in artifacts.iterdir()}, before)

    def test_ignored_recommendations_do_not_validate_or_apply_review_fields(self):
        for confidence, model in [('low', None), ('high', 'en')]:
            with self.subTest(confidence=confidence, model=model):
                entry = {'position': 1, 'language': 'ja', 'confidence': confidence,
                         'review_note': 'invalid but ignored', 'flags': None, 'notes': '不应采用'}
                root = self.fixture(entry, wrapped=True, model=model)
                self.invoke(root)
                report = json.loads((root / 'artifacts/全库分类-逐曲结果.json').read_text(encoding='utf-8'))
                record = report['records'][0]
                self.assertEqual(record['language'], '待辨识' if model is None else '英语')
                self.assertNotIn('不应采用', record['language_evidence_note'])
                self.assertIs(record['review_note'], True)
                self.assertEqual(report['summary']['review_note_count'], 1)

    def conflict_fixture(self, conflicts, *, entry=None, model='other', hints=None):
        root = self.fixture(entry or {'position': 1, 'language': None}, model=model)
        artifacts = root / 'artifacts'
        (artifacts / '分类语言核验01.json').write_text(json.dumps({
            'recommendations': [] if entry is None else [entry], 'conflicts': conflicts,
        }, ensure_ascii=False), encoding='utf-8')
        (artifacts / '全库分类-歌词特征.json').write_text(json.dumps({'tracks': {'3': {
            'status': 'ok', 'has_original_lyric_text': True, 'original_line_count': 40,
            'script_counts': {'latin': 1200}, 'language_hints': hints or ['fr'],
        }}}), encoding='utf-8')
        return root

    def read_exports(self, root):
        artifacts = root / 'artifacts'
        report = json.loads((artifacts / '全库分类-逐曲结果.json').read_text(encoding='utf-8'))
        reviews = json.loads((artifacts / '全库分类-需复核录音.json').read_text(encoding='utf-8'))
        plan = json.loads((artifacts / '分类整理计划.json').read_text(encoding='utf-8'))
        return report, reviews, plan

    def test_unresolved_conflict_retains_specific_evidence_without_adopting_suggested_label(self):
        reason = '词表提示与具体录音矛盾，需要核对版本，不能按最高词分改语言。'
        root = self.conflict_fixture([{
            'position': 1, 'suggested_language': 'en', 'confidence': 'low',
            'reason': reason, 'flags': ['function_word_conflict', 'recording_version_review'],
        }])
        self.invoke(root)
        report, reviews, plan = self.read_exports(root)
        record = report['records'][0]
        self.assertEqual(record['language'], '其他与多语')
        self.assertEqual(record['pending_reasons'], [])
        self.assertIn(reason, record['language_evidence_note'])
        self.assertIs(record['review_note'], True)
        self.assertEqual(report['summary']['review_note_count'], 1)
        self.assertEqual([(x['position'], x['assigned'], x['note']) for x in reviews],
                         [(1, 'other', record['language_evidence_note'])])
        self.assertEqual([job['name'] for job in plan['jobs']],
                         ['场景 · 通勤散步', '风格 · 流行抒情', '语言 · 其他与多语'])
        self.assertTrue(all(job['candidate_track_ids'] == ['A' * 32] for job in plan['jobs']))

    def test_only_exact_unresolved_legacy_conflict_flags_add_review(self):
        cases = [('model_text_conflict', True), ('possible_wrong_lyric', True),
                 ('possible_description_text', True), ('recording_version_review', True),
                 ('specific_bilingual_version', False), ('recording_version_review_extra', False),
                 ('future_flag', False)]
        for flag, expected in cases:
            with self.subTest(flag=flag):
                root = self.conflict_fixture([{'position': 1, 'reason': '保留具体语言依据',
                                               'confidence': 'low', 'flags': [flag]}])
                self.invoke(root)
                report, reviews, _ = self.read_exports(root)
                record = report['records'][0]
                self.assertIn('保留具体语言依据', record['language_evidence_note'])
                self.assertIs(record['review_note'], expected)
                self.assertEqual(len(reviews), int(expected))

    def test_explicit_conflict_status_overrides_legacy_flags_but_does_not_clear_inferred_review(self):
        for explicit, flags, model, expected in [
            (True, [], 'other', True),
            (False, ['recording_version_review'], 'other', False),
            (False, [], 'en', True),
        ]:
            with self.subTest(explicit=explicit, model=model):
                root = self.conflict_fixture([{'position': 1, 'reason': '具体补充说明',
                                               'review_note': explicit, 'flags': flags}], model=model)
                self.invoke(root)
                report, reviews, _ = self.read_exports(root)
                self.assertIs(report['records'][0]['review_note'], expected)
                self.assertEqual(len(reviews), int(expected))

    def test_explicit_accepted_resolution_wins_but_null_stays_unresolved(self):
        for wrapped, label, expected in [(False, 'en', False), (True, 'en', False), (False, None, True)]:
            with self.subTest(wrapped=wrapped, label=label):
                root = self.conflict_fixture([{'position': 1, 'reason': '较早的录音待核提示',
                                               'flags': ['recording_version_review']}], model=None)
                entry = {'position': 1, 'language': label, 'review_note': False,
                         'confidence': 'high', 'notes': '新近明确复核结论'}
                (root / 'artifacts/分类语言核验02.json').write_text(json.dumps(
                    {'recommendations': [entry]} if wrapped else [entry]), encoding='utf-8')
                self.invoke(root)
                report, reviews, _ = self.read_exports(root)
                record = report['records'][0]
                self.assertEqual(record['language'], '英语' if label == 'en' else '待辨识')
                self.assertIs(record['review_note'], expected)
                self.assertEqual(len(reviews), int(expected))
                self.assertIn('新近明确复核结论', record['language_evidence_note'])
                self.assertIn('较早的录音待核提示', record['language_evidence_note'])

    def test_informational_conflict_cannot_erase_manual_unresolved_state(self):
        entry = {'position': 1, 'language': 'en', 'confidence': 'high',
                 'review_note': True, 'notes': '仍需复核的具体录音'}
        root = self.conflict_fixture([{'position': 1, 'reason': '已知补充事实', 'review_note': False}],
                                     entry=entry, model=None)
        self.invoke(root)
        report, reviews, _ = self.read_exports(root)
        self.assertIs(report['records'][0]['review_note'], True)
        self.assertEqual(len(reviews), 1)

    def test_invalid_consumed_conflicts_fail_without_writing_exports(self):
        valid = {'position': 1, 'reason': '具体录音依据', 'flags': []}
        cases = [None, {}, 'invalid', [None], [valid, valid]]
        cases += [[{**valid, 'position': value}] for value in (True, 0, 2, '1')]
        cases += [[{**valid, 'reason': value}] for value in (None, 1, '', '  ') ]
        cases += [[{**valid, 'flags': value}] for value in (None, 'flag', [1])]
        cases += [[{**valid, 'review_note': value}] for value in (None, 0, 'false')]
        for conflicts in cases:
            with self.subTest(conflicts=conflicts):
                root = self.conflict_fixture(conflicts)
                artifacts = root / 'artifacts'
                before = {p.name: p.read_bytes() for p in artifacts.iterdir()}
                with self.assertRaises(ValueError):
                    self.invoke(root)
                self.assertEqual({p.name: p.read_bytes() for p in artifacts.iterdir()}, before)

    def test_misaligned_original_input_refuses_legacy_positional_review_without_outputs(self):
        root = self.fixture({'position': 1, 'language': 'en', 'notes': '另一录音的复核'})
        artifacts = root / 'artifacts'
        (artifacts / '分类输入-分块01.jsonl').write_text(json.dumps(
            [1, '原来核对的另一录音', ['模拟艺人'], '', None, True], ensure_ascii=False), encoding='utf-8')
        before = {p.name: p.read_bytes() for p in artifacts.iterdir()}
        with self.assertRaises(ValueError):
            self.invoke(root)
        self.assertEqual({p.name: p.read_bytes() for p in artifacts.iterdir()}, before)

    def test_missing_original_input_refuses_without_inventing_identity(self):
        root = self.fixture({'position': 1, 'language': 'en', 'notes': '具体录音依据'})
        artifacts = root / 'artifacts'
        (artifacts / '分类输入-分块01.jsonl').unlink()
        before = {p.name: p.read_bytes() for p in artifacts.iterdir()}
        with self.assertRaises(ValueError):
            self.invoke(root)
        self.assertEqual({p.name: p.read_bytes() for p in artifacts.iterdir()}, before)

    def test_declared_review_source_scope_or_name_mismatch_refuses_before_publication(self):
        valid_entry = {'position': 1, 'language': 'en', 'confidence': 'high', 'notes': '具体语言依据'}
        cases = [
            {'recommendations': [valid_entry], 'sources': {'全库分类-歌词特征.json': '0' * 64}},
            {'recommendations': [valid_entry], 'scope': {'start': 2, 'end': 2, 'count': 1}},
            {'recommendations': [{**valid_entry, 'name': '另一首歌'}]},
            {'recommendations': [], 'conflicts': [{'position': 1, 'name': '另一首歌',
                                                 'reason': '不得转给不同录音', 'flags': ['recording_version_review']}]},
        ]
        for review in cases:
            with self.subTest(review=review):
                root = self.fixture(valid_entry, wrapped=True)
                artifacts = root / 'artifacts'
                (artifacts / '分类语言核验01.json').write_text(json.dumps(review, ensure_ascii=False), encoding='utf-8')
                before = {p.name: p.read_bytes() for p in artifacts.iterdir()}
                with self.assertRaises(ValueError):
                    self.invoke(root)
                self.assertEqual({p.name: p.read_bytes() for p in artifacts.iterdir()}, before)

    def test_source_change_during_build_refuses_before_any_exports(self):
        root = self.fixture({'position': 1, 'language': 'en', 'notes': '具体录音依据'})
        artifacts = root / 'artifacts'
        real_infer = builder.infer_language

        def infer_then_change(*args):
            value = real_infer(*args)
            with (artifacts / '全库分类-歌词特征.json').open('a', encoding='utf-8') as stream:
                stream.write(' ')
            return value

        with patch.object(builder, 'infer_language', side_effect=infer_then_change):
            with self.assertRaises(ValueError):
                self.invoke(root)
        for name in ('分类整理计划.json', '全库分类-逐曲结果.json', '全库分类-需复核录音.json',
                     '喜欢的音乐-分类明细.csv', '喜欢的音乐-分类清单.md'):
            self.assertFalse((artifacts / name).exists())

    def test_csv_text_fields_escape_hidden_and_fullwidth_formula_prefixes(self):
        cases = (
            ('=1+1', "'=1+1"), ('+1+1', "'+1+1"),
            ('-1+1', "'-1+1"), ('@SUM(1,1)', "'@SUM(1,1)"),
            ('\t=1+1', "'\t=1+1"), ('\r=1+1', "'\r=1+1"),
            ('\n=1+1', "'\n=1+1"), ('\t普通文本', "'\t普通文本"),
            (' =1+1', "' =1+1"), (' \t\r=1+1', "' \t\r=1+1"),
            ('\u00a0+1+1', "'\u00a0+1+1"), ('　＠SUM(1,1)', "'　＠SUM(1,1)"),
            ('＝1+1', "'＝1+1"), ('＋1+1', "'＋1+1"),
            ('－1+1', "'－1+1"), ('＠SUM(1,1)', "'＠SUM(1,1)"),
        )
        for value, escaped in cases:
            with self.subTest(value=value):
                self.assert_exported_text(value, escaped)

    def test_csv_ordinary_and_already_escaped_text_keeps_delimiters_and_record_boundaries(self):
        for value in ('普通标题', '  普通标题', "'=1+1", '录音,\r=1+1\n"引号";@SUM(1,1)'):
            with self.subTest(value=value):
                self.assert_exported_text(value, value)

    def assert_exported_text(self, value, expected_csv):
        root = self.fixture({'position': 1, 'language': 'en', 'notes': '核验依据'})
        artifacts = root / 'artifacts'
        baseline_path = artifacts / '全库分类-账号基线.json'
        baseline = json.loads(baseline_path.read_text(encoding='utf-8'))
        track = baseline['snapshot']['liked']['tracks'][0]
        track.update(name=value, artists=[{'name': value}])
        baseline_path.write_text(json.dumps(baseline, ensure_ascii=False), encoding='utf-8')
        (artifacts / '分类输入-分块01.jsonl').write_text(json.dumps(
            [1, value, [value], '', None, True], ensure_ascii=False) + '\n', encoding='utf-8')
        judgment_path = artifacts / '分类判断-分块01.json'
        judgment = json.loads(judgment_path.read_text(encoding='utf-8'))
        judgment[0][5] = value
        judgment_path.write_text(json.dumps(judgment, ensure_ascii=False), encoding='utf-8')
        before = {path.name: path.read_bytes() for path in artifacts.iterdir()}
        self.invoke(root)
        with (artifacts / '喜欢的音乐-分类明细.csv').open(encoding='utf-8-sig', newline='') as stream:
            rows = list(csv.reader(stream))
        self.assertEqual([len(row) for row in rows], [9, 9])
        self.assertEqual([rows[1][index] for index in (1, 2, 7)], [expected_csv] * 3)
        self.assertEqual(rows[1][0], '1')
        self.assertEqual(rows[1][8], '单曲录音复核：核验依据')
        record = json.loads((artifacts / '全库分类-逐曲结果.json').read_text(encoding='utf-8'))['records'][0]
        self.assertEqual([record[key] for key in ('name', 'artists', 'evidence_note')], [value] * 3)
        plan = json.loads((artifacts / '分类整理计划.json').read_text(encoding='utf-8'))
        self.assertEqual(plan['source_track_ids'], ['A' * 32])
        self.assertTrue(all(job['candidate_track_ids'] == ['A' * 32] for job in plan['jobs']))
        self.assertEqual({name: (artifacts / name).read_bytes() for name in before}, before)


if __name__ == '__main__':
    unittest.main()
