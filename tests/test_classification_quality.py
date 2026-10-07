"""Offline review signals and diagnostic sampling must preserve source records."""

import copy
import hashlib
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from netease_organizer.classification_quality import review_reasons, select_pilot, version_hints, diagnostic_strata


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "audit_classification_quality.py"


def record(position, **changes):
    result = {
        "position": position,
        "name": f"模拟歌曲 {position}",
        "artists": "模拟艺人",
        "styles": ["流行抒情"],
        "scenes": ["通勤散步"],
        "language": "英语",
        "style_judgment_score": 0.9,
        "evidence_note": "已核对具体录音的编制与演唱版本",
        "language_evidence_note": "待试听核对的来源说明",
        "pending_reasons": [],
        "review_note": False,
    }
    result.update(changes)
    return result


class ClassificationQualityTests(unittest.TestCase):
    def test_existing_pending_and_hidden_signals_are_visible_without_mutation(self):
        source = record(
            1,
            pending_reasons=["语言待辨识", "语言待辨识"],
            review_note=True,
            style_judgment_score=0.6,
            evidence_note="具体曲目、艺人与专辑的宽风格模型判断",
        )
        before = copy.deepcopy(source)
        self.assertEqual(
            review_reasons(source),
            [
                "语言待辨识",
                "录音或语言证据存在冲突",
                "风格判断把握较低",
                "风格或场景依据过于宽泛",
            ],
        )
        self.assertEqual(source, before)

    def test_low_score_assigned_track_enters_review(self):
        self.assertEqual(
            review_reasons(record(1, style_judgment_score=0.69)),
            ["风格判断把握较低"],
        )
        self.assertEqual(review_reasons(record(2, style_judgment_score=0.7)), [])

    def test_generic_evidence_still_requires_review_with_high_self_score(self):
        self.assertEqual(
            review_reasons(record(
                1,
                style_judgment_score=0.99,
                evidence_note="具体录音、艺人与专辑的宽风格模型判断；听歌场景为建议",
            )),
            ["风格或场景依据过于宽泛"],
        )

    def test_invalid_or_missing_scores_do_not_become_low_confidence_measurements(self):
        for score in (None, True, False, "0.2", float("nan"), float("inf"), -1, 2, 10 ** 400):
            with self.subTest(score=score):
                self.assertEqual(review_reasons(record(1, style_judgment_score=score)), [])
        self.assertEqual(review_reasons(record(1, review_note="false")), [])

    def test_only_the_known_generic_templates_are_flagged(self):
        self.assertEqual(review_reasons(record(
            1, evidence_note="具体曲目、艺人与专辑的宽风格模型判断；已附逐段听辨证据"
        )), [])
        self.assertEqual(review_reasons(record(2, evidence_note=[])), [])

    def test_generic_style_explanation_with_only_language_addendum_still_needs_review(self):
        for suffix in (
            '语言按具体演唱版本判定，不按歌曲标题或艺人国籍',
            '吟唱使用构造语言，不能视为纯音乐',
        ):
            with self.subTest(suffix=suffix):
                source = record(1, evidence_note='具体录音、艺人与专辑的宽风格模型判断；听歌场景为建议；' + suffix)
                before = copy.deepcopy(source)
                self.assertIn('风格或场景依据过于宽泛', review_reasons(source))
                self.assertEqual(source, before)
        self.assertIn('风格或场景依据过于宽泛', review_reasons(record(
            2, evidence_note=' 具体曲目、艺人与专辑的宽风格模型判断。 ')))

    def test_explicit_unverified_recording_note_is_not_hidden_by_high_score(self):
        for note in ('模拟艺人的独立流行取向，录音细节待核',
                     '模拟艺人旋律说唱取向，具体曲目待核',
                     '了解此曲采样，采样语种留待核对。'):
            with self.subTest(note=note):
                source = record(1, style_judgment_score=.99, evidence_note=note)
                self.assertEqual(review_reasons(source), ['分类依据明确标注待核对'])
                self.assertIn('weak_evidence', diagnostic_strata(source))
        self.assertEqual(review_reasons(record(2, evidence_note='具体曲目待核的问题已核对原专辑解决。')), [])
        self.assertEqual(review_reasons(record(3, name='歌曲待核', evidence_note='已核对具体录音')), [])

    def test_version_hints_cover_alternate_speed_edit_and_clip_recordings_without_changing_labels(self):
        examples = {
            '模拟曲目 (EXPO Ver.)': 'EXPO 版本',
            '模拟曲目 (Speed Up Version)': '加速版本',
            '模拟曲目 [Sped-Up]': '加速版本',
            '模拟曲目 (Slowed + Reverb)': '减速版本',
            '模拟曲目 (Radio Edit)': '剪辑或扩展版本',
            '模拟曲目 (Extended Mix)': '剪辑或扩展版本',
            '模拟曲目 (TV Size)': '短片段或电视截取版',
            '模拟曲目（片段）': '短片段或电视截取版',
            '模拟曲目 (Demo)': 'Demo 或试作版本',
        }
        for name, label in examples.items():
            with self.subTest(name=name):
                source = record(1, name=name)
                before = copy.deepcopy(source)
                self.assertIn(label, version_hints(source))
                self.assertEqual(review_reasons(source), [])
                self.assertEqual(source, before)
        for title in ('Olive credit Speedway', 'Democracy', '编辑爱情', 'Expo night'):
            self.assertEqual(version_hints(record(1, name=title)), [], title)

    def test_sampling_covers_sparse_labels_review_layers_and_recording_versions(self):
        records = [record(position) for position in range(1, 121)]
        changes = {
            101: {"review_note": True},
            102: {"style_judgment_score": 0.6},
            103: {"styles": ["待辨识"], "language": "待辨识", "pending_reasons": ["风格待辨识"]},
            104: {"evidence_note": "具体曲目、艺人与专辑的宽风格模型判断"},
            105: {"language": "韩语"},
            106: {"styles": ["古典与合唱"], "scenes": ["学习专注"]},
            107: {"name": "模拟歌曲 (Live)"},
            108: {"name": "模拟歌曲 (Remix)"},
            109: {"name": "模拟歌曲（翻唱）"},
            110: {"name": "模拟歌曲（伴奏）"},
        }
        for position, change in changes.items():
            records[position - 1].update(change)
        before = copy.deepcopy(records)
        selected = select_pilot(records)
        self.assertEqual(len(selected), 50)
        self.assertEqual(len(set(selected)), 50)
        self.assertTrue(set(range(101, 111)).issubset(selected))
        self.assertTrue(any(position <= 100 for position in selected))
        self.assertEqual(selected, sorted(selected))
        self.assertEqual(select_pilot(records), selected)
        self.assertEqual(records, before)

    def test_small_sample_preserves_source_order_instead_of_sorting_positions(self):
        records = [record(8), record(2, language="韩语"), record(5)]
        self.assertEqual(select_pilot(records, limit=10), [8, 2, 5])
        self.assertEqual(select_pilot(records, limit=1), [2])
        self.assertEqual(select_pilot([], limit=50), [])
        self.assertEqual(select_pilot(records, limit=0), [])

    def test_sampling_rejects_ambiguous_positions_and_invalid_limits(self):
        for records in ([record(1), record(1)], [record(True)], [record(0)]):
            with self.subTest(records=records):
                with self.assertRaises(ValueError):
                    select_pilot(records)
        for limit in (-1, True, 2.5, "50"):
            with self.subTest(limit=limit):
                with self.assertRaises(ValueError):
                    select_pilot([record(1)], limit=limit)

    def test_sampling_spreads_one_layer_across_the_source_instead_of_taking_its_prefix(self):
        records = [record(position) for position in range(1, 201)]
        selected = select_pilot(records)
        self.assertEqual(len(selected), 50)
        for lower, upper in ((1, 40), (81, 120), (161, 200)):
            self.assertTrue(any(lower <= position <= upper for position in selected))
        self.assertLessEqual(sum(position <= 50 for position in selected), 15)

    def test_sampling_spreads_each_diagnostic_layer_across_input_blocks(self):
        changes = (
            {},
            {"review_note": True},
            {"style_judgment_score": 0.6},
            {"styles": ["待辨识"], "pending_reasons": ["风格待辨识"]},
            {"evidence_note": "具体曲目、艺人与专辑的宽风格模型判断"},
        )
        records = [record(position, **changes[(position - 1) % 5]) for position in range(1, 601)]
        selected = select_pilot(records)
        self.assertEqual(len(selected), 50)
        for layer in range(5):
            for lower, upper in ((1, 200), (201, 400), (401, 600)):
                with self.subTest(layer=layer, block=(lower, upper)):
                    self.assertTrue(any(
                        lower <= position <= upper and (position - 1) % 5 == layer
                        for position in selected
                    ))

    def test_sampling_uses_different_artists_when_the_layer_offers_them(self):
        records = [
            record(position, artists=f"模拟艺人 {position}" if position % 10 == 0 else "高频模拟艺人")
            for position in range(1, 201)
        ]
        selected = set(select_pilot(records))
        artists = {entry["artists"] for entry in records if entry["position"] in selected}
        self.assertGreaterEqual(len(artists), 16)


class ClassificationQualityScriptTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source_dir = self.root / "source-artifacts"
        self.source_dir.mkdir()
        self.source = self.source_dir / "全库分类-逐曲结果.json"
        self.output = self.root / "quality-draft"
        self.records = [
            record(1, name="Olive lively recording"),
            record(2, style_judgment_score=0.5),
            record(3, review_note=True, name="模拟歌曲 (Live)"),
            record(4, styles=["待辨识"], pending_reasons=["风格待辨识"]),
            record(5, evidence_note="具体曲目、艺人与专辑的宽风格模型判断"),
        ]
        self.write_source()

    def write_source(self, **changes):
        document = {"kind": "classification_report", "summary": {}, "records": self.records}
        document.update(changes)
        self.source.write_text(json.dumps(document, ensure_ascii=False), encoding="utf-8")

    def run_script(self, output=None, *extra):
        return subprocess.run(
            [sys.executable, "-X", "utf8", str(SCRIPT), "--input", str(self.source),
             "--output-dir", str(output or self.output), *extra],
            cwd=ROOT,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=15,
        )

    def test_script_creates_independent_drafts_with_hashes_and_blank_evaluation(self):
        before = self.source.read_bytes()
        result = self.run_script(self.output, "--limit", "3")
        self.assertEqual(result.returncode, 0, result.stderr)
        quality = json.loads((self.output / "quality-report.json").read_text(encoding="utf-8"))
        pilot = json.loads((self.output / "pilot-review.json").read_text(encoding="utf-8"))
        expected_hash = hashlib.sha256(before).hexdigest()
        self.assertEqual(quality["source_sha256_before"], expected_hash)
        self.assertEqual(quality["source_sha256_after"], expected_hash)
        self.assertEqual(quality["kind"], "classification_quality_audit")
        self.assertEqual(quality["summary"]["source_count"], 5)
        self.assertEqual(quality["summary"]["needs_review_count"], 4)
        self.assertEqual(quality["summary"]["low_score_without_pending_count"], 1)
        self.assertEqual(quality["summary"]["conflict_without_pending_count"], 1)
        self.assertEqual([entry["position"] for entry in quality["flagged_records"]], [2, 3, 4, 5])
        self.assertEqual(pilot["kind"], "classification_quality_pilot")
        self.assertEqual(pilot["status"], "diagnostic_draft")
        self.assertEqual(len(pilot["records"]), 3)
        self.assertEqual(len(pilot["scene_rules"]), 6)
        for rule in pilot["scene_rules"]:
            self.assertTrue(rule["include"])
            self.assertTrue(rule["exclude"])
        for entry in pilot["records"]:
            self.assertFalse(entry["evaluation"]["evaluated"])
            self.assertIsNone(entry["evaluation"]["verified_styles"])
            self.assertIsNone(entry["evaluation"]["verified_language"])
            self.assertEqual(entry["evaluation"]["evidence_sources"], [])
        self.assertEqual(self.source.read_bytes(), before)

    def test_summary_counts_language_addenda_and_explicit_unverified_notes_consistently(self):
        self.records[0]['evidence_note'] = '具体录音、艺人与专辑的宽风格模型判断；听歌场景为建议；语言按具体演唱版本判定，不按歌曲标题或艺人国籍'
        self.records[1].update(style_judgment_score=.99, evidence_note='模拟艺人风格取向，具体录音待核')
        self.write_source()
        result = self.run_script()
        self.assertEqual(result.returncode, 0, result.stderr)
        quality = json.loads((self.output / 'quality-report.json').read_text(encoding='utf-8'))
        summary = quality['summary']
        self.assertEqual(summary['generic_evidence_count'], 2)
        self.assertEqual(summary['explicit_unverified_evidence_count'], 1)
        self.assertEqual(summary['needs_review_count'], 5)

    def test_rerun_and_partial_existing_outputs_are_never_overwritten(self):
        for filename in ("quality-report.json", "pilot-review.json"):
            with self.subTest(filename=filename):
                output = self.root / filename.removesuffix(".json")
                output.mkdir()
                target = output / filename
                target.write_bytes(b"existing personal draft")
                result = self.run_script(output)
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(target.read_bytes(), b"existing personal draft")
                self.assertEqual(list(output.iterdir()), [target])

    def test_source_artifact_directory_is_not_an_output_directory(self):
        before = self.source.read_bytes()
        result = self.run_script(self.source_dir)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.source.read_bytes(), before)
        self.assertEqual(list(self.source_dir.iterdir()), [self.source])

    def test_script_rejects_oversized_source_before_writing_any_drafts(self):
        self.source.write_bytes(b" " * (16 * 1024 * 1024 + 1))
        result = self.run_script()
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(self.output.exists())

    def test_script_rejects_wrong_kind_duplicate_positions_and_nonfinite_scores(self):
        bad_documents = [
            {"kind": "approved_classification_plan", "records": self.records},
            {"records": [record(1), record(1)]},
            {"records": [record(1, style_judgment_score=float("nan"))]},
            {"records": [record(1, style_judgment_score=True)]},
            {"records": [record(1, style_judgment_score=10 ** 400)]},
        ]
        for document in bad_documents:
            with self.subTest(document=document):
                self.write_source(**document)
                result = self.run_script()
                self.assertEqual(result.returncode, 2, result.stderr)
                self.assertFalse(self.output.exists())


if __name__ == "__main__":
    unittest.main()
