import concurrent.futures
import http.client
import json
from pathlib import Path
import re
import tempfile
import threading
from types import MappingProxyType
import unittest
from unittest.mock import Mock

from netease_organizer.web_server import create_server, shutdown_server
from tests.test_web_server import FakeController, SECRET


REVISION = "a" * 64
NEXT_REVISION = "b" * 64
CONTROL = "c" * 64
INDEX = (b'<meta name="organizer-session" content="__ORGANIZER_SESSION__">'
         b'<meta name="organizer-login-job" content="__ORGANIZER_LOGIN_JOB__">snapshot')


class WebUpdateTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.project = Path(self.temp.name)
        self.assets = self.project / "assets"
        self.assets.mkdir()
        (self.assets / "index.html").write_bytes(INDEX)
        (self.assets / "main.js").write_bytes(b"disk-original")
        self.servers = []
        self.start()

    def start(self, **kwargs):
        controller = FakeController(self.project)
        server = create_server(controller, assets=self.assets, revision=REVISION,
                               control_key=CONTROL, **kwargs)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.servers.append((server, controller, thread))
        self.server, self.controller = server, controller
        self.port = server.server_address[1]
        self.origin = f"http://127.0.0.1:{self.port}"
        status, body = self.request("GET", "/", session=False)
        self.assertEqual(status, 200)
        self.session = re.search(rb'name="organizer-session" content="([a-f0-9]{64})"', body)[1].decode()

    def tearDown(self):
        for server, controller, thread in self.servers:
            controller.release.set()
            shutdown_server(server)
            thread.join(2)
        self.temp.cleanup()

    def request(self, method, path, body=None, *, session=True, control=False, headers=None):
        request_headers = {"Origin": self.origin}
        if session:
            request_headers["X-Organizer-Session"] = self.session
        if control:
            request_headers["X-Organizer-Control"] = CONTROL
        if isinstance(body, dict):
            body = json.dumps(body).encode("utf-8")
        if body is not None:
            request_headers["Content-Type"] = "application/json"
        request_headers.update(headers or {})
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=3)
        try:
            connection.request(method, path, body=body, headers=request_headers)
            response = connection.getresponse()
            return response.status, response.read()
        finally:
            connection.close()

    def update(self, **kwargs):
        body = kwargs.pop("body", {"instance": self.server.application.instance_id,
                                   "revision": NEXT_REVISION})
        status, raw = self.request("POST", "/api/update", body, session=False, control=True, **kwargs)
        return status, json.loads(raw)

    def state(self):
        status, body = self.request("GET", "/api/state")
        self.assertEqual(status, 200)
        return json.loads(body)

    def submit(self, action="rename"):
        status, body = self.request("POST", "/api/actions", {"action": action})
        self.assertEqual(status, 202, body)
        return json.loads(body)

    def test_revision_is_birth_value_and_control_key_never_public(self):
        status, raw = self.request("GET", "/health", session=False)
        self.assertEqual(status, 200)
        health = json.loads(raw)
        self.assertEqual(health, {"kind": "netease-organizer-local",
                                 "instance": self.server.application.instance_id,
                                 "revision": REVISION, "update_status": "none"})
        state = self.state()
        self.assertEqual(state["update"], {"status": "none"})
        self.assertNotIn(CONTROL, json.dumps(state) + raw.decode())
        self.assertEqual(self.controller.calls, [("doctor", {})])

    def test_idle_update_claims_stop_without_pause_or_session_rotation(self):
        session = self.session
        self.assertEqual(self.update(), (202, {"accepted": True, "reason": "stopping"}))
        app = self.server.application
        self.assertTrue(app.stopping)
        self.assertTrue(app.stop_requested)
        self.assertFalse(app.pause_requested)
        self.assertFalse(self.controller.pause.is_set())
        self.assertEqual(app.session, session)
        self.assertEqual(self.state()["update"], {"status": "stopping"})
        status, body = self.request("GET", "/health", session=False)
        self.assertEqual(status, 503)
        self.assertEqual(json.loads(body)["revision"], REVISION)
        self.assertEqual(json.loads(body)["update_status"], "stopping")
        status, _ = self.request("POST", "/api/actions", {"action": "rename"})
        self.assertEqual(status, 409)
        self.assertEqual(self.controller.calls, [("doctor", {})])

    def test_wrong_control_or_page_nonce_cannot_request_update(self):
        for headers in ({"X-Organizer-Control": "d" * 64},
                        {"X-Organizer-Control": self.session}, {}):
            with self.subTest(headers=list(headers)):
                status, body = self.request("POST", "/api/update",
                                            {"instance": self.server.application.instance_id,
                                             "revision": NEXT_REVISION}, headers=headers)
                self.assertEqual(status, 403)
                self.assertNotIn(CONTROL, body.decode())
                self.assertFalse(self.server.application.stop_requested)
        status, _ = self.request("POST", "/api/actions", {"action": "rename"},
                                 session=False, control=True)
        self.assertEqual(status, 403)

    def test_control_route_retains_host_origin_and_fetch_guards(self):
        for headers in ({"Host": "example.invalid"}, {"Origin": "https://example.invalid"},
                        {"Sec-Fetch-Site": "cross-site"}):
            with self.subTest(headers=headers):
                self.assertEqual(self.update(headers=headers)[0], 403)
                self.assertFalse(self.server.application.stop_requested)

    def test_duplicate_control_header_is_rejected(self):
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=3)
        body = json.dumps({"instance": self.server.application.instance_id,
                           "revision": NEXT_REVISION}).encode()
        try:
            connection.putrequest("POST", "/api/update")
            connection.putheader("Origin", self.origin)
            connection.putheader("X-Organizer-Control", CONTROL)
            connection.putheader("X-Organizer-Control", CONTROL)
            connection.putheader("Content-Type", "application/json")
            connection.putheader("Content-Length", str(len(body)))
            connection.endheaders(body)
            response = connection.getresponse()
            self.assertEqual(response.status, 403)
            response.read()
        finally:
            connection.close()
        self.assertFalse(self.server.application.stop_requested)

    def test_wrong_instance_and_current_revision_leave_server_running(self):
        self.assertEqual(self.update(body={"instance": "e" * 32, "revision": NEXT_REVISION}),
                         (409, {"accepted": False, "reason": "instance_mismatch"}))
        self.assertEqual(self.update(body={"instance": self.server.application.instance_id,
                                          "revision": REVISION}),
                         (200, {"accepted": False, "reason": "current"}))
        self.assertFalse(self.server.application.stop_requested)
        self.assertEqual(self.state()["update"], {"status": "none"})

    def test_exact_bounded_update_body_and_post_only(self):
        valid = {"instance": self.server.application.instance_id, "revision": NEXT_REVISION}
        for raw in ([], {**valid, "extra": SECRET}, {"instance": valid["instance"]},
                    {**valid, "revision": True}, {**valid, "instance": "x" * 32},
                    {**valid, "revision": "a" * 63},
                    b'{"instance":"' + valid["instance"].encode() + b'","revision":"' +
                    NEXT_REVISION.encode() + b'","revision":"' + REVISION.encode() + b'"}'):
            with self.subTest(raw_type=type(raw).__name__):
                if isinstance(raw, list):
                    raw = json.dumps(raw).encode()
                status, body = self.request("POST", "/api/update", raw,
                                            session=False, control=True)
                self.assertEqual(status, 400)
                self.assertNotIn(SECRET, body.decode())
                self.assertFalse(self.server.application.stop_requested)
        self.assertEqual(self.request("GET", "/api/update", session=False, control=True)[0], 404)
        self.assertFalse(self.server.application.stop_requested)

    def test_busy_write_finishes_readback_before_a_new_update_request(self):
        pause_spy = Mock(wraps=self.controller.request_pause)
        self.controller.request_pause = pause_spy
        self.controller.block = True
        self.submit()
        self.assertTrue(self.controller.started.wait(1))
        worker = self.server.application.worker
        self.assertFalse(worker.daemon)
        self.assertEqual(self.update(), (409, {"accepted": False, "reason": "busy"}))
        self.assertEqual(self.state()["update"], {"status": "busy"})
        self.assertFalse(self.server.application.stop_requested)
        self.assertFalse(self.server.application.pause_requested)
        self.assertFalse(self.controller.pause.is_set())
        self.assertFalse(self.server.application.wait_for_idle(0.01))
        self.controller.release.set()
        self.assertTrue(self.server.application.wait_for_idle(2))
        self.assertEqual(self.state()["job"]["result"]["completed_count"], 1)
        self.assertEqual(self.state()["update"], {"status": "none"})
        self.assertFalse(self.server.application.stop_requested)
        self.assertEqual([name for name, _ in self.controller.calls], ["doctor", "rename"])
        pause_spy.assert_not_called()
        self.assertEqual(self.update()[0], 202)
        pause_spy.assert_not_called()

    def test_unsafe_write_results_require_review_without_stop(self):
        for result in ({"status": "uncertain"},
                       {"status": "partial", "write_attempted": True, "outcome_known": False},
                       {"status": "completed", "record_saved": False, "applied_to_account": True}):
            with self.subTest(result=result):
                self.start()
                self.controller.result = result
                self.submit()
                self.assertTrue(self.server.application.wait_for_idle(2))
                self.assertEqual(self.update(), (409, {"accepted": False, "reason": "review_required"}))
                self.assertEqual(self.state()["update"], {"status": "review_required"})
                self.assertFalse(self.server.application.stop_requested)
                self.assertFalse(self.controller.pause.is_set())

    def test_known_result_with_unsaved_record_and_no_account_effect_can_stop(self):
        self.controller.result = {"status": "blocked", "record_saved": False,
                                  "applied_to_account": False, "outcome_known": True}
        self.submit()
        self.assertTrue(self.server.application.wait_for_idle(2))
        self.assertEqual(self.update()[0], 202)

    def test_unknown_write_review_survives_a_later_successful_read(self):
        self.controller.result = {"status": "uncertain", "write_attempted": True,
                                  "outcome_known": False}
        self.submit()
        self.assertTrue(self.server.application.wait_for_idle(2))
        self.assertEqual(self.state()["update"], {"status": "review_required"})
        self.submit("check")
        self.assertTrue(self.server.application.wait_for_idle(2))
        self.assertEqual(self.state()["job"]["status"], "completed")
        self.assertEqual(self.update(), (409, {"accepted": False, "reason": "review_required"}))
        self.assertFalse(self.server.application.stop_requested)
        self.assertFalse(self.controller.pause.is_set())

    def assert_write_freeze_survives_check_and_new_document(self, result):
        pause_spy = Mock(wraps=self.controller.request_pause)
        self.controller.request_pause = pause_spy
        self.controller.result = result
        self.submit("rename")
        self.assertTrue(self.server.application.wait_for_idle(2))
        self.submit("check")
        self.assertTrue(self.server.application.wait_for_idle(2))
        old_session = self.session
        status, page = self.request("GET", "/", session=False)
        self.assertEqual(status, 200)
        self.session = re.search(rb'name="organizer-session" content="([a-f0-9]{64})"', page)[1].decode()
        self.assertNotEqual(old_session, self.session)
        previous_job = self.state()["job"]
        previous_prepared = list(self.controller.prepared)
        for action in ("rename", "artists", "resume"):
            with self.subTest(action=action):
                body = {"action": action}
                if action == "artists":
                    body["payload"] = {"accept_default_visibility": True}
                status, raw = self.request("POST", "/api/actions", body)
                self.assertEqual(status, 409)
                self.assertEqual(json.loads(raw), {
                    "accepted": False, "message": "上次账号操作仍需核对，请先查看本地记录，勿重复提交。"})
                self.assertEqual(self.state()["job"], previous_job)
                self.assertEqual(self.controller.prepared, previous_prepared)
                self.assertEqual(self.state()["update"], {"status": "review_required"})
        self.assertEqual([name for name, _ in self.controller.calls], ["doctor", "rename", "doctor"])
        self.controller.result = {"status": "online_plan_ready"}
        self.submit("preview_names")
        self.assertTrue(self.server.application.wait_for_idle(2))
        self.assertEqual(self.state()["job"]["result"]["status"], "online_plan_ready")
        self.assertEqual(self.state()["update"], {"status": "review_required"})
        pause_spy.assert_not_called()
        self.assertFalse(self.server.application.stop_requested)

    def test_unknown_rename_cannot_replay_any_write_after_check_and_refresh(self):
        self.assert_write_freeze_survives_check_and_new_document(
            {"status": "uncertain", "write_attempted": True, "outcome_known": False})

    def test_unsaved_confirmed_effect_cannot_replay_any_write_after_check_and_refresh(self):
        self.assert_write_freeze_survives_check_and_new_document(
            {"status": "completed", "record_saved": False, "applied_to_account": True,
             "write_attempted": True, "outcome_known": True})

    def test_blocked_write_with_no_effect_does_not_freeze_later_write(self):
        self.controller.result = {"status": "blocked", "applied_to_account": False,
                                  "write_attempted": False, "outcome_known": True, "record_saved": False}
        self.submit("rename")
        self.assertTrue(self.server.application.wait_for_idle(2))
        self.assertEqual(self.state()["update"], {"status": "none"})
        self.submit("check")
        self.assertTrue(self.server.application.wait_for_idle(2))
        _, page = self.request("GET", "/", session=False)
        self.session = re.search(rb'name="organizer-session" content="([a-f0-9]{64})"', page)[1].decode()
        self.controller.result = {"status": "completed", "applied_to_account": True,
                                  "write_attempted": True, "outcome_known": True}
        self.submit("rename")
        self.assertTrue(self.server.application.wait_for_idle(2))
        self.assertEqual(self.state()["job"]["result"]["status"], "completed")
        self.assertEqual([name for name, _ in self.controller.calls], ["doctor", "rename", "doctor", "rename"])

    def test_update_and_submit_claim_are_atomic(self):
        pause_spy = Mock(wraps=self.controller.request_pause)
        self.controller.request_pause = pause_spy
        self.controller.block = True
        barrier = threading.Barrier(2)

        def action():
            barrier.wait(1)
            return self.request("POST", "/api/actions", {"action": "rename"})[0]

        def update():
            barrier.wait(1)
            return self.update()[0]

        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            action_future, update_future = pool.submit(action), pool.submit(update)
            statuses = action_future.result(3), update_future.result(3)
        self.assertIn(statuses, ((202, 409), (409, 202)))
        self.controller.release.set()
        self.assertTrue(self.server.application.wait_for_idle(2))
        writes = [name for name, _ in self.controller.calls if name == "rename"]
        self.assertEqual(len(writes), 1 if statuses[0] == 202 else 0)
        if writes:
            self.assertFalse(self.server.application.stop_requested)
        pause_spy.assert_not_called()

    def test_static_snapshot_survives_disk_updates_and_caller_mutation(self):
        snapshot = {"index.html": INDEX, "assets/main.js": b"snapshot-script"}
        self.start(asset_snapshot=snapshot)
        (self.assets / "index.html").write_bytes(b"new-disk-index")
        (self.assets / "only-disk.js").write_bytes(b"new-disk-script")
        snapshot["assets/main.js"] = b"caller-mutated"
        snapshot["only-disk.js"] = b"caller-added"
        self.assertEqual(self.request("GET", "/assets/main.js", session=False),
                         (200, b"snapshot-script"))
        self.assertEqual(self.request("GET", "/only-disk.js", session=False)[0], 404)
        status, body = self.request("GET", "/index.html", session=False)
        self.assertEqual(status, 200)
        self.assertIn(b"snapshot", body)
        self.assertNotIn(b"new-disk-index", body)
        self.assertNotIn(b"__ORGANIZER_SESSION__", body)
        self.assertNotIn(CONTROL.encode(), body)

    def test_build_readonly_mapping_is_accepted_and_copied(self):
        source = {"index.html": INDEX, "main.js": b"immutable-build"}
        self.start(asset_snapshot=MappingProxyType(source))
        source["main.js"] = b"changed-after-start"
        self.assertEqual(self.request("GET", "/main.js", session=False), (200, b"immutable-build"))

    def test_snapshot_keeps_static_path_whitelist_and_login_bootstrap(self):
        self.start(asset_snapshot={"index.html": INDEX, "assets/main.js": b"safe"})
        for path in ("/%2e%2e/secret.txt", "/assets%5cmain.js", "/assets/../main.js", "/secret.txt"):
            with self.subTest(path=path):
                status, body = self.request("GET", path, session=False)
                self.assertIn(status, (400, 404))
                self.assertNotIn(SECRET.encode(), body)
        self.assertEqual(self.server.application.submit_startup_login()[0], 202)
        self.assertTrue(self.server.application.wait_for_idle(2))
        job_id = self.server.application.job["id"]
        _, page = self.request("GET", "/", session=False)
        self.assertIn(f'content="{job_id}"'.encode(), page)
        _, next_page = self.request("GET", "/", session=False)
        self.assertIn(b'name="organizer-login-job" content=""', next_page)

    def test_constructor_rejects_invalid_update_config_before_any_controller_calls(self):
        for kwargs in ({"revision": "x" * 64}, {"control_key": True},
                       {"asset_snapshot": {"../index.html": INDEX}},
                       {"asset_snapshot": {"/index.html": INDEX}},
                       {"asset_snapshot": {"secret.txt": b"bad"}},
                       {"asset_snapshot": {"index.html": "not bytes"}}):
            with self.subTest(keys=list(kwargs)):
                controller = FakeController(self.project)
                with self.assertRaises(ValueError):
                    create_server(controller, assets=self.assets, **kwargs)
                self.assertEqual(controller.calls, [])


if __name__ == "__main__":
    unittest.main()
