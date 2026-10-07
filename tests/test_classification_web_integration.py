import http.client
import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from netease_organizer.web_server import create_server, shutdown_server
from netease_organizer.web_state import _history, build_local_state
from test_web_server import FakeController


class ClassificationIntegrationTests(unittest.TestCase):
    def test_history_projects_all_classification_items_without_private_fields(self):
        record = {'status': 'completed', 'completed_count': 24,
                  'items': [{'name': f'分类 {i}', 'status': 'completed', 'count': 8,
                             'playlist_id': 'PRIVATE-ID', 'private_key': 'SECRET'} for i in range(24)]}
        history = _history(record, 'classification')
        self.assertIsNotNone(history)
        self.assertEqual(len(history['items']), 24)
        self.assertFalse(history['resumable'])
        self.assertNotIn('PRIVATE-ID', json.dumps(history))
        self.assertNotIn('SECRET', json.dumps(history))

    def test_history_never_grants_generic_resume_to_classification(self):
        class LocalController:
            def _last_result(self):
                return {'operation': 'classification', 'status': 'paused', 'completed_count': 1,
                        'items': [{'name': '分类', 'status': 'completed', 'count': 2}], 'resumable': True}
        with tempfile.TemporaryDirectory() as project:
            history = build_local_state(project, organizer=LocalController())['history']
        self.assertIsNotNone(history)
        self.assertEqual(history['operation'], 'classification')
        self.assertFalse(history['resumable'])

    def test_partial_history_keeps_verified_song_boundary_and_expected_count(self):
        history = _history({'status': 'uncertain', 'completed_count': 0, 'items': [{
            'name': '场景 · 通勤散步', 'status': 'partial', 'phase': 'adding', 'count': 300,
            'added_count': 300, 'expected_count': 329, 'add_offset': 999, 'private_id': 'SECRET'}]}, 'classification')
        self.assertEqual(history['items'][0], {'name': '场景 · 通勤散步', 'status': 'partial',
                          'phase': 'adding', 'count': 300, 'added_count': 300, 'expected_count': 329})


class ClassificationHttpTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.project = Path(self.temp.name)
        self.assets = self.project / 'dist'
        self.assets.mkdir()
        (self.assets / 'index.html').write_text('__ORGANIZER_SESSION__', encoding='utf-8')
        self.controller = FakeController(self.project)
        self.server = create_server(self.controller, assets=self.assets,
                                    state_provider=lambda: build_local_state(self.project))
        self.port = self.server.server_address[1]
        self.origin = f'http://127.0.0.1:{self.port}'
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={'poll_interval': .01}, daemon=True)
        self.thread.start()
        self.token = self.server.application.attach_page()

    def tearDown(self):
        shutdown_server(self.server)
        self.thread.join(2)
        self.temp.cleanup()

    def get(self, suffix='', headers=None):
        conn = http.client.HTTPConnection('127.0.0.1', self.port, timeout=3)
        actual = {'Origin': self.origin, 'X-Organizer-Session': self.token}
        actual.update(headers or {})
        conn.request('GET', '/api/classification' + suffix, headers=actual)
        response = conn.getresponse()
        value = response.status, json.loads(response.read())
        conn.close()
        return value

    def test_local_query_does_not_submit_account_task(self):
        with patch('netease_organizer.web_server.build_local_classification', return_value={'status': 'not_loaded'}) as reader:
            status, data = self.get('?offset=50&limit=20&q=JJ&dimension=style&tag=Pop&review=pending')
        self.assertEqual(status, 200)
        self.assertEqual(data['status'], 'not_loaded')
        reader.assert_called_once_with(self.project, offset=50, limit=20, query='JJ',
                                       dimension='style', tag='Pop', review='pending')
        self.assertEqual(self.controller.calls, [('doctor', {})])
        self.assertEqual(self.controller.prepared, [])
        self.assertIsNone(self.server.application.state()['job'])

    def test_bounded_parameters_and_session_are_required(self):
        for suffix in ('?offset=-1', '?limit=0', '?limit=101', '?offset=10001', '?offset=01',
                       '?q=%FF', '?q=%00', '?q='+'x'*161, '?dimension=secret', '?tag=%00',
                       '?review=yes', '?offset=0&offset=1', '?path=secret', '?q'):
            with self.subTest(suffix=suffix):
                self.assertEqual(self.get(suffix)[0], 400)
        for headers in ({'X-Organizer-Session': 'expired'}, {'Origin': 'https://elsewhere.example'},
                        {'Sec-Fetch-Site': 'cross-site'}):
            self.assertEqual(self.get(headers=headers)[0], 403)

    def test_explicit_local_refresh_accepts_all_seven_fields_without_forwarding_refresh_to_mapper(self):
        with patch('netease_organizer.web_server.build_local_classification', return_value={'status': 'not_loaded'}) as reader:
            status, data = self.get('?offset=50&limit=20&q=JJ&dimension=style&tag=Pop&review=pending&refresh=local')
        self.assertEqual(status, 200)
        self.assertEqual(data, {'status': 'not_loaded'})
        reader.assert_called_once_with(self.project, offset=50, limit=20, query='JJ',
                                       dimension='style', tag='Pop', review='pending')
        self.assertEqual(self.controller.calls, [('doctor', {})])
        self.assertIsNone(self.server.application.state()['job'])

    def test_errors_are_fixed_and_never_echo_sensitive_exception(self):
        with patch('netease_organizer.web_server.build_local_classification', side_effect=RuntimeError('SECRET-RAW')):
            status, data = self.get()
        self.assertEqual(status, 503)
        self.assertNotIn('SECRET', json.dumps(data))

    def test_classification_recovery_is_recognized_and_freezes_writes(self):
        # Submitting an action refreshes local recovery; ordinary state polling stays cached.
        with patch.object(FakeController, 'write_recovery',
                          lambda self: {'status': 'review_required', 'operation': 'classification', 'can_reconcile': False}, create=True):
            self.assertEqual(self.server.application.submit('resume', {})[0], 409)
            recovery = self.server.application.state()['recovery']
            self.assertEqual(recovery['operation'], 'classification')
