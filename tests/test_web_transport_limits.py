import http.client
import json
from pathlib import Path
import socket
import tempfile
import threading
import unittest
from unittest.mock import patch

from netease_organizer.web_server import _WorkbenchServer, create_server, shutdown_server


class _Controller:
    project = None

    def doctor(self):
        return {}

    def set_progress_listener(self, callback):
        pass

    def request_pause(self):
        pass

    def prepare_operation(self, label):
        pass

    def execute_renames(self):
        return {'status': 'completed', 'outcome_known': True, 'items': [
            {'name': 'bad\ud800', 'old_name': 'bad\udfff', 'status': 'completed'},
            {'name': '歌曲 🎵', 'status': 'completed'},
        ]}


class WebTransportLimitsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.assets = Path(self.temp.name)
        (self.assets / 'index.html').write_text('__ORGANIZER_SESSION__', encoding='utf-8')
        self.sockets = []
        self.server = None
        self.serving = None

    def tearDown(self):
        for connection in self.sockets:
            connection.close()
        if self.server is not None:
            shutdown_server(self.server)
            self.serving.join(2)

    def start_server(self, *, timeout=0.3, limit=16):
        with patch.object(_WorkbenchServer, 'request_timeout', timeout, create=True), \
                patch.object(_WorkbenchServer, 'request_limit', limit, create=True):
            self.server = create_server(_Controller(), assets=self.assets)
        # Per-instance values keep test deadlines independent of other servers.
        self.server.request_timeout = timeout
        self.server.request_limit = limit
        self.serving = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.serving.start()
        self.port = self.server.server_address[1]

    def connect(self):
        connection = socket.create_connection(('127.0.0.1', self.port), timeout=1)
        self.sockets.append(connection)
        return connection

    def health(self):
        connection = http.client.HTTPConnection('127.0.0.1', self.port, timeout=1)
        try:
            connection.request('GET', '/health')
            response = connection.getresponse()
            response.read()
            return response.status
        finally:
            connection.close()

    def drip(self, connection):
        stop = threading.Event()

        def sending():
            while not stop.wait(0.04):
                try:
                    connection.sendall(b' ')
                except OSError:
                    break

        sender = threading.Thread(target=sending, daemon=True)
        sender.start()
        return stop, sender

    def test_result_surrogates_cannot_break_repeated_state_responses(self):
        self.start_server()
        self.assertEqual(self.server.application.submit('rename', {})[0], 202)
        self.assertTrue(self.server.application.wait_for_idle(1))
        for _ in range(2):
            connection = http.client.HTTPConnection('127.0.0.1', self.port, timeout=1)
            try:
                connection.request('GET', '/api/state', headers={
                    'X-Organizer-Session': self.server.application.session,
                })
                try:
                    response = connection.getresponse()
                    status, body = response.status, response.read()
                except http.client.RemoteDisconnected:
                    status, body = None, b''
                self.assertEqual(status, 200, 'An invalid result name must not close every state request.')
                items = json.loads(body)['job']['result']['items']
                self.assertNotIn('name', items[0])
                self.assertNotIn('old_name', items[0])
                self.assertEqual(items[1]['name'], '歌曲 🎵')
            finally:
                connection.close()

    def test_slow_header_has_total_receive_deadline(self):
        self.start_server()
        connection = self.connect()
        connection.sendall(f'GET /health HTTP/1.1\r\nHost: 127.0.0.1:{self.port}'.encode())
        stop, sender = self.drip(connection)
        try:
            connection.settimeout(0.9)
            try:
                closed = connection.recv(1) == b''
            except (ConnectionResetError, ConnectionAbortedError):
                closed = True
            except TimeoutError:
                closed = False
            self.assertTrue(closed, 'Receiving one byte at a time must not renew the header deadline.')
        finally:
            stop.set()
            sender.join(1)
            connection.close()
        self.assertEqual(self.health(), 200)

    def test_slow_json_body_has_total_receive_deadline(self):
        self.start_server()
        connection = self.connect()
        connection.sendall((f'POST /api/actions HTTP/1.1\r\nHost: 127.0.0.1:{self.port}\r\n'
                            f'X-Organizer-Session: {self.server.application.session}\r\n'
                            'Content-Type: application/json\r\nContent-Length: 65536\r\n\r\n{').encode())
        stop, sender = self.drip(connection)
        try:
            connection.settimeout(0.9)
            response = http.client.HTTPResponse(connection)
            try:
                response.begin()
                status = response.status
                response.read()
            except TimeoutError:
                status = None
            finally:
                response.close()
            self.assertEqual(status, 400, 'An incomplete JSON body must be rejected within the total deadline.')
            self.assertIsNone(self.server.application.job)
        finally:
            stop.set()
            sender.join(1)
            connection.close()

    def test_connection_limit_rejects_overflow_and_releases_slots(self):
        self.start_server(timeout=2, limit=2)
        active = 0
        peak = 0
        changed = threading.Condition()
        original = self.server.process_request_thread

        def observe(request, address):
            nonlocal active, peak
            with changed:
                active += 1
                peak = max(peak, active)
                changed.notify_all()
            try:
                return original(request, address)
            finally:
                with changed:
                    active -= 1
                    changed.notify_all()

        self.server.process_request_thread = observe
        occupied = [self.connect(), self.connect()]
        for connection in occupied:
            connection.sendall(b'G')
        with changed:
            self.assertTrue(changed.wait_for(lambda: active == 2, timeout=1))
        overflow = self.connect()
        overflow.sendall(f'GET /health HTTP/1.1\r\nHost: 127.0.0.1:{self.port}\r\n\r\n'.encode())
        try:
            rejected = overflow.recv(1) == b''
        except (ConnectionResetError, ConnectionAbortedError):
            rejected = True
        self.assertTrue(rejected, 'An over-capacity connection must not create another handler thread.')
        for connection in occupied:
            connection.close()
        with changed:
            self.assertTrue(changed.wait_for(lambda: active == 0, timeout=1))
        self.assertEqual(self.health(), 200)
        self.assertLessEqual(peak, 2)
        self.assertFalse(self.server.daemon_threads)

    def test_receive_deadline_does_not_cancel_dispatched_worker(self):
        self.start_server(timeout=0.1)
        started, release, paused = threading.Event(), threading.Event(), threading.Event()

        def operation():
            started.set()
            release.wait(2)
            return {'status': 'completed', 'outcome_known': True}

        self.server.application.controller.execute_renames = operation
        self.server.application.controller.request_pause = paused.set
        connection = http.client.HTTPConnection('127.0.0.1', self.port, timeout=1)
        try:
            connection.request('POST', '/api/actions', body=json.dumps({'action': 'rename'}), headers={
                'Content-Type': 'application/json',
                'X-Organizer-Session': self.server.application.session,
            })
            response = connection.getresponse()
            response.read()
            self.assertEqual(response.status, 202)
            self.assertTrue(started.wait(1))
            self.assertFalse(self.server.application.wait_for_idle(0.3))
            self.assertFalse(paused.is_set())
            self.assertFalse(self.server.application.worker.daemon)
        finally:
            connection.close()
            release.set()
        self.assertTrue(self.server.application.wait_for_idle(1))
        self.assertEqual(self.server.application.state()['job']['status'], 'completed')

    def test_slow_client_cannot_keep_server_close_waiting(self):
        self.start_server()
        entered = threading.Event()
        original = self.server.finish_request

        def observe(request, address):
            entered.set()
            return original(request, address)

        self.server.finish_request = observe
        connection = self.connect()
        connection.sendall(b'GET /health HTTP/1.1\r\nHost: ')
        stop, sender = self.drip(connection)
        finished = threading.Event()

        def closing():
            shutdown_server(self.server)
            finished.set()

        closer = threading.Thread(target=closing, daemon=True)
        try:
            self.assertTrue(entered.wait(1))
            closer.start()
            self.assertTrue(finished.wait(0.9), 'Slow receiving must not indefinitely block non-daemon cleanup.')
        finally:
            stop.set()
            sender.join(1)
            connection.close()
            if closer.ident is not None:
                closer.join(2)


if __name__ == '__main__':
    unittest.main()
