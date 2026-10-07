import copy
import hashlib
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from netease_organizer.classification_execution import _encoded, _jobs, plan_digest
from netease_organizer.web_classification import build_local_classification, build_classification_history


REPORT = '全库分类-逐曲结果.json'
PLAN = '分类整理计划.json'
INTENT = '分类整理执行进度.json'
RECEIPT = '分类整理执行结果.json'
FINAL = '分类整理最终核验.json'
DIRECTORY = '在线名称整理快照.json'


class WebClassificationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.project = Path(self.temp.name).resolve()
        (self.project / 'artifacts').mkdir()
        self.ids = [f'{index:032X}' for index in range(1, 5)]
        memberships = [
            ('场景 · 通勤散步', 'scene', [0, 1]),
            ('场景 · 学习专注', 'scene', [3]),
            ('风格 · 流行抒情', 'style', [0, 1]),
            ('风格 · 摇滚与独立', 'style', [3]),
            ('语言 · 英语', 'language', [0]),
            ('语言 · 国语', 'language', [1]),
            ('分类 · 待辨识', 'style', [2, 3]),
        ]
        self.plan = {
            'kind': 'approved_classification_plan', 'version': 1,
            'account_original_id': '12345678', 'account_id': 'A' * 32,
            'source_playlist_id': 'C' * 32, 'original_source_playlist_id': '87654321',
            'source_track_count': 4, 'source_track_ids': self.ids,
            'jobs': [{'kind': 'create_classification_playlist', 'name': name,
                      'dimension': dimension, 'candidate_track_ids': [self.ids[index] for index in indices],
                      'intended_visibility': 'provider_default'} for name, dimension, indices in memberships],
        }
        self.records = [
            self.row(1, 'Rain · 雨の音', 'Taylor Swift', ['流行抒情'], ['通勤散步'], '英语'),
            self.row(2, '喜欢', '蔡健雅', ['流行抒情'], ['通勤散步'], '国语'),
            self.row(3, '前奏', '', ['待辨识'], [], '器乐或配乐录音', ['风格待辨识']),
            self.row(4, '夜色', '测试歌手', ['摇滚与独立'], ['学习专注'], '待辨识', ['语言待辨识']),
        ]
        self.summary = {'source_count': 4, 'covered_count': 4, 'playlist_count': 7,
                        'pending_count': 2, 'unknown_style_count': 1, 'unknown_language_count': 1,
                        'review_note_count': 1, 'dimensions': {'scene': 2, 'style': 3, 'language': 2},
                        'playlists': [{'name': job['name'], 'count': len(job['candidate_track_ids'])}
                                      for job in self.plan['jobs']]}
        self.report = {'kind': 'classification_report', 'created_at': '1970-01-01T00:01:40+00:00',
                       'summary': self.summary, 'records': self.records}
        liked = {'id': 'C' * 32, 'original_id': '87654321', 'name': '我喜欢的音乐',
                 'track_count': 4, 'special_type': 5}
        self.directory = {'account': {'id': 'A' * 32, 'original_id': '12345678', 'nickname': '离线用户'},
                          'playlists': [liked], 'liked': liked}
        self.write(PLAN, self.plan, 100)
        self.write(REPORT, self.report, 101)
        self.write(DIRECTORY, self.directory, 105)

    @staticmethod
    def row(position, name, artists, styles, scenes, language, pending=None):
        return {'position': position, 'name': name, 'artists': artists, 'styles': styles,
                'scenes': scenes, 'language': language, 'pending_reasons': pending or [],
                'evidence_note': '具体录音及风格判断', 'language_evidence_note': '原歌词文字补充核对',
                'style_judgment_score': .8, 'review_note': False, 'live_metadata_available': True,
                'raw_response': {'token': 'RAW-SECRET'}}

    def write(self, name, value, timestamp):
        path = self.project / 'artifacts' / name
        path.write_text(json.dumps(value, ensure_ascii=False), encoding='utf-8')
        os.utime(path, (timestamp, timestamp))
        return path

    def view(self, **kwargs):
        return build_local_classification(self.project, **kwargs)

    def complete_evidence(self, *, status='completed'):
        items = []
        for index, job in enumerate(self.plan['jobs']):
            count = len(job['candidate_track_ids'])
            item = {'kind': job['kind'], 'name': job['name'], 'playlist_id': f'{100 + index:032X}',
                    'original_playlist_id': str(900000 + index), 'count': count, 'expected_count': count,
                    'phase': 'completed', 'status': 'completed', 'added_count': count, 'message': 'SAFE'}
            items.append(item)
            self.directory['playlists'].append({'id': item['playlist_id'], 'original_id': item['original_playlist_id'],
                                               'name': job['name'], 'track_count': count, 'special_type': 0})
        result = {'status': status, 'completed_count': len(items), 'items': items,
                  'applied_to_account': True, 'write_attempted': True, 'outcome_known': True}
        intent = {**copy.deepcopy(result), 'status': 'running', 'kind': 'classification_execution_journal',
                  'version': 1, 'run_id': 'd' * 32, 'plan_digest': plan_digest(self.plan), 'plan': self.plan,
                  'phase': 'completed', 'job_index': len(items) - 1, 'jobs': _jobs(self.plan),
                  'expected_owner_id': self.plan['account_original_id'], 'add_offset': 0, 'add_count': 2}
        receipt = {**copy.deepcopy(result), 'kind': 'classification_execution_receipt', 'version': 1,
                   'run_id': intent['run_id'], 'plan_digest': intent['plan_digest'],
                   'intent_digest': hashlib.sha256(_encoded(intent)).hexdigest(), 'record_saved': True,
                   'source_expected_count': 4, 'source_observed_count': 4, 'source_missing_count': 0,
                   'raw_response': 'RECEIPT-SECRET'}
        final = {'kind': 'classification_final_readback', 'status': 'verified',
                 'verified_at': '1970-01-01T00:01:44+00:00',
                 **{key: self.summary[key] for key in ('source_count', 'covered_count', 'playlist_count',
                                                       'pending_count', 'unknown_style_count', 'unknown_language_count')},
                 'original_favorites_membership_and_order_unchanged': True,
                 'playlists': [{'name': job['name'], 'dimension': job['dimension'], 'count': item['count'],
                                'original_playlist_id': item['original_playlist_id'], 'track_update_time': 20,
                                'url': 'https://example.invalid/PRIVATE-URL'}
                               for job, item in zip(self.plan['jobs'], items)]}
        self.write(INTENT, intent, 102)
        self.write(RECEIPT, receipt, 103)
        self.write(FINAL, final, 104)
        self.write(DIRECTORY, self.directory, 105)
        return intent, receipt, final

    def test_public_projection_whitelists_records_and_derives_pending_counts(self):
        result = self.view()
        self.assertEqual(result['status'], 'available')
        self.assertEqual(result['source'], 'local_record')
        self.assertEqual(result['verification'], 'local_only')
        self.assertIsNone(result['verified_at'])
        self.assertEqual(result['summary'], {key: self.summary[key] for key in (
            'source_count', 'covered_count', 'playlist_count', 'pending_count', 'unknown_style_count', 'unknown_language_count')})
        self.assertEqual(result['records'][2]['artists'], '')
        self.assertEqual(result['records'][2]['pending_reasons'], ['风格待辨识'])
        self.assertEqual(set(result['records'][0]), {'position', 'name', 'artists', 'styles', 'scenes', 'language',
                                                  'pending_reasons', 'evidence_note', 'language_evidence_note'})
        self.assertEqual(result['options']['style'], ['流行抒情', '摇滚与独立', '待辨识'])
        self.assertTrue(all(row['key'] is None for row in result['playlists']))
        self.assertEqual(result['playlists'][-1]['dimension'], 'review')
        public = json.dumps(result, ensure_ascii=False)
        for value in ('RAW-SECRET', '12345678', '87654321', 'A' * 32, self.ids[0], 'raw_response'):
            self.assertNotIn(value, public)

    def test_search_and_multilabel_filters_intersect_before_pagination(self):
        result = self.view(query='ＴＡＹＬＯＲ', dimension='scene', tag='通勤散步', limit=1)
        self.assertEqual([row['position'] for row in result['records']], [1])
        self.assertEqual(result['pagination'], {'offset': 0, 'limit': 1, 'total': 1, 'next_offset': None})
        self.assertEqual(result['filters'], {'query': 'ＴＡＹＬＯＲ', 'dimension': 'scene', 'tag': '通勤散步', 'review': 'all'})
        self.assertEqual(self.view(query='蔡健雅')['records'][0]['position'], 2)
        self.assertEqual([row['position'] for row in self.view(review='pending', limit=1, offset=1)['records']], [4])
        self.assertEqual(self.view(dimension='language', tag='英语', review='pending')['pagination']['total'], 0)
        self.assertEqual(self.view(dimension='all', tag='学习专注')['records'][0]['position'], 4)
        self.assertEqual(self.view(tag='不存在')['records'], [])
        self.assertEqual(self.view(offset=10)['pagination']['total'], 4)

    def test_pagination_default_fifty_and_preserves_global_summary(self):
        # A local report can contain more than one page without making new account calls.
        n = 63
        self.plan['source_track_count'] = n
        self.plan['source_track_ids'] = [f'{index:032X}' for index in range(1, n + 1)]
        self.plan['jobs'] = [{**self.plan['jobs'][2], 'candidate_track_ids': self.plan['source_track_ids']}]
        self.report['records'] = [self.row(index, f'歌曲 {index}', '离线艺人', ['流行抒情'], [], '器乐或配乐录音')
                                  for index in range(1, n + 1)]
        self.report['summary'] = {'source_count': n, 'covered_count': n, 'playlist_count': 1,
                                  'pending_count': 0, 'unknown_style_count': 0, 'unknown_language_count': 0,
                                  'review_note_count': 0, 'dimensions': {'style': 1},
                                  'playlists': [{'name': '风格 · 流行抒情', 'count': n}]}
        self.directory['liked']['track_count'] = n
        self.write(PLAN, self.plan, 100)
        self.write(REPORT, self.report, 101)
        self.write(DIRECTORY, self.directory, 105)
        first, second = self.view(), self.view(offset=50)
        self.assertEqual(len(first['records']), 50)
        self.assertEqual(first['pagination']['next_offset'], 50)
        self.assertEqual(len(second['records']), 13)
        self.assertIsNone(second['pagination']['next_offset'])
        self.assertEqual(first['summary'], second['summary'])

    def test_bad_query_types_and_bounds_raise_value_error_without_reading_files(self):
        bad = [{'offset': True}, {'offset': -1}, {'offset': 10001}, {'limit': 0}, {'limit': 101},
               {'query': []}, {'query': 'x' * 161}, {'query': '\x00'}, {'tag': {}}, {'tag': 'x' * 161},
               {'dimension': []}, {'dimension': 'bad'}, {'review': False}, {'review': 'maybe'}]
        with patch('netease_organizer.web_classification._load_directory') as directory:
            for kwargs in bad:
                with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                    self.view(**kwargs)
            directory.assert_not_called()

    def test_missing_files_are_not_loaded_but_corruption_is_unavailable(self):
        (self.project / 'artifacts' / REPORT).unlink()
        result = self.view()
        self.assertEqual(result['status'], 'not_loaded')
        self.assertIsNone(result['summary'])
        for raw in ('null', '{}', '{"kind":"classification_report","kind":"duplicate"}', '{bad', 'NaN'):
            with self.subTest(raw=raw):
                (self.project / 'artifacts' / REPORT).write_text(raw, encoding='utf-8')
                result = self.view()
                self.assertEqual(result['status'], 'unavailable')
                self.assertIsNone(result['summary'])
                self.assertEqual(result['records'], [])
        with patch('netease_organizer.web_classification.MAX_REPORT_BYTES', 64):
            self.write(REPORT, self.report, 101)
            self.assertEqual(self.view()['status'], 'unavailable')

    def test_current_directory_account_and_redheart_double_ids_and_count_bind_report(self):
        mutations = [lambda d: d['account'].update(original_id='999'),
                     lambda d: d['account'].update(id='B' * 32),
                     lambda d: d['liked'].update(id='D' * 32),
                     lambda d: d['liked'].update(original_id='999'),
                     lambda d: d['liked'].update(track_count=5),
                     lambda d: d['playlists'].append(copy.deepcopy(d['liked']))]
        for mutate in mutations:
            with self.subTest(mutate=mutate):
                directory = copy.deepcopy(self.directory)
                mutate(directory)
                self.write(DIRECTORY, directory, 105)
                self.assertEqual(self.view()['status'], 'unavailable')

    def test_bad_positions_labels_pending_summary_or_plan_membership_cannot_publish(self):
        cases = []
        for field, value in [('position', 2), ('position', True), ('styles', ['流行抒情', '流行抒情']),
                             ('styles', ['未知私有标签']), ('styles', ['待辨识', '流行抒情']),
                             ('scenes', {}), ('artists', []), ('language', []),
                             ('pending_reasons', ['语言待辨识']), ('evidence_note', '\ud800')]:
            report = copy.deepcopy(self.report)
            report['records'][0][field] = value
            cases.append((report, self.plan))
        for key in ('source_count', 'covered_count', 'playlist_count', 'pending_count', 'unknown_style_count', 'unknown_language_count'):
            report = copy.deepcopy(self.report)
            report['summary'][key] += 1
            cases.append((report, self.plan))
        report = copy.deepcopy(self.report)
        report['records'].pop()
        cases.append((report, self.plan))
        report = copy.deepcopy(self.report)
        report['summary']['playlists'][0]['count'] = 1
        cases.append((report, self.plan))
        report = copy.deepcopy(self.report)
        report['summary']['playlists'][4]['count'] = True  # JSON bool is not the one-song count.
        cases.append((report, self.plan))
        plan = copy.deepcopy(self.plan)
        plan['jobs'][4]['candidate_track_ids'] = [self.ids[1]]  # Same count, wrong English member.
        cases.append((self.report, plan))
        plan = copy.deepcopy(self.plan)
        plan['source_track_ids'][1] = plan['source_track_ids'][0]
        cases.append((self.report, plan))
        for index, (report, plan) in enumerate(cases):
            with self.subTest(index=index):
                # ensure_ascii protects deliberately malformed surrogates in the test source.
                (self.project / 'artifacts' / REPORT).write_text(json.dumps(report), encoding='utf-8')
                self.write(PLAN, plan, 100)
                self.assertEqual(self.view()['status'], 'unavailable')

    def test_only_bound_completed_evidence_and_matching_directory_marks_verified(self):
        self.complete_evidence()
        result = self.view()
        self.assertEqual(result['verification'], 'verified')
        self.assertEqual(result['verified_at'], '1970-01-01T00:01:44+00:00')
        self.assertEqual([row['key'] for row in result['playlists']], [str(900000 + i) for i in range(7)])
        self.assertNotIn('PRIVATE-URL', json.dumps(result))
        self.assertNotIn('RECEIPT-SECRET', json.dumps(result))

    def test_forged_final_strings_and_stale_receipts_never_mark_online_verified(self):
        intent, receipt, final = self.complete_evidence()
        cases = [
            (RECEIPT, {**receipt, 'run_id': 'e' * 32}),
            (RECEIPT, {**receipt, 'intent_digest': 'f' * 64}),
            (RECEIPT, {**receipt, 'plan_digest': 'f' * 64}),
            (RECEIPT, {**receipt, 'outcome_known': False}),
            (RECEIPT, {**receipt, 'record_saved': False}),
            (RECEIPT, {**receipt, 'status': 'uncertain'}),
            (FINAL, {**final, 'source_count': 5}),
            (FINAL, {**final, 'original_favorites_membership_and_order_unchanged': False}),
            (FINAL, {**final, 'verified_at': '1970-01-01T00:01:41+00:00'}),
            (FINAL, {**final, 'verified_at': '1970-01-01T00:01:46+00:00'}),
        ]
        altered = copy.deepcopy(final)
        altered['playlists'][0]['original_playlist_id'] = '999'
        cases.append((FINAL, altered))
        altered = copy.deepcopy(final)
        altered['playlists'][0]['count'] = 1
        cases.append((FINAL, altered))
        for filename, value in cases:
            with self.subTest(filename=filename, value=value.get('status')):
                self.write(RECEIPT, receipt, 103)
                self.write(FINAL, final, 104)
                self.write(filename, value, 103 if filename == RECEIPT else 104)
                result = self.view()
                self.assertEqual(result['status'], 'available')
                self.assertEqual(result['verification'], 'local_only')
                self.assertIsNone(result['verified_at'])
        self.write(RECEIPT, receipt, 103)
        self.write(FINAL, final, 104)
        directory = copy.deepcopy(self.directory)
        directory['playlists'][1]['id'] = 'E' * 32
        self.write(DIRECTORY, directory, 105)
        result = self.view()
        self.assertEqual(result['verification'], 'local_only')
        self.assertIsNone(result['playlists'][0]['key'])

    def test_completed_receipt_does_not_verify_an_attempted_or_nonfinal_intent(self):
        intent, receipt, _ = self.complete_evidence()
        for changes in ({'phase': 'add_attempted'}, {'job_index': 0, 'add_count': 0}):
            with self.subTest(changes=changes):
                altered = {**intent, **changes}
                receipt['intent_digest'] = hashlib.sha256(_encoded(altered)).hexdigest()
                self.write(INTENT, altered, 102)
                self.write(RECEIPT, receipt, 103)
                self.assertEqual(self.view()['verification'], 'local_only')

    def test_file_growth_is_read_with_a_fixed_byte_limit_and_target_symlink_is_rejected(self):
        requested = []
        original_open = Path.open
        class GrowingFile(io.BytesIO):
            def read(self, size=-1):
                requested.append(size)
                return b' ' * size
        def growing_open(path, *args, **kwargs):
            if path.name == REPORT and args == ('rb',):
                return GrowingFile()
            return original_open(path, *args, **kwargs)
        with patch('netease_organizer.web_classification.MAX_REPORT_BYTES', 4096), patch.object(Path, 'open', growing_open):
            self.assertEqual(self.view()['status'], 'unavailable')
        self.assertEqual(requested, [4097])
        path = self.project / 'artifacts' / REPORT
        path.unlink()
        target = self.project / 'source.json'
        target.write_text(json.dumps(self.report), encoding='utf-8')
        try:
            path.symlink_to(target)
        except OSError:
            return  # Windows developer-mode symlink privileges may be unavailable.
        self.assertEqual(self.view()['status'], 'unavailable')

    def test_file_change_during_projection_cannot_publish_a_mixed_generation(self):
        from netease_organizer.web_state import _load_directory as real_directory
        def change(project):
            directory = real_directory(project)
            (self.project / 'artifacts' / PLAN).write_text('{}', encoding='utf-8')
            return directory
        with patch('netease_organizer.web_classification._load_directory', side_effect=change):
            self.assertEqual(self.view()['status'], 'unavailable')

    def test_history_is_bound_whitelisted_and_includes_every_job_but_not_generic_resume(self):
        self.complete_evidence()
        history, timestamp = build_classification_history(self.project)
        self.assertEqual(timestamp, 103)
        self.assertEqual(history['operation'], 'classification')
        self.assertEqual(history['completed_count'], 7)
        self.assertEqual(len(history['items']), 7)
        self.assertFalse(history['resumable'])
        self.assertEqual(set(history['items'][0]), {'name', 'status', 'count', 'expected_count', 'added_count', 'phase'})
        public = json.dumps(history, ensure_ascii=False)
        for value in ('RECEIPT-SECRET', '12345678', 'A' * 32, '900000', 'message', 'playlist_id'):
            self.assertNotIn(value, public)
        self.write(REPORT, {}, 101)  # History does not depend on the separate public report.
        self.assertIsNotNone(build_classification_history(self.project)[0])

    def test_uncertain_history_can_show_bound_last_outcome_without_claiming_verified_or_known_success(self):
        intent, receipt, _ = self.complete_evidence()
        item = intent['items'][-1]
        item.update(status='pending', phase='adding', count=0, added_count=0)
        intent.update(completed_count=6, phase='add_attempted', outcome_known=False)
        receipt.update(status='uncertain', completed_count=6, outcome_known=False)
        receipt['items'][-1].update(status='uncertain', phase='adding', count=1, added_count=0, message='SECRET-CLI')
        receipt['intent_digest'] = hashlib.sha256(_encoded(intent)).hexdigest()
        self.write(INTENT, intent, 102)
        self.write(RECEIPT, receipt, 103)
        history, _ = build_classification_history(self.project)
        self.assertEqual(history['status'], 'uncertain')
        self.assertEqual(history['items'][-1]['count'], 1)
        self.assertEqual(history['items'][-1]['added_count'], 0)
        self.assertFalse(history['resumable'])
        self.assertNotIn('SECRET-CLI', json.dumps(history))
        self.assertEqual(self.view()['verification'], 'local_only')

    def test_history_rejects_run_digest_account_or_same_slot_identity_conflicts(self):
        intent, receipt, _ = self.complete_evidence()
        bad = [{**receipt, 'run_id': 'e' * 32}, {**receipt, 'intent_digest': 'f' * 64},
               {**receipt, 'source_observed_count': True}, {**receipt, 'record_saved': False}]
        altered = copy.deepcopy(receipt)
        altered['items'][0]['playlist_id'] = 'F' * 32
        bad.append(altered)
        for record in bad:
            self.write(RECEIPT, record, 103)
            self.assertEqual(build_classification_history(self.project), (None, 0))
        self.write(RECEIPT, receipt, 103)
        self.directory['account']['id'] = 'E' * 32
        self.write(DIRECTORY, self.directory, 105)
        self.assertEqual(build_classification_history(self.project), (None, 0))


if __name__ == '__main__':
    unittest.main()
