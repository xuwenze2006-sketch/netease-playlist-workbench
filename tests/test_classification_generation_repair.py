"""Offline acceptance of missing-only export recovery after saved execution."""

import contextlib
import csv
from datetime import datetime, timezone
import errno
import io
import json
import os
from pathlib import Path
import socket
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from netease_organizer import service
from netease_organizer.classification_execution import (
    classification_pending, execute_classification,
)
from netease_organizer.web_classification import (
    build_local_classification, save_local_classification_draft,
)
from scripts import build_classification_plan as builder
from tests import test_classification_execution as execution_fixtures
from tests.test_create import OWNER, OWNER_ENC


PLAN = '分类整理计划.json'
INTENT = '分类整理执行进度.json'
RECEIPT = '分类整理执行结果.json'
PENDING = '分类生成未完成.json'
REPORT = '全库分类-逐曲结果.json'
CSV = '喜欢的音乐-分类明细.csv'
AUXILIARY = (REPORT, '全库分类-需复核录音.json', CSV, '喜欢的音乐-分类清单.md')
EVIDENCE = (PLAN, INTENT, RECEIPT)


class ClassificationGenerationRepairTests(unittest.TestCase):
    def setUp(self):
        # The provider below is an in-memory account, never the installed CLI.
        for module, name in ((socket, 'create_connection'), (subprocess, 'Popen')):
            guard = patch.object(module, name, side_effect=AssertionError('offline acceptance forbids external I/O'))
            guard.start()
            self.addCleanup(guard.stop)

    def fixture(self, *, language='en'):
        temporary = tempfile.TemporaryDirectory(prefix='classification-repair-acceptance-')
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name).resolve()
        artifacts = root / 'artifacts'
        artifacts.mkdir()
        cli = execution_fixtures.ClassificationCli(count=1)
        controller = service.Organizer(root, cli=cli, reader=execution_fixtures.LocalReader(),
                                       data_dir=root / 'fake-desktop')
        liked = {'id': cli.favorite_id,
                 'original_id': str(cli.created[cli.favorite_id]['originalId']),
                 'name': '离线红心', 'track_count': 1, 'special_type': 5,
                 'membership_complete': True,
                 'tracks': [{'id': cli.source_ids[0], 'original_id': '1', 'name': '模拟录音',
                             'artists': [{'name': '模拟艺人'}], 'metadata_available': True}]}
        account = {'original_id': OWNER, 'id': OWNER_ENC, 'nickname': '离线用户'}
        inputs = {
            '全库分类-账号基线.json': {'snapshot': {'account': account, 'liked': liked}},
            '分类判断-分块01.json': [[1, ['pop'], ['commute'], None, .9, '模拟录音风格依据']],
            '分类判断-分块02.json': [], '分类判断-分块03.json': [],
            '全库分类-歌词特征.json': {'tracks': {'1': {
                'status': 'ok', 'has_original_lyric_text': True, 'original_line_count': 54,
                'script_counts': {'han': 391}, 'language_hints': ['zh_or_yue']}}},
            '网易云本地歌单快照.json': {'tracks': {}},
            '分类语言核验01.json': [{'position': 1, 'language': language, 'review_note': True,
                                'notes': '人工保留的版本依据'}],
            '在线名称整理快照.json': {'account': account, 'liked': liked, 'playlists': [liked]},
        }
        for name, value in inputs.items():
            (artifacts / name).write_text(json.dumps(value, ensure_ascii=False), encoding='utf-8')
        return SimpleNamespace(root=root, artifacts=artifacts, cli=cli, controller=controller,
                               inputs=self.bytes_in(artifacts), account=account, liked=liked)

    @staticmethod
    def bytes_in(folder, names=None):
        return {name: (folder / name).read_bytes() for name in names} if names is not None else {
            path.name: path.read_bytes() for path in folder.iterdir() if path.is_file()}

    def invoke(self, fixture, *, repair=True):
        output = io.StringIO()
        with patch.object(builder, '__file__', str(fixture.root / 'scripts/build_classification_plan.py')), \
                patch.object(builder, 'Organizer', return_value=fixture.controller), \
                patch.object(fixture.cli, 'run_json', side_effect=AssertionError('repair accessed account')), \
                patch.object(fixture.controller.reader, 'load', side_effect=AssertionError('repair read account')), \
                contextlib.redirect_stdout(output):
            builder.main(['--repair-missing'] if repair else [])
        return json.loads(output.getvalue())

    def seed_legacy_execution(self, fixture, *, paused=False, full_exports=False):
        if full_exports:
            self.invoke(fixture, repair=False)
        else:
            # Render the legacy approved plan without publishing a pending marker or exports.
            plan, _, outputs = builder.build_exports(fixture.root)
            text = next(text for name, text, _ in outputs if name == PLAN)
            fixture.controller._write_artifact(PLAN, text)
            self.assertFalse(any((fixture.artifacts / name).exists() for name in AUXILIARY))
        plan = json.loads((fixture.artifacts / PLAN).read_text(encoding='utf-8'))
        if paused:
            def pause_after_inflight_add(arguments):
                if arguments[:2] == ['playlist', 'add']:
                    fixture.controller.request_pause()
            fixture.cli.on_write = pause_after_inflight_add
        result = execute_classification(fixture.controller, plan, accept_default_visibility=True)
        self.assertEqual(result['status'], 'paused' if paused else 'completed')
        self.assertEqual(result['completed_count'], 1 if paused else 3)
        self.assertTrue(result['record_saved'])
        self.assertTrue(result['outcome_known'])
        self.assertFalse(classification_pending(fixture.controller))
        self.assertFalse((fixture.artifacts / PENDING).exists())
        return result

    def assert_preserved(self, fixture, evidence):
        self.assertEqual(self.bytes_in(fixture.artifacts, evidence), evidence)
        self.assertEqual(self.bytes_in(fixture.artifacts, fixture.inputs), fixture.inputs)
        self.assertEqual(list(fixture.artifacts.glob('.organizer-*.tmp')), [])

    def test_historical_plan_only_completed_execution_reconstructs_four_exports_without_replay(self):
        fixture = self.fixture()
        self.seed_legacy_execution(fixture)
        evidence = self.bytes_in(fixture.artifacts, EVIDENCE)
        calls, writes = list(fixture.cli.calls), list(fixture.cli.writes)
        summary = self.invoke(fixture)
        self.assertEqual(summary['mode'], 'repair_missing')
        self.assertEqual(summary['reconstructed_files'], list(AUXILIARY))
        self.assert_preserved(fixture, evidence)
        self.assertEqual(fixture.cli.calls, calls)
        self.assertEqual(fixture.cli.writes, writes)
        report = json.loads((fixture.artifacts / REPORT).read_text(encoding='utf-8'))
        self.assertEqual(report['records'][0]['name'], '模拟录音')
        self.assertEqual(report['records'][0]['language'], '英语')
        with (fixture.artifacts / CSV).open(encoding='utf-8-sig', newline='') as stream:
            rows = list(csv.reader(stream))
        self.assertEqual(rows[1][:3], ['1', '模拟录音', '模拟艺人'])
        view = build_local_classification(fixture.root)
        self.assertEqual((view['status'], view['verification']), ('available', 'local_only'))
        all_bytes = self.bytes_in(fixture.artifacts)
        with patch.object(fixture.controller, '_write_artifact', side_effect=AssertionError('no-op rewrote output')):
            self.assertEqual(self.invoke(fixture)['reconstructed_files'], [])
        self.assertEqual(self.bytes_in(fixture.artifacts), all_bytes)

    def test_known_paused_execution_can_reconstruct_without_continuing_account_batch(self):
        fixture = self.fixture()
        self.seed_legacy_execution(fixture, paused=True)
        evidence = self.bytes_in(fixture.artifacts, EVIDENCE)
        calls, writes = list(fixture.cli.calls), list(fixture.cli.writes)
        self.assertEqual(self.invoke(fixture)['reconstructed_files'], list(AUXILIARY))
        self.assert_preserved(fixture, evidence)
        self.assertEqual(fixture.cli.calls, calls)
        self.assertEqual(fixture.cli.writes, writes)
        self.assertFalse(classification_pending(fixture.controller))

    def test_each_auxiliary_atomic_replace_failure_leaves_pending_and_retry_preserves_evidence(self):
        for index, failed in enumerate(AUXILIARY):
            with self.subTest(failed=failed):
                fixture = self.fixture()
                self.seed_legacy_execution(fixture)
                evidence = self.bytes_in(fixture.artifacts, EVIDENCE)
                real_replace = service.os.replace
                attempts = []
                def replace(source, target):
                    self.assertTrue(Path(target).resolve().is_relative_to(fixture.root))
                    attempts.append(Path(target).name)
                    if Path(target).name == failed:
                        raise OSError(errno.ENOSPC, 'injected local export save failure')
                    return real_replace(source, target)
                with patch.object(service.os, 'replace', side_effect=replace):
                    with self.assertRaises(OSError) as failure:
                        self.invoke(fixture)
                self.assertEqual(failure.exception.errno, errno.ENOSPC)
                self.assertEqual(attempts, [PENDING, *AUXILIARY[:index + 1]])
                self.assertTrue((fixture.artifacts / PENDING).is_file())
                self.assertEqual([name for name in AUXILIARY if (fixture.artifacts / name).exists()],
                                 list(AUXILIARY[:index]))
                self.assertFalse(classification_pending(fixture.controller))
                self.assertEqual(build_local_classification(fixture.root)['status'], 'unavailable')
                self.assert_preserved(fixture, evidence)
                retained = self.bytes_in(fixture.artifacts, AUXILIARY[:index])
                self.assertEqual(self.invoke(fixture)['reconstructed_files'], list(AUXILIARY[index:]))
                self.assertFalse((fixture.artifacts / PENDING).exists())
                self.assertEqual(self.bytes_in(fixture.artifacts, retained), retained)
                self.assert_preserved(fixture, evidence)

    def test_changed_candidate_is_refused_before_any_repair_write(self):
        fixture = self.fixture()
        self.seed_legacy_execution(fixture)
        path = fixture.artifacts / '分类语言核验01.json'
        review = json.loads(path.read_text(encoding='utf-8'))
        review[0]['language'] = 'ja'
        path.write_text(json.dumps(review), encoding='utf-8')
        self.assert_refused_without_write(fixture)

    def test_valid_known_execution_bound_to_another_plan_is_refused_before_any_repair_write(self):
        fixture, other = self.fixture(), self.fixture(language='ja')
        self.seed_legacy_execution(fixture)
        self.seed_legacy_execution(other)
        for name in (INTENT, RECEIPT):
            (fixture.artifacts / name).write_bytes((other.artifacts / name).read_bytes())
        # The copied pair is internally valid and known, but does not bind the saved English plan.
        self.assertFalse(classification_pending(fixture.controller))
        self.assert_refused_without_write(fixture)

    def test_mismatched_receipt_binding_or_unknown_outcome_is_refused_before_any_repair_write(self):
        for field, value in (('run_id', 'f' * 32), ('outcome_known', False)):
            with self.subTest(field=field):
                fixture = self.fixture()
                self.seed_legacy_execution(fixture)
                path = fixture.artifacts / RECEIPT
                receipt = json.loads(path.read_text(encoding='utf-8'))
                receipt[field] = value
                path.write_text(json.dumps(receipt), encoding='utf-8')
                self.assertTrue(classification_pending(fixture.controller))
                self.assert_refused_without_write(fixture)

    def assert_refused_without_write(self, fixture):
        before = self.bytes_in(fixture.artifacts)
        with patch.object(fixture.controller, '_write_artifact', side_effect=AssertionError('write before validation')):
            with self.assertRaises((ValueError, RuntimeError)):
                self.invoke(fixture)
        self.assertEqual(self.bytes_in(fixture.artifacts), before)
        self.assertFalse((fixture.artifacts / PENDING).exists())

    def save_draft(self, fixture):
        view = build_local_classification(fixture.root)
        self.assertEqual(view['status'], 'available')
        request = {'action': 'save', 'source_version': view['quality']['source_version'],
                   'revision': view['quality']['revision'], 'record_key': view['records'][0]['record_key'],
                   'styles': ['摇滚与独立'], 'scenes': ['运动提神'], 'language': '英语',
                   'reason': '本地保存的具体录音复核依据', 'recording_note': '专辑录音版本'}
        self.assertTrue(save_local_classification_draft(fixture.root, request)['accepted'])
        return build_local_classification(fixture.root)

    def test_missing_csv_repair_keeps_existing_report_and_saved_draft_source_identity(self):
        fixture = self.fixture()
        self.seed_legacy_execution(fixture, full_exports=True)
        before_view = self.save_draft(fixture)
        draft_path = fixture.root / '.organizer/classification-draft.json'
        draft_bytes = draft_path.read_bytes()
        evidence = self.bytes_in(fixture.artifacts, (*EVIDENCE, REPORT))
        (fixture.artifacts / CSV).unlink()
        self.assertEqual(self.invoke(fixture)['reconstructed_files'], [CSV])
        self.assert_preserved(fixture, evidence)
        self.assertEqual(draft_path.read_bytes(), draft_bytes)
        after_view = build_local_classification(fixture.root)
        self.assertEqual(after_view['quality']['source_version'], before_view['quality']['source_version'])
        self.assertEqual(after_view['quality']['draft_status'], 'ready')
        self.assertEqual(after_view['quality']['revision'], 1)
        self.assertEqual(after_view['quality']['changed_count'], 1)
        self.assertEqual(after_view['records'][0]['draft'], before_view['records'][0]['draft'])

    def test_missing_report_reconstruction_downgrades_verified_chain_and_preserves_stale_draft(self):
        fixture = self.fixture()
        result = self.seed_legacy_execution(fixture, full_exports=True)
        self.make_verified_history(fixture, result)
        before_view = self.save_draft(fixture)
        self.assertEqual(before_view['verification'], 'verified')
        draft_path = fixture.root / '.organizer/classification-draft.json'
        draft_bytes = draft_path.read_bytes()
        evidence = self.bytes_in(fixture.artifacts, (*EVIDENCE, '分类整理最终核验.json'))
        receipt_time = (fixture.artifacts / RECEIPT).stat().st_mtime
        (fixture.artifacts / REPORT).unlink()
        self.assertEqual(self.invoke(fixture)['reconstructed_files'], [REPORT])
        self.assertGreater((fixture.artifacts / REPORT).stat().st_mtime, receipt_time)
        self.assert_preserved(fixture, evidence)
        self.assertEqual(draft_path.read_bytes(), draft_bytes)
        after_view = build_local_classification(fixture.root)
        self.assertEqual((after_view['status'], after_view['verification']), ('available', 'local_only'))
        self.assertIsNone(after_view['verified_at'])
        self.assertNotEqual(after_view['quality']['source_version'], before_view['quality']['source_version'])
        self.assertEqual(after_view['quality']['draft_status'], 'stale')
        self.assertIsNone(after_view['records'][0]['draft'])

    @staticmethod
    def make_verified_history(fixture, result):
        """A dated, internally consistent offline history, verified before deleting its report."""
        base = 946684800  # 2000-01-01 UTC, deliberately older than reconstruction.
        def save(name, value, timestamp):
            path = fixture.artifacts / name
            path.write_text(json.dumps(value, ensure_ascii=False), encoding='utf-8')
            os.utime(path, (timestamp, timestamp))
        report = json.loads((fixture.artifacts / REPORT).read_text(encoding='utf-8'))
        report['created_at'] = '2000-01-01T00:00:00+00:00'
        save(REPORT, report, base + 1)
        for name, offset in ((INTENT, 2), (RECEIPT, 3)):
            os.utime(fixture.artifacts / name, (base + offset, base + offset))
        plan = json.loads((fixture.artifacts / PLAN).read_text(encoding='utf-8'))
        playlists = [{'id': item['playlist_id'], 'original_id': item['original_playlist_id'],
                      'name': job['name'], 'track_count': 1, 'special_type': 0}
                     for job, item in zip(plan['jobs'], result['items'])]
        final = {'kind': 'classification_final_readback', 'status': 'verified',
                 'verified_at': datetime.fromtimestamp(base + 4, timezone.utc).isoformat(),
                 'source_count': 1, 'covered_count': 1, 'playlist_count': 3, 'pending_count': 0,
                 'unknown_style_count': 0, 'unknown_language_count': 0,
                 'original_favorites_membership_and_order_unchanged': True,
                 'playlists': [{'name': job['name'], 'dimension': job['dimension'], 'count': 1,
                                'original_playlist_id': item['original_playlist_id'], 'track_update_time': 20}
                               for job, item in zip(plan['jobs'], result['items'])]}
        save('分类整理最终核验.json', final, base + 4)
        save('在线名称整理快照.json', {'account': fixture.account, 'liked': fixture.liked,
                                 'playlists': [fixture.liked, *playlists]}, base + 5)
        fixture.inputs['在线名称整理快照.json'] = (fixture.artifacts / '在线名称整理快照.json').read_bytes()


if __name__ == '__main__':
    unittest.main()
