"""Interrupted offline generation and strictly missing-only reconstruction."""
import contextlib
import io
import json
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from netease_organizer.service import Organizer
from netease_organizer.classification_execution import execute_classification
from netease_organizer.write_journal import write_lease
from scripts import build_classification_plan as builder
from tests import test_build_classification_plan as builder_fixtures


PENDING = '分类生成未完成.json'
PLAN = '分类整理计划.json'
REPORT = '全库分类-逐曲结果.json'
OUTPUTS = (REPORT, '全库分类-需复核录音.json', '喜欢的音乐-分类明细.csv',
           '喜欢的音乐-分类清单.md', PLAN)


class ClassificationGenerationTests(unittest.TestCase):
    fixture = builder_fixtures.BuildClassificationPlanTests.fixture

    def setUp(self):
        self.root = self.fixture({'position': 1, 'language': 'en', 'notes': '模拟版本依据'})
        self.artifacts = self.root / 'artifacts'
        self.cli, self.reader = Mock(), Mock()
        self.controller = Organizer(self.root, cli=self.cli, reader=self.reader,
                                    data_dir=self.root / 'cache')
        self.inputs = {p.name: p.read_bytes() for p in self.artifacts.iterdir()}

    def invoke(self, args=()):
        with patch.object(builder, '__file__', str(self.root / 'scripts/build_classification_plan.py')), \
                patch.object(builder, 'Organizer', return_value=self.controller), \
                contextlib.redirect_stdout(io.StringIO()):
            return builder.main(args) if args else builder.main()

    def unchanged_inputs(self):
        self.assertEqual({name: (self.artifacts / name).read_bytes() for name in self.inputs}, self.inputs)
        self.reader.load.assert_not_called()
        self.cli.run_json.assert_not_called()

    def test_each_publish_failure_retains_pending_and_retry_completes(self):
        for failed in (PENDING, *OUTPUTS):
            with self.subTest(failed=failed):
                self.setUp()
                real = self.controller._write_artifact
                calls = []
                def save(name, text, **kwargs):
                    calls.append(name)
                    if name == failed:
                        raise OSError('simulated output failure')
                    return real(name, text, **kwargs)
                with patch.object(self.controller, '_write_artifact', side_effect=save):
                    with self.assertRaises(OSError):
                        self.invoke()
                self.assertEqual(calls[0], PENDING)
                self.assertEqual((self.artifacts / PENDING).exists(), failed != PENDING)
                if failed == PENDING:
                    self.assertFalse(any((self.artifacts / name).exists() for name in OUTPUTS))
                else:
                    self.assertFalse((self.artifacts / PLAN).exists())
                self.invoke()
                self.assertFalse((self.artifacts / PENDING).exists())
                self.assertTrue(all((self.artifacts / name).is_file() for name in OUTPUTS))
                self.unchanged_inputs()

    def test_render_failure_precedes_any_publication(self):
        with patch.object(builder.csv, 'writer', side_effect=ValueError('render failed')):
            with self.assertRaises(ValueError):
                self.invoke()
        self.assertEqual({p.name: p.read_bytes() for p in self.artifacts.iterdir()}, self.inputs)

    def test_shared_account_lease_blocks_before_reading_inputs(self):
        with write_lease(self.root / '.organizer/account-write.lock'), \
                patch.object(Path, 'read_text', side_effect=AssertionError('inputs read while busy')):
            with self.assertRaises(RuntimeError):
                self.invoke()
        self.assertEqual({p.name: p.read_bytes() for p in self.artifacts.iterdir()}, self.inputs)

    def test_failed_replacement_keeps_old_plan_but_blocks_its_execution(self):
        self.invoke()
        plan_path = self.artifacts / PLAN
        old_bytes = plan_path.read_bytes()
        old_plan = json.loads(old_bytes)
        source = self.artifacts / '分类语言核验01.json'
        source.write_text(json.dumps([{'position': 1, 'language': 'ja', 'notes': '新的模拟依据'}]),
                          encoding='utf-8')
        real = self.controller._write_artifact
        def fail_csv(name, text, **kwargs):
            if name.endswith('.csv'):
                raise OSError('simulated replacement failure')
            return real(name, text, **kwargs)
        with patch.object(self.controller, '_write_artifact', side_effect=fail_csv):
            with self.assertRaises(OSError):
                self.invoke()
        self.assertEqual(plan_path.read_bytes(), old_bytes)
        result = execute_classification(self.controller, old_plan)
        self.assertEqual(result['status'], 'blocked')
        self.assertFalse(result['write_attempted'])
        self.reader.load.assert_not_called()
        self.cli.run_json.assert_not_called()
        self.invoke()
        self.assertFalse((self.artifacts / PENDING).exists())
        self.assertNotEqual(json.loads(plan_path.read_bytes()), old_plan)

    def test_existing_execution_record_blocks_normal_rebuild_before_input_read(self):
        (self.artifacts / '分类整理执行进度.json').write_text('{}', encoding='utf-8')
        before = {p.name: p.read_bytes() for p in self.artifacts.iterdir()}
        with patch.object(Path, 'read_text', side_effect=AssertionError('read before execution gate')):
            with self.assertRaises(ValueError):
                self.invoke()
        self.assertEqual({p.name: p.read_bytes() for p in self.artifacts.iterdir()}, before)

    def test_repair_missing_preserves_existing_bytes_and_is_idempotent(self):
        self.invoke()
        plan = self.artifacts / PLAN
        plan.write_text(json.dumps(json.loads(plan.read_text(encoding='utf-8'))), encoding='utf-8')
        kept = {name: (self.artifacts / name).read_bytes() for name in (PLAN, REPORT)}
        for name in OUTPUTS:
            if name not in kept:
                (self.artifacts / name).unlink()
        self.invoke(['--repair-missing'])
        self.assertEqual({name: (self.artifacts / name).read_bytes() for name in kept}, kept)
        before = {name: (self.artifacts / name).read_bytes() for name in OUTPUTS}
        with patch.object(self.controller, '_write_artifact', side_effect=AssertionError('noop rewrote output')):
            self.invoke(['--repair-missing'])
        self.assertEqual({name: (self.artifacts / name).read_bytes() for name in OUTPUTS}, before)
        self.unchanged_inputs()

    def test_marker_removal_failure_can_be_repaired_without_output_rewrites(self):
        real_unlink = Path.unlink
        def unlink(path, *args, **kwargs):
            if path.name == PENDING:
                raise OSError('simulated marker removal failure')
            return real_unlink(path, *args, **kwargs)
        with patch.object(Path, 'unlink', unlink):
            with self.assertRaises(OSError):
                self.invoke()
        self.assertTrue((self.artifacts / PENDING).exists())
        before = {name: (self.artifacts / name).read_bytes() for name in OUTPUTS}
        with patch.object(self.controller, '_write_artifact', side_effect=AssertionError('existing outputs rewritten')):
            self.invoke(['--repair-missing'])
        self.assertFalse((self.artifacts / PENDING).exists())
        self.assertEqual({name: (self.artifacts / name).read_bytes() for name in OUTPUTS}, before)

    def test_repair_mismatched_evidence_or_plan_refuses_before_any_write(self):
        for field in ('note', 'plan', 'timestamp', 'boolean_position', 'csv'):
            with self.subTest(field=field):
                self.setUp()
                self.invoke()
                if field == 'note':
                    path = self.artifacts / '分类判断-分块01.json'
                    rows = json.loads(path.read_text(encoding='utf-8'))
                    rows[0][5] = '变更的依据，成员范围相同'
                    path.write_text(json.dumps(rows), encoding='utf-8')
                elif field == 'plan':
                    path = self.artifacts / PLAN
                    plan = json.loads(path.read_text(encoding='utf-8'))
                    plan['account_original_id'] = '9'
                    path.write_text(json.dumps(plan), encoding='utf-8')
                elif field in ('timestamp', 'boolean_position'):
                    path = self.artifacts / REPORT
                    report = json.loads(path.read_text(encoding='utf-8'))
                    if field == 'timestamp':
                        report['created_at'] = 'invalid timestamp'
                    else:
                        report['records'][0]['position'] = True
                    path.write_text(json.dumps(report), encoding='utf-8')
                else:
                    (self.artifacts / '喜欢的音乐-分类明细.csv').write_text('wrong export', encoding='utf-8')
                (self.artifacts / '喜欢的音乐-分类清单.md').unlink()
                before = {p.name: p.read_bytes() for p in self.artifacts.iterdir()}
                with patch.object(self.controller, '_write_artifact', side_effect=AssertionError('write before validation')):
                    with self.assertRaises((ValueError, RuntimeError)):
                        self.invoke(['--repair-missing'])
                self.assertEqual({p.name: p.read_bytes() for p in self.artifacts.iterdir()}, before)

    def test_unknown_or_orphan_execution_evidence_cannot_be_repaired(self):
        self.invoke()
        (self.artifacts / REPORT).unlink()
        (self.artifacts / '分类整理执行进度.json').write_text('{}', encoding='utf-8')
        before = {p.name: p.read_bytes() for p in self.artifacts.iterdir()}
        with self.assertRaises((ValueError, RuntimeError)):
            self.invoke(['--repair-missing'])
        self.assertEqual({p.name: p.read_bytes() for p in self.artifacts.iterdir()}, before)


if __name__ == '__main__':
    unittest.main()
