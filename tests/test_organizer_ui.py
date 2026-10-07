"""Verify GUI boundaries without creating a desktop window."""

import unittest
import base64
import queue
import threading
from pathlib import Path
from unittest.mock import patch

from netease_organizer.ui import (
    authorization_confirmed, authorization_probe_decision, check_onboarding,
    onboarding_followup, private_key_source, public_error_message, result_lines,
    safe_authorization_url, safe_qr_png_base64, OrganizerWindow, _TaskResult,
)


class UserFacingError(Exception):
    pass


class OnboardingController:
    def __init__(self, configured=True, authorized=False, discovery_error=None):
        self.configured = configured
        self.authorized = authorized
        self.operations = []
        self.discovery_error = discovery_error

    def doctor(self):
        self.operations.append("doctor")
        return {"configured": self.configured, "status": "credentials_saved"}

    def login_status(self):
        self.operations.append("login_status")
        return {"authorized": self.authorized, "status": "authorized" if self.authorized else "authorization_required"}

    def discover(self):
        self.operations.append("discover")
        if self.discovery_error is not None:
            raise self.discovery_error
        return {"status": "schema_available", "command_count": 3}


class MemoryVariable:
    def __init__(self, value=""):
        self.value = value

    def set(self, value):
        self.value = value

    def get(self):
        return self.value


class MemoryFrame:
    def __init__(self):
        self.visible = True

    def grid(self, **options):
        self.visible = True

    def grid_remove(self):
        self.visible = False


class PendingCallbacks:
    def __init__(self):
        self.pending = {"old-probe": object()}

    def after_cancel(self, identifier):
        self.pending.pop(identifier)

    def after(self, interval, callback):
        identifier = f"callback-{len(self.pending)}"
        self.pending[identifier] = callback
        return identifier

    def destroy(self):
        self.destroyed = True


class MemoryQrWindow:
    def __init__(self, children=()):
        self.destroyed = False
        self.children = children

    def destroy(self):
        self.destroyed = True
        for child in self.children:
            child.destroyed = True


class MemoryButton:
    def __init__(self):
        self.state = "normal"
        self.destroyed = False
        self.text = ""

    def configure(self, *, state=None, text=None):
        if self.destroyed:
            raise RuntimeError("Destroyed button was kept in the task controls")
        if state is not None:
            self.state = state
        if text is not None:
            self.text = text


class MemoryProgress:
    def __init__(self):
        self.options = {}

    def configure(self, **options):
        self.options.update(options)

    def start(self, interval):
        pass

    def stop(self):
        pass


def waiting_qr_window():
    window = OrganizerWindow.__new__(OrganizerWindow)
    window.root = PendingCallbacks()
    window._closed = False
    window._busy = False
    window._probe_deadline = 500.0
    window._probe_after_id = "old-probe"
    window._authorization_url = MemoryVariable("https://163cn.tv/old-authorization")
    button = MemoryButton()
    window._qr_refresh_button = button
    window._qr_window = MemoryQrWindow((button,))
    window._qr_image = object()
    window._buttons = [button]
    window._credential_entries = []
    window._progress = MemoryProgress()
    window.logs = []
    window.requests = []
    window._append_log = lambda lines: window.logs.extend(lines)
    window._start_task = lambda action, operation: window.requests.append(action)
    return window


class OrganizerUiHelperTests(unittest.TestCase):
    def test_official_https_authorization_url_is_preserved(self):
        url = "https://music.163.com/oauth/authorize?state=example"
        self.assertEqual(safe_authorization_url(url), url)
        self.assertEqual(
            safe_authorization_url("https://developer.music.163.com/st/developer/apply/account?type=INDIVIDUAL"),
            "https://developer.music.163.com/st/developer/apply/account?type=INDIVIDUAL",
        )

    def test_official_cli_short_authorization_url_is_preserved(self):
        self.assertEqual(safe_authorization_url("https://163cn.tv/local-example"), "https://163cn.tv/local-example")
        for value in ("http://163cn.tv/local-example", "https://163cn.tv.evil.test/local-example",
                      "https://evil.163cn.tv/local-example", "https://secret@163cn.tv/local-example",
                      "https://163cn.tv:444/local-example", "https://evil.test\\.163cn.tv/local-example"):
            with self.subTest(value=value):
                self.assertEqual(safe_authorization_url(value), "")

    def test_authorization_url_rejects_impostors_and_embedded_credentials(self):
        for value in (
            "http://music.163.com/oauth",
            "https://music.163.com.example.org/oauth",
            "https://example.org/music.163.com",
            "https://secret@music.163.com/oauth",
            "https://evil.test\\.music.163.com/oauth",
            "https://bad_host.music.163.com/oauth",
            "https://music.163.com/oauth\x7fprivate-key=secret",
            "https://music.163.com/oauth\nprivate-key=secret",
            "file:///C:/credentials.json",
            None,
            {"url": "https://music.163.com/oauth"},
        ):
            with self.subTest(value=value):
                self.assertEqual(safe_authorization_url(value), "")

    def test_unknown_exception_does_not_expose_raw_message(self):
        result = public_error_message(RuntimeError("PRIVATE-KEY-WOULD-LEAK"), UserFacingError)
        self.assertNotIn("PRIVATE-KEY", result)
        self.assertIn("未完成", result)

    def test_public_error_explains_next_step_and_redacts_credential(self):
        result = public_error_message(
            UserFacingError("请检查接入。错误文本意外包含 PRIVATE-KEY。"),
            UserFacingError,
            sensitive_values=("PRIVATE-KEY",),
        )
        self.assertIn("请检查接入", result)
        self.assertNotIn("PRIVATE-KEY", result)

    def test_result_summary_uses_public_fields_only(self):
        lines = result_lines("生成整理清单", {
            "status": "prepared", "message": "已生成本地清单。", "job_count": 3,
            "path": "D:/artifacts/整理清单.json", "private_key": "SECRET-PRIVATE-KEY",
            "raw": {"token": "SECRET-TOKEN"},
        })
        rendered = "\n".join(lines)
        self.assertIn("3", rendered)
        self.assertIn("D:/artifacts/整理清单.json", rendered)
        self.assertNotIn("SECRET", rendered)
        self.assertNotIn("raw", rendered)

    def test_result_summary_redacts_sensitive_values_even_in_public_message(self):
        rendered = "\n".join(result_lines("保存凭证", {
            "status": "saved", "message": "已保存 PRIVATE-KEY 和 APP-ID。",
        }, sensitive_values=("PRIVATE-KEY", "APP-ID")))
        self.assertNotIn("PRIVATE-KEY", rendered)
        self.assertNotIn("APP-ID", rendered)

    def test_unknown_exception_is_safe_without_a_declared_public_error_type(self):
        self.assertNotIn("SECRET", public_error_message(RuntimeError("SECRET")))

    def test_selected_private_key_file_wins_without_reading_its_content(self):
        file_path = Path("D:/not-opened-by-the-ui/开发凭证.pem")
        kind, value = private_key_source(file_path, "PASTED-PRIVATE-KEY")
        self.assertEqual(kind, "file")
        self.assertEqual(value, file_path)

    def test_private_key_paste_fallback_preserves_pem_line_breaks(self):
        kind, value = private_key_source(None, "\n-----BEGIN PRIVATE KEY-----\nSECRET\n-----END PRIVATE KEY-----\n")
        self.assertEqual(kind, "text")
        self.assertEqual(value, "-----BEGIN PRIVATE KEY-----\nSECRET\n-----END PRIVATE KEY-----")

    def test_missing_private_key_source_explains_both_input_options(self):
        with self.assertRaises(ValueError) as raised:
            private_key_source(None, "  ")
        self.assertIn("文件", str(raised.exception))
        self.assertIn("粘贴", str(raised.exception))

    def test_manual_onboarding_check_does_not_probe_without_credentials(self):
        controller = OnboardingController(configured=False)
        check_onboarding(controller)
        self.assertEqual(controller.operations, ["doctor"])

    def test_manual_onboarding_check_probes_once_and_waits_for_authorization(self):
        controller = OnboardingController(authorized=False)
        check_onboarding(controller)
        self.assertEqual(controller.operations, ["doctor", "login_status"])

    def test_manual_onboarding_check_discovers_only_after_confirmed_authorization(self):
        controller = OnboardingController(authorized=True)
        results = check_onboarding(controller)
        self.assertEqual(controller.operations, ["doctor", "login_status", "discover"])
        self.assertEqual(results[-1][1]["command_count"], 3)

    def test_schema_availability_and_saved_credentials_do_not_confirm_authorization(self):
        for result in ({"status": "schema_available"}, {"status": "credentials_saved"}, {"status": "authorized", "authorized": False}):
            with self.subTest(result=result):
                self.assertFalse(authorization_confirmed(result))
        self.assertTrue(authorization_confirmed({"status": "authorized", "authorized": True}))

    def test_saving_credentials_advances_to_login_only_on_success(self):
        self.assertEqual(onboarding_followup("保存凭证", {"status": "credentials_saved"}), "login")
        self.assertIsNone(onboarding_followup("保存凭证", {"status": "blocked"}))

    def test_automatic_probe_advances_to_discovery_only_on_authorization(self):
        self.assertEqual(onboarding_followup("等待扫码授权", {"status": "authorized", "authorized": True}), "discover")
        self.assertIsNone(onboarding_followup("等待扫码授权", {"status": "authorization_pending"}))
        self.assertIsNone(onboarding_followup("等待扫码授权", {"status": "schema_available"}))

    def test_automatic_probe_has_a_deadline_and_waits_when_busy(self):
        self.assertEqual(authorization_probe_decision(100.0, 105.0, busy=False), "run")
        self.assertEqual(authorization_probe_decision(100.0, 105.0, busy=True), "wait")
        self.assertEqual(authorization_probe_decision(105.0, 105.0, busy=True), "stop")
        self.assertEqual(authorization_probe_decision(100.0, None, busy=False), "stop")

    def test_qr_data_requires_strict_small_base64_and_png_signature(self):
        png = base64.b64encode(b"\x89PNG\r\n\x1a\nPNG-CONTENT").decode("ascii")
        self.assertEqual(safe_qr_png_base64(png), png)
        for value in (None, png + "\n", "not-base64!", base64.b64encode(b"GIF89a").decode("ascii"), "A" * (256 * 1024 + 4)):
            with self.subTest(value=str(value)[:30]):
                self.assertIsNone(safe_qr_png_base64(value))

    def test_credential_save_clears_previous_authorization_before_failing_worker_runs(self):
        # Only the task boundary runs: no Tk, controller, files, or network.
        window = OrganizerWindow.__new__(OrganizerWindow)
        window.root = PendingCallbacks()
        window.public_error_type = UserFacingError
        window._busy = False
        window._closed = False
        window._results = queue.Queue()
        window._authorization_url = MemoryVariable("https://music.163.com/old-authorization")
        window._status = MemoryVariable()
        window._probe_after_id = "old-probe"
        window._probe_deadline = 500.0
        old_qr = MemoryQrWindow()
        window._qr_window = old_qr
        window._qr_image = object()
        window._qr_refresh_button = None
        window._set_busy = lambda busy: setattr(window, "_busy", busy)
        window._append_log = lambda lines: None
        observed = []

        def failed_save():
            observed.append((window._authorization_url.value, window._probe_deadline, window._qr_window, window._qr_image))
            raise RuntimeError("SECRET-SAVE-ERROR")

        self.assertTrue(window._start_task("保存凭证", failed_save))
        result = window._results.get(timeout=2)
        self.assertEqual(observed, [("", None, None, None)])
        self.assertEqual(window.root.pending, {})
        self.assertTrue(old_qr.destroyed)
        self.assertTrue(result.failed)
        self.assertNotIn("SECRET", "\n".join(result.lines))

    def test_discovery_failure_preserves_confirmed_authorization_and_allows_retry(self):
        controller = OnboardingController(authorized=True, discovery_error=RuntimeError("SECRET-DISCOVERY-ERROR"))
        results = check_onboarding(controller)
        self.assertTrue(results[1][1]["authorized"])
        self.assertEqual(results[-1][1]["status"], "discovery_failed")
        rendered = "\n".join(line for label, item in results for line in result_lines(label, item))
        self.assertNotIn("SECRET", rendered)
        self.assertIn("再次", rendered)
        controller.discovery_error = None
        retried = check_onboarding(controller)
        self.assertEqual(retried[-1][1]["status"], "schema_available")

    def test_explicit_startup_authorization_is_consumed_once_after_configured_doctor(self):
        window = OrganizerWindow.__new__(OrganizerWindow)
        window._authorize_on_start = True
        result = _TaskResult("检查本地接入", (), configured=True)
        self.assertEqual(window._consume_startup_authorization(result), "login")
        self.assertIsNone(window._consume_startup_authorization(result))

    def test_startup_worker_carries_configuration_result_for_main_thread_decision(self):
        window = OrganizerWindow.__new__(OrganizerWindow)
        window._results = queue.Queue()
        window.public_error_type = UserFacingError
        window._authorize_on_start = True
        window._worker("检查本地接入", lambda: {"configured": True, "installed": True}, ())
        result = window._results.get_nowait()
        self.assertEqual(window._consume_startup_authorization(result), "login")

    def test_default_startup_never_advances_to_login(self):
        window = OrganizerWindow.__new__(OrganizerWindow)
        window._authorize_on_start = False
        self.assertIsNone(window._consume_startup_authorization(_TaskResult("检查本地接入", (), configured=True)))

    def test_failed_or_unconfigured_startup_consumes_request_without_login(self):
        for result in (
            _TaskResult("检查本地接入", (), failed=True, configured=True),
            _TaskResult("检查本地接入", (), configured=False),
            _TaskResult("检查本地接入", ()),
        ):
            with self.subTest(result=result):
                window = OrganizerWindow.__new__(OrganizerWindow)
                window._authorize_on_start = True
                self.assertIsNone(window._consume_startup_authorization(result))
                self.assertIsNone(window._consume_startup_authorization(_TaskResult("检查本地接入", (), configured=True)))

    def test_manual_check_does_not_consume_explicit_startup_request(self):
        window = OrganizerWindow.__new__(OrganizerWindow)
        window._authorize_on_start = True
        self.assertIsNone(window._consume_startup_authorization(_TaskResult("检查接入", (), configured=True)))
        self.assertEqual(window._consume_startup_authorization(_TaskResult("检查本地接入", (), configured=True)), "login")

    def test_both_wait_deadline_paths_clear_old_qr_and_url_without_new_login(self):
        for method in ("_schedule_authorization_probe", "_probe_authorization_once"):
            with self.subTest(method=method):
                window = waiting_qr_window()
                old_qr = window._qr_window
                if method == "_probe_authorization_once":
                    # The scheduler already consumed this callback before entry.
                    window.root.pending.clear()
                    window._probe_after_id = None
                with patch("netease_organizer.ui.time.monotonic", return_value=501.0):
                    getattr(window, method)()
                self.assertEqual(window._authorization_url.value, "")
                self.assertIsNone(window._probe_deadline)
                self.assertIsNone(window._qr_window)
                self.assertIsNone(window._qr_image)
                self.assertTrue(old_qr.destroyed)
                self.assertEqual(window.requests, [])
                self.assertIn("重新获取", "\n".join(window.logs))

    def test_qr_refresh_button_is_disabled_when_busy_and_removed_on_close(self):
        window = waiting_qr_window()
        button = window._qr_refresh_button
        window._set_busy(True)
        self.assertEqual(button.state, "disabled")
        window._close_qr_window()
        self.assertIsNone(window._qr_refresh_button)
        self.assertNotIn(button, window._buttons)
        window._set_busy(False)
        self.assertTrue(button.destroyed)

    def test_artist_creation_button_uses_authorized_default_visibility_in_background_task(self):
        accepted = []
        class ArtistController:
            def execute_artist_playlists(self, *, accept_default_visibility=False):
                accepted.append(accept_default_visibility)
                return {"status": "applied"}
        window = OrganizerWindow.__new__(OrganizerWindow)
        window.controller = ArtistController()
        requested = []
        window._start_task = lambda action, operation: requested.append((action, operation()))
        window._create_artist_playlists()
        self.assertEqual(accepted, [True])
        self.assertEqual(requested, [("创建歌手精选", {"status": "applied"})])

    def progress_window(self):
        window = OrganizerWindow.__new__(OrganizerWindow)
        window.root = PendingCallbacks()
        window.root.pending.clear()
        window._results = queue.Queue()
        window._busy = True
        window._closed = False
        window._pause_requested = False
        window._can_pause = True
        window._close_after_task = False
        window._buttons = []
        window._credential_entries = []
        window._progress = MemoryProgress()
        window._pause_button = MemoryButton()
        window._resume_button = MemoryButton()
        window._resume_button.state = "disabled"
        window._resumable = False
        window._configured = False
        window._credentials_visible = True
        window._credentials_frame = MemoryFrame()
        window._credential_status = MemoryVariable()
        window._settings_toggle = MemoryButton()
        window._authorization_frame = MemoryFrame()
        window._authorization_frame.visible = False
        window._quick_names = MemoryVariable(True)
        window._status = MemoryVariable()
        window._elapsed = MemoryVariable()
        window._last_result = MemoryVariable()
        window._authorization_url = MemoryVariable()
        window._plan_path = MemoryVariable()
        window._private_key = MemoryVariable("PRIVATE-KEY")
        window._authorize_on_start = False
        window._probe_after_id = None
        window._probe_deadline = None
        window._last_probe_lines = None
        window._qr_window = None
        window._qr_image = None
        window._qr_refresh_button = None
        window._task_started_at = 10.0
        window._elapsed_after_id = None
        window._poll_after_id = None
        window._startup_after_id = None
        window._task_action = "创建歌手精选"
        window._task_sensitive_values = ()
        window.logs = []
        window._append_log = lambda lines: window.logs.extend(lines)
        window.public_error_type = UserFacingError
        return window

    def test_worker_preserves_paused_status_and_local_history_without_secret_fields(self):
        window = self.progress_window()
        window._worker("检查本地接入", lambda: {
            "status": "credentials_saved", "configured": True,
            "last_result": {"status": "paused", "completed_count": 2,
                "source": "local_record", "private_key": "SECRET-KEY",
                "items": [{"name": "林俊杰", "count": 21, "status": "completed", "raw": "SECRET-RAW"}]},
        }, ())
        result = window._results.get_nowait()
        self.assertTrue(hasattr(result, "last_result_lines"), "Task result must carry a public history summary")
        self.assertIn("上次执行记录", "\n".join(result.last_result_lines))
        self.assertIn("21", "\n".join(result.last_result_lines))
        self.assertNotIn("SECRET", "\n".join(result.last_result_lines))
        window._worker("创建歌手精选", lambda: {"status": "paused", "completed_count": 2}, ())
        result = window._results.get_nowait()
        self.assertEqual(result.status, "paused")

    def test_progress_callback_only_queues_public_fields_until_main_thread_poll(self):
        window = self.progress_window()
        callback = getattr(window, "_queue_progress", None)
        self.assertTrue(callable(callback), "Progress listener must queue updates for the Tk thread")
        callback({"kind": "progress", "phase": "verify", "label": "核对歌曲",
                  "completed_count": 1, "total_count": 5, "elapsed_seconds": 3.2,
                  "token": "SECRET-TOKEN", "raw": {"private_key": "SECRET-KEY"}})
        self.assertEqual(window._status.value, "")
        self.assertTrue(window._busy)
        window._poll_results()
        self.assertTrue(window._busy)
        self.assertIn("核对歌曲", window._status.value)
        self.assertIn("1/5", window._status.value)
        self.assertNotIn("SECRET", window._status.value)
        self.assertEqual(window.logs, [])

    def test_pause_is_immediate_and_double_click_never_launches_second_job(self):
        window = self.progress_window()
        requests = []
        class PauseController:
            def request_pause(self):
                requests.append(threading.current_thread().name)
                return {"status": "pause_requested"}
        window.controller = PauseController()
        pause = getattr(window, "_request_pause", None)
        self.assertTrue(callable(pause), "A busy operation must have a direct pause request")
        with patch("netease_organizer.ui.threading.Thread") as worker:
            pause()
            pause()
            self.assertFalse(window._start_task("执行名称整理", lambda: {}))
        worker.assert_not_called()
        self.assertEqual(requests, [threading.current_thread().name])
        self.assertTrue(window._busy)
        self.assertEqual(window._pause_button.state, "disabled")
        self.assertIn("核对", window._status.value)

    def test_progress_then_paused_result_stops_timer_and_renders_pause(self):
        window = self.progress_window()
        callback = getattr(window, "_queue_progress", None)
        self.assertTrue(callable(callback))
        callback({"kind": "progress", "label": "核对歌曲", "completed_count": 2, "total_count": 5})
        window._worker("创建歌手精选", lambda: {"status": "paused", "completed_count": 2}, ())
        window._elapsed_after_id = "elapsed"
        window.root.pending["elapsed"] = object()
        with patch("netease_organizer.ui.time.monotonic", return_value=18.3):
            window._poll_results()
        self.assertFalse(window._busy)
        self.assertIn("已暂停", window._status.value)
        self.assertNotIn("已结束", window._status.value)
        self.assertIsNone(window._elapsed_after_id)
        self.assertNotIn("elapsed", window.root.pending)
        self.assertIn("8", window._elapsed.value)

    def test_elapsed_timer_and_local_startup_do_not_call_account_or_reset_pause(self):
        window = self.progress_window()
        window._busy = False
        requests = []
        class LocalController:
            def prepare_operation(self, action):
                requests.append(("prepare", action))
            def doctor(self):
                requests.append(("doctor",))
                return {"configured": True}
        window.controller = LocalController()
        with patch("netease_organizer.ui.time.monotonic", return_value=10.0):
            window._check_local_on_startup()
        window._results.get(timeout=2)
        self.assertEqual(requests, [("doctor",)])
        self.assertTrue(hasattr(window, "_elapsed_after_id"))
        self.assertIsNotNone(window._elapsed_after_id)
        window._set_busy(False)
        self.assertIsNone(window._elapsed_after_id)
        requests.clear()
        self.assertTrue(window._start_task("读取在线清单", lambda: {"status": "prepared"}))
        window._results.get(timeout=2)
        self.assertEqual(requests, [("prepare", "读取在线清单")])
        window._set_busy(False)

    def test_close_busy_requests_pause_and_waits_for_result_before_destroying(self):
        window = self.progress_window()
        calls = []
        class PauseController:
            def request_pause(self):
                calls.append("pause")
                return {"status": "pause_requested"}
        window.controller = PauseController()
        with patch("netease_organizer.ui.messagebox.showinfo") as popup:
            window._close()
        popup.assert_not_called()
        self.assertEqual(calls, ["pause"])
        self.assertFalse(window._closed)
        self.assertTrue(window._close_after_task)
        window._worker("创建歌手精选", lambda: {"status": "paused"}, ())
        window._poll_results()
        self.assertTrue(window._closed)
        self.assertTrue(window.root.destroyed)
        self.assertEqual(window.root.pending, {})
        self.assertEqual(window._private_key.value, "")

    def test_next_operation_restores_indeterminate_progress_before_new_counts_arrive(self):
        window = self.progress_window()
        window._progress.configure(mode="determinate", maximum=5, value=2)
        window._set_busy(False)
        window._busy = False
        window.controller = object()
        self.assertTrue(window._start_task("读取在线清单", lambda: {"status": "prepared"}))
        window._results.get(timeout=2)
        self.assertEqual(window._progress.options.get("mode"), "indeterminate")
        window._set_busy(False)

    def test_busy_pause_button_stays_enabled_and_stops_only_after_pause_request(self):
        window = self.progress_window()
        window._pause_button.state = "disabled"
        window._set_busy(True)
        self.assertEqual(window._pause_button.state, "normal")
        window._set_busy(False)
        self.assertEqual(window._pause_button.state, "disabled")

    def test_pause_request_failure_does_not_escape_tk_callback_or_expose_internal_error(self):
        window = self.progress_window()
        class BrokenController:
            def request_pause(self):
                raise RuntimeError("SECRET-TOKEN")
        window.controller = BrokenController()
        window._request_pause()
        self.assertFalse(window._pause_requested)
        self.assertEqual(window._pause_button.state, "normal")
        self.assertNotIn("SECRET", "\n".join(window.logs))

    def test_local_resumable_record_enables_resume_only_after_task_is_idle(self):
        window = self.progress_window()
        window._worker("检查本地接入", lambda: {"status": "credentials_saved", "last_result": {
            "source": "local_record", "operation": "artists", "status": "paused",
            "resumable": True, "completed_count": 2, "items": [],
        }}, ())
        result = window._results.get_nowait()
        self.assertTrue(hasattr(result, "resumable"), "Resume state must come from the public local record")
        self.assertIs(result.resumable, True)
        window._results.put(result)
        window._poll_results()
        self.assertTrue(window._resumable)
        self.assertEqual(window._resume_button.state, "normal")
        window._set_busy(True)
        self.assertEqual(window._resume_button.state, "disabled")
        window._set_busy(False)

    def test_missing_unverified_completed_and_uncertain_records_never_enable_resume(self):
        for record in (
            {"resumable": True, "operation": "artists", "status": "paused"},
            {"source": "local_record", "resumable": True, "operation": "other", "status": "paused"},
            {"source": "local_record", "resumable": True, "operation": "artists", "status": "completed"},
            {"source": "local_record", "resumable": True, "operation": "artists", "status": "uncertain"},
            {"source": "local_record", "resumable": False, "operation": "renames", "status": "paused"},
        ):
            with self.subTest(record=record):
                window = self.progress_window()
                window._worker("检查本地接入", lambda: {"last_result": record}, ())
                result = window._results.get_nowait()
                self.assertTrue(hasattr(result, "resumable"))
                self.assertIsNot(result.resumable, True)

    def test_resume_click_uses_existing_task_boundary_and_never_runs_automatically(self):
        window = self.progress_window()
        window._busy = False
        window._resumable = True
        calls = []
        class ResumeController:
            def resume_last_operation(self):
                calls.append("resume")
                return {"status": "paused"}
        window.controller = ResumeController()
        requested = []
        window._start_task = lambda action, operation: requested.append((action, operation()))
        callback = getattr(window, "_resume_last_operation", None)
        self.assertTrue(callable(callback), "Resume must be a user action")
        self.assertEqual(calls, [])
        callback()
        self.assertEqual(calls, ["resume"])
        self.assertEqual(requested, [("继续上次任务", {"status": "paused"})])
        window._busy = True
        callback()
        window._busy = False
        window._resumable = False
        callback()
        self.assertEqual(calls, ["resume"])

    def test_result_performance_summary_uses_only_counts_and_finite_elapsed_time(self):
        rendered = "\n".join(result_lines("读取在线清单", {"performance": {
            "call_count": 8, "elapsed_seconds": 2.36,
            "commands": ["SECRET-COMMAND"], "token": "SECRET-TOKEN",
        }}))
        self.assertIn("8 次", rendered)
        self.assertIn("2.4 秒", rendered)
        self.assertNotIn("SECRET", rendered)
        for value in ({"call_count": True, "elapsed_seconds": float("inf")},
                      {"call_count": -1, "elapsed_seconds": float("nan")},
                      {"call_count": "SECRET", "elapsed_seconds": "SECRET"}):
            rendered = "\n".join(result_lines("读取在线清单", {"performance": value}))
            self.assertNotIn("本次接口", rendered)
            self.assertNotIn("SECRET", rendered)

    def test_any_doctor_result_collapses_saved_credentials_only_on_main_thread_poll(self):
        window = self.progress_window()
        window._worker("检查接入", lambda: [("检查接入", {"configured": True}),
                                             ("检查授权状态", {"authorized": False})], ())
        result = window._results.get_nowait()
        self.assertIs(result.configured, True)
        self.assertTrue(window._credentials_frame.visible)
        window._results.put(result)
        window._poll_results()
        self.assertFalse(window._credentials_frame.visible)
        self.assertIn("凭证已保存", window._credential_status.value)
        window._toggle_credentials()
        self.assertTrue(window._credentials_frame.visible)
        window._busy = True
        window._toggle_credentials()
        self.assertTrue(window._credentials_frame.visible)

    def test_unconfigured_result_keeps_first_use_fields_expanded(self):
        window = self.progress_window()
        window._credentials_frame.visible = False
        window._credentials_visible = False
        window._results.put(_TaskResult("检查本地接入", (), configured=False))
        window._poll_results()
        self.assertTrue(window._credentials_frame.visible)
        self.assertNotIn("凭证已保存", window._credential_status.value)

    def test_quick_names_checkbox_value_is_captured_before_background_worker(self):
        window = self.progress_window()
        calls = []
        class PreviewController:
            def online_preview(self, *, names_only=False):
                calls.append(names_only)
                return {"status": "prepared"}
        window.controller = PreviewController()
        requested = []
        window._start_task = lambda action, operation: requested.append((action, operation))
        callback = getattr(window, "_read_online_plan", None)
        self.assertTrue(callable(callback))
        callback()
        window._quick_names.set(False)
        requested.pop()[1]()
        callback()
        window._quick_names.set(True)
        requested.pop()[1]()
        self.assertEqual(calls, [True, False])

    def test_authorization_region_exists_only_for_pending_url_and_clears_on_success(self):
        window = self.progress_window()
        window.controller = object()
        window._results.put(_TaskResult("账号授权", (), authorization_url="https://163cn.tv/pending-example"))
        window._poll_results()
        self.assertTrue(window._authorization_frame.visible)
        self.assertIn("pending-example", window._authorization_url.value)
        window._results.put(_TaskResult("检查接入", (), authorized=True))
        window._poll_results()
        self.assertFalse(window._authorization_frame.visible)
        self.assertEqual(window._authorization_url.value, "")

    def test_cli_progress_keeps_job_phase_and_latest_known_page_count(self):
        window = self.progress_window()
        window._queue_progress({"kind": "progress", "label": "读取红心曲目", "completed_count": 2, "total_count": 5})
        window._queue_progress({"kind": "cli", "label": "正在核对官方接口"})
        window._poll_results()
        self.assertTrue(window._busy)
        self.assertIn("读取红心曲目", window._status.value)
        self.assertIn("2/5", window._status.value)
        self.assertIn("核对官方接口", window._status.value)


if __name__ == "__main__":
    unittest.main()
