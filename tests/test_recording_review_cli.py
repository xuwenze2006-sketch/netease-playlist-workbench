import json
import importlib.util
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / 'scripts/review_recordings.py'


class RecordingReviewCliTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.project = Path(self.temp.name)
        (self.project / 'artifacts').mkdir()
        self.source = self.project / 'artifacts/全库分类-逐曲结果.json'
        self.source.write_text(json.dumps({'kind': 'classification_report', 'records': [
            {'position': 1, 'name': '<script>alert(1)</script>', 'artists': 'Example & Artist',
             'styles': ['流行抒情'], 'scenes': ['放松睡前'], 'language': '英语',
             'evidence_note': '示例原始依据', 'language_evidence_note': '示例',
             'pending_reasons': [], 'style_judgment_score': 0.8, 'review_note': False},
            {'position': 2, 'name': 'Example Two', 'artists': 'Example Artist',
             'styles': ['待辨识'], 'scenes': [], 'language': '待辨识',
             'evidence_note': '待核对', 'language_evidence_note': '未知',
             'pending_reasons': ['风格待辨识'], 'style_judgment_score': 0.4, 'review_note': True},
        ]}, ensure_ascii=False), encoding='utf-8')
        self.original = self.source.read_bytes()

    def run_cli(self, *args, success=True):
        run = subprocess.run([sys.executable, '-B', '-X', 'utf8', str(SCRIPT),
                              '--project', str(self.project), *args],
                             capture_output=True, text=True, encoding='utf-8', timeout=20)
        self.assertEqual(run.returncode == 0, success, run.stderr + run.stdout)
        self.assertEqual(self.source.read_bytes(), self.original)
        return run

    def init(self):
        self.run_cli('init', '--positions', '1,2', '--bundle', 'first')
        return self.project / 'artifacts/recording-reviews/first/review.json'

    def test_init_read_and_preview_keep_unverified_members_and_escape_html(self):
        review = self.init()
        raw = review.read_bytes()
        result = json.loads(self.run_cli('read', '--bundle', 'first').stdout)
        self.assertEqual(result['summary']['reviewed_records'], 2)
        self.assertEqual(result['summary']['scene_verified_records'], 0)
        self.run_cli('preview', '--bundle', 'first')
        directory = self.project / 'artifacts/recording-previews/first'
        preview = json.loads((directory / 'preview.json').read_text(encoding='utf-8'))
        self.assertEqual(preview['records'][0]['after_scenes'], ['放松睡前'])
        self.assertEqual(preview['summary']['removed_count'], 0)
        html = (directory / 'index.html').read_text(encoding='utf-8')
        self.assertNotIn('<script>alert(1)</script>', html)
        self.assertIn('&lt;script&gt;alert(1)&lt;/script&gt;', html)
        self.assertIn('尚未完成试听', html)
        self.assertIn('未核验', html)
        self.assertEqual(review.read_bytes(), raw)

    def test_existing_bundle_and_preview_are_not_overwritten(self):
        review = self.init()
        raw = review.read_bytes()
        self.run_cli('init', '--positions', '1', '--bundle', 'first', success=False)
        self.run_cli('preview', '--bundle', 'first')
        html = self.project / 'artifacts/recording-previews/first/index.html'
        before = html.read_bytes()
        self.run_cli('preview', '--bundle', 'first', success=False)
        self.assertEqual(html.read_bytes(), before)
        self.assertEqual(review.read_bytes(), raw)

    def test_reviewed_audio_decision_saves_to_new_bundle_and_survives_reload(self):
        review = self.init()
        doc = json.loads(review.read_text(encoding='utf-8'))
        row = doc['records'][0]
        row['recording'].update(status='audio_verified', version='Test album version',
            note='Synthetic listening fixture, not a real recording review.',
            evidence_sources=[{'url': 'https://example.com/audio', 'title': 'Fixture',
                              'kind': 'audio', 'note': 'Synthetic recording source'}],
            listened_segments=[{'start_seconds': 0, 'end_seconds': 60,
                                'source_url': 'https://example.com/audio', 'note': 'Synthetic loud climax'}])
        row['scene_decisions'] = [{'scene': '放松睡前', 'decision': 'unsuitable',
                                   'reason': 'Synthetic abrupt climax'}]
        filled = self.project / 'filled.json'
        filled.write_text(json.dumps(doc, ensure_ascii=False), encoding='utf-8')
        self.run_cli('save', '--input', str(filled), '--bundle', 'second')
        result = json.loads(self.run_cli('read', '--bundle', 'second').stdout)
        self.assertEqual(result['summary']['removed_count'], 1)
        self.assertEqual(result['summary']['scene_verified_records'], 1)
        self.run_cli('preview', '--bundle', 'second')
        self.assertEqual(json.loads(review.read_text(encoding='utf-8'))['records'][0]
                         ['recording']['status'], 'unresolved')

    def test_stale_source_refuses_read_and_does_not_publish_preview(self):
        self.init()
        self.original += b'\n'
        self.source.write_bytes(self.original)
        self.run_cli('read', '--bundle', 'first', success=False)
        self.run_cli('preview', '--bundle', 'first', success=False)
        self.assertFalse((self.project / 'artifacts/recording-previews/first').exists())

    def test_save_rejects_duplicate_json_fields(self):
        self.init()
        path = self.project / 'bad.json'
        path.write_text('{"kind":"x","kind":"y"}', encoding='utf-8')
        self.run_cli('save', '--input', str(path), '--bundle', 'second', success=False)
        self.assertFalse((self.project / 'artifacts/recording-reviews/second').exists())

    def test_paths_and_empty_selection_are_rejected(self):
        self.run_cli('init', '--positions', '', '--bundle', 'first', success=False)
        self.run_cli('init', '--positions', '1', '--bundle', '../outside', success=False)
        self.run_cli('init', '--positions', '1,1', '--bundle', 'first', success=False)
        self.assertFalse((self.project / 'artifacts/outside').exists())

    def test_preview_directory_replacement_cannot_report_success(self):
        self.init()
        spec = importlib.util.spec_from_file_location('recording_review_cli_race', SCRIPT)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        review = module.read_review(self.project, 'first')
        target = self.project / 'artifacts/recording-previews/first'
        moved = target.with_name('preserved-old-directory')
        original_render = module.render_preview
        def replace_directory(preview):
            # At this boundary the output directory exists. Replace it with
            # another ordinary directory, without symlink permissions.
            target.rename(moved)
            target.mkdir()
            return original_render(preview)
        with patch.object(module, 'render_preview', side_effect=replace_directory):
            with self.assertRaises(ValueError):
                module._publish_preview(self.project, 'first', review, self.original)
        self.assertTrue(moved.exists())
        self.assertEqual(self.source.read_bytes(), self.original)

    def test_preview_content_changed_before_final_readback_cannot_report_success(self):
        self.init()
        spec = importlib.util.spec_from_file_location('recording_review_cli_corruption', SCRIPT)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        review = module.read_review(self.project, 'first')
        target = self.project / 'artifacts/recording-previews/first/preview.json'
        original_load = module.load_source
        calls = 0
        def corrupt_on_final_read(project):
            nonlocal calls
            calls += 1
            if calls == 2:
                target.write_text('{}', encoding='utf-8')
            return original_load(project)
        with patch.object(module, 'load_source', side_effect=corrupt_on_final_read):
            with self.assertRaises(ValueError):
                module._publish_preview(self.project, 'first', review, self.original)


if __name__ == '__main__':
    unittest.main()
