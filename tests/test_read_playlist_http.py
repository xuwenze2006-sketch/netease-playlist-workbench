import http.client
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

from netease_organizer.service import Organizer
from netease_organizer.web_server import create_server, shutdown_server, _safe_result
from netease_organizer.web_state import build_local_state
from tests.test_online_playlist import PlaylistCli
from tests.test_online import encrypted
from tests.test_write_journal import intent


class ReadPlaylistHttpTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.project = Path(self.temp.name)
        self.assets = self.project/'dist'
        self.assets.mkdir()
        (self.assets/'index.html').write_text('<meta content="__ORGANIZER_SESSION__">', encoding='utf-8')
        self.cli = PlaylistCli()
        self.cli.installed = lambda: True
        self.cli.configured = lambda: True
        self.cli.version = lambda: 'fake-only'
        reader = Mock()
        reader.load.side_effect = AssertionError('No desktop cache')
        self.owner = Organizer(self.project, cli=self.cli, reader=reader, data_dir=self.project/'cache')
        self.artifacts = self.project/'artifacts'
        self.artifacts.mkdir()
        live = {'account': {'id': encrypted(123), 'original_id': '123', 'nickname': '模拟用户'},
                'playlists': [{'id': encrypted(1000), 'original_id': '1000', 'name': 'Funk',
                               'track_count': 2, 'special_type': 0}], 'liked': None}
        (self.artifacts/'在线名称整理快照.json').write_text(json.dumps(live), encoding='utf-8')
        self.server = create_server(self.owner, assets=self.assets,
                                    state_provider=lambda: build_local_state(self.project, organizer=self.owner))
        self.port = self.server.server_address[1]
        self.origin = f'http://127.0.0.1:{self.port}'
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={'poll_interval': .01}, daemon=True)
        self.thread.start()
        self.token = self.server.application.attach_page()

    def tearDown(self):
        shutdown_server(self.server)
        self.thread.join(2)
        self.temp.cleanup()

    def request(self, method, path, body=None, headers=None):
        connection = http.client.HTTPConnection('127.0.0.1', self.port, timeout=3)
        actual = {'Origin': self.origin, 'X-Organizer-Session': self.token, 'Content-Type': 'application/json'}
        actual.update(headers or {})
        connection.request(method, path, body=json.dumps(body) if body is not None else None, headers=actual)
        response = connection.getresponse()
        value = response.status, json.loads(response.read().decode('utf-8'))
        connection.close()
        return value

    def start(self, payload=None):
        return self.request('POST', '/api/actions', {'action': 'read_playlist',
                                                   'payload': {'key': '1000'} if payload is None else payload})

    def finish(self):
        self.server.application.worker.join(3)
        self.assertFalse(self.server.application.worker.is_alive())
        return self.request('GET', '/api/state')[1]

    def test_only_explicit_valid_action_reads_and_saved_details_are_then_local(self):
        self.assertEqual(self.cli.calls, [])
        self.assertEqual(self.request('GET', '/api/playlists/1000/tracks')[1]['status'], 'not_loaded')
        self.assertEqual(self.cli.calls, [])
        status, accepted = self.start()
        self.assertEqual(status, 202)
        state = self.finish()
        job = state['job']
        self.assertEqual(job['id'], accepted['job_id'])
        self.assertEqual(job['playlist_key'], '1000')
        self.assertEqual(job['result']['playlist_key'], '1000')
        self.assertEqual(job['result']['status'], 'completed')
        self.assertTrue(job['result']['record_saved'])
        detail = self.request('GET', '/api/playlists/1000/tracks')[1]
        self.assertEqual(detail['status'], 'available')
        self.assertEqual(len(detail['tracks']), 2)
        self.assertEqual(len(self.cli.calls), 5)
        self.server.application.attach_page()
        self.assertEqual(len(self.cli.calls), 5)
        self.assertNotIn(encrypted(123), json.dumps(state))

    def test_payload_identity_cannot_be_supplied_as_encrypted_id_or_guessed_name(self):
        for payload in ({}, {'key': 1000}, {'key': True}, {'key': '01000'}, {'key': '../1000'},
                        {'key': 'Funk'}, {'key': '1'*21}, {'key': '1000', 'id': encrypted(1000)}):
            with self.subTest(payload=payload):
                self.assertEqual(self.start(payload)[0], 400)
                self.assertIsNone(self.server.application.job)
        self.assertEqual(self.cli.calls, [])

    def test_recovery_latch_survives_successful_read_and_still_blocks_all_writes(self):
        journal = intent()
        journal.update(expected_owner_id='123', owner_id=encrypted(123))
        path = self.artifacts/'名称整理执行进度.json'
        path.write_text(json.dumps(journal), encoding='utf-8')
        before = path.read_bytes()
        self.assertEqual(self.start()[0], 202)
        state = self.finish()
        self.assertTrue(state['job']['result']['record_saved'])
        self.assertEqual(state['recovery']['status'], 'review_required')
        for action, payload in [('rename', {}), ('artists', {'accept_default_visibility': True}), ('resume', {})]:
            with self.subTest(action=action):
                status, reply = self.request('POST', '/api/actions', {'action': action, 'payload': payload})
                self.assertEqual(status, 409)
                self.assertFalse(reply['accepted'])
        self.assertEqual(path.read_bytes(), before)
        self.assertEqual(len(self.cli.calls), 5)

    def test_failed_save_reports_read_target_without_unknown_account_effect(self):
        with patch.object(self.owner, '_write_artifact', side_effect=PermissionError('SECRET')):
            self.assertEqual(self.start()[0], 202)
            state = self.finish()
        result = state['job']['result']
        self.assertEqual(result['status'], 'failed')
        self.assertEqual(result['playlist_key'], '1000')
        self.assertFalse(result['record_saved'])
        self.assertFalse(result['write_attempted'])
        self.assertTrue(result['outcome_known'])
        self.assertEqual(state['recovery']['status'], 'clear')
        self.assertNotIn('SECRET', json.dumps(state))

    def test_read_actions_still_require_session_and_origin(self):
        for headers in ({'X-Organizer-Session': 'bad'}, {'Origin': 'https://elsewhere.example'}):
            with self.subTest(headers=headers):
                self.assertEqual(self.request('POST', '/api/actions',
                    {'action': 'read_playlist', 'payload': {'key': '1000'}}, headers=headers)[0], 403)
        self.assertEqual(self.cli.calls, [])

    def test_paused_read_keeps_its_public_target_and_is_safe_to_retry_explicitly(self):
        original = self.cli.overrides[self.cli.page_key(0)]
        def pause(occurrence):
            self.owner.request_pause()
            return original(occurrence)
        self.cli.overrides[self.cli.page_key(0)] = pause
        self.assertEqual(self.start()[0], 202)
        state = self.finish()
        result = state['job']['result']
        self.assertEqual((state['job']['playlist_key'], result['playlist_key']), ('1000', '1000'))
        self.assertEqual(result['status'], 'paused')
        self.assertFalse(result['record_saved'])
        self.assertFalse(result['write_attempted'])
        self.assertTrue(result['outcome_known'])
        self.assertEqual(len(self.cli.calls), 3)
        self.assertFalse((self.artifacts/'歌单明细/1000.json').exists())

    def test_worker_exception_never_loses_owned_target_or_claims_account_effect(self):
        with patch.object(self.owner, 'read_playlist', side_effect=RuntimeError('SECRET')):
            self.assertEqual(self.start()[0], 202)
            result = self.finish()['job']['result']
        self.assertEqual(result['playlist_key'], '1000')
        self.assertEqual(result['status'], 'failed')
        self.assertFalse(result['record_saved'])
        self.assertFalse(result['applied_to_account'])
        self.assertTrue(result['outcome_known'])
        self.assertNotIn('SECRET', json.dumps(result))

    def test_application_direct_read_submission_validates_before_creating_job(self):
        app = self.server.application
        self.assertEqual(app.submit('read_playlist', {'key': 1000})[0], 400)
        self.assertIsNone(app.job)
        self.assertEqual(self.cli.calls, [])

    def test_raw_result_cannot_move_read_job_to_a_different_playlist(self):
        raw = {'status': 'blocked', 'playlist_key': '9999', 'write_attempted': True,
               'applied_to_account': True, 'outcome_known': False, 'record_saved': True}
        with patch.object(self.owner, 'read_playlist', return_value=raw):
            self.assertEqual(self.start()[0], 202)
            result = self.finish()['job']['result']
        self.assertEqual(result['playlist_key'], '1000')
        self.assertFalse(result['record_saved'])
        self.assertFalse(result['write_attempted'])
        self.assertFalse(result['applied_to_account'])
        self.assertTrue(result['outcome_known'])

    def test_public_result_only_keeps_valid_public_target_and_bounded_counts(self):
        raw = {'status': 'partial', 'record_saved': True, 'outcome_known': True,
               'playlist_key': '1000', 'expected_count': 3, 'count': 2, 'missing_count': 1,
               'missing_metadata_count': 1, 'private_key': 'SECRET', 'playlist_id': encrypted(1000)}
        safe = _safe_result(raw, 'read_playlist', self.project)
        self.assertEqual(safe['playlist_key'], '1000')
        self.assertEqual(safe['missing_count'], 1)
        self.assertNotIn('SECRET', json.dumps(safe))
        self.assertNotIn('playlist_id', safe)
        for key in (True, '01000', '../1000', encrypted(1000)):
            self.assertNotIn('playlist_key', _safe_result({**raw, 'playlist_key': key}, 'read_playlist', self.project))
