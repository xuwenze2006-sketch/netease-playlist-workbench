"""Classification display caching uses disposable local records only."""

import copy
import json
import os
from pathlib import Path
import stat
import unittest
from unittest.mock import patch

from netease_organizer import web_classification as display
from tests import test_web_classification as fixtures
from tests.test_web_classification import (
    DIRECTORY, FINAL, INTENT, PLAN, RECEIPT, REPORT,
)


PENDING = '分类生成未完成.json'


class ClassificationCacheTests(unittest.TestCase):
    def setUp(self):
        self.fixture = self.make_fixture()

    def make_fixture(self):
        fixture = fixtures.WebClassificationTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        fixture.complete_evidence()
        return fixture

    def test_warm_search_filters_and_pages_recheck_content_without_repeating_parsing_or_validation(self):
        self.assertEqual(self.fixture.view()['verification'], 'verified')
        with patch.object(display, '_load', wraps=display._load) as read, \
                patch.object(display, 'validate_plan', wraps=display.validate_plan) as plan, \
                patch.object(display, '_report', wraps=display._report) as report, \
                patch.object(display, '_receipt', wraps=display._receipt) as receipt, \
                patch.object(display.json, 'loads', wraps=display.json.loads) as parse:
            page = self.fixture.view(offset=1, limit=1)
            query = self.fixture.view(query='ＲＡＩＮ')
            pending = self.fixture.view(review='pending', dimension='style', tag='摇滚与独立')
        self.assertEqual(page['records'][0]['position'], 2)
        self.assertEqual(query['records'][0]['position'], 1)
        self.assertEqual(pending['records'][0]['position'], 4)
        for operation in (read, plan, report, receipt, parse):
            self.assertEqual(operation.call_count, 0)

    def test_public_results_are_deep_copies_and_cache_contains_no_private_source_data(self):
        first = self.fixture.view()
        expected = copy.deepcopy(first)
        first['records'][0]['styles'].clear()
        first['records'][0]['name'] = 'changed by caller'
        first['summary']['source_count'] = 999
        first['options']['style'].clear()
        first['playlists'][0]['key'] = '999'
        first['filters']['query'] = 'changed by caller'
        self.assertEqual(self.fixture.view(), expected)
        serialized = json.dumps([entry[1] for entry in display._PROJECTION_CACHE.values()], ensure_ascii=False)
        for private in ('RAW-SECRET', 'RECEIPT-SECRET', 'PRIVATE-URL', 'account_original_id', 'candidate_track_ids'):
            self.assertNotIn(private, serialized)

    def test_each_evidence_change_invalidates_old_verified_projection(self):
        for filename in (PLAN, REPORT, INTENT, RECEIPT, FINAL, DIRECTORY):
            with self.subTest(filename=filename):
                fixture = self.make_fixture()
                self.assertEqual(fixture.view()['verification'], 'verified')
                fixture.write(filename, {}, 110)
                result = fixture.view()
                self.assertNotEqual(result['verification'], 'verified')
                self.assertIsNone(result['verified_at'])

    def test_removed_report_is_not_loaded_and_restored_report_is_revalidated(self):
        self.assertEqual(self.fixture.view()['verification'], 'verified')
        (self.fixture.project / 'artifacts' / REPORT).unlink()
        self.assertEqual(self.fixture.view()['status'], 'not_loaded')
        self.fixture.write(REPORT, self.fixture.report, 101)
        with patch.object(display, '_report', wraps=display._report) as report:
            self.assertEqual(self.fixture.view()['verification'], 'verified')
        self.assertEqual(report.call_count, 1)

    def test_pending_marker_alone_evicts_warm_projection_and_clear_revalidates(self):
        sources = {name: (self.fixture.project / 'artifacts' / name).read_bytes()
                   for name in (PLAN, REPORT, INTENT, RECEIPT, FINAL, DIRECTORY)}
        self.assertEqual(self.fixture.view()['verification'], 'verified')
        marker = self.fixture.write(PENDING, {'status': 'pending'}, 110)
        result = self.fixture.view()
        self.assertEqual(result['status'], 'unavailable')
        self.assertEqual(result['records'], [])
        self.assertIsNone(result['verified_at'])
        self.assertNotIn(self.fixture.project, display._PROJECTION_CACHE)
        history, timestamp = display.build_classification_history(self.fixture.project)
        self.assertEqual(history['status'], 'completed')
        self.assertEqual(timestamp, 103)
        marker.unlink()
        with patch.object(display, '_report', wraps=display._report) as report:
            self.assertEqual(self.fixture.view()['verification'], 'verified')
        self.assertEqual(report.call_count, 1)
        self.assertEqual({name: (self.fixture.project / 'artifacts' / name).read_bytes()
                          for name in sources}, sources)

    def test_valid_pending_marker_explains_unavailable_without_retaining_source_or_cache(self):
        for mode in ('build', 'repair_missing'):
            for warm in (False, True):
                with self.subTest(mode=mode, warm=warm):
                    fixture = self.make_fixture()
                    if warm:
                        self.assertEqual(fixture.view()['verification'], 'verified')
                    marker = fixture.write(PENDING, {
                        'kind': 'classification_generation_pending', 'version': 1, 'mode': mode}, 110)
                    if not warm:
                        marker.write_bytes(marker.read_bytes().ljust(4096, b' '))
                    result = fixture.view()
                    self.assertEqual(result['status'], 'unavailable')
                    self.assertEqual(result.get('error_code'), 'classification_generation_incomplete')
                    self.assertEqual(result['records'], [])
                    self.assertIsNone(result['verified_at'])
                    self.assertNotIn('quality', result)
                    self.assertNotIn('mode', result)
                    self.assertNotIn(fixture.project, display._PROJECTION_CACHE)
                    marker.unlink()
                    self.assertEqual(fixture.view()['verification'], 'verified')
                    self.assertNotIn('error_code', fixture.view())

    def test_pending_diagnostic_requires_exact_bounded_production_schema(self):
        marker = self.fixture.project / 'artifacts' / PENDING
        valid = {'kind': 'classification_generation_pending', 'version': 1, 'mode': 'build'}
        malformed = [b'{broken', b'[]', b'null', b'{}', b'{"status":"pending"}',
                     json.dumps({**valid, 'kind': 'unknown'}).encode(),
                     json.dumps({**valid, 'version': True}).encode(),
                     json.dumps({**valid, 'version': 1.0}).encode(),
                     json.dumps({**valid, 'version': 2}).encode(),
                     json.dumps({**valid, 'mode': 'unknown'}).encode(),
                     json.dumps({**valid, 'mode': []}).encode(),
                     json.dumps({**valid, 'private': 'PRIVATE-MARKER'}).encode(),
                     b'{"kind":"classification_generation_pending","version":1,"version":1,"mode":"build"}',
                     json.dumps(valid).encode().ljust(4097, b' ')]
        for raw in malformed:
            with self.subTest(raw=raw[:120]):
                self.assertEqual(self.fixture.view()['verification'], 'verified')
                marker.write_bytes(raw)
                try:
                    result = self.fixture.view()
                    self.assertEqual(result['status'], 'unavailable')
                    self.assertNotIn('error_code', result)
                    self.assertNotIn('PRIVATE-MARKER', json.dumps(result))
                    self.assertNotIn(self.fixture.project, display._PROJECTION_CACHE)
                finally:
                    marker.unlink()

    def test_unrelated_corruption_has_no_pending_generation_diagnostic(self):
        self.assertEqual(self.fixture.view()['verification'], 'verified')
        (self.fixture.project / 'artifacts' / REPORT).write_bytes(b'{PRIVATE-CORRUPTION')
        result = self.fixture.view()
        self.assertEqual(result['status'], 'unavailable')
        self.assertNotIn('error_code', result)
        self.assertNotIn('PRIVATE', json.dumps(result))

    def test_pending_marker_changed_after_diagnostic_read_stays_generic(self):
        for removed in (False, True):
            with self.subTest(removed=removed):
                fixture = self.make_fixture()
                marker = fixture.write(PENDING, {
                    'kind': 'classification_generation_pending', 'version': 1, 'mode': 'build'}, 110)
                load = display._load

                def change_marker(project, name, maximum):
                    result = load(project, name, maximum)
                    if name == PENDING:
                        if removed:
                            marker.unlink()
                        else:
                            marker.write_text('{PRIVATE-CHANGED}', encoding='utf-8')
                    return result

                with patch.object(display, '_load', side_effect=change_marker):
                    result = fixture.view()
                self.assertEqual(result['status'], 'unavailable')
                self.assertNotIn('error_code', result)
                self.assertNotIn(fixture.project, display._PROJECTION_CACHE)

    def test_pending_generation_recheck_failure_stays_generic(self):
        self.fixture.write(PENDING, {
            'kind': 'classification_generation_pending', 'version': 1, 'mode': 'build'}, 110)
        cache_generation = display._cache_generation
        first = True

        def denied_recheck(project):
            nonlocal first
            if not first:
                raise PermissionError('PRIVATE-generation-recheck')
            first = False
            return cache_generation(project)

        with patch.object(display, '_cache_generation', side_effect=denied_recheck):
            result = self.fixture.view()
        self.assertEqual(result['status'], 'unavailable')
        self.assertNotIn('error_code', result)
        self.assertNotIn('PRIVATE', json.dumps(result))

    def test_pending_marker_created_mid_projection_discards_result(self):
        load_directory = display._load_directory

        def mark_pending(project):
            self.fixture.write(PENDING, {'status': 'pending'}, 110)
            return load_directory(project)

        with patch.object(display, '_load_directory', side_effect=mark_pending):
            result = self.fixture.view()
        self.assertEqual(result['status'], 'unavailable')
        self.assertEqual(result['records'], [])
        self.assertIsNone(result['verified_at'])
        self.assertNotIn('error_code', result)
        self.assertNotIn(self.fixture.project, display._PROJECTION_CACHE)

    def test_pending_marker_created_while_cached_filtering_discards_and_evicts(self):
        self.assertEqual(self.fixture.view()['verification'], 'verified')
        match = display._matches
        changed = False

        def mark_pending(row, filters, pilot_positions=()):
            nonlocal changed
            if not changed:
                changed = True
                self.fixture.write(PENDING, {'status': 'pending'}, 110)
            return match(row, filters, pilot_positions)

        with patch.object(display, '_matches', side_effect=mark_pending):
            result = self.fixture.view(query='Rain')
        self.assertEqual(result['status'], 'unavailable')
        self.assertEqual(result['records'], [])
        self.assertIsNone(result['verified_at'])
        self.assertNotIn('error_code', result)
        self.assertNotIn(self.fixture.project, display._PROJECTION_CACHE)

    def test_pending_marker_removed_mid_projection_discards_result(self):
        marker = self.fixture.write(PENDING, {'status': 'pending'}, 110)
        build_projection = display._build_projection

        def finish_generation(project):
            marker.unlink()
            return build_projection(project)

        with patch.object(display, '_build_projection', side_effect=finish_generation):
            result = self.fixture.view()
        self.assertEqual(result['status'], 'unavailable')
        self.assertEqual(result['records'], [])
        self.assertNotIn(self.fixture.project, display._PROJECTION_CACHE)
        self.assertEqual(self.fixture.view()['verification'], 'verified')

    def test_pending_marker_directory_invalid_json_and_oversize_fail_closed(self):
        marker = self.fixture.project / 'artifacts' / PENDING
        for kind in ('directory', 'invalid_json', 'oversize'):
            with self.subTest(kind=kind):
                self.assertEqual(self.fixture.view()['verification'], 'verified')
                if kind == 'directory':
                    marker.mkdir()
                else:
                    marker.write_bytes(b'?' if kind == 'invalid_json' else
                                       b' ' * (display.MAX_PRIVATE_BYTES + 1))
                try:
                    result = self.fixture.view()
                    self.assertEqual(result['status'], 'unavailable')
                    self.assertEqual(result['records'], [])
                    self.assertNotIn('error_code', result)
                    self.assertNotIn(self.fixture.project, display._PROJECTION_CACHE)
                finally:
                    marker.rmdir() if kind == 'directory' else marker.unlink()

    def test_pending_marker_symlink_and_dangling_symlink_fail_closed(self):
        marker = self.fixture.project / 'artifacts' / PENDING
        for dangling in (False, True):
            with self.subTest(dangling=dangling):
                self.assertEqual(self.fixture.view()['verification'], 'verified')
                target = marker.with_name('missing-marker-target') if dangling else marker.with_name(REPORT)
                try:
                    marker.symlink_to(target)
                except (NotImplementedError, OSError) as error:
                    self.skipTest(f'Creating local symlinks is unavailable: {error}')
                try:
                    result = self.fixture.view()
                    self.assertEqual(result['status'], 'unavailable')
                    self.assertEqual(result['records'], [])
                    self.assertNotIn('error_code', result)
                    self.assertNotIn(self.fixture.project, display._PROJECTION_CACHE)
                finally:
                    marker.unlink()

    def test_unreadable_pending_marker_path_evicts_verified_cache(self):
        self.assertEqual(self.fixture.view()['verification'], 'verified')
        original = Path.lstat

        def unreadable(path, *args, **kwargs):
            if path.name == PENDING:
                raise PermissionError('unreadable generation marker')
            return original(path, *args, **kwargs)

        with patch.object(Path, 'lstat', unreadable):
            result = self.fixture.view()
        self.assertEqual(result['status'], 'unavailable')
        self.assertEqual(result['records'], [])
        self.assertNotIn('error_code', result)
        self.assertNotIn(self.fixture.project, display._PROJECTION_CACHE)

    def test_atomic_same_size_and_mtime_report_replacement_cannot_reuse_old_rows(self):
        self.assertEqual(self.fixture.view()['records'][0]['name'], 'Rain · 雨の音')
        target = self.fixture.project / 'artifacts' / REPORT
        old = target.stat()
        replacement = target.with_suffix('.replacement')
        changed = copy.deepcopy(self.fixture.report)
        changed['records'][0]['name'] = 'Snow · 雨の音'
        replacement.write_text(json.dumps(changed, ensure_ascii=False), encoding='utf-8')
        self.assertEqual(replacement.stat().st_size, old.st_size)
        os.utime(replacement, ns=(old.st_atime_ns, old.st_mtime_ns))
        os.replace(replacement, target)
        self.assertEqual(self.fixture.view()['records'][0]['name'], 'Snow · 雨の音')

    def test_in_place_same_size_and_restored_mtime_edit_cannot_reuse_old_rows(self):
        self.assertEqual(self.fixture.view()['records'][0]['name'], 'Rain · 雨の音')
        target = self.fixture.project / 'artifacts' / REPORT
        old = target.stat()
        changed = copy.deepcopy(self.fixture.report)
        changed['records'][0]['name'] = 'Snow · 雨の音'
        target.write_text(json.dumps(changed, ensure_ascii=False), encoding='utf-8')
        self.assertEqual(target.stat().st_size, old.st_size)
        os.utime(target, ns=(old.st_atime_ns, old.st_mtime_ns))
        self.assertEqual(self.fixture.view()['records'][0]['name'], 'Snow · 雨の音')

    def test_change_during_cached_filtering_discards_page_and_evicts_old_projection(self):
        self.assertEqual(self.fixture.view()['verification'], 'verified')
        match = display._matches
        changed = False

        def change(row, filters, pilot_positions=()):
            nonlocal changed
            if not changed:
                changed = True
                self.fixture.write(REPORT, {}, 110)
            return match(row, filters, pilot_positions)

        with patch.object(display, '_matches', side_effect=change):
            result = self.fixture.view(query='Rain')
        self.assertEqual(result['status'], 'unavailable')
        self.assertEqual(result['records'], [])
        self.assertEqual(self.fixture.view()['status'], 'unavailable')

    def test_signature_failure_evicts_verified_cache_and_next_read_revalidates(self):
        self.assertEqual(self.fixture.view()['verification'], 'verified')
        with patch.object(display, '_signatures', side_effect=PermissionError('unreadable')):
            result = self.fixture.view()
        self.assertEqual(result['status'], 'unavailable')
        with patch.object(display, '_report', wraps=display._report) as report:
            self.assertEqual(self.fixture.view()['verification'], 'verified')
        self.assertEqual(report.call_count, 1)

    def test_unreadable_source_evicts_verified_cache_and_next_read_revalidates(self):
        self.assertEqual(self.fixture.view()['verification'], 'verified')
        original = Path.open

        def unreadable(path, *args, **kwargs):
            if path.name == REPORT and args == ('rb',):
                raise PermissionError('unreadable local report')
            return original(path, *args, **kwargs)

        with patch.object(Path, 'open', unreadable):
            self.assertEqual(self.fixture.view()['status'], 'unavailable')
        with patch.object(display, '_report', wraps=display._report) as report:
            self.assertEqual(self.fixture.view()['verification'], 'verified')
        self.assertEqual(report.call_count, 1)

    def test_newer_previously_absent_directory_invalidates_old_account_binding(self):
        self.assertEqual(self.fixture.view()['verification'], 'verified')
        directory = copy.deepcopy(self.fixture.directory)
        directory['account']['id'] = 'E' * 32
        self.fixture.write('在线整理快照.json', directory, 110)
        self.assertEqual(self.fixture.view()['status'], 'unavailable')

    def test_directory_and_file_reparse_points_reject_cache_even_if_child_files_match(self):
        self.assertEqual(self.fixture.view()['verification'], 'verified')
        original = Path.lstat
        artifacts = self.fixture.project / 'artifacts'

        class ReparseStat:
            def __init__(self, value):
                self.value = value
                self.st_file_attributes = getattr(stat, 'FILE_ATTRIBUTE_REPARSE_POINT', 0x400)

            def __getattr__(self, name):
                return getattr(self.value, name)

        for target in (artifacts, artifacts / REPORT, artifacts / FINAL):
            with self.subTest(target=target.name):
                self.assertEqual(self.fixture.view()['verification'], 'verified')

                def redirected(path, *args, **kwargs):
                    value = original(path, *args, **kwargs)
                    return ReparseStat(value) if path == target else value

                with patch.object(Path, 'lstat', redirected):
                    result = self.fixture.view()
                self.assertEqual(result['status'], 'unavailable')
                self.assertEqual(result['records'], [])

    def test_cache_is_bounded_and_least_recently_used_project_is_revalidated(self):
        with patch.object(display, '_PROJECTION_CACHE_LIMIT', 2):
            first, second, third = (self.make_fixture() for _ in range(3))
            self.assertEqual(first.view()['status'], 'available')
            self.assertEqual(second.view()['status'], 'available')
            self.assertEqual(first.view()['status'], 'available')
            self.assertEqual(third.view()['status'], 'available')
            self.assertLessEqual(len(display._PROJECTION_CACHE), 2)
            with patch.object(display, '_report', wraps=display._report) as report:
                self.assertEqual(second.view()['status'], 'available')
            self.assertEqual(report.call_count, 1)


if __name__ == '__main__':
    unittest.main()
