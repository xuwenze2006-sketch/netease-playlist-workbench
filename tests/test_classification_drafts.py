import copy
import importlib
import json
import os
import queue
import stat
import subprocess
import sys
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch


class ClassificationDraftTests(unittest.TestCase):
    def setUp(self):
        try:
            self.drafts = importlib.import_module('netease_organizer.classification_drafts')
        except ModuleNotFoundError as error:
            if error.name == 'netease_organizer.classification_drafts':
                self.fail('The isolated classification draft store has not been implemented.')
            raise
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.project = Path(self.temp.name).resolve()
        self.version = 'a' * 64
        self.key = '1' * 32
        self.records = [
            {'record_key': self.key, 'styles': ['流行抒情'], 'scenes': ['通勤散步'], 'language': '英语'},
            {'record_key': '2' * 32, 'styles': ['待辨识'], 'scenes': [], 'language': '待辨识'},
        ]
        self.path = self.project / '.organizer' / 'classification-draft.json'

    def request(self, **changes):
        request = {'action': 'save', 'source_version': self.version, 'revision': 0,
                   'record_key': self.key, 'styles': ['摇滚与独立'], 'scenes': ['运动提神'],
                   'language': '英语', 'reason': '核对了具体录音的风格和场景适配。',
                   'recording_note': '专辑录音版本'}
        request.update(changes)
        return request

    def save(self, **changes):
        return self.drafts.mutate_draft(self.project, self.version, self.records, self.request(**changes))

    def load(self, version=None, records=None):
        return self.drafts.load_draft(self.project, version or self.version,
                                      self.records if records is None else records)

    def write_raw(self, value):
        self.path.parent.mkdir(exist_ok=True)
        self.path.write_text(value if type(value) is str else json.dumps(value, ensure_ascii=False),
                             encoding='utf-8')

    def test_empty_read_does_not_create_directory(self):
        self.assertEqual(self.load(), {'status': 'ready', 'revision': 0, 'changed_count': 0, 'edits': {}})
        self.assertFalse(self.path.parent.exists())

    def test_save_restart_replace_and_remove_preserve_baseline_and_revision(self):
        original = copy.deepcopy(self.records)
        source = self.project / 'artifacts' / 'source.json'
        source.parent.mkdir()
        source.write_bytes(b'PERSONAL-ORIGINAL')
        result = self.save()
        edit = result['edits'][self.key]
        self.assertEqual(result['status'], 'ready')
        self.assertEqual((result['revision'], result['changed_count']), (1, 1))
        self.assertEqual(edit['before'], {field: original[0][field] for field in ('styles', 'scenes', 'language')})
        self.assertEqual(edit['after']['styles'], ['摇滚与独立'])
        self.assertEqual(edit['reason'], self.request()['reason'])
        self.assertEqual(edit['recording_note'], '专辑录音版本')
        restarted = subprocess.run(
            [sys.executable, '-c',
             'import json,sys; from netease_organizer.classification_drafts import load_draft; '
             'print(json.dumps(load_draft(sys.argv[1],sys.argv[2],json.loads(sys.argv[3]))))',
             str(self.project), self.version, json.dumps(self.records)],
            capture_output=True, text=True, check=True,
        )
        self.assertEqual(json.loads(restarted.stdout), result)
        replaced = self.save(revision=1, styles=['爵士与 Lo-Fi'], scenes=[], reason='修正此前判断。')
        self.assertEqual(replaced['edits'][self.key]['before'], edit['before'])
        self.assertEqual(replaced['edits'][self.key]['after']['styles'], ['爵士与 Lo-Fi'])
        self.assertEqual(replaced['revision'], 2)
        removed = self.drafts.mutate_draft(self.project, self.version, self.records,
                                          {'action': 'remove', 'source_version': self.version,
                                           'revision': 2, 'record_key': self.key})
        self.assertEqual(removed, {'status': 'ready', 'revision': 3, 'changed_count': 0, 'edits': {}})
        self.assertEqual(self.load(), removed)
        self.assertTrue(self.path.is_file())
        self.assertEqual(self.records, original)
        self.assertEqual(source.read_bytes(), b'PERSONAL-ORIGINAL')

    def test_same_labels_can_store_additional_evidence(self):
        result = self.save(styles=['流行抒情'], scenes=['通勤散步'], recording_note='')
        self.assertEqual(result['changed_count'], 1)
        self.assertEqual(result['edits'][self.key]['before'], result['edits'][self.key]['after'])

    def test_returned_edits_cannot_mutate_disk_or_input_records(self):
        result = self.save()
        result['edits'][self.key]['before']['styles'].append('爵士与 Lo-Fi')
        self.assertEqual(self.load()['edits'][self.key]['before']['styles'], ['流行抒情'])
        self.assertEqual(self.records[0]['styles'], ['流行抒情'])

    def test_source_and_revision_conflicts_leave_file_unchanged(self):
        self.save()
        before = self.path.read_bytes()
        for changes in ({'revision': 0}, {'source_version': 'b' * 64, 'revision': 1}):
            with self.subTest(changes=changes), self.assertRaises(self.drafts.DraftConflict):
                self.save(**changes)
            self.assertEqual(self.path.read_bytes(), before)
        stale = self.load(version='b' * 64)
        self.assertEqual((stale['status'], stale['revision'], stale['changed_count'], stale['edits']),
                         ('stale', 1, 1, {}))
        with self.assertRaises(self.drafts.DraftConflict):
            self.drafts.mutate_draft(self.project, 'b' * 64, self.records,
                                     self.request(source_version='b' * 64, revision=1))
        self.assertEqual(self.path.read_bytes(), before)

    def test_concurrent_compare_and_swap_only_commits_one_request(self):
        barrier = threading.Barrier(2)

        def mutate(style):
            barrier.wait(timeout=5)
            try:
                return self.save(styles=[style])['revision']
            except self.drafts.DraftConflict:
                return 'conflict'

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(mutate, ('摇滚与独立', '民谣与木吉他')))
        self.assertCountEqual(results, [1, 'conflict'])
        self.assertEqual(self.load()['revision'], 1)

    def test_two_processes_same_revision_cannot_both_commit_different_records(self):
        # Pause after reading and immediately before replace, so an unprotected
        # implementation deterministically lets both processes pass the CAS.
        self.save()
        previous = self.load()['edits'][self.key]
        worker = '''
import json,sys
from unittest.mock import patch
from netease_organizer import classification_drafts as drafts
data=json.loads(sys.argv[1])
original_read=drafts._read
original_replace=drafts.os.replace
def paused_read(*args,**kwargs):
    result=original_read(*args,**kwargs)
    print('read',flush=True)
    if sys.stdin.readline().strip()!='continue': sys.exit(2)
    return result
def paused_replace(*args,**kwargs):
    print('replace',flush=True)
    if sys.stdin.readline().strip()!='continue': sys.exit(2)
    return original_replace(*args,**kwargs)
try:
    with patch.object(drafts,'_read',paused_read), patch.object(drafts.os,'replace',paused_replace):
        result=drafts.mutate_draft(data['project'],data['source_version'],data['records'],data['request'])
    print(json.dumps({'status':'ready','revision':result['revision']}),flush=True)
except drafts.DraftConflict:
    print(json.dumps({'status':'conflict'}),flush=True)
except drafts.DraftError as error:
    print(json.dumps({'status':'error','code':error.code}),flush=True)
'''

        def launch(key, style):
            data = {'project': str(self.project), 'source_version': self.version, 'records': self.records,
                    'request': self.request(revision=1, record_key=key, styles=[style])}
            process = subprocess.Popen([sys.executable, '-c', worker, json.dumps(data)],
                                       stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                       text=True)
            lines = queue.Queue()

            def pump():
                for line in process.stdout:
                    lines.put(line.strip())

            threading.Thread(target=pump, daemon=True).start()

            def cleanup():
                if process.poll() is None:
                    process.kill()
                process.wait(timeout=5)
                for stream in (process.stdin, process.stdout, process.stderr):
                    stream.close()

            self.addCleanup(cleanup)
            return process, lines

        def receive(lines):
            try:
                return lines.get(timeout=10)
            except queue.Empty:
                self.fail('A child process did not report its draft mutation stage.')

        def release(process):
            process.stdin.write('continue\n')
            process.stdin.flush()

        first, first_lines = launch(self.key, '爵士与 Lo-Fi')
        self.assertEqual(receive(first_lines), 'read')
        second, second_lines = launch('2' * 32, '民谣与木吉他')
        second_stage = receive(second_lines)
        release(first)
        self.assertEqual(receive(first_lines), 'replace')
        if second_stage == 'read':
            release(second)
            self.assertEqual(receive(second_lines), 'replace')
        release(first)
        first_result = json.loads(receive(first_lines))
        if second_stage == 'read':
            release(second)
            second_result = json.loads(receive(second_lines))
        else:
            second_result = json.loads(second_stage)
        first.wait(timeout=5)
        second.wait(timeout=5)
        self.assertEqual(first.returncode, 0, first.stderr.read())
        self.assertEqual(second.returncode, 0, second.stderr.read())
        self.assertEqual(first_result, {'status': 'ready', 'revision': 2})
        self.assertEqual(second_result, {'status': 'conflict'})
        saved = self.load()
        self.assertEqual(saved['revision'], 2)
        self.assertEqual(saved['changed_count'], 1)
        self.assertEqual(saved['edits'][self.key]['before'], previous['before'])
        self.assertEqual(saved['edits'][self.key]['after']['styles'], ['爵士与 Lo-Fi'])

    def test_request_validation_rejects_unknown_fields_keys_types_and_labels(self):
        cases = [
            {'extra': 'unexpected'}, {'action': 'reset'}, {'record_key': '3' * 32},
            {'record_key': '../source'}, {'revision': True}, {'revision': -1},
            {'source_version': 'not-a-hash'}, {'styles': []},
            {'styles': ['待辨识', '流行抒情']}, {'styles': ['流行抒情', '流行抒情']},
            {'styles': ['pop']}, {'scenes': ['commute']}, {'scenes': ['通勤散步'] * 2},
            {'scenes': ['学习专注', '通勤散步', '放松睡前', '运动提神']},
            {'language': None}, {'language': 'en'}, {'reason': ''}, {'reason': '  '},
            {'reason': 'a' * 1001}, {'reason': 'unsafe\ntext'}, {'recording_note': '\ud800'},
            {'recording_note': '\x7f'}, {'recording_note': 'a' * 1001},
        ]
        for changes in cases:
            with self.subTest(changes=repr(changes)), self.assertRaises(self.drafts.DraftError):
                self.save(**changes)
        self.assertFalse(self.path.exists())

    def test_remove_requires_exact_request_shape_and_existing_record(self):
        self.save()
        remove = {'action': 'remove', 'source_version': self.version, 'revision': 1, 'record_key': self.key}
        for changes in ({'reason': 'unexpected'}, {'record_key': '3' * 32}):
            with self.subTest(changes=changes), self.assertRaises(self.drafts.DraftError):
                self.drafts.mutate_draft(self.project, self.version, self.records, {**remove, **changes})
        self.assertEqual(self.load()['revision'], 1)

    def test_error_codes_distinguish_invalid_requests_from_unavailable_storage(self):
        with self.assertRaises(self.drafts.DraftError) as invalid:
            self.save(styles=['invalid label'])
        self.assertEqual(invalid.exception.code, 'invalid_request')
        self.write_raw('{}')
        with self.assertRaises(self.drafts.DraftError) as corrupted:
            self.save()
        self.assertEqual(corrupted.exception.code, 'unavailable')
        self.path.unlink()
        self.save()
        with patch.object(self.drafts.os, 'replace', side_effect=OSError('disk unavailable')):
            with self.assertRaises(self.drafts.DraftError) as write_failure:
                self.save(revision=1)
        self.assertEqual(write_failure.exception.code, 'unavailable')

    def test_corrupt_duplicate_key_nonfinite_and_oversized_files_are_not_overwritten(self):
        values = ['not json', '{}', '{"revision":0,"revision":1}', '{"revision":NaN}',
                  '{"revision":Infinity}', '{"revision":1e9999}', '[]']
        for value in values:
            with self.subTest(value=value):
                self.write_raw(value)
                self.assertEqual(self.load()['status'], 'unavailable')
                before = self.path.read_bytes()
                with self.assertRaises(self.drafts.DraftError):
                    self.save()
                self.assertEqual(self.path.read_bytes(), before)
        self.write_raw(' ' * 129)
        with patch.object(self.drafts, 'MAX_DRAFT_BYTES', 128):
            self.assertEqual(self.load()['status'], 'unavailable')
            with self.assertRaises(self.drafts.DraftError):
                self.save()

    def test_every_saved_edit_is_validated_and_bound_to_current_baseline(self):
        self.save()
        raw = json.loads(self.path.read_text(encoding='utf-8'))
        malformed = []
        for target, field, value in (
            ('before', 'styles', ['摇滚与独立']), ('after', 'styles', ['unknown']),
            ('after', 'language', 'en'), ('after', 'scenes', ['通勤散步', '通勤散步']),
        ):
            changed = copy.deepcopy(raw)
            changed['edits'][self.key][target][field] = value
            malformed.append(changed)
        changed = copy.deepcopy(raw)
        changed['edits']['3' * 32] = changed['edits'].pop(self.key)
        malformed.append(changed)
        changed = copy.deepcopy(raw)
        changed['edits'][self.key]['extra'] = True
        malformed.append(changed)
        changed = copy.deepcopy(raw)
        changed['edits'][self.key]['reason'] = '\ud800'
        # ASCII escaping permits storing invalid Unicode for the validation test.
        for index, value in enumerate([*malformed, changed]):
            with self.subTest(index=index):
                self.write_raw(json.dumps(value, ensure_ascii=True))
                before = self.path.read_bytes()
                self.assertEqual(self.load()['status'], 'unavailable')
                with self.assertRaises(self.drafts.DraftError):
                    self.save(revision=1)
                self.assertEqual(self.path.read_bytes(), before)

    def test_same_source_with_changed_baseline_is_unavailable(self):
        self.save()
        changed = copy.deepcopy(self.records)
        changed[0]['language'] = '国语'
        self.assertEqual(self.load(records=changed)['status'], 'unavailable')

    def test_output_size_limit_and_atomic_replace_failure_preserve_previous_draft(self):
        self.save()
        before = self.path.read_bytes()
        with patch.object(self.drafts, 'MAX_DRAFT_BYTES', 128), self.assertRaises(self.drafts.DraftError):
            self.save(revision=1)
        self.assertEqual(self.path.read_bytes(), before)
        with patch.object(self.drafts.os, 'replace', side_effect=OSError('disk unavailable')):
            with self.assertRaises(self.drafts.DraftError):
                self.save(revision=1, reason='另一次修正。')
        self.assertEqual(self.path.read_bytes(), before)
        self.assertCountEqual(list(self.path.parent.iterdir()),
                              [self.path, self.path.parent / self.drafts.LOCK_FILE])

    def test_new_output_size_limit_does_not_create_draft_or_leave_temporary_files(self):
        with patch.object(self.drafts, 'MAX_DRAFT_BYTES', 128), self.assertRaises(self.drafts.DraftError):
            self.save()
        self.assertFalse(self.path.exists())
        self.assertEqual(list(self.path.parent.iterdir()), [self.path.parent / self.drafts.LOCK_FILE])

    def test_external_same_size_same_mtime_edit_is_not_overwritten(self):
        self.save()
        before = self.path.read_bytes()
        changed = before.replace(b'"revision":1', b'"revision":9')
        original_fsync = self.drafts.os.fsync

        def change_during_save(descriptor):
            original_fsync(descriptor)
            info = self.path.stat()
            self.path.write_bytes(changed)
            os.utime(self.path, ns=(info.st_atime_ns, info.st_mtime_ns))

        with patch.object(self.drafts.os, 'fsync', change_during_save):
            with self.assertRaises(self.drafts.DraftConflict):
                self.save(revision=1)
        self.assertEqual(self.path.read_bytes(), changed)
        self.assertCountEqual(list(self.path.parent.iterdir()),
                              [self.path, self.path.parent / self.drafts.LOCK_FILE])

    def test_lock_is_released_after_write_failure_and_lock_file_is_preserved(self):
        self.save()
        lock = self.path.parent / self.drafts.LOCK_FILE
        before = lock.lstat()
        with patch.object(self.drafts.os, 'replace', side_effect=OSError('disk unavailable')):
            with self.assertRaises(self.drafts.DraftError):
                self.save(revision=1)
        self.assertEqual(self.save(revision=1)['revision'], 2)
        after = lock.lstat()
        self.assertEqual((before.st_dev, before.st_ino), (after.st_dev, after.st_ino))

    def test_lock_directory_and_oversized_lock_file_are_rejected_without_touching_draft(self):
        self.save()
        before = self.path.read_bytes()
        lock = self.path.parent / self.drafts.LOCK_FILE
        lock.unlink()
        lock.mkdir()
        self.assertEqual(self.load()['status'], 'unavailable')
        with self.assertRaises(self.drafts.DraftError) as unsafe_directory:
            self.save(revision=1)
        self.assertEqual(unsafe_directory.exception.code, 'unavailable')
        self.assertEqual(self.path.read_bytes(), before)
        lock.rmdir()
        lock.write_bytes(b'unexpected personal data')
        self.assertEqual(self.load()['status'], 'unavailable')
        with self.assertRaises(self.drafts.DraftError):
            self.save(revision=1)
        self.assertEqual(lock.read_bytes(), b'unexpected personal data')
        self.assertEqual(self.path.read_bytes(), before)

    def test_existing_organizer_file_and_draft_directory_are_rejected(self):
        self.path.parent.write_text('personal file', encoding='utf-8')
        self.assertEqual(self.load()['status'], 'unavailable')
        with self.assertRaises(self.drafts.DraftError):
            self.save()
        self.assertEqual(self.path.parent.read_text(encoding='utf-8'), 'personal file')
        self.path.parent.unlink()
        self.path.mkdir(parents=True)
        self.assertEqual(self.load()['status'], 'unavailable')
        with self.assertRaises(self.drafts.DraftError):
            self.save()

    def test_reparse_directory_or_file_is_rejected(self):
        self.save()
        original = Path.lstat
        for flagged in (self.project, self.path.parent, self.path, self.path.parent / self.drafts.LOCK_FILE):
            def flagged_stat(path, *args, **kwargs):
                info = original(path, *args, **kwargs)
                if path == flagged:
                    return SimpleNamespace(st_mode=info.st_mode, st_dev=info.st_dev, st_ino=info.st_ino,
                                           st_size=info.st_size, st_mtime_ns=info.st_mtime_ns,
                                           st_ctime_ns=info.st_ctime_ns,
                                           st_file_attributes=getattr(stat, 'FILE_ATTRIBUTE_REPARSE_POINT', 0x400))
                return info
            with self.subTest(flagged=flagged), patch.object(Path, 'lstat', flagged_stat):
                self.assertEqual(self.load()['status'], 'unavailable')
                with self.assertRaises(self.drafts.DraftError):
                    self.save(revision=1)

    def test_symlink_targets_and_project_ancestors_are_rejected_when_supported(self):
        outside = self.project / 'outside'
        outside.mkdir()
        try:
            self.path.parent.symlink_to(outside, target_is_directory=True)
        except OSError:
            self.skipTest('Symlink creation is unavailable on this Windows host.')
        self.assertEqual(self.load()['status'], 'unavailable')
        with self.assertRaises(self.drafts.DraftError):
            self.save()
        self.assertEqual(list(outside.iterdir()), [])
        self.path.parent.unlink()
        self.path.parent.mkdir()
        target = outside / 'untouched.json'
        target.write_text('personal', encoding='utf-8')
        self.path.symlink_to(target)
        self.assertEqual(self.load()['status'], 'unavailable')
        with self.assertRaises(self.drafts.DraftError):
            self.save()
        self.assertEqual(target.read_text(encoding='utf-8'), 'personal')
        linked_project = self.project / 'linked'
        linked_project.symlink_to(outside, target_is_directory=True)
        self.assertEqual(self.drafts.load_draft(linked_project, self.version, self.records)['status'], 'unavailable')


if __name__ == '__main__':
    unittest.main()
