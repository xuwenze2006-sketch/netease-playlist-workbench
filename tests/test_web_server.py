import base64
import copy
import http.client
import json
from pathlib import Path
import socket
import tempfile
import threading
import time
import unittest

try:
    from netease_organizer.web_server import create_server, shutdown_server
except ModuleNotFoundError as error:
    if error.name != "netease_organizer.web_server":
        raise
    create_server = shutdown_server = None


SECRET = "PRIVATE-TOKEN-DO-NOT-ECHO"
PNG = base64.b64encode(b"\x89PNG\r\n\x1a\n" + b"fake-image").decode("ascii")


class FakeController:
    def __init__(self, project):
        self.project = project
        self.calls = []
        self.prepared = []
        self.listener = None
        self.started = threading.Event()
        self.release = threading.Event()
        self.pause = threading.Event()
        self.block = False
        self.result = {"status": "completed", "completed_count": 1,
                       "applied_to_account": True, "outcome_known": True,
                       "message": SECRET, "token": SECRET,
                       "performance": {"call_count": 2, "elapsed_seconds": 0.1, "token": SECRET},
                       "summary": {"job_count": 1, "raw": SECRET}, "raw_list": [SECRET]}

    def set_progress_listener(self, callback):
        self.listener = callback

    def prepare_operation(self, label):
        self.prepared.append(label)
        self.pause.clear()

    def request_pause(self):
        self.pause.set()
        return {"message": SECRET, "status": "pause_requested"}

    def doctor(self):
        self.calls.append(("doctor", {}))
        return {"status": "credentials_saved", "installed": True,
                "configured": True, "message": SECRET, "private_key": SECRET}

    def _call(self, action, kwargs=None):
        self.calls.append((action, kwargs or {}))
        self.started.set()
        if self.listener:
            self.listener({"phase": "checking", "label": SECRET,
                           "completed_count": 1, "total_count": 3, "raw": SECRET})
        if self.block:
            if not self.release.wait(5):
                raise RuntimeError(SECRET)
        result = copy.deepcopy(self.result)
        if self.pause.is_set():
            result["status"] = "paused"
        return result

    def online_preview(self, *, names_only=False):
        return self._call("preview", {"names_only": names_only})

    def execute_renames(self):
        return self._call("rename")

    def execute_artist_playlists(self, *, accept_default_visibility=False):
        return self._call("artists", {"accept_default_visibility": accept_default_visibility})

    def resume_last_operation(self):
        return self._call("resume")

    def login(self):
        self._call("login")
        return {"status": "authorization_pending", "authorized": False,
                "url": "https://163cn.tv/fake-authorization", "qr_png_base64": PNG, "message": SECRET}

    def login_status(self):
        self._call("login_status")
        return {"status": "authorized", "authorized": True, "message": SECRET}

    def authorization_probe(self):
        return self._call("authorization_probe")

    def discover(self):
        return self._call("discover")

    def prepare(self):
        return self._call("local_plan")

    def save_credentials(self, app_id, private_key):
        return self._call("save_credentials", {"app_id": app_id, "private_key": private_key})


class WebServerTests(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(create_server, "HTTP implementation is missing")
        self.temp = tempfile.TemporaryDirectory()
        self.project = Path(self.temp.name)
        self.assets = self.project / "assets"
        self.assets.mkdir()
        (self.assets / "index.html").write_text(
            '<meta name="organizer-session" content="__ORGANIZER_SESSION__">'
            '<meta name="organizer-login-job" content="__ORGANIZER_LOGIN_JOB__"><main>歌单工作台</main>',
            encoding="utf-8")
        (self.assets / "main.js").write_text("window.localOnly=true;", encoding="utf-8")
        (self.project / "secret.txt").write_text(SECRET, encoding="utf-8")
        self.controller = FakeController(self.project)
        self.state_calls = 0

        def provider():
            self.state_calls += 1
            return {"account": None, "playlists": [], "history": None,
                    "source": "local_record", "artists_completed": True}

        self.server = create_server(self.controller, assets=self.assets, state_provider=provider)
        self.port = self.server.server_address[1]
        self.origin = f"http://127.0.0.1:{self.port}"
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        _, _, page = self.request("GET", "/", session=False)
        self.token = page.decode().split('content="', 1)[1].split('"', 1)[0]

    def tearDown(self):
        if hasattr(self, "server"):
            self.controller.release.set()
            shutdown_server(self.server)
            self.thread.join(2)
        if hasattr(self, "temp"):
            self.temp.cleanup()

    def request(self, method, target, body=None, *, session=True, headers=None):
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=3)
        request_headers = {"Origin": self.origin}
        if session:
            request_headers["X-Organizer-Session"] = self.token
        if isinstance(body, dict):
            body = json.dumps(body).encode()
        if body is not None:
            request_headers["Content-Type"] = "application/json"
        request_headers.update(headers or {})
        connection.request(method, target, body=body, headers=request_headers)
        response = connection.getresponse()
        result = response.status, dict(response.getheaders()), response.read()
        connection.close()
        return result

    def state(self):
        status, _, body = self.request("GET", "/api/state")
        self.assertEqual(status, 200)
        return json.loads(body)

    def submit(self, action, payload=None):
        body = {"action": action}
        if payload is not None:
            body["payload"] = payload
        status, _, response = self.request("POST", "/api/actions", body)
        return status, json.loads(response)

    def test_initial_page_nonce_and_polling_are_local_cached_only(self):
        self.assertGreaterEqual(len(self.token), 32)
        self.assertNotIn("__ORGANIZER_SESSION__", self.token)
        for _ in range(3):
            state = self.state()
            self.assertIsNone(state["job"])
            self.assertEqual(state["connection"], {"installed": True, "configured": True, "authorized": None})
        self.assertEqual(self.controller.calls, [("doctor", {})])
        self.assertEqual(self.state_calls, 1)

    def test_startup_login_intent_is_explicit_document_scoped_and_consumed_once(self):
        _, _, page = self.request("GET", "/", session=False)
        self.assertIn(b'name="organizer-login-job" content=""', page)
        self.token = self.server.application.session
        # A normal page action is handled by that page's job id; a fresh page
        # must not mistake it for a new instruction to consume an old QR.
        self.assertEqual(self.submit("login")[0], 202)
        self.assertTrue(self.server.application.wait_for_idle(1))
        _, _, page = self.request("GET", "/", session=False)
        self.assertIn(b'name="organizer-login-job" content=""', page)
        status, accepted = self.server.application.submit_startup_login()
        self.assertEqual(status, 202)
        self.assertTrue(self.server.application.wait_for_idle(1))
        _, _, first = self.request("GET", "/", session=False)
        self.assertIn(('name="organizer-login-job" content="' + accepted['job_id'] + '"').encode(), first)
        self.assertNotIn(SECRET.encode(), first)
        self.assertNotIn(b'__ORGANIZER_LOGIN_JOB__', first)
        _, _, second = self.request("GET", "/", session=False)
        self.assertIn(b'name="organizer-login-job" content=""', second)
        self.assertEqual(sum(action == "login" for action, _ in self.controller.calls), 2)

    def test_expired_startup_login_intent_is_not_shown_to_new_document(self):
        self.server.application.submit_startup_login()
        self.assertTrue(self.server.application.wait_for_idle(1))
        self.server.application.startup_login_at -= 301
        _, _, page = self.request("GET", "/", session=False)
        self.assertIn(b'name="organizer-login-job" content=""', page)

    def test_manual_pause_stops_further_authorization_probes_until_explicit_login(self):
        self.assertEqual(self.submit('login')[0], 202)
        self.assertTrue(self.server.application.wait_for_idle(1))
        self.request('POST', '/api/pause', {})
        self.assertEqual(self.submit('authorization_probe')[0], 409)
        self.assertTrue(self.server.application.pause_requested)
        self.assertFalse(any(action == 'authorization_probe' for action, _ in self.controller.calls))
        self.assertEqual(self.submit('login')[0], 202)
        self.assertTrue(self.server.application.wait_for_idle(1))
        self.assertEqual(self.submit('authorization_probe')[0], 202)
        self.assertTrue(self.server.application.wait_for_idle(1))

    def test_inflight_login_url_does_not_restart_wait_after_pause(self):
        self.controller.block = True
        self.assertEqual(self.submit('login')[0], 202)
        self.assertTrue(self.controller.started.wait(1))
        self.request('POST', '/api/pause', {})
        self.controller.release.set()
        self.assertTrue(self.server.application.wait_for_idle(1))
        self.assertIsNone(self.server.application.login_started)
        self.assertEqual(self.submit('authorization_probe')[0], 409)

    def test_health_has_independent_instance_without_session_or_activity_refresh(self):
        application = self.server.application
        before = application.last_activity_at - 1
        application.last_activity_at = before
        status, _, body = self.request("GET", "/health", session=False)
        self.assertEqual(status, 200)
        health = json.loads(body)
        self.assertEqual(set(health), {"kind", "instance"})
        self.assertEqual(health["kind"], "netease-organizer-local")
        self.assertEqual(health["instance"], application.instance_id)
        self.assertNotEqual(health["instance"], self.token)
        self.assertEqual(application.last_activity_at, before)
        self.state()
        self.assertGreater(application.last_activity_at, before)
        self.assertFalse(application.stop_requested)

    def test_duplicate_headers_and_non_ascii_session_are_rejected(self):
        for duplicate in ("Host", "Origin", "X-Organizer-Session", "Content-Length", "Content-Type"):
            with self.subTest(duplicate=duplicate):
                connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=3)
                connection.putrequest("POST", "/api/actions", skip_host=True)
                headers = {"Host": f"127.0.0.1:{self.port}", "Origin": self.origin,
                           "X-Organizer-Session": self.token, "Content-Length": "19",
                           "Content-Type": "application/json"}
                for name, value in headers.items():
                    connection.putheader(name, value)
                    if name == duplicate:
                        connection.putheader(name, value)
                connection.endheaders()
                response = connection.getresponse()
                self.assertIn(response.status, (400, 403))
                response.read()
                connection.close()
        self.assertEqual(self.request("GET", "/api/state", headers={"X-Organizer-Session": "\xe9"})[0], 403)
        self.assertEqual(self.controller.calls, [("doctor", {})])

    def test_encoded_api_paths_still_require_session(self):
        for path in ("/%61pi/state", "/api%2Fstate"):
            with self.subTest(path=path):
                status, _, response = self.request("GET", path, session=False)
                self.assertEqual(status, 403)
                self.assertNotIn("connection", response.decode())

    def test_pause_cannot_cross_reset_of_an_explicit_new_job(self):
        entered, release_pause, submitted = threading.Event(), threading.Event(), threading.Event()

        def delayed_pause():
            entered.set()
            release_pause.wait(1)
            self.controller.pause.set()

        self.controller.request_pause = delayed_pause
        pause_thread = threading.Thread(target=self.server.application.request_pause)
        pause_thread.start()
        self.assertTrue(entered.wait(1))

        def submitting():
            self.server.application.submit("rename", {})
            submitted.set()

        submit_thread = threading.Thread(target=submitting)
        submit_thread.start()
        self.assertFalse(submitted.wait(0.03))
        self.assertEqual(self.controller.prepared, [])
        release_pause.set()
        pause_thread.join(1)
        submit_thread.join(1)
        self.assertTrue(self.server.application.wait_for_idle(1))
        self.assertEqual(self.state()["job"]["status"], "completed")

    def test_unstarted_server_can_be_closed_without_waiting_for_serve_forever(self):
        controller = FakeController(self.project)
        unstarted = create_server(controller, assets=self.assets)
        finished = threading.Event()

        def closing():
            shutdown_server(unstarted, timeout=0.1)
            finished.set()

        closer = threading.Thread(target=closing, daemon=True)
        closer.start()
        passed = finished.wait(0.3)
        if not passed:
            helper = threading.Thread(target=unstarted.serve_forever, daemon=True)
            helper.start()
            closer.join(1)
            helper.join(1)
        self.assertTrue(passed)

    def test_host_origin_session_and_cross_site_requests_rejected(self):
        for target, headers, session in (
                ("/api/state", {}, False), ("/api/state", {"X-Organizer-Session": "wrong"}, True),
                ("/", {"Host": "evil.example"}, False), ("/", {"Origin": "https://evil.example"}, False),
                ("/api/state", {"Origin": "null"}, True),
                ("/api/state", {"Sec-Fetch-Site": "cross-site"}, True)):
            with self.subTest(target=target, headers=headers):
                status, returned, body = self.request("GET", target, session=session, headers=headers)
                self.assertEqual(status, 403)
                self.assertNotIn("Access-Control-Allow-Origin", returned)
                self.assertNotIn(SECRET, body.decode())
        self.assertEqual(self.controller.calls, [("doctor", {})])

    def test_static_resources_cannot_escape_assets(self):
        status, headers, body = self.request("GET", "/main.js", session=False)
        self.assertEqual(status, 200)
        self.assertEqual(body, b"window.localOnly=true;")
        self.assertEqual(headers["Cache-Control"], "no-store")
        for path in ("/../secret.txt", "/%2e%2e/secret.txt", "/%5c..%5csecret.txt",
                     "/C:/secret.txt", "/secret.txt", "http://evil.example/main.js"):
            with self.subTest(path=path):
                status, _, body = self.request("GET", path, session=False)
                self.assertIn(status, (400, 403, 404))
                self.assertNotIn(SECRET, body.decode())

    def test_malformed_oversized_or_unknown_payloads_make_no_controller_calls(self):
        for body in (b"[]", b"{", b'\xff', b'{"action":"rename","action":"login"}',
                     b'{"action":"rename","payload":{"count":NaN}}', b"x" * 65537,
                     {"action": "arbitrary"}, {"action": "rename", "payload": {"path": SECRET}},
                     {"action": "save_credentials", "payload": {"app_id": "x", "file_path": SECRET}},
                     {"action": "artists", "payload": {"accept_default_visibility": 1}},
                     {"action": "artists"}):
            with self.subTest(body_type=type(body)):
                status, _, response = self.request("POST", "/api/actions", body)
                self.assertIn(status, (400, 413))
                self.assertNotIn(SECRET, response.decode())
        self.assertEqual(self.controller.calls, [("doctor", {})])
        self.assertEqual(self.controller.prepared, [])

    def test_rejected_body_discard_is_bounded_and_near_limit_returns_json(self):
        for _ in range(3):
            status, _, body = self.request("POST", "/api/actions", b"x" * 65537)
            self.assertEqual(status, 413)
            self.assertFalse(json.loads(body)["accepted"])
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=2)
        connection.putrequest("POST", "/api/actions")
        connection.putheader("Origin", self.origin)
        connection.putheader("X-Organizer-Session", self.token)
        connection.putheader("Content-Type", "application/json")
        connection.putheader("Content-Length", "99999999")
        started = time.monotonic()
        connection.endheaders()
        response = connection.getresponse()
        self.assertEqual(response.status, 413)
        response.read()
        self.assertLess(time.monotonic() - started, 0.5)
        connection.close()
        self.assertEqual(self.controller.calls, [("doctor", {})])

    def test_close_detaches_and_late_state_never_reactivates_late_action(self):
        status, _, _ = self.request("POST", "/api/close", {})
        self.assertEqual(status, 200)
        self.state()
        self.assertEqual(self.submit("rename")[0], 409)
        old = self.token
        _, _, page = self.request("GET", "/", session=False)
        self.token = page.decode().split('content="', 1)[1].split('"', 1)[0]
        self.assertNotEqual(self.token, old)
        self.assertEqual(self.submit("rename")[0], 202)
        self.assertTrue(self.server.application.wait_for_idle(1))

    def test_refresh_old_close_cannot_pause_new_document_job(self):
        old = self.token
        _, _, page = self.request("GET", "/index.html", session=False)
        self.token = page.decode().split('content="', 1)[1].split('"', 1)[0]
        self.controller.block = True
        self.assertEqual(self.submit("rename")[0], 202)
        self.assertTrue(self.controller.started.wait(1))
        status, _, _ = self.request("POST", "/api/close", {}, headers={"X-Organizer-Session": old})
        self.assertEqual(status, 403)
        self.assertFalse(self.controller.pause.is_set())
        self.assertEqual(self.state()["job"]["status"], "running")

    def test_refresh_old_close_can_only_pause_its_still_running_job(self):
        old = self.token
        self.controller.block = True
        self.assertEqual(self.submit("rename")[0], 202)
        self.assertTrue(self.controller.started.wait(1))
        _, _, page = self.request("GET", "/", session=False)
        self.token = page.decode().split('content="', 1)[1].split('"', 1)[0]
        status, _, _ = self.request("POST", "/api/close", {}, headers={"X-Organizer-Session": old})
        self.assertEqual(status, 200)
        self.assertTrue(self.controller.pause.is_set())
        self.assertFalse(self.server.application.page_closing)
        self.controller.release.set()
        self.assertTrue(self.server.application.wait_for_idle(1))
        self.assertEqual(self.submit("rename")[0], 202)
        self.assertTrue(self.server.application.wait_for_idle(1))
        self.assertEqual(self.state()["job"]["status"], "completed")

    def test_late_action_rechecks_nonce_after_guard_under_submit_lock(self):
        entered, release = threading.Event(), threading.Event()
        original = self.server.application.submit
        statuses = []

        def delayed_submit(*args, **kwargs):
            entered.set()
            release.wait(1)
            return original(*args, **kwargs)

        self.server.application.submit = delayed_submit
        requester = threading.Thread(target=lambda: statuses.append(self.submit("rename")[0]))
        requester.start()
        self.assertTrue(entered.wait(1))
        _, _, page = self.request("GET", "/", session=False)
        self.token = page.decode().split('content="', 1)[1].split('"', 1)[0]
        release.set()
        requester.join(1)
        self.assertEqual(statuses, [409])
        self.assertEqual(self.controller.calls, [("doctor", {})])

    def test_single_job_busy_pause_keeps_worker_alive_until_readback(self):
        self.controller.block = True
        status, result = self.submit("rename")
        self.assertEqual(status, 202)
        self.assertTrue(result["accepted"])
        self.assertTrue(self.controller.started.wait(1))
        self.assertFalse(self.server.application.worker.daemon)
        self.assertEqual(self.submit("preview_full")[0], 409)
        started = time.monotonic()
        status, _, response = self.request("POST", "/api/pause", {})
        self.assertEqual(status, 200)
        self.assertLess(time.monotonic() - started, 0.5)
        self.assertTrue(self.controller.pause.is_set())
        self.assertEqual(self.state()["job"]["status"], "pause_requested")
        self.assertFalse(self.server.application.wait_for_idle(0.01))
        self.controller.release.set()
        self.assertTrue(self.server.application.wait_for_idle(1))
        job_state = self.state()["job"]
        self.assertEqual(job_state["status"], "paused")
        self.assertEqual(job_state["result"]["completed_count"], 1)
        self.assertEqual(len(self.controller.prepared), 1)

    def test_result_progress_logs_and_credentials_do_not_echo_sensitive_values(self):
        self.controller.result["items"] = [{"name": SECRET, "status": "completed", "count": 1}]
        self.controller.result["last_result"] = {"source": "local_record", "operation": "artists", "status": "completed",
                                                 "items": [{"name": SECRET, "status": "completed"}]}
        self.assertEqual(self.submit("save_credentials", {"app_id": "app-1", "private_key": SECRET})[0], 202)
        self.assertTrue(self.server.application.wait_for_idle(1))
        state = self.state()
        self.assertNotIn(SECRET, json.dumps(state))
        job_state = state["job"]
        self.assertEqual(job_state["result"]["summary"], {"job_count": 1})
        self.assertEqual(set(job_state["result"]["performance"]), {"call_count", "elapsed_seconds"})
        self.assertEqual(set(job_state["progress"]), {"label", "completed_count", "total_count", "elapsed_seconds"})
        self.assertTrue(all(set(item) == {"time", "level", "message"} for item in job_state["logs"]))
        self.assertNotIn("payload", job_state)
        self.assertEqual(self.state_calls, 2)

    def test_all_fixed_actions_map_to_explicit_controller_methods(self):
        for action, payload, expected in (
                ("preview_names", None, ("preview", {"names_only": True})),
                ("preview_full", None, ("preview", {"names_only": False})),
                ("rename", None, ("rename", {})),
                ("artists", {"accept_default_visibility": True}, ("artists", {"accept_default_visibility": True})),
                ("resume", None, ("resume", {})), ("check", None, ("doctor", {})),
                ("login_status", None, ("login_status", {})), ("discover", None, ("discover", {})),
                ("local_plan", None, ("local_plan", {}))):
            with self.subTest(action=action):
                self.assertEqual(self.submit(action, payload)[0], 202)
                self.assertTrue(self.server.application.wait_for_idle(1))
                self.assertEqual(self.controller.calls[-1], expected)

    def test_login_url_and_png_are_allowed_only_for_login(self):
        self.assertEqual(self.submit("login")[0], 202)
        self.assertTrue(self.server.application.wait_for_idle(1))
        result = self.state()["job"]["result"]
        self.assertEqual(result["url"], "https://163cn.tv/fake-authorization")
        self.assertEqual(result["qr_png_base64"], PNG)
        self.controller.result.update(url=result["url"], qr_png_base64=PNG)
        self.assertEqual(self.submit("rename")[0], 202)
        self.assertTrue(self.server.application.wait_for_idle(1))
        result = self.state()["job"]["result"]
        self.assertNotIn("url", result)
        self.assertNotIn("qr_png_base64", result)

    def test_controller_exceptions_and_invalid_login_values_are_safely_reported(self):
        self.controller.login = lambda: {"status": "authorization_pending", "url": "https://evil.example/" + SECRET,
                                        "qr_png_base64": PNG, "message": SECRET}
        self.assertEqual(self.submit("login")[0], 202)
        self.assertTrue(self.server.application.wait_for_idle(1))
        result = self.state()["job"]["result"]
        self.assertNotIn("url", result)
        self.assertNotIn("qr_png_base64", result)
        self.assertEqual(self.submit("authorization_probe")[0], 409)

        def failing():
            raise RuntimeError(SECRET)

        self.controller.execute_renames = failing
        self.assertEqual(self.submit("rename")[0], 202)
        self.assertTrue(self.server.application.wait_for_idle(1))
        state = self.state()
        self.assertEqual(state["job"]["status"], "uncertain")
        self.assertFalse(state["job"]["result"]["outcome_known"])
        self.assertNotIn(SECRET, json.dumps(state))

    def test_authorization_probe_requires_login_and_does_not_reset_pause_control(self):
        self.assertEqual(self.submit("authorization_probe")[0], 409)
        self.assertEqual(self.submit("login")[0], 202)
        self.assertTrue(self.server.application.wait_for_idle(1))
        prepared = list(self.controller.prepared)
        self.assertEqual(self.submit("authorization_probe")[0], 202)
        self.assertTrue(self.server.application.wait_for_idle(1))
        self.assertEqual(self.controller.prepared, prepared)
        self.assertEqual(self.controller.calls[-1], ("authorization_probe", {}))

    def test_shutdown_requests_pause_and_waits_for_inflight_worker(self):
        self.controller.block = True
        self.assertEqual(self.submit("rename")[0], 202)
        self.assertTrue(self.controller.started.wait(1))
        shutdown_done = threading.Event()

        def closing():
            shutdown_server(self.server)
            shutdown_done.set()

        closer = threading.Thread(target=closing)
        closer.start()
        self.assertTrue(self.controller.pause.wait(1))
        self.assertFalse(shutdown_done.wait(0.05))
        self.assertTrue(self.server.application.worker.is_alive())
        self.assertEqual(self.submit("login")[0], 409)
        self.controller.release.set()
        self.assertTrue(shutdown_done.wait(2))
        closer.join(1)
        self.thread.join(1)
        self.assertFalse(self.server.application.worker.is_alive())
        self.assertEqual([call[0] for call in self.controller.calls], ["doctor", "rename"])

    def test_shutdown_timeout_retains_server_and_direct_shutdown_waits_for_worker(self):
        self.controller.block = True
        self.assertEqual(self.submit("rename")[0], 202)
        self.assertTrue(self.controller.started.wait(1))
        self.assertFalse(shutdown_server(self.server, timeout=0.01))
        self.assertTrue(self.server.application.stop_requested)
        status, _, body = self.request("GET", "/health", session=False)
        self.assertEqual(status, 503)
        self.assertEqual(json.loads(body), {"kind": "netease-organizer-stopping"})
        # The HTTP server still responds while waiting for the in-flight
        # readback, but a new document must not revive a claimed shutdown.
        old_session = self.server.application.session
        status, _, body = self.request("GET", "/", session=False)
        self.assertEqual(status, 503)
        self.assertEqual(self.server.application.session, old_session)
        self.assertNotIn(SECRET.encode(), body)
        finished = threading.Event()

        def direct_shutdown():
            self.server.shutdown()
            finished.set()

        closer = threading.Thread(target=direct_shutdown)
        closer.start()
        self.assertFalse(finished.wait(0.05))
        self.controller.release.set()
        self.assertTrue(finished.wait(2))
        closer.join(1)
        self.assertFalse(self.server.application.worker.is_alive())


    def test_shutdown_cannot_return_between_start_check_and_serving_publication(self):
        unstarted = create_server(FakeController(self.project), assets=self.assets)
        entered, release, finished = threading.Event(), threading.Event(), threading.Event()
        original = unstarted._serving

        class DelayedPublication:
            def set(self):
                entered.set()
                release.wait(1)
                original.set()

            def clear(self):
                original.clear()

            def is_set(self):
                return original.is_set()

        unstarted._serving = DelayedPublication()
        serving = threading.Thread(target=unstarted.serve_forever, daemon=True)
        serving.start()
        self.assertTrue(entered.wait(1))

        def shutting_down():
            unstarted.shutdown()
            finished.set()

        closer = threading.Thread(target=shutting_down, daemon=True)
        closer.start()
        returned_early = finished.wait(0.03)
        release.set()
        closer.join(1)
        unstarted.shutdown()
        serving.join(1)
        unstarted.server_close()
        self.assertFalse(returned_early)
        self.assertFalse(serving.is_alive())

    def test_local_doctor_never_proves_live_authorization(self):
        controller = FakeController(self.project)
        controller.doctor = lambda: {"installed": True, "configured": True,
                                     "authorized": True, "status": "credentials_saved"}
        unstarted = create_server(controller, assets=self.assets)
        try:
            self.assertIsNone(unstarted.application.state()["connection"]["authorized"])
        finally:
            shutdown_server(unstarted)


if __name__ == "__main__":
    unittest.main()
