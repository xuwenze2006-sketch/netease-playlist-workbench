"""Synthetic CLI guards for incomplete local classification generations."""

import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from netease_organizer.service import Organizer
from netease_organizer.write_journal import WriteLeaseBusy, write_lease
from scripts import execute_classification as execute_script
from scripts import verify_classification_account as verify_script


class ClassificationGenerationScriptTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='classification-script-guard-')
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.artifacts = self.root / 'artifacts'
        self.artifacts.mkdir()
        self.pending = self.artifacts / '分类生成未完成.json'
        self.plan_path = self.artifacts / '分类整理计划.json'
        self.plan = {
            'kind': 'approved_classification_plan', 'version': 1,
            'account_original_id': '1', 'account_id': 'A' * 32,
            'source_playlist_id': 'B' * 32, 'original_source_playlist_id': '2',
            'source_track_count': 1, 'source_track_ids': ['C' * 32],
            'jobs': [{'kind': 'create_classification_playlist', 'name': '合成分类',
                      'dimension': 'style', 'candidate_track_ids': ['C' * 32],
                      'intended_visibility': 'provider_default'}],
        }

    def invoke_execute(self, arguments=()):
        with patch.object(execute_script, '__file__', str(self.root / 'scripts/execute_classification.py')), \
                patch('sys.argv', ['execute_classification.py', *arguments]), \
                contextlib.redirect_stdout(io.StringIO()) as output:
            execute_script.main()
        return output.getvalue()

    def invoke_verify(self):
        with patch.object(verify_script, '__file__', str(self.root / 'scripts/verify_classification_account.py')), \
                contextlib.redirect_stdout(io.StringIO()):
            verify_script.main()

    def offline_controller(self):
        # Supplying every dependency avoids real CLI/cache initialization or calls.
        return Organizer(self.root, cli=object(), reader=object(), data_dir=self.root / 'desktop')

    def test_execute_pending_blocks_preview_execute_and_resume_before_plan_or_controller(self):
        self.pending.write_text('malformed private marker', encoding='utf-8')
        for arguments in ((), ('--execute-approved',), ('--resume-verified',)):
            with self.subTest(arguments=arguments), \
                    patch.object(Path, 'read_text', side_effect=AssertionError('plan read before pending guard')), \
                    patch.object(execute_script, 'Organizer', side_effect=AssertionError('controller constructed while pending')):
                with self.assertRaises(ValueError):
                    self.invoke_execute(arguments)
        self.assertEqual(self.pending.read_bytes(), b'malformed private marker')

    def test_execute_directory_marker_blocks_before_plan_read(self):
        self.pending.mkdir()
        with patch.object(Path, 'read_text', side_effect=AssertionError('directory marker was ignored')), \
                patch.object(execute_script, 'Organizer', side_effect=AssertionError('controller constructed while pending')):
            with self.assertRaises(ValueError):
                self.invoke_execute()
        self.assertTrue(self.pending.is_dir())

    def test_execute_dangling_symlink_marker_blocks_before_plan_read(self):
        try:
            self.pending.symlink_to(self.artifacts / 'absent-marker-target')
        except OSError as error:
            self.skipTest(f'symlink creation unavailable: {error}')
        with patch.object(Path, 'read_text', side_effect=AssertionError('dangling marker was ignored')):
            with self.assertRaises(ValueError):
                self.invoke_execute()
        self.assertTrue(self.pending.is_symlink())

    def test_execute_rechecks_pending_after_plan_read_before_preview_or_controller(self):
        self.plan_path.write_text(json.dumps(self.plan), encoding='utf-8')
        original_read = Path.read_text

        def read_then_interrupt(path, *args, **kwargs):
            text = original_read(path, *args, **kwargs)
            if path == self.plan_path:
                self.pending.write_text('{}', encoding='utf-8')
            return text

        for arguments in ((), ('--execute-approved',), ('--resume-verified',)):
            self.pending.unlink(missing_ok=True)
            with self.subTest(arguments=arguments), \
                    patch.object(Path, 'read_text', read_then_interrupt), \
                    patch.object(execute_script, 'Organizer', side_effect=AssertionError('controller constructed after interruption')):
                with self.assertRaises(ValueError):
                    self.invoke_execute(arguments)

    def test_execute_default_preview_retains_zero_controller_or_account_initialization(self):
        self.plan_path.write_text(json.dumps(self.plan), encoding='utf-8')
        with patch.object(execute_script, 'Organizer', side_effect=AssertionError('preview constructed controller')):
            result = json.loads(self.invoke_execute())
        self.assertEqual(result, {'status': 'preview', 'jobs': 1, 'source_count': 1, 'account_changed': False})
        self.assertFalse((self.root / '.organizer').exists())

    def test_verify_pending_blocks_before_controller_or_plan_read(self):
        self.pending.write_text('private malformed marker', encoding='utf-8')
        with patch.object(verify_script, 'Organizer', side_effect=AssertionError('verifier constructed controller while pending')), \
                patch.object(Path, 'read_text', side_effect=AssertionError('verifier read plan while pending')):
            with self.assertRaises(ValueError):
                self.invoke_verify()
        self.assertEqual(self.pending.read_bytes(), b'private malformed marker')

    def test_verify_holds_shared_account_lease_through_readback_and_publication(self):
        controller = self.offline_controller()
        stages = []

        def verify_under_lease(root, owner):
            self.assertEqual(root, self.root)
            self.assertIs(owner, controller)
            for stage in ('readback', 'publication'):
                with self.assertRaises(WriteLeaseBusy):
                    with write_lease(self.root / '.organizer/account-write.lock'):
                        self.fail('generation acquired lease during verification')
                if stage == 'publication':
                    owner._write_artifact('synthetic-final-output.json', '{"verified":true}')
                stages.append(stage)

        with patch.object(verify_script, 'Organizer', return_value=controller), \
                patch.object(verify_script, '_verify', side_effect=verify_under_lease, create=True), \
                patch.object(Path, 'read_text', side_effect=AssertionError('readback bypassed locked verification helper')):
            self.invoke_verify()
        self.assertEqual(stages, ['readback', 'publication'])
        self.assertEqual(json.loads((self.artifacts / 'synthetic-final-output.json').read_text(encoding='utf-8')),
                         {'verified': True})
        with write_lease(self.root / '.organizer/account-write.lock'):
            pass

    def test_verify_rechecks_pending_inside_lease_after_controller_initialization(self):
        controller = self.offline_controller()

        def construct_then_interrupt(root):
            self.assertEqual(root, self.root)
            self.pending.write_text('{}', encoding='utf-8')
            return controller

        with patch.object(verify_script, 'Organizer', side_effect=construct_then_interrupt), \
                patch.object(verify_script, '_verify', side_effect=AssertionError('readback started after interruption'), create=True), \
                patch.object(Path, 'read_text', side_effect=AssertionError('plan read after interruption')):
            with self.assertRaises(ValueError):
                self.invoke_verify()
        self.assertTrue(self.pending.exists())
        with write_lease(self.root / '.organizer/account-write.lock'):
            pass

    def test_verify_failure_releases_shared_lease(self):
        controller = self.offline_controller()

        def fail_readback(root, owner):
            with self.assertRaises(WriteLeaseBusy):
                with write_lease(self.root / '.organizer/account-write.lock'):
                    self.fail('verification did not hold lease')
            raise ValueError('synthetic readback failure')

        with patch.object(verify_script, 'Organizer', return_value=controller), \
                patch.object(verify_script, '_verify', side_effect=fail_readback, create=True), \
                patch.object(Path, 'read_text', side_effect=AssertionError('readback bypassed locked verification helper')):
            with self.assertRaisesRegex(ValueError, 'synthetic readback failure'):
                self.invoke_verify()
        with write_lease(self.root / '.organizer/account-write.lock'):
            pass


if __name__ == '__main__':
    unittest.main()
