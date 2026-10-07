import http.client
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from netease_organizer.web_server import create_server, shutdown_server
from netease_organizer.web_state import build_local_state
from test_web_preview import snapshot
from test_web_server import FakeController


class LocalTracksHttpTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.project = Path(self.temp.name)
        self.assets = self.project / 'dist'
        self.assets.mkdir()
        (self.assets / 'index.html').write_text('<meta content="__ORGANIZER_SESSION__">', encoding='utf-8')
        (self.project / 'artifacts').mkdir()
        (self.project / 'artifacts/在线整理快照.json').write_text(json.dumps(snapshot(track_count=121)), encoding='utf-8')
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

    def get(self, suffix='', *, key='23456789', headers=None):
        connection = http.client.HTTPConnection('127.0.0.1', self.port, timeout=3)
        actual_headers = {'Origin': self.origin, 'X-Organizer-Session': self.token}
        actual_headers.update(headers or {})
        connection.request('GET', f'/api/playlists/{key}/tracks{suffix}', headers=actual_headers)
        response = connection.getresponse()
        result = response.status, json.loads(response.read().decode('utf-8'))
        connection.close()
        return result

    def test_paged_reads_are_local_and_leave_current_job_and_write_state_untouched(self):
        before = self.server.application.state()
        status, data = self.get('?offset=50&limit=20&q=')
        self.assertEqual(status, 200)
        self.assertEqual(data['tracks'][0]['position'], 51)
        self.assertEqual(len(data['tracks']), 20)
        self.assertEqual(data['pagination']['next_offset'], 70)
        self.assertEqual(self.server.application.state(), before)
        self.assertEqual(self.controller.calls, [('doctor', {})])
        self.assertEqual(self.controller.prepared, [])

    def test_unicode_search_and_encoded_controls_have_different_results(self):
        status, data = self.get('?q=%E8%94%A1%E5%81%A5%E9%9B%85&limit=1')
        self.assertEqual(status, 200)
        self.assertEqual(data['pagination']['total'], 121)
        for suffix in ('?q=%00', '?q=%FF', '?q='+'a'*161):
            with self.subTest(suffix=suffix):
                self.assertEqual(self.get(suffix)[0], 400)

    def test_duplicate_unknown_or_out_of_range_parameters_are_rejected(self):
        for suffix in ('?offset=0&offset=50', '?limit=1&limit=2', '?path=secret', '?limit=0',
                       '?limit=101', '?offset=10001', '?offset=-1', '?limit=true', '?offset=01',
                       '?offset=0&q=x&limit=10&extra=x', '?q'):
            with self.subTest(suffix=suffix):
                status, data = self.get(suffix)
                self.assertEqual(status, 400)
                self.assertEqual(data['message'], '歌曲明细请求参数无效。')

    def test_song_reads_require_the_same_session_and_same_origin(self):
        for headers in ({'X-Organizer-Session': 'old-session'}, {'Origin': 'https://elsewhere.example'},
                        {'Sec-Fetch-Site': 'cross-site'}):
            with self.subTest(headers=headers):
                self.assertEqual(self.get(headers=headers)[0], 403)

    def test_missing_playlist_returns_fixed_not_found_without_file_access_parameters(self):
        status, data = self.get(key='99999')
        self.assertEqual(status, 404)
        self.assertEqual(data['message'], '该歌单已不在本地目录中，请更新页面后重试。')
        for key in ('0', '001', 'A'*32, '1'*21):
            self.assertEqual(self.get(key=key)[0], 400)

    def test_local_read_failure_has_a_fixed_message_and_does_not_expose_exception_text(self):
        with patch('netease_organizer.web_server.build_local_tracks', side_effect=RuntimeError('PRIVATE-TOKEN-NO-ECHO')):
            status, data = self.get()
        self.assertEqual(status, 503)
        self.assertEqual(data['message'], '本地歌曲明细暂时无法读取，请稍后重试。')
        self.assertNotIn('PRIVATE-TOKEN', json.dumps(data))

    def test_recovery_freeze_does_not_prevent_browsing_saved_songs(self):
        self.server.application._review_required = True
        self.server.application._review_reasons.add('renames')
        before = self.server.application.state()
        self.assertEqual(self.get('?limit=1')[0], 200)
        self.assertEqual(self.server.application.state(), before)
        self.assertEqual(self.controller.calls, [('doctor', {})])

    def test_metadata_filter_and_search_are_local_and_accept_four_bounded_parameters(self):
        live = snapshot(track_count=121)
        live['liked']['tracks'][54]['metadata_available'] = False
        live['liked'].update(metadata_complete=False,
                            missing_metadata_track_ids=[live['liked']['tracks'][54]['original_id']])
        live['complete'] = False
        (self.project/'artifacts/在线整理快照.json').write_text(json.dumps(live), encoding='utf-8')
        before = self.server.application.state()
        status, result = self.get('?offset=0&limit=1&q=%E8%94%A1%E5%81%A5%E9%9B%85&metadata=incomplete')
        self.assertEqual(status, 200)
        self.assertEqual(result['metadata_filter'], 'incomplete')
        self.assertEqual(result['counts']['observed'], 121)
        self.assertEqual(result['counts']['metadata_missing'], 1)
        self.assertEqual(result['pagination']['total'], 1)
        self.assertEqual(result['tracks'][0]['position'], 55)
        self.assertEqual(self.server.application.state(), before)
        self.assertEqual(self.controller.calls, [('doctor', {})])

    def test_metadata_parameter_rejects_duplicates_unknown_values_and_extra_fields(self):
        for suffix in ('?metadata=incomplete&metadata=all', '?metadata=', '?metadata=true',
                       '?metadata=INCOMPLETE', '?metadata=%20incomplete', '?metadata=%FF',
                       '?metadata=all&offset=0&limit=1&q=x&extra=y'):
            with self.subTest(suffix=suffix):
                self.assertEqual(self.get(suffix)[0], 400)
        self.assertEqual(self.controller.calls, [('doctor', {})])

    def test_all_filter_is_the_default_without_changing_unfiltered_membership(self):
        status, default = self.get('?limit=1')
        other_status, explicit = self.get('?limit=1&metadata=all')
        self.assertEqual((status, other_status), (200, 200))
        self.assertEqual(default['metadata_filter'], 'all')
        self.assertEqual(default, explicit)
