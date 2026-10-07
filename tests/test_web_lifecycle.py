"""Real loopback lifecycle with a fake controller; no account or browser calls."""

import http.client
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from netease_organizer.web_launcher import launch_web
from netease_organizer.web_server import create_server
from test_web_server import FakeController


class WebLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.project = Path(self.temp.name)
        assets = self.project / 'frontend/dist'
        assets.mkdir(parents=True)
        (assets / 'index.html').write_text(
            '<meta name="organizer-session" content="__ORGANIZER_SESSION__">', encoding='utf-8')
        self.controller = FakeController(self.project)
        self.server = None
        self.finished = threading.Event()
        self.ready = threading.Event()
        self.failures = []

    def run_launcher(self, *, open_page=False):
        def make_server(*args, **kwargs):
            self.server = create_server(*args, **kwargs)
            if not open_page:
                self.ready.set()
            return self.server

        def open_document(*args):
            self.document_status = self.get('/')[0]
            self.ready.set()

        def run():
            try:
                with patch('netease_organizer.web_server.create_server', side_effect=make_server), \
                        patch('netease_organizer.web_launcher._open_window', side_effect=open_document):
                    launch_web(self.controller, open_window=open_page, idle_seconds=0.05)
            except Exception as error:
                self.failures.append(type(error).__name__)
            finally:
                self.finished.set()

        self.launcher = threading.Thread(target=run)
        self.launcher.start()
        self.addCleanup(self.stop_launcher)
        self.assertTrue(self.ready.wait(2), 'Local fake service did not start')

    def stop_launcher(self):
        if self.server is not None:
            self.server.application.request_stop()
        self.launcher.join(timeout=3)
        self.assertFalse(self.launcher.is_alive(), 'Fake launcher did not stop cleanly')
        self.assertEqual(self.failures, [])

    def get(self, path):
        client = http.client.HTTPConnection('127.0.0.1', self.server.server_address[1], timeout=2)
        try:
            client.request('GET', path)
            response = client.getresponse()
            return response.status, response.read()
        finally:
            client.close()

    def test_open_page_survives_no_polling_past_idle_deadline(self):
        self.run_launcher(open_page=True)
        self.assertEqual(self.document_status, 200)
        # A real document is open, but a sleeping renderer sends no heartbeat.
        # The old unconditional deadline closes the server within this bound.
        self.assertFalse(self.finished.wait(0.85), 'Open sleeping page lost its backend')
        self.assertEqual(self.get('/health')[0], 200)
        self.assertEqual(self.controller.calls, [('doctor', {})])

    def test_service_without_an_opened_page_still_exits_after_idle_deadline(self):
        self.run_launcher()
        self.assertTrue(self.finished.wait(2), 'Unattached service remained running')
        self.assertEqual(self.controller.calls, [('doctor', {})])

    def test_closed_page_exits_after_grace_and_a_reopened_page_cancels_exit(self):
        self.server = create_server(self.controller, assets=self.project / 'frontend/dist')
        self.addCleanup(self.server.server_close)
        app = self.server.application
        session = app.attach_page()
        app.last_activity_at = 0
        app.close_page(session)
        # Reload before the deadline cancels the previous document's close.
        new_session = app.attach_page()
        self.assertIsInstance(new_session, str)
        app.last_activity_at = 0
        self.assertFalse(app.claim_idle_stop(300, now=1000))
        self.assertFalse(app.stopping)
        app.close_page(new_session)
        self.assertTrue(app.claim_idle_stop(300, now=1000))
        self.assertTrue(app.stopping)
        self.assertTrue(app.stop_requested)
        self.assertEqual(app.bootstrap_page(), (None, ''))

    def test_old_document_close_cannot_expire_a_new_open_document(self):
        self.server = create_server(self.controller, assets=self.project / 'frontend/dist')
        self.addCleanup(self.server.server_close)
        app = self.server.application
        old_session = app.attach_page()
        new_session = app.attach_page()
        app.last_activity_at = 0
        self.assertEqual(app.close_page(old_session)[0], 403)
        self.assertFalse(app.claim_idle_stop(300, now=1000))
        self.assertTrue(app.valid_session(new_session))


if __name__ == '__main__':
    unittest.main()
