"""Actual loopback/OS lease acceptance, using only disposable fake projects."""

from contextlib import ExitStack
import http.client
import json
from pathlib import Path
import re
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch
from urllib.parse import urlsplit

from netease_organizer import web_launcher, web_server
from netease_organizer.web_build import load_build
from tests.test_web_server import FakeController


class WebLaunchIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.project = Path(self.temp.name)
        self.assets = self.project / "frontend/dist"
        (self.assets / "assets").mkdir(parents=True)
        self.write_frontend("A")
        self.changed = threading.Condition()
        self.stopping = threading.Event()
        self.servers, self.launches, self.opened = [], [], []
        self.patches = ExitStack()
        real_create = web_server.create_server

        def capture_server(controller, **kwargs):
            if self.stopping.is_set():
                raise RuntimeError("fake fixture is closing")
            server = real_create(controller, **kwargs)
            with self.changed:
                self.servers.append((server, controller))
                self.changed.notify_all()
            return server

        def fake_browser(project, url):
            if Path(project).resolve() != self.project.resolve():
                raise AssertionError("browser escaped temporary fake project")
            parsed = urlsplit(url)
            if parsed.hostname != "127.0.0.1" or parsed.scheme != "http":
                raise AssertionError("browser escaped fake loopback")
            with self.changed:
                self.opened.append(url)
                self.changed.notify_all()

        self.create_spy = self.patches.enter_context(
            patch.object(web_server, "create_server", side_effect=capture_server))
        self.browser_spy = self.patches.enter_context(
            patch.object(web_launcher, "_open_window", side_effect=fake_browser))

    def tearDown(self):
        self.stopping.set()
        for handle in self.launches:
            handle["controller"].release.set()
        # Only our captured fake servers are stopped, through the cooperative API.
        for server, controller in list(self.servers):
            controller.release.set()
            server.application.request_stop()
        for handle in self.launches:
            handle["thread"].join(5)
        alive = [handle["thread"].name for handle in self.launches if handle["thread"].is_alive()]
        for server, _ in list(self.servers):
            web_server.shutdown_server(server, timeout=2)
        self.patches.close()
        self.temp.cleanup()
        self.assertEqual(alive, [], "fake launcher did not finish cooperative shutdown")

    def write_frontend(self, marker):
        (self.assets / "index.html").write_text(
            '<meta name="organizer-session" content="__ORGANIZER_SESSION__">'
            '<meta name="organizer-login-job" content="__ORGANIZER_LOGIN_JOB__">'
            f'<main>fake version {marker}</main>', encoding="utf-8")
        (self.assets / "assets/main.js").write_text(f'window.fakeVersion="{marker}";', encoding="utf-8")

    def launch(self, controller, build, *, barrier=None):
        handle = {"controller": controller, "done": threading.Event(), "result": None, "error": None}

        def run():
            try:
                if barrier is not None:
                    barrier.wait(3)
                handle["result"] = web_launcher.launch_web(controller, build=build, idle_seconds=3600)
            except Exception as error:
                handle["error"] = error
            finally:
                handle["done"].set()
                with self.changed:
                    self.changed.notify_all()

        handle["thread"] = threading.Thread(target=run, name=f"fake-launch-{len(self.launches)}", daemon=False)
        self.launches.append(handle)
        handle["thread"].start()
        return handle

    @staticmethod
    def url(server):
        return f"http://127.0.0.1:{server.server_address[1]}/"

    def opened_server(self, revision, *, opened_count=1):
        with self.changed:
            def ready():
                return next((server for server, _ in self.servers
                             if server.application.revision == revision
                             and self.opened.count(self.url(server)) >= opened_count), None)

            self.assertTrue(self.changed.wait_for(ready, timeout=5),
                            "fake launcher did not publish and open the expected local version")
            return ready()

    def finished(self, handle):
        self.assertTrue(handle["done"].wait(5), "fake launcher did not finish within its deadline")
        self.assertIsNone(handle["error"], "fake launcher failed before publishing its result")
        return handle["result"]

    def request(self, server, method, path, body=None, *, session=None):
        port = server.server_address[1]
        headers = {"Origin": f"http://127.0.0.1:{port}"}
        if session is not None:
            headers["X-Organizer-Session"] = session
        if body is not None:
            headers["Content-Type"] = "application/json"
            body = json.dumps(body).encode("utf-8")
        connection = http.client.HTTPConnection("127.0.0.1", port, timeout=3)
        try:
            connection.request(method, path, body=body, headers=headers)
            response = connection.getresponse()
            return response.status, response.read()
        finally:
            connection.close()

    def page_session(self, server):
        status, page = self.request(server, "GET", "/")
        self.assertEqual(status, 200)
        return re.search(rb'name="organizer-session" content="([a-f0-9]{64})"', page)[1].decode()

    def test_idle_update_handoffs_and_keeps_the_old_frontend_snapshot(self):
        build_a = load_build(self.project)
        old_controller = FakeController(self.project)
        old_launch = self.launch(old_controller, build_a)
        old_server = self.opened_server(build_a.revision)
        self.write_frontend("B")
        build_b = load_build(self.project)
        self.assertNotEqual(build_a.revision, build_b.revision)
        self.assertEqual(self.request(old_server, "GET", "/assets/main.js"),
                         (200, b'window.fakeVersion="A";'))
        self.assertIn(b"fake version A", self.request(old_server, "GET", "/")[1])
        new_controller = FakeController(self.project)
        new_launch = self.launch(new_controller, build_b)
        new_server = self.opened_server(build_b.revision)
        self.assertEqual(self.finished(old_launch), self.url(old_server))
        self.assertNotEqual(self.url(old_server), self.url(new_server))
        self.assertEqual(self.request(new_server, "GET", "/assets/main.js"),
                         (200, b'window.fakeVersion="B";'))
        record = json.loads((self.project / ".organizer/web-instance.json").read_text(encoding="utf-8"))
        self.assertEqual((record["instance"], record["revision"]),
                         (new_server.application.instance_id, build_b.revision))
        self.assertEqual(web_launcher.read_running_instance(self.project), self.url(new_server))
        self.assertEqual(self.create_spy.call_count, 2)
        self.assertEqual(old_controller.calls, [("doctor", {})])
        self.assertEqual(new_controller.calls, [("doctor", {})])
        new_server.application.request_stop()
        self.assertEqual(self.finished(new_launch), self.url(new_server))

    def test_two_new_launchers_share_one_new_backend_after_old_cleanup(self):
        build_a = load_build(self.project)
        old_launch = self.launch(FakeController(self.project), build_a)
        old_server = self.opened_server(build_a.revision)
        self.write_frontend("B")
        build_b = load_build(self.project)
        barrier = threading.Barrier(3)
        update_barrier = threading.Barrier(2)
        real_update = web_launcher._request_update

        def synchronize_update(record, revision):
            # Both launchers have read the live A record before either may
            # send its actual control request. No HTTP response is mocked.
            update_barrier.wait(3)
            return real_update(record, revision)

        update_spy = self.patches.enter_context(
            patch.object(web_launcher, "_request_update", side_effect=synchronize_update))
        candidates = [FakeController(self.project), FakeController(self.project)]
        new_launches = [self.launch(controller, build_b, barrier=barrier) for controller in candidates]
        barrier.wait(3)
        new_server = self.opened_server(build_b.revision, opened_count=2)
        self.assertEqual(self.finished(old_launch), self.url(old_server))
        self.assertEqual(self.create_spy.call_count, 2, "concurrent update started a duplicate new backend")
        self.assertEqual(update_spy.call_count, 2)
        self.assertEqual(len([server for server, _ in self.servers
                              if server.application.revision == build_b.revision]), 1)
        # The old launch's finally has completed; the successor metadata must survive.
        record = json.loads((self.project / ".organizer/web-instance.json").read_text(encoding="utf-8"))
        self.assertEqual(record["instance"], new_server.application.instance_id)
        self.assertEqual(record["revision"], build_b.revision)
        self.assertEqual(web_launcher.read_running_instance(self.project), self.url(new_server))
        self.assertEqual(sum(controller.calls.count(("doctor", {})) for controller in candidates), 1)
        new_server.application.request_stop()
        self.assertEqual([self.finished(handle) for handle in new_launches],
                         [self.url(new_server), self.url(new_server)])

    def test_busy_update_reuses_without_pause_then_handoffs_after_readback(self):
        build_a = load_build(self.project)
        old_controller = FakeController(self.project)
        old_controller.block = True
        old_controller.request_pause = Mock(wraps=old_controller.request_pause)
        old_launch = self.launch(old_controller, build_a)
        old_server = self.opened_server(build_a.revision)
        session = self.page_session(old_server)
        self.assertEqual(self.request(old_server, "POST", "/api/actions", {"action": "rename"},
                                      session=session)[0], 202)
        self.assertTrue(old_controller.started.wait(2))
        self.write_frontend("B")
        build_b = load_build(self.project)
        new_controller = FakeController(self.project)
        refused_launch = self.launch(new_controller, build_b)
        self.assertEqual(self.finished(refused_launch), self.url(old_server))
        self.assertEqual(self.create_spy.call_count, 1)
        self.assertEqual(old_server.application.state()["update"], {"status": "busy"})
        self.assertTrue(old_server.application.busy)
        self.assertFalse(old_server.application.stop_requested)
        self.assertFalse(old_server.application.pause_requested)
        old_controller.request_pause.assert_not_called()
        self.assertEqual(new_controller.calls, [])
        self.assertEqual(self.request(old_server, "GET", "/assets/main.js"),
                         (200, b'window.fakeVersion="A";'))
        old_controller.release.set()
        self.assertTrue(old_server.application.wait_for_idle(2))
        result = old_server.application.state()["job"]["result"]
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["completed_count"], 1)
        self.assertTrue(result["applied_to_account"])
        self.assertFalse(old_server.application.stop_requested)
        old_controller.request_pause.assert_not_called()
        accepted_launch = self.launch(new_controller, build_b)
        new_server = self.opened_server(build_b.revision)
        self.assertEqual(self.finished(old_launch), self.url(old_server))
        self.assertEqual([name for name, _ in old_controller.calls], ["doctor", "rename"])
        self.assertEqual(self.create_spy.call_count, 2)
        new_server.application.request_stop()
        self.assertEqual(self.finished(accepted_launch), self.url(new_server))


if __name__ == "__main__":
    unittest.main()
