"""Actual optional history reads must not turn refresh failures into empty history."""

import copy
import json
from pathlib import Path
import stat
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from netease_organizer.web_server import WorkbenchApplication
from netease_organizer.web_state import build_local_state
from netease_organizer.service import Organizer
from tests import test_web_server as server_fixture
from tests import test_web_state as state_fixture


ARTIST = '歌手精选执行结果.json'
RENAME = '名称整理执行结果.json'
RECEIPT = {'status': 'completed', 'completed_count': 1,
           'items': [{'name': 'Funk', 'status': 'completed'}]}


class HistoryRefreshTests(unittest.TestCase):
    def setUp(self):
        self.records = state_fixture.WebStateTests()
        self.records.setUp()
        self.addCleanup(self.records.tearDown)
        self.project = self.records.project
        self.records.write('在线名称整理快照.json', self.records.snapshot())
        self.path = self.records.write(RENAME, RECEIPT)
        self.controller = server_fixture.FakeController(self.project)
        self.provider = lambda: build_local_state(self.project)
        self.app = WorkbenchApplication(self.controller, state_provider=self.provider)
        self.addCleanup(self.app.wait_for_idle, 3)

    def run_job(self):
        self.assertEqual(self.app.submit('preview_names', {})[0], 202)
        self.assertTrue(self.app.wait_for_idle(3))
        return self.app.state()

    def assert_failed_refresh(self, before):
        status, response = self.app.refresh_local_records()
        self.assertEqual(status, 503)
        self.assertNotIn('PRIVATE', json.dumps(response))
        self.assertEqual(self.app.state(), before)

    def test_actual_open_permission_failure_preserves_history_and_successful_job_then_retry(self):
        original_open = Path.open
        for provider in (self.provider, None):
            for name in (ARTIST, RENAME):
                for one_shot in (False, True):
                    with self.subTest(default=provider is None, name=name, one_shot=one_shot):
                        target = self.records.write(name, RECEIPT)
                        self.app.state_provider = provider
                        self.app.data = build_local_state(self.project)
                        before = copy.deepcopy(self.app.data)
                        calls = []

                        def denied(path, *args, **kwargs):
                            if path == target and (not one_shot or not calls):
                                calls.append(True)
                                raise PermissionError('PRIVATE-receipt-path')
                            return original_open(path, *args, **kwargs)

                        with patch.object(Path, 'open', denied):
                            state = self.run_job()
                        self.assertTrue(calls)
                        self.assertEqual(state['data'], before)
                        self.assertEqual(state['local_records'], {'status': 'stale'})
                        self.assertEqual(state['job']['status'], 'completed')
                        self.assertEqual(state['job']['result']['status'], 'completed')
                        self.assertNotIn('PRIVATE', json.dumps(state))
                        self.assertEqual(self.app.refresh_local_records()[0], 200)
                        self.assertEqual(self.app.state()['local_records'], {'status': 'current'})
                        with patch.object(Path, 'open', denied):
                            calls.clear()
                            self.assert_failed_refresh(self.app.state())
                        if name == ARTIST:
                            target.unlink()

    def test_explicit_refresh_rejects_physical_and_json_errors_with_both_providers(self):
        good = self.path.read_bytes()
        for provider in (self.provider, None):
            self.app.state_provider = provider
            for payload in (b'', b'{PRIVATE-incomplete', b'[]', b'null', b'"PRIVATE"',
                            b'{"x":1,"x":2}', b'{"x":NaN}', b'{"x":Infinity}',
                            b'\xff', b'{}' + b' ' * (256 * 1024)):
                with self.subTest(default=provider is None, payload=payload[:30]):
                    before = self.app.state()
                    self.path.write_bytes(payload)
                    self.assert_failed_refresh(before)
                    self.path.write_bytes(good)
                    self.assertEqual(self.app.refresh_local_records()[0], 200)

    def test_startup_is_permissive_and_initial_absence_can_publish_empty_history(self):
        self.path.write_bytes(b'{PRIVATE-invalid')
        startup = WorkbenchApplication(self.controller, state_provider=self.provider)
        self.assertIsNone(startup.state()['data']['history'])
        for path in self.records.artifacts.iterdir():
            path.unlink()
        for remove_folder in (False, True):
            if remove_folder:
                self.records.artifacts.rmdir()
            for provider in (self.provider, None):
                self.app.state_provider = provider
                self.assertEqual(self.app.refresh_local_records()[0], 200)
                self.assertEqual(self.app.state()['data']['source'], 'empty')
                self.assertIsNone(self.app.state()['data']['history'])

    def test_successful_organizer_hook_cannot_mask_either_broken_receipt(self):
        self.app.state_provider = None
        historical = {**RECEIPT, 'operation': 'renames'}
        with patch.object(server_fixture.FakeController, '_last_result', lambda unused: historical, create=True):
            for name in (ARTIST, RENAME):
                with self.subTest(name=name):
                    target = self.records.artifacts / name
                    saved = target.read_bytes() if target.exists() else None
                    target.write_bytes(b'{PRIVATE-invalid')
                    self.assert_failed_refresh(self.app.state())
                    if saved is None:
                        target.unlink()
                    else:
                        target.write_bytes(saved)

    def actual_organizer_app(self):
        organizer = Organizer(self.project, cli=SimpleNamespace(), reader=SimpleNamespace())
        organizer.doctor = lambda: {'status': 'credentials_saved', 'installed': True, 'configured': True}
        organizer.write_recovery = lambda: {'status': 'clear', 'operation': 'unknown', 'can_reconcile': False}
        provider = lambda: build_local_state(self.project, organizer=organizer)
        return organizer, provider, WorkbenchApplication(organizer, state_provider=provider)

    def test_actual_organizer_post_preload_read_failure_cannot_publish_older_history(self):
        self.records.write(ARTIST, RECEIPT, timestamp=100)
        self.records.write(RENAME, {**RECEIPT, 'completed_count': 2}, timestamp=200)
        _, provider, app = self.actual_organizer_app()
        original_open = Path.open
        for selected_provider in (provider, None):
            with self.subTest(default=selected_provider is None):
                app.state_provider = selected_provider
                app.data = provider()
                before = app.state()
                self.assertEqual(before['data']['history']['completed_count'], 2)
                reads = []

                def denied_after_preload(path, *args, **kwargs):
                    if path == self.path:
                        reads.append(True)
                        if len(reads) > 1:
                            raise PermissionError('PRIVATE-hook-second-read')
                    return original_open(path, *args, **kwargs)

                with patch.object(Path, 'open', denied_after_preload):
                    status, result = app.refresh_local_records()
                self.assertGreater(len(reads), 1)
                self.assertEqual(status, 503)
                self.assertNotIn('PRIVATE', json.dumps(result))
                self.assertEqual(app.state(), before)
                self.assertEqual(app.refresh_local_records()[0], 200)
                self.assertEqual(app.data['history']['completed_count'], 2)

    def test_actual_organizer_second_metadata_failure_or_oversize_cannot_publish_older_history(self):
        self.records.write(ARTIST, RECEIPT, timestamp=100)
        self.records.write(RENAME, {**RECEIPT, 'completed_count': 2}, timestamp=200)
        organizer, provider, app = self.actual_organizer_app()
        original_stat, original_lstat = Path.stat, Path.lstat
        actual_hook = organizer._last_result
        inside_hook = []

        def hook():
            inside_hook.append(True)
            try:
                return actual_hook()
            finally:
                inside_hook.clear()

        organizer._last_result = hook
        for failure in ('metadata', 'oversized'):
            for selected_provider in (provider, None):
                with self.subTest(failure=failure, default=selected_provider is None):
                    app.state_provider = selected_provider
                    app.data = provider()
                    before = app.state()
                    injected = []

                    def metadata(method):
                        def read_metadata(path, *args, **kwargs):
                            info = method(path, *args, **kwargs)
                            if path == self.path and inside_hook:
                                injected.append(True)
                                if failure == 'metadata':
                                    raise PermissionError('PRIVATE-hook-metadata')
                                values = {field: getattr(info, field) for field in (
                                    'st_dev', 'st_ino', 'st_mode', 'st_mtime_ns', 'st_ctime_ns')}
                                values['st_file_attributes'] = getattr(info, 'st_file_attributes', 0)
                                return SimpleNamespace(**values, st_size=256 * 1024 + 1)
                            return info
                        return read_metadata

                    with patch.object(Path, 'stat', metadata(original_stat)), \
                            patch.object(Path, 'lstat', metadata(original_lstat)):
                        status, result = app.refresh_local_records()
                    self.assertTrue(injected)
                    self.assertEqual(status, 503)
                    self.assertNotIn('PRIVATE', json.dumps(result))
                    self.assertEqual(app.state(), before)
                    self.assertEqual(app.refresh_local_records()[0], 200)

    def test_actual_organizer_paused_history_retains_resumability_and_recovery_precedence(self):
        self.records.write(ARTIST, RECEIPT, timestamp=100)
        self.records.write(RENAME, {**RECEIPT, 'status': 'paused', 'outcome_known': True}, timestamp=200)
        organizer, provider, app = self.actual_organizer_app()
        for selected_provider in (provider, None):
            app.state_provider = selected_provider
            self.assertEqual(app.refresh_local_records()[0], 200)
            self.assertEqual(app.data['history']['operation'], 'renames')
            self.assertTrue(app.data['history']['resumable'])
        organizer.write_recovery = lambda: {'status': 'review_required', 'operation': 'artists', 'can_reconcile': False}
        for selected_provider in (provider, None):
            app.state_provider = selected_provider
            self.assertEqual(app.refresh_local_records()[0], 200)
            self.assertEqual(app.data['history']['operation'], 'artists')
            self.assertFalse(app.data['history']['resumable'])

    def test_strict_scope_resets_after_exception_and_does_not_cross_threads(self):
        original_open = Path.open
        entered, release = threading.Event(), threading.Event()
        errors = []

        def denied(path, *args, **kwargs):
            if path == self.path:
                raise PermissionError('PRIVATE-denied')
            return original_open(path, *args, **kwargs)

        def blocked_provider():
            entered.set()
            if not release.wait(3):
                raise RuntimeError('fixture timeout')
            return self.provider()

        def refresh():
            try:
                self.app._strict_local_data()
            except Exception as error:
                errors.append(error)

        self.app.state_provider = blocked_provider
        worker = threading.Thread(target=refresh)
        with patch.object(Path, 'open', denied):
            worker.start()
            try:
                self.assertTrue(entered.wait(2))
                self.assertIsNone(build_local_state(self.project)['history'])
            finally:
                release.set()
                worker.join(3)
            self.assertEqual(len(errors), 1)
            self.app.state_provider = self.provider
            with self.assertRaises((ValueError, OSError)):
                self.app._strict_local_data()
            self.assertIsNone(build_local_state(self.project)['history'])
        self.assertEqual(self.app._strict_local_data()['history']['operation'], 'renames')

    def test_disappearance_after_initial_metadata_is_failure_not_valid_absence(self):
        original_open = Path.open

        def disappeared(path, *args, **kwargs):
            if path == self.path:
                path.unlink()
            return original_open(path, *args, **kwargs)

        before = self.app.state()
        with patch.object(Path, 'open', disappeared):
            self.assert_failed_refresh(before)
        self.assertEqual(self.app.refresh_local_records()[0], 200)
        self.assertIsNone(self.app.state()['data']['history'])

    def test_unsafe_receipt_metadata_and_read_race_reject_candidate(self):
        original_lstat, original_open = Path.lstat, Path.open
        before = self.app.state()
        for target in (self.records.artifacts, self.path):
            def denied(path, *args, **kwargs):
                if path == target:
                    raise PermissionError('PRIVATE-metadata-denied')
                return original_lstat(path, *args, **kwargs)
            with self.subTest(target=target.name), patch.object(Path, 'lstat', denied):
                self.assert_failed_refresh(before)
        for mode, attributes in ((stat.S_IFDIR, 0), (stat.S_IFLNK, 0), (stat.S_IFREG, 0x400)):
            def unsafe(path, *args, **kwargs):
                if path == self.path:
                    return SimpleNamespace(st_mode=mode, st_file_attributes=attributes)
                return original_lstat(path, *args, **kwargs)
            with self.subTest(mode=mode, attributes=attributes), patch.object(Path, 'lstat', unsafe):
                self.assert_failed_refresh(before)

        def changed(path, *args, **kwargs):
            if path == self.path:
                with original_open(path, 'wb') as stream:
                    stream.write(b'{}')
            return original_open(path, *args, **kwargs)

        with patch.object(Path, 'open', changed):
            self.assert_failed_refresh(before)

    def test_unsupported_legacy_object_keeps_existing_semantic_filtering(self):
        for record in ({'status': 'failed'}, {'status': 'future'}, {}, {'items': 'PRIVATE-unsupported'}):
            self.records.write(RENAME, record)
            self.assertEqual(self.app.refresh_local_records()[0], 200)
            self.assertIsNone(self.app.state()['data']['history'])

    def test_unrelated_sibling_write_during_receipt_read_does_not_reject_refresh(self):
        original_open = Path.open

        def with_sibling_write(path, *args, **kwargs):
            if path == self.path:
                with original_open(path.parent / 'unrelated-diagnostic.txt', 'wb') as stream:
                    stream.write(b'local diagnostic')
            return original_open(path, *args, **kwargs)

        with patch.object(Path, 'open', with_sibling_write):
            self.assertEqual(self.app.refresh_local_records()[0], 200)
        self.assertEqual(self.app.state()['data']['history']['operation'], 'renames')

    def test_slow_actual_receipt_read_keeps_state_and_stop_responsive(self):
        original_open = Path.open
        entered, release, observed = threading.Event(), threading.Event(), threading.Event()
        before = copy.deepcopy(self.app.data)

        def slow(path, *args, **kwargs):
            if path == self.path:
                entered.set()
                if not release.wait(3):
                    raise RuntimeError('fixture timeout')
                raise PermissionError('PRIVATE-slow-read')
            return original_open(path, *args, **kwargs)

        def observe():
            self.app.state()
            self.app.attach_page()
            self.app.request_stop()
            observed.set()

        observer = threading.Thread(target=observe)
        with patch.object(Path, 'open', slow):
            self.assertEqual(self.app.submit('preview_names', {})[0], 202)
            try:
                self.assertTrue(entered.wait(2))
                observer.start()
                self.assertTrue(observed.wait(1))
            finally:
                release.set()
                if observer.ident is not None:
                    observer.join(3)
                self.assertTrue(self.app.wait_for_idle(3))
        self.assertEqual(self.app.data, before)
        self.assertEqual(self.app.state()['local_records'], {'status': 'stale'})
