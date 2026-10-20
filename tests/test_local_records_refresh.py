import copy
import http.client
import json
import os
from pathlib import Path
import threading
import unittest
from unittest.mock import patch

from netease_organizer.web_server import WorkbenchApplication, create_server, shutdown_server
from netease_organizer.web_state import build_local_state
from tests import test_web_classification as classification_fixture
from tests.test_web_server import FakeController


class LocalRecordsRefreshTests(unittest.TestCase):
    def setUp(self):
        self.records = classification_fixture.WebClassificationTests()
        self.records.setUp()
        self.addCleanup(self.records.doCleanups)
        self.project = self.records.project.resolve()
        assets = self.project / 'dist'
        assets.mkdir()
        (assets / 'index.html').write_text('__ORGANIZER_SESSION__', encoding='utf-8')
        self.controller = FakeController(self.project)
        self.provider_calls = 0
        def provider():
            self.provider_calls += 1
            return build_local_state(self.project)
        self.provider = provider
        self.server = create_server(self.controller, assets=assets, state_provider=provider)
        self.app = self.server.application
        self.port = self.server.server_address[1]
        self.token = self.app.attach_page()
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={'poll_interval': .01}, daemon=True)
        self.thread.start()
        self.provider_calls = 0

    def tearDown(self):
        self.controller.release.set()
        shutdown_server(self.server)
        self.thread.join(3)

    def get(self, path='/api/classification?refresh=local'):
        connection = http.client.HTTPConnection('127.0.0.1', self.port, timeout=3)
        connection.request('GET', path, headers={
            'Origin': f'http://127.0.0.1:{self.port}', 'X-Organizer-Session': self.token})
        response = connection.getresponse()
        result = response.status, json.loads(response.read())
        connection.close()
        return result

    def test_explicit_local_refresh_publishes_external_completed_history_immediately(self):
        before = self.app.state()
        self.assertIsNone(before['data']['history'])
        self.records.complete_evidence()
        status, result = self.get()
        self.assertEqual(status, 200)
        self.assertEqual(result['verification'], 'verified')
        after = self.app.state()
        self.assertEqual(after['data']['history']['operation'], 'classification')
        self.assertEqual(after['data']['history']['status'], 'completed')
        self.assertEqual(after['data']['history']['completed_count'], 7)
        self.assertEqual(len(after['data']['playlists']), 8)
        for key in ('job', 'connection', 'recovery', 'update'):
            self.assertEqual(after[key], before[key])
        self.assertEqual(self.provider_calls, 1)
        self.assertEqual(self.controller.calls, [('doctor', {})])

    def test_pending_generation_rejects_http_refresh_without_replacing_cached_state(self):
        self.records.complete_evidence()
        self.assertEqual(self.get()[0], 200)
        before = self.app.state()
        marker = self.project / 'artifacts/分类生成未完成.json'
        marker.write_text('incomplete generation', encoding='utf-8')
        status, response = self.get()
        self.assertEqual(status, 503)
        self.assertFalse(response['accepted'])
        self.assertEqual(self.app.state(), before)
        marker.unlink()
        status, response = self.get()
        self.assertEqual(status, 200)
        self.assertEqual(response['verification'], 'verified')
        self.assertEqual(self.controller.calls, [('doctor', {})])

    def test_valid_pending_diagnostic_preserves_get_status_refresh_cache_and_unknown_write_freeze(self):
        self.records.complete_evidence()
        self.assertEqual(self.get()[0], 200)
        self.app.job = {'id': 'old-job', 'action': 'rename', 'status': 'uncertain',
                        'result': {'status': 'uncertain', 'write_attempted': True, 'outcome_known': False}}
        self.app._review_required = True
        self.app._review_reasons = {'renames'}
        self.app.recovery = {'status': 'review_required', 'operation': 'renames', 'can_reconcile': True}
        before = self.app.state()
        self.records.write('分类生成未完成.json', {
            'kind': 'classification_generation_pending', 'version': 1, 'mode': 'build'}, 110)
        status, response = self.get('/api/classification')
        self.assertEqual(status, 200)
        self.assertEqual(response['status'], 'unavailable')
        self.assertEqual(response.get('error_code'), 'classification_generation_incomplete')
        self.assertEqual(response['records'], [])
        self.assertEqual(self.app.state(), before)
        status, response = self.get()
        self.assertEqual(status, 503)
        self.assertFalse(response['accepted'])
        self.assertEqual(response.get('error_code'), 'classification_generation_incomplete')
        self.assertIn('生成', response['message'])
        self.assertEqual(set(response), {'accepted', 'error_code', 'message'})
        self.assertEqual(self.app.state(), before)
        self.assertTrue(self.app._review_required)
        self.assertEqual(self.app._review_reasons, {'renames'})
        self.assertEqual(self.controller.calls, [('doctor', {})])
        self.assertEqual(self.controller.prepared, [])

    def test_pending_diagnostic_does_not_mask_private_provider_failure_or_mid_refresh_changes(self):
        self.records.write('分类生成未完成.json', {
            'kind': 'classification_generation_pending', 'version': 1, 'mode': 'build'}, 110)
        before = self.app.state()

        def denied_provider():
            raise PermissionError('PRIVATE-provider')

        self.app.state_provider = denied_provider
        status, response = self.get()
        self.assertEqual(status, 503)
        self.assertNotIn('error_code', response)
        self.assertNotIn('PRIVATE', json.dumps(response))
        self.assertEqual(self.app.state(), before)
        self.app.state_provider = self.provider
        from netease_organizer.web_server import build_local_classification as read

        def changed_after_read(project, **kwargs):
            result = read(project, **kwargs)
            (self.project / 'artifacts/分类生成未完成.json').unlink()
            return result

        with patch('netease_organizer.web_server.build_local_classification', side_effect=changed_after_read):
            status, response = self.get()
        self.assertEqual(status, 503)
        self.assertNotIn('error_code', response)
        self.assertEqual(self.app.state(), before)

    def test_invalid_pending_schema_http_refresh_remains_generic_and_preserves_cache(self):
        before = self.app.state()
        self.records.write('分类生成未完成.json', {
            'kind': 'classification_generation_pending', 'version': True, 'mode': 'build',
            'private': 'PRIVATE-MARKER'}, 110)
        status, response = self.get()
        self.assertEqual(status, 503)
        self.assertNotIn('error_code', response)
        self.assertNotIn('PRIVATE', json.dumps(response))
        self.assertEqual(self.app.state(), before)

    def test_pending_appearing_during_http_refresh_cannot_publish_new_state(self):
        before = self.app.state()
        marker = self.project / 'artifacts/分类生成未完成.json'
        def changing_provider():
            state = self.provider()
            marker.write_text('generation started during refresh', encoding='utf-8')
            return state
        self.app.state_provider = changing_provider
        self.assertEqual(self.get()[0], 503)
        self.assertEqual(self.app.state(), before)
        marker.unlink()
        self.app.state_provider = self.provider
        self.assertEqual(self.get()[0], 200)
        self.assertEqual(self.controller.calls, [('doctor', {})])
        self.assertEqual(self.controller.prepared, [])

    def test_normal_classification_queries_and_state_polling_never_refresh_cached_records(self):
        before = self.app.state()['data']
        self.records.complete_evidence()
        for path in ('/api/classification', '/api/classification?q=Rain&dimension=style&tag=流行抒情', '/api/state'):
            # ASCII query encoding is enough for this cache boundary test.
            if '流行抒情' in path:
                path = path.replace('流行抒情', '%E6%B5%81%E8%A1%8C%E6%8A%92%E6%83%85')
            self.assertEqual(self.get(path)[0], 200)
        self.assertEqual(self.app.state()['data'], before)
        self.assertEqual(self.provider_calls, 0)
        self.assertEqual(self.controller.calls, [('doctor', {})])

    def test_invalid_refresh_and_query_parameters_do_not_call_provider(self):
        before = self.app.state()['data']
        for suffix in ('refresh=', 'refresh=online', 'refresh=local&refresh=local', 'refresh=true',
                       'refresh=local&limit=101', 'refresh=local&offset=10001', 'refresh=local&q=%00',
                       'refresh=local&dimension=secret', 'refresh=local&review=secret', 'refresh=local&extra=x'):
            with self.subTest(suffix=suffix):
                self.assertEqual(self.get('/api/classification?' + suffix)[0], 400)
        self.assertEqual(self.provider_calls, 0)
        self.assertEqual(self.app.state()['data'], before)

    def test_busy_or_stopping_refresh_is_rejected_without_pause_or_cache_changes(self):
        self.controller.block = True
        self.assertEqual(self.app.submit('preview_full', {}, session=self.token)[0], 202)
        self.assertTrue(self.controller.started.wait(2))
        before = self.app.state()['data']
        with patch.object(self.controller, 'request_pause', wraps=self.controller.request_pause) as pause:
            self.assertEqual(self.get()[0], 409)
            self.assertEqual(self.provider_calls, 0)
            self.assertEqual(self.app.state()['data'], before)
            self.assertFalse(self.app.pause_requested)
            pause.assert_not_called()
        self.controller.release.set()
        self.assertTrue(self.app.wait_for_idle(3))
        calls = self.provider_calls
        self.app.stopping = self.app.stop_requested = True
        with patch.object(self.controller, 'request_pause', wraps=self.controller.request_pause) as pause:
            self.assertEqual(self.get()[0], 409)
            self.assertEqual(self.provider_calls, calls)
            pause.assert_not_called()

    def test_failed_provider_or_bad_schema_does_not_publish_fallback_empty_data(self):
        before = self.app.state()
        def failed():
            raise RuntimeError('PRIVATE-RAW-TOKEN')
        for provider in (failed, lambda: [], lambda: {'source': 'unavailable'},
                         lambda: {**before['data'], 'source': 'unavailable'},
                         lambda: {**before['data'], 'history': []},
                         lambda: {**before['data'], 'account': {}},
                         lambda: {**before['data'], 'playlists': [{}]},
                         lambda: {**before['data'], 'artists_completed': 1}):
            with self.subTest(provider=provider):
                self.app.state_provider = provider
                status, response = self.get()
                self.assertEqual(status, 503)
                self.assertNotIn('PRIVATE', json.dumps(response))
                self.assertEqual(self.app.state(), before)

    def test_existing_damaged_directory_preserves_cache_without_classification_records(self):
        for name in (classification_fixture.PLAN, classification_fixture.REPORT):
            (self.project / 'artifacts' / name).unlink()
        directory = self.project / 'artifacts' / classification_fixture.DIRECTORY
        for provider in (self.provider, None):
            self.app.state_provider = provider
            for damaged in ('{PRIVATE-incomplete', json.dumps({'account': 'PRIVATE-bad-schema'})):
                with self.subTest(provider='callable' if provider else 'default', damaged=damaged):
                    self.records.write(classification_fixture.DIRECTORY, self.records.directory, 105)
                    self.app.data = build_local_state(self.project)
                    before = self.app.state()
                    directory.write_text(damaged, encoding='utf-8')
                    status, response = self.get()
                    self.assertEqual(status, 503)
                    self.assertNotIn('PRIVATE', json.dumps(response))
                    self.assertEqual(self.app.state(), before)
                    self.records.write(classification_fixture.DIRECTORY, self.records.directory, 105)
                    self.assertEqual(self.get()[0], 200)
                    self.assertEqual(self.app.state()['data']['account']['nickname'], '离线用户')
                    self.assertEqual(len(self.app.state()['data']['playlists']), 1)
        self.assertEqual(self.controller.calls, [('doctor', {})])

    def test_public_state_stays_compatible_while_explicit_refresh_preserves_cache(self):
        for name in (classification_fixture.PLAN, classification_fixture.REPORT):
            (self.project / 'artifacts' / name).unlink()
        before = self.app.state()
        (self.project / 'artifacts' / classification_fixture.DIRECTORY).write_text('{incomplete', encoding='utf-8')
        self.assertEqual(build_local_state(self.project)['source'], 'empty')
        startup = WorkbenchApplication(FakeController(self.project), state_provider=self.provider)
        self.assertEqual(startup.state()['data']['source'], 'empty')
        self.assertTrue(startup.state()['connection']['installed'])
        self.assertEqual(self.get()[0], 503)
        self.assertEqual(self.app.state(), before)
        self.assertEqual(self.app.submit('preview_names', {}, session=self.token)[0], 202)
        self.assertTrue(self.app.wait_for_idle(3))
        status, state = self.get('/api/state')
        self.assertEqual(status, 200)
        self.assertEqual(state['data']['source'], 'empty')
        self.assertTrue(state['connection']['installed'])
        self.assertEqual(state['job']['status'], 'completed')

    def test_directory_read_permission_failure_preserves_cache_and_allows_retry(self):
        for name in (classification_fixture.PLAN, classification_fixture.REPORT):
            (self.project / 'artifacts' / name).unlink()
        directory = self.project / 'artifacts' / classification_fixture.DIRECTORY
        directory_key = os.path.normcase(os.path.abspath(directory))
        original_open = Path.open
        providers = (('callable', self.provider), ('default', None),
                     ('aliased-project', lambda: build_local_state(self.project / 'dist' / '..')))
        for provider_name, provider in providers:
            with self.subTest(provider=provider_name):
                self.app.state_provider = provider
                before = self.app.state()
                denied = False
                def denied_once(path, *args, **kwargs):
                    nonlocal denied
                    if os.path.normcase(os.path.abspath(path)) == directory_key and not denied:
                        denied = True
                        raise PermissionError('PRIVATE-snapshot-read-denied')
                    return original_open(path, *args, **kwargs)
                with patch.object(Path, 'open', denied_once):
                    status, response = self.get()
                self.assertTrue(denied, 'snapshot read failure must be injected')
                self.assertEqual(status, 503)
                self.assertNotIn('PRIVATE', json.dumps(response))
                self.assertEqual(self.app.state(), before)
                self.assertEqual(self.get()[0], 200)
                self.assertEqual(self.app.state()['data']['account']['nickname'], '离线用户')
        self.assertEqual(self.controller.calls, [('doctor', {})])

    def test_confirmed_directory_removal_and_empty_project_can_refresh_and_restore(self):
        for name in (classification_fixture.PLAN, classification_fixture.REPORT,
                     classification_fixture.DIRECTORY):
            (self.project / 'artifacts' / name).unlink()
        for provider in (self.provider, None):
            with self.subTest(provider='callable' if provider else 'default'):
                self.app.state_provider = provider
                for _ in range(2):
                    status, response = self.get()
                    self.assertEqual(status, 200)
                    self.assertEqual(response['status'], 'not_loaded')
                    self.assertEqual(self.app.state()['data']['source'], 'empty')
                    self.assertEqual(self.app.state()['data']['playlists'], [])
                    self.assertIsNone(self.app.state()['data']['account'])
                self.records.write(classification_fixture.DIRECTORY, self.records.directory, 105)
                self.assertEqual(self.get()[0], 200)
                self.assertEqual(self.app.state()['data']['source'], 'local_record')
                self.assertEqual(len(self.app.state()['data']['playlists']), 1)
                (self.project / 'artifacts' / classification_fixture.DIRECTORY).unlink()
        self.assertEqual(self.controller.calls, [('doctor', {})])

    def test_valid_alternate_directory_still_refreshes_when_other_snapshot_is_damaged(self):
        for name in (classification_fixture.PLAN, classification_fixture.REPORT):
            (self.project / 'artifacts' / name).unlink()
        (self.project / 'artifacts' / classification_fixture.DIRECTORY).write_text('{incomplete', encoding='utf-8')
        self.records.write('在线整理快照.json', self.records.directory, 106)
        for provider in (self.provider, None):
            with self.subTest(provider='callable' if provider else 'default'):
                self.app.state_provider = provider
                self.assertEqual(self.get()[0], 200)
                self.assertEqual(self.app.state()['data']['source'], 'local_record')
                self.assertEqual(self.app.state()['data']['account']['nickname'], '离线用户')

    def test_unreachable_project_root_preserves_cache_and_recovers_when_restored(self):
        for name in (classification_fixture.PLAN, classification_fixture.REPORT):
            (self.project / 'artifacts' / name).unlink()
        moved = self.project.with_name(self.project.name + '-directory-read-test')
        for provider in (self.provider, None):
            with self.subTest(provider='callable' if provider else 'default'):
                self.app.state_provider = provider
                before = self.app.state()
                self.project.rename(moved)
                try:
                    status, _ = self.get()
                    self.assertEqual(status, 503)
                    self.assertEqual(self.app.state(), before)
                finally:
                    moved.rename(self.project)
                self.assertEqual(self.get()[0], 200)
                self.assertEqual(self.app.state()['data']['account']['nickname'], '离线用户')

    def test_unknown_write_latch_and_original_job_are_preserved_even_if_classification_now_complete(self):
        self.app.job = {'id': 'old-job', 'action': 'rename', 'status': 'uncertain',
                        'result': {'status': 'uncertain', 'write_attempted': True, 'outcome_known': False}}
        self.app._review_required = True
        self.app._review_reasons = {'renames'}
        self.app.recovery = {'status': 'review_required', 'operation': 'renames', 'can_reconcile': True}
        self.app.update_status = 'review_required'
        self.app.pause_requested = True
        self.app.connection['authorized'] = False
        before = self.app.state()
        self.records.complete_evidence()
        self.assertEqual(self.get()[0], 200)
        after = self.app.state()
        for key in ('job', 'connection', 'recovery', 'update'):
            self.assertEqual(after[key], before[key])
        self.assertTrue(self.app._review_required)
        self.assertEqual(self.app._review_reasons, {'renames'})
        self.assertTrue(self.app.pause_requested)
        self.assertEqual(self.app.session, self.token)
        for action in ('rename', 'artists', 'resume'):
            self.assertEqual(self.app.submit(action, {}, session=self.token)[0], 409)
        self.assertEqual(self.controller.calls, [('doctor', {})])

    def test_directory_or_report_version_changes_fail_closed_before_cache_commit(self):
        before = self.app.state()['data']
        def change_identity():
            snapshot = self.provider()
            directory = copy.deepcopy(self.records.directory)
            directory['account']['id'] = 'B' * 32
            self.records.write(classification_fixture.DIRECTORY, directory, 106)
            return snapshot
        self.app.state_provider = change_identity
        self.assertEqual(self.get()[0], 503)
        self.assertEqual(self.app.state()['data'], before)

        self.records.write(classification_fixture.DIRECTORY, self.records.directory, 105)
        from netease_organizer.web_server import build_local_classification as read
        def change_report(*args, **kwargs):
            result = read(*args, **kwargs)
            changed = copy.deepcopy(self.records.report)
            changed['records'][0]['name'] = '本地版本发生变化'
            self.records.write(classification_fixture.REPORT, changed, 108)
            return result
        self.app.state_provider = self.provider
        with patch('netease_organizer.web_server.build_local_classification', side_effect=change_report):
            self.assertEqual(self.get()[0], 503)
        self.assertEqual(self.app.state()['data'], before)

    def test_rename_intent_change_is_guarded_even_when_classification_files_are_stable(self):
        before = self.app.state()['data']
        def change_rename_intent():
            candidate = self.provider()
            self.records.write('名称整理执行进度.json', {'phase': 'rename_attempted'}, 109)
            return candidate
        self.app.state_provider = change_rename_intent
        self.assertEqual(self.get()[0], 503)
        self.assertEqual(self.app.state()['data'], before)

    def test_session_changed_during_local_read_cannot_publish_cache(self):
        before = self.app.state()['data']
        def change_session():
            snapshot = self.provider()
            self.app.attach_page()
            return snapshot
        self.app.state_provider = change_session
        status, result = self.get()
        self.assertEqual(status, 403)
        self.assertEqual(self.app.state()['data'], before)
        self.assertNotEqual(self.app.session, self.token)
        self.assertNotIn(self.app.session, json.dumps(result))

    def test_no_provider_uses_only_declared_local_history_and_never_doctor_or_account_commands(self):
        self.records.complete_evidence()
        self.app.state_provider = None
        local_calls = []
        def local_history(controller):
            local_calls.append('history')
            return None
        with patch.object(FakeController, '_last_result', local_history, create=True):
            self.assertEqual(self.get()[0], 200)
        self.assertEqual(local_calls, ['history'])
        self.assertEqual(self.app.state()['data']['history']['completed_count'], 7)
        self.assertEqual(self.controller.calls, [('doctor', {})])

    def test_refresh_and_job_submission_share_one_idle_transaction(self):
        entered, release, submission_started = threading.Event(), threading.Event(), threading.Event()
        results = []
        def blocking_provider():
            entered.set()
            if not release.wait(2):
                raise RuntimeError('bounded fake timeout')
            self.assertFalse(self.app.busy)
            return self.provider()
        self.app.state_provider = blocking_provider
        refreshing = threading.Thread(target=lambda: results.append(('refresh', self.get()[0])))
        def submit():
            submission_started.set()
            results.append(('submit', self.app.submit('preview_names', {}, session=self.token)[0]))
        submitting = threading.Thread(target=submit)
        try:
            refreshing.start()
            self.assertTrue(entered.wait(2))
            submitting.start()
            self.assertTrue(submission_started.wait(2))
            self.assertFalse(self.controller.started.is_set())
        finally:
            release.set()
            refreshing.join(3)
            if submitting.ident is not None:
                submitting.join(3)
        self.assertFalse(refreshing.is_alive())
        self.assertFalse(submitting.is_alive())
        self.assertIn(('refresh', 200), results)
        self.assertIn(('submit', 202), results)

    def test_slow_refresh_keeps_state_and_stop_responsive_and_cannot_publish_after_stop(self):
        entered, release, observed = threading.Event(), threading.Event(), threading.Event()
        results = []
        before = self.app.state()['data']
        self.records.complete_evidence()

        def blocking_provider():
            entered.set()
            if not release.wait(5):
                raise RuntimeError('bounded fake timeout')
            return self.provider()

        def inspect_and_stop():
            results.append(('state', self.app.state()['data']))
            self.app.request_stop()
            observed.set()

        self.app.state_provider = blocking_provider
        refreshing = threading.Thread(target=lambda: results.append(('refresh', self.get()[0])))
        observing = threading.Thread(target=inspect_and_stop)
        try:
            refreshing.start()
            self.assertTrue(entered.wait(2))
            observing.start()
            self.assertTrue(observed.wait(1), 'local disk reads must not hold the state/stop lock')
        finally:
            release.set()
            refreshing.join(3)
            if observing.ident is not None:
                observing.join(3)
        self.assertIn(('state', before), results)
        self.assertIn(('refresh', 409), results)
        self.assertEqual(self.app.state()['data'], before)

    def test_new_page_can_attach_during_slow_refresh_and_discards_old_generation(self):
        entered, release, attached = threading.Event(), threading.Event(), threading.Event()
        results = []
        before = self.app.state()['data']
        self.records.complete_evidence()

        def blocking_provider():
            entered.set()
            if not release.wait(5):
                raise RuntimeError('bounded fake timeout')
            return self.provider()

        def attach():
            self.app.attach_page()
            attached.set()

        self.app.state_provider = blocking_provider
        refreshing = threading.Thread(target=lambda: results.append(self.get()[0]))
        attaching = threading.Thread(target=attach)
        try:
            refreshing.start()
            self.assertTrue(entered.wait(2))
            attaching.start()
            self.assertTrue(attached.wait(1), 'new documents must not wait for disk refresh')
        finally:
            release.set()
            refreshing.join(3)
            if attaching.ident is not None:
                attaching.join(3)
        self.assertEqual(results, [403])
        self.assertEqual(self.app.state()['data'], before)
        self.assertTrue(self.app.wait_for_idle(3))


if __name__ == '__main__':
    unittest.main()
