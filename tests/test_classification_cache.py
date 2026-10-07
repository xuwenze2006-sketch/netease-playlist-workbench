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
