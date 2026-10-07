"""Small local Tkinter front end; controller work never runs on the Tk thread."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
import base64
import binascii
import math
import queue
import threading
import time
import tkinter as tk
from tkinter import filedialog, messagebox, scrolledtext, ttk
from typing import Any, Protocol
from .authorization import safe_authorization_url


DEVELOPER_APPLICATION_URL = (
    "https://developer.music.163.com/st/developer/apply/account?type=INDIVIDUAL"
)


class OrganizerController(Protocol):
    def set_progress_listener(self, callback: Callable[[dict[str, Any]], None]) -> None: ...
    def prepare_operation(self, action: str) -> None: ...
    def request_pause(self) -> dict[str, Any]: ...
    def resume_last_operation(self) -> dict[str, Any]: ...
    def doctor(self) -> dict[str, Any]: ...
    def save_credentials(self, app_id: str, private_key: str) -> dict[str, Any]: ...
    def save_credentials_file(self, app_id: str, path: Path) -> dict[str, Any]: ...
    def login(self) -> dict[str, Any]: ...
    def login_status(self) -> dict[str, Any]: ...
    def authorization_probe(self) -> dict[str, Any]: ...
    def discover(self) -> dict[str, Any]: ...
    def prepare(self) -> dict[str, Any]: ...
    def execute(self) -> dict[str, Any]: ...
    def online_preview(self, *, names_only: bool = False) -> dict[str, Any]: ...
    def execute_renames(self) -> dict[str, Any]: ...
    def execute_artist_playlists(self, *, accept_default_visibility: bool = False) -> dict[str, Any]: ...


def private_key_source(file_path: Path | None, pasted_key: str) -> tuple[str, Path | str]:
    """Prefer the selected file; let the controller read and validate its content."""
    if file_path is not None:
        return "file", file_path
    private_key = pasted_key.strip()
    if private_key:
        return "text", private_key
    raise ValueError("请选择 UTF-8 私钥文件，或在备用输入框粘贴完整 Private Key。")


def authorization_confirmed(result: Mapping[str, Any]) -> bool:
    """Schema discovery and saved credentials are separate from account consent."""
    authorized = result.get("authorized")
    if isinstance(authorized, bool):
        return authorized
    return result.get("status") == "authorized"


def onboarding_followup(action: str, result: Mapping[str, Any]) -> str | None:
    if action == "保存凭证" and result.get("status") == "credentials_saved":
        return "login"
    if action == "等待扫码授权" and authorization_confirmed(result):
        return "discover"
    return None


def check_onboarding(controller: OrganizerController) -> list[tuple[str, dict[str, Any]]]:
    """A manual check validates consent once before discovering official commands."""
    doctor = controller.doctor()
    checked = [("检查接入", doctor)]
    if doctor.get("configured") is not True:
        return checked
    login_status = getattr(controller, "login_status", None)
    if not callable(login_status):
        checked.append(("检查授权状态", {
            "message": "当前控制器尚不支持授权状态检查，请更新程序后再检查接入。",
        }))
        return checked
    status = login_status()
    checked.append(("检查授权状态", status))
    if authorization_confirmed(status):
        try:
            discovery = controller.discover()
        except Exception:
            discovery = {
                "status": "discovery_failed", "authorized": True,
                "message": "账号授权已确认，暂未取得官方命令。请稍后再次点击“检查接入”。",
            }
        checked.append(("发现官方命令", discovery))
    return checked


def authorization_probe_decision(now: float, deadline: float | None, *, busy: bool) -> str:
    if deadline is None or now >= deadline:
        return "stop"
    return "wait" if busy else "run"


def safe_qr_png_base64(value: Any) -> str | None:
    """Validate the transport and PNG signature; Tk validates the actual image."""
    if not isinstance(value, str) or not value or len(value) > 256 * 1024:
        return None
    try:
        decoded = base64.b64decode(value, validate=True)
    except (binascii.Error, ValueError):
        return None
    return value if decoded.startswith(b"\x89PNG\r\n\x1a\n") else None


def _display_text(value: str, sensitive_values: Sequence[str] = ()) -> str:
    """Bound public text and remove any credential submitted with this task."""
    for sensitive in sensitive_values:
        if sensitive:
            value = value.replace(sensitive, "[已隐藏]")
            for line in sensitive.splitlines():
                if line.strip():
                    value = value.replace(line, "[已隐藏]")
    value = "".join(char for char in value if char in "\n\t" or ord(char) >= 32)
    return value[:2000]


def public_error_message(
    error: Exception,
    public_error_type: type[Exception] | tuple[type[Exception], ...] = (),
    *,
    sensitive_values: Sequence[str] = (),
) -> str:
    """Public controller errors may explain recovery; other errors stay private."""
    if isinstance(error, public_error_type):
        message = _display_text(str(error), sensitive_values).strip()
        if message:
            return message
    return "本次操作未完成。请先检查接入，再重试；运行记录不会显示内部错误或凭证。"


def result_lines(
    action: str,
    result: Mapping[str, Any],
    *,
    sensitive_values: Sequence[str] = (),
) -> list[str]:
    """Use a small public-field allowlist instead of dumping CLI/controller JSON."""
    lines = [f"{action}已返回结果。"]
    message = result.get("message")
    if isinstance(message, str) and message.strip():
        lines.append(_display_text(message, sensitive_values))
    if isinstance(result.get("installed"), bool):
        lines.append("官方 CLI：已安装。" if result["installed"] else "官方 CLI：尚未安装。")
    if isinstance(result.get("configured"), bool):
        lines.append("开放平台凭证：已配置。" if result["configured"] else "开放平台凭证：尚未配置。")
    for field, label in (
        ("command_count", "可用命令"),
        ("job_count", "整理任务"),
        ("completed_count", "已完成"),
        ("blocked_count", "已阻止"),
    ):
        value = result.get(field)
        if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
            lines.append(f"{label}：{value} 项。")
    writable = result.get("writable_commands")
    if isinstance(writable, list):
        lines.append(f"识别到的写入命令：{len(writable)} 项。")
    path = result.get("path")
    if isinstance(path, (str, Path)):
        label = "清单位置：" if action == "生成整理清单" else "本地记录："
        lines.append(label + _display_text(str(path), sensitive_values))
    performance = result.get("performance")
    if isinstance(performance, Mapping):
        call_count = performance.get("call_count")
        elapsed = performance.get("elapsed_seconds")
        if (isinstance(call_count, int) and not isinstance(call_count, bool) and call_count >= 0
                and isinstance(elapsed, (int, float)) and not isinstance(elapsed, bool)
                and math.isfinite(elapsed) and elapsed >= 0):
            lines.append(f"本次接口调用：{call_count} 次；耗时：{elapsed:.1f} 秒。")
    return lines


def _result_status_text(action: str, status: str, *, failed: bool = False) -> str:
    if failed:
        return f"{action}未完成，请查看运行记录。"
    descriptions = {
        "paused": "已暂停", "pause_requested": "正在暂停",
        "partial": "部分完成", "blocked": "已阻止", "uncertain": "结果待核对",
        "completed": "已完成", "applied": "已完成", "prepared": "清单已生成",
    }
    return f"{action}{descriptions.get(status, '已返回结果')}，请查看运行记录。"


def _last_result_lines(value: Any, sensitive_values: Sequence[str] = ()) -> tuple[str, ...]:
    """Historical local receipts are useful without claiming current account state."""
    if not isinstance(value, Mapping) or value.get("source") != "local_record":
        return ()
    lines = ["上次执行记录（本地保存）："]
    count = value.get("completed_count")
    if isinstance(count, int) and not isinstance(count, bool) and count >= 0:
        lines.append(f"已完成 {count} 项。")
    items = value.get("items")
    if isinstance(items, list):
        for item in items[:5]:
            if not isinstance(item, Mapping) or not isinstance(item.get("name"), str):
                continue
            name = _display_text(item["name"], sensitive_values).replace("\n", " ")[:100]
            count = item.get("count")
            detail = f" {count} 首" if isinstance(count, int) and not isinstance(count, bool) and count >= 0 else ""
            status = {"completed": "已核对", "paused": "已暂停", "blocked": "已阻止",
                      "uncertain": "待核对", "not_attempted": "未执行"}.get(item.get("status"), "已记录")
            lines.append(f"{name}{detail}（{status}）。")
    return tuple(lines)


@dataclass(frozen=True)
class _TaskResult:
    action: str
    lines: tuple[str, ...]
    failed: bool = False
    authorization_url: str | None = None
    plan_path: str | None = None
    follow_up: str | None = None
    authorized: bool = False
    qr_png_base64: str | None = None
    configured: bool | None = None
    status: str = "returned"
    last_result_lines: tuple[str, ...] = ()
    resumable: bool | None = None


@dataclass(frozen=True)
class _TaskProgress:
    label: str
    completed_count: int | None = None
    total_count: int | None = None
    elapsed_seconds: float | None = None
    kind: str = "progress"


class OrganizerWindow:
    """Keep all widgets on the main thread and use a queue for worker results."""

    def __init__(
        self,
        root: tk.Tk,
        controller: OrganizerController,
        public_error_type: type[Exception] | tuple[type[Exception], ...] = (),
        *,
        authorize_on_start: bool = False,
    ) -> None:
        self.root = root
        self.controller = controller
        self.public_error_type = public_error_type
        self._authorize_on_start = authorize_on_start
        self._results: queue.Queue[_TaskResult | _TaskProgress] = queue.Queue()
        self._busy = False
        self._closed = False
        self._poll_after_id: str | None = None
        self._startup_after_id: str | None = None
        self._elapsed_after_id: str | None = None
        self._task_started_at: float | None = None
        self._task_action = ""
        self._task_sensitive_values: tuple[str, ...] = ()
        self._pause_requested = False
        self._can_pause = False
        self._close_after_task = False
        self._resumable = False
        self._configured = False
        self._credentials_visible = True
        self._phase_label = ""
        self._progress_counts: tuple[int, int] | None = None
        self._probe_deadline: float | None = None
        self._probe_after_id: str | None = None
        self._last_probe_lines: tuple[str, ...] | None = None
        self._qr_window: tk.Toplevel | None = None
        self._qr_image: tk.PhotoImage | None = None
        self._qr_refresh_button: ttk.Button | None = None
        self._buttons: list[ttk.Button] = []
        self._credential_entries: list[ttk.Entry] = []
        self._app_id = tk.StringVar(root)
        self._private_key = tk.StringVar(root)
        self._private_key_file: Path | None = None
        self._private_key_filename = tk.StringVar(root, value="尚未选择私钥文件")
        self._authorization_url = tk.StringVar(root)
        self._plan_path = tk.StringVar(root)
        self._status = tk.StringVar(root, value="尚未检查接入。")
        self._elapsed = tk.StringVar(root, value="耗时：0 秒")
        self._last_result = tk.StringVar(root, value="尚无本地执行记录。")
        self._credential_status = tk.StringVar(root, value="首次使用：请配置开放平台凭证。")
        self._quick_names = tk.BooleanVar(root, value=True)
        self._build_window()
        listener = getattr(controller, "set_progress_listener", None)
        if callable(listener):
            listener(self._queue_progress)
        self.root.protocol("WM_DELETE_WINDOW", self._close)
        self._poll_after_id = self.root.after(100, self._poll_results)
        self._startup_after_id = self.root.after(0, self._check_local_on_startup)

    def _check_local_on_startup(self) -> None:
        self._startup_after_id = None
        self._start_task("检查本地接入", self.controller.doctor)

    def _build_window(self) -> None:
        self.root.title("网易云个人歌单整理")
        self.root.geometry("900x800")
        self.root.minsize(720, 720)
        container = ttk.Frame(self.root, padding=16)
        container.pack(fill="both", expand=True)
        container.columnconfigure(0, weight=1)
        container.rowconfigure(7, weight=1)
        ttk.Label(container, text="网易云个人歌单整理", font=("Microsoft YaHei UI", 17, "bold")).grid(row=0, column=0, sticky="w")
        ttk.Label(container, text="沿用现有分类、规范名称，并从红心曲目创建歌手精选。", wraplength=820).grid(row=1, column=0, sticky="w", pady=(4, 8))
        ttk.Label(container, text="授权后可执行名称整理与歌手精选创建（默认可见性，可能公开）；歌单列表排列暂不支持。", wraplength=820, foreground="#8a5200").grid(row=2, column=0, sticky="w", pady=(0, 12))

        connection = ttk.Frame(container)
        connection.grid(row=3, column=0, sticky="ew")
        connection.columnconfigure(0, weight=1)
        ttk.Label(connection, textvariable=self._credential_status).grid(row=0, column=0, sticky="w")
        self._settings_toggle = ttk.Button(connection, text="收起接入设置", command=self._toggle_credentials)
        self._settings_toggle.grid(row=0, column=1, sticky="e")
        self._buttons.append(self._settings_toggle)
        credentials = ttk.LabelFrame(connection, text="官方开放平台接入（首次使用）", padding=10)
        credentials.grid(row=1, column=0, columnspan=2, sticky="ew", pady=(8, 0))
        self._credentials_frame = credentials
        credentials.columnconfigure(1, weight=1)
        for row, label, variable in ((0, "App ID", self._app_id), (2, "备用掩码输入", self._private_key)):
            ttk.Label(credentials, text=label).grid(row=row, column=0, sticky="w", padx=(0, 12), pady=3)
            entry = ttk.Entry(credentials, textvariable=variable, show="*")
            entry.grid(row=row, column=1, sticky="ew", pady=3)
            self._credential_entries.append(entry)
        ttk.Label(credentials, text="Private Key 文件").grid(row=1, column=0, sticky="w", padx=(0, 12), pady=3)
        ttk.Entry(credentials, textvariable=self._private_key_filename, state="readonly").grid(row=1, column=1, sticky="ew", pady=3)
        choose_file = ttk.Button(credentials, text="选择私钥文件", command=self._select_private_key_file)
        choose_file.grid(row=1, column=2, padx=(10, 0))
        self._buttons.append(choose_file)
        save = ttk.Button(credentials, text="保存并接入", command=self._save_credentials)
        save.grid(row=2, column=2, padx=(10, 0))
        self._buttons.append(save)
        ttk.Label(credentials, text="优先使用 UTF-8 私钥文件（支持多行 PEM），仅显示文件名；未选文件时使用备用输入。", wraplength=650).grid(row=3, column=0, columnspan=2, sticky="w", pady=(5, 0))
        clear_file = ttk.Button(credentials, text="清除文件选择", command=self._clear_private_key_file)
        clear_file.grid(row=3, column=2, padx=(10, 0), pady=(5, 0))
        self._buttons.append(clear_file)
        ttk.Label(credentials, text="首次入驻链接（选中后 Ctrl+C 复制，自行在浏览器打开）").grid(row=4, column=0, columnspan=3, sticky="w", pady=(8, 3))
        application_url = tk.StringVar(self.root, value=DEVELOPER_APPLICATION_URL)
        # Keep this variable alive for the lifetime of its widget.
        self._application_url = application_url
        ttk.Entry(credentials, textvariable=application_url, state="readonly").grid(row=5, column=0, columnspan=3, sticky="ew")

        controls = ttk.Frame(container)
        controls.grid(row=4, column=0, sticky="ew", pady=12)
        actions = (
            ("检查接入", lambda: self._start_task("检查接入", self._check_connection)),
            ("账号授权", lambda: self._start_task("账号授权", self.controller.login)),
            ("生成整理清单", lambda: self._start_task("生成整理清单", self.controller.prepare)),
            ("检查执行条件", lambda: self._start_task("检查执行条件", self.controller.execute)),
        )
        for column, (label, callback) in enumerate(actions):
            controls.columnconfigure(column, weight=1)
            button = ttk.Button(controls, text=label, command=callback)
            button.grid(row=0, column=column, sticky="ew", padx=(0 if column == 0 else 8, 0))
            self._buttons.append(button)

        for column, (label, callback) in enumerate((
            ("读取在线清单", self._read_online_plan),
            ("执行名称整理", lambda: self._start_task("执行名称整理", self.controller.execute_renames)),
            ("创建歌手精选（可能公开）", self._create_artist_playlists),
        )):
            button = ttk.Button(controls, text=label, command=callback)
            button.grid(row=1, column=column, columnspan=2 if column == 2 else 1,
                        sticky="ew", pady=(8, 0), padx=(0 if column == 0 else 8, 0))
            self._buttons.append(button)

        quick_names = ttk.Checkbutton(controls, text="快速名称清单（不读取红心明细）", variable=self._quick_names)
        quick_names.grid(row=2, column=0, columnspan=4, sticky="w", pady=(8, 0))
        self._buttons.append(quick_names)

        authorization = ttk.LabelFrame(container, text="账号授权链接", padding=10)
        authorization.grid(row=5, column=0, sticky="ew")
        self._authorization_frame = authorization
        authorization.columnconfigure(0, weight=1)
        ttk.Entry(authorization, textvariable=self._authorization_url, state="readonly").grid(row=0, column=0, sticky="ew")
        ttk.Label(authorization, text="保存凭证后自动生成授权链接。请扫码授权，程序最多自动等待 5 分钟；也可手动“检查接入”。", wraplength=820).grid(row=1, column=0, sticky="w", pady=(5, 0))
        authorization.grid_remove()

        plan = ttk.Frame(container)
        plan.grid(row=6, column=0, sticky="ew", pady=(12, 8))
        plan.columnconfigure(1, weight=1)
        ttk.Label(plan, text="本地清单：").grid(row=0, column=0, sticky="w")
        ttk.Entry(plan, textvariable=self._plan_path, state="readonly").grid(row=0, column=1, sticky="ew")
        ttk.Label(plan, text="先查看整理清单；在线操作会重新核对账号、歌单与候选歌曲。", wraplength=820).grid(row=1, column=0, columnspan=2, sticky="w", pady=(4, 0))

        log_frame = ttk.LabelFrame(container, text="运行记录", padding=5)
        log_frame.grid(row=7, column=0, sticky="nsew")
        self._log = scrolledtext.ScrolledText(log_frame, height=8, wrap="word", state="disabled", font=("Microsoft YaHei UI", 10))
        self._log.pack(fill="both", expand=True)
        footer = ttk.Frame(container)
        footer.grid(row=8, column=0, sticky="ew", pady=(8, 0))
        footer.columnconfigure(0, weight=1)
        ttk.Label(footer, textvariable=self._status, wraplength=490).grid(row=0, column=0, sticky="w")
        ttk.Label(footer, textvariable=self._elapsed).grid(row=0, column=1, padx=(10, 0))
        self._progress = ttk.Progressbar(footer, mode="indeterminate", length=140)
        self._progress.grid(row=0, column=2, padx=(10, 0), sticky="e")
        self._pause_button = ttk.Button(footer, text="暂停", command=self._request_pause, state="disabled")
        self._pause_button.grid(row=0, column=3, padx=(10, 0), sticky="e")
        ttk.Label(footer, textvariable=self._last_result, wraplength=700).grid(
            row=1, column=0, columnspan=3, sticky="w", pady=(8, 0))
        self._resume_button = ttk.Button(footer, text="继续上次任务", command=self._resume_last_operation, state="disabled")
        self._resume_button.grid(row=1, column=3, padx=(10, 0), pady=(8, 0), sticky="e")

    def _check_connection(self) -> list[tuple[str, dict[str, Any]]]:
        return check_onboarding(self.controller)

    def _read_online_plan(self) -> None:
        names_only = bool(self._quick_names.get())
        self._start_task("读取在线清单", lambda: self.controller.online_preview(names_only=names_only))

    def _set_credentials_visible(self, visible: bool) -> None:
        self._credentials_visible = visible
        frame = getattr(self, "_credentials_frame", None)
        if frame is not None:
            frame.grid() if visible else frame.grid_remove()
        toggle = getattr(self, "_settings_toggle", None)
        if toggle is not None:
            toggle.configure(text="收起接入设置" if visible else "接入设置")

    def _toggle_credentials(self) -> None:
        if not self._busy and not self._closed:
            self._set_credentials_visible(not self._credentials_visible)

    def _apply_configuration(self, configured: bool) -> None:
        self._configured = configured
        status = getattr(self, "_credential_status", None)
        if status is not None:
            status.set("凭证已保存" if configured else "首次使用：请配置开放平台凭证。")
        self._set_credentials_visible(not configured)

    def _set_authorization_url(self, url: str) -> None:
        self._authorization_url.set(url)
        frame = getattr(self, "_authorization_frame", None)
        if frame is not None:
            frame.grid() if url else frame.grid_remove()

    def _create_artist_playlists(self) -> None:
        self._start_task("创建歌手精选", lambda: self.controller.execute_artist_playlists(
            accept_default_visibility=True))

    def _select_private_key_file(self) -> None:
        selected = filedialog.askopenfilename(
            parent=self.root, title="选择 UTF-8 私钥文件",
            filetypes=(("私钥文本文件", "*.pem *.key *.txt"), ("所有文件", "*.*")),
        )
        if selected:
            self._private_key_file = Path(selected)
            self._private_key_filename.set(self._private_key_file.name)

    def _clear_private_key_file(self) -> None:
        self._private_key_file = None
        self._private_key_filename.set("尚未选择私钥文件")

    def _save_credentials(self) -> None:
        app_id = self._app_id.get().strip()
        if not app_id:
            messagebox.showwarning("凭证未填完整", "请填写官方开放平台 App ID。", parent=self.root)
            return
        try:
            kind, value = private_key_source(self._private_key_file, self._private_key.get())
        except ValueError as error:
            messagebox.showwarning("凭证未填完整", str(error), parent=self.root)
            return
        self._private_key.set("")
        if kind == "file":
            operation = lambda: self.controller.save_credentials_file(app_id, value)
        else:
            operation = lambda: self.controller.save_credentials(app_id, value)
        self._start_task(
            "保存凭证", operation, sensitive_values=(app_id, str(value)),
        )

    def _set_busy(self, busy: bool) -> None:
        self._busy = busy
        state = "disabled" if busy else "normal"
        for widget in (*self._buttons, *self._credential_entries):
            widget.configure(state=state)
        if busy:
            self._progress.configure(mode="indeterminate", maximum=100, value=0)
            self._progress.start(12)
            if getattr(self, "_task_started_at", None) is not None:
                self._update_elapsed()
        else:
            self._progress.stop()
            self._stop_elapsed_timer()
            self._render_elapsed()
        pause_button = getattr(self, "_pause_button", None)
        if pause_button is not None:
            enabled = busy and getattr(self, "_can_pause", False) and not getattr(self, "_pause_requested", False)
            pause_button.configure(state="normal" if enabled else "disabled")
        resume_button = getattr(self, "_resume_button", None)
        if resume_button is not None:
            resume_button.configure(state="normal" if not busy and getattr(self, "_resumable", False) else "disabled")

    def _resume_last_operation(self) -> None:
        if self._busy or self._closed or not getattr(self, "_resumable", False):
            return
        resume = getattr(self.controller, "resume_last_operation", None)
        if callable(resume):
            self._start_task("继续上次任务", resume)

    def _render_elapsed(self) -> None:
        started = getattr(self, "_task_started_at", None)
        if started is not None and hasattr(self, "_elapsed"):
            seconds = max(0, int(time.monotonic() - started))
            self._elapsed.set(f"耗时：{seconds} 秒")

    def _update_elapsed(self) -> None:
        self._elapsed_after_id = None
        if self._closed or not self._busy:
            return
        self._render_elapsed()
        self._elapsed_after_id = self.root.after(500, self._update_elapsed)

    def _stop_elapsed_timer(self) -> None:
        identifier = getattr(self, "_elapsed_after_id", None)
        if identifier is not None:
            try:
                self.root.after_cancel(identifier)
            except tk.TclError:
                pass
            self._elapsed_after_id = None

    def _request_pause(self) -> None:
        if self._closed or not self._busy or getattr(self, "_pause_requested", False):
            return
        pause = getattr(getattr(self, "controller", None), "request_pause", None)
        if not callable(pause):
            return
        self._pause_requested = True
        if getattr(self, "_pause_button", None) is not None:
            self._pause_button.configure(state="disabled")
        try:
            pause()
        except Exception:
            self._pause_requested = False
            if getattr(self, "_pause_button", None) is not None:
                self._pause_button.configure(state="normal")
            self._append_log(("暂未能请求暂停，请再次点击暂停；当前任务仍在进行。",))
            return
        self._status.set("已请求暂停，当前请求完成并核对后停止。")
        self._append_log(("已请求暂停；当前请求完成并核对后停止，不再开始下一项。",))

    def _queue_progress(self, value: Any) -> None:
        """Worker callbacks enqueue a small allowlist; only Tk consumes it."""
        if self._closed or not isinstance(value, Mapping) or value.get("kind") not in ("progress", "cli"):
            return
        label = value.get("label", value.get("command"))
        if not isinstance(label, str) or not label.strip():
            return
        sensitive = getattr(self, "_task_sensitive_values", ())
        label = _display_text(label, sensitive).replace("\n", " ")[:160]
        def count(name: str) -> int | None:
            number = value.get(name)
            return number if isinstance(number, int) and not isinstance(number, bool) and 0 <= number <= 100000 else None
        elapsed = value.get("elapsed_seconds")
        if isinstance(elapsed, bool) or not isinstance(elapsed, (int, float)) or not math.isfinite(elapsed) or elapsed < 0:
            elapsed = None
        self._results.put(_TaskProgress(label, count("completed_count"), count("total_count"), elapsed, value["kind"]))

    def _show_progress(self, result: _TaskProgress) -> None:
        if not self._busy or getattr(self, "_pause_requested", False):
            return
        if result.kind == "progress":
            self._phase_label = result.label
        phase = getattr(self, "_phase_label", "")
        label = f"{phase}；{result.label}" if result.kind == "cli" and phase and phase != result.label else result.label
        if result.completed_count is not None and result.total_count is not None and result.total_count > 0:
            completed = min(result.completed_count, result.total_count)
            self._progress_counts = (completed, result.total_count)
        counts = getattr(self, "_progress_counts", None)
        if counts is not None:
            completed, total = counts
            label += f"（{completed}/{total}）"
            self._progress.stop()
            self._progress.configure(mode="determinate", maximum=total, value=completed)
        self._status.set(label)

    def _start_task(
        self,
        action: str,
        operation: Callable[[], Any],
        *,
        sensitive_values: Sequence[str] = (),
    ) -> bool:
        if self._busy or self._closed:
            return False
        prepare = getattr(getattr(self, "controller", None), "prepare_operation", None)
        if action not in ("检查本地接入", "等待扫码授权") and callable(prepare):
            prepare(action)
        self._pause_requested = False
        self._can_pause = action not in ("检查本地接入", "等待扫码授权", "账号授权", "发现官方命令")
        self._task_action = action
        self._task_sensitive_values = tuple(sensitive_values)
        self._task_started_at = time.monotonic()
        self._phase_label = ""
        self._progress_counts = None
        if action in ("账号授权", "保存凭证"):
            self._set_authorization_url("")
            self._stop_authorization_probe()
            self._close_qr_window()
        self._set_busy(True)
        self._status.set(f"正在{action}，请稍候…")
        if action != "等待扫码授权":
            self._append_log((f"开始{action}。",))
        try:
            threading.Thread(
                target=self._worker, args=(action, operation, tuple(sensitive_values)),
                daemon=True, name="netease-organizer-task",
            ).start()
        except Exception as error:
            self._set_busy(False)
            self._status.set(f"{action}未完成，请查看运行记录。")
            self._append_log((public_error_message(
                error, self.public_error_type, sensitive_values=sensitive_values,
            ),))
            return False
        return True

    def _worker(self, action: str, operation: Callable[[], Any], sensitive_values: tuple[str, ...]) -> None:
        try:
            result = operation()
            items = result if isinstance(result, list) else [(action, result)]
            lines: list[str] = []
            authorization_url = None
            plan_path = None
            follow_up = None
            authorized = False
            qr_png_base64 = None
            configured = None
            status = "returned"
            historical_lines: tuple[str, ...] = ()
            resumable: bool | None = None
            for label, item in items:
                if not isinstance(item, Mapping):
                    raise TypeError("Controller result is not a mapping")
                lines.extend(result_lines(label, item, sensitive_values=sensitive_values))
                if isinstance(item.get("status"), str):
                    status = item["status"]
                historical_lines = _last_result_lines(item.get("last_result"), sensitive_values) or historical_lines
                previous = item.get("last_result")
                if isinstance(previous, Mapping) and previous.get("source") == "local_record":
                    resumable = (previous.get("resumable") is True
                                 and previous.get("operation") in ("artists", "renames")
                                 and previous.get("status") not in ("completed", "applied", "uncertain"))
                follow_up = onboarding_followup(label, item) or follow_up
                authorized = authorized or authorization_confirmed(item)
                if isinstance(item.get("configured"), bool):
                    configured = item["configured"]
                if label == "账号授权" and "url" in item:
                    authorization_url = safe_authorization_url(item["url"])
                    if authorization_url:
                        qr_png_base64 = safe_qr_png_base64(item.get("qr_png_base64"))
                        lines.append("授权链接已显示，请使用网易云 App 扫码或自行打开链接完成授权。")
                    else:
                        lines.append("未收到可显示的官方授权链接。请检查接入后重试。")
                if action in ("生成整理清单", "读取在线清单") and isinstance(item.get("path"), (str, Path)):
                    plan_path = _display_text(str(item["path"]), sensitive_values)
            self._results.put(_TaskResult(
                action, tuple(lines), authorization_url=authorization_url,
                plan_path=plan_path, follow_up=follow_up, authorized=authorized,
                qr_png_base64=qr_png_base64,
                configured=configured,
                status=status, last_result_lines=historical_lines,
                resumable=resumable,
            ))
        except Exception as error:
            message = public_error_message(error, self.public_error_type, sensitive_values=sensitive_values)
            self._results.put(_TaskResult(action, (message,), failed=True))

    def _consume_startup_authorization(self, result: _TaskResult) -> str | None:
        if result.action != "检查本地接入":
            return result.follow_up
        requested = self._authorize_on_start
        self._authorize_on_start = False
        if requested and not result.failed and result.configured is True:
            return "login"
        return result.follow_up

    def _poll_results(self) -> None:
        self._poll_after_id = None
        if self._closed:
            return
        try:
            while True:
                result = self._results.get_nowait()
                if isinstance(result, _TaskProgress):
                    self._show_progress(result)
                    continue
                follow_up = self._consume_startup_authorization(result)
                if result.action != "等待扫码授权" or result.lines != self._last_probe_lines:
                    self._append_log(result.lines)
                if result.action == "等待扫码授权":
                    self._last_probe_lines = result.lines
                if result.authorization_url is not None:
                    self._set_authorization_url(result.authorization_url)
                    if result.authorization_url:
                        if result.qr_png_base64 is not None:
                            self._show_qr_window(result.qr_png_base64)
                        self._begin_authorization_probe()
                if result.plan_path is not None:
                    self._plan_path.set(result.plan_path)
                if result.resumable is not None:
                    self._resumable = result.resumable
                if result.configured is not None:
                    self._apply_configuration(result.configured)
                self._set_busy(False)
                self._status.set(_result_status_text(result.action, result.status, failed=result.failed))
                if result.last_result_lines:
                    self._last_result.set(" ".join(result.last_result_lines))
                if getattr(self, "_close_after_task", False):
                    self._close()
                    return
                if result.authorized:
                    self._set_authorization_url("")
                    self._stop_authorization_probe()
                    self._close_qr_window()
                if follow_up == "login" and not result.failed:
                    self._start_task("账号授权", self.controller.login)
                elif follow_up == "discover" and not result.failed:
                    self._start_task("发现官方命令", self.controller.discover)
                elif result.action == "等待扫码授权":
                    if result.failed:
                        self._stop_authorization_probe()
                        self._append_log(("自动授权检查已停止。请完成扫码后手动检查接入。",))
                    elif not result.authorized:
                        self._schedule_authorization_probe()
        except queue.Empty:
            pass
        self._poll_after_id = self.root.after(100, self._poll_results)

    def _begin_authorization_probe(self) -> None:
        self._stop_authorization_probe()
        if not callable(getattr(self.controller, "authorization_probe", None)):
            self._append_log(("请完成扫码后点击“检查接入”。",))
            return
        self._probe_deadline = time.monotonic() + 5 * 60
        self._last_probe_lines = None
        self._append_log(("正在等待扫码授权，最多自动检查 5 分钟。",))
        self._schedule_authorization_probe()

    def _schedule_authorization_probe(self) -> None:
        if self._closed or self._probe_deadline is None:
            return
        if time.monotonic() >= self._probe_deadline:
            self._finish_authorization_wait()
            return
        self._probe_after_id = self.root.after(5000, self._probe_authorization_once)

    def _probe_authorization_once(self) -> None:
        self._probe_after_id = None
        if self._closed:
            return
        decision = authorization_probe_decision(time.monotonic(), self._probe_deadline, busy=self._busy)
        if decision == "stop":
            if self._probe_deadline is not None:
                self._finish_authorization_wait()
            return
        if decision == "wait":
            self._schedule_authorization_probe()
            return
        probe = getattr(self.controller, "authorization_probe", None)
        if not callable(probe) or not self._start_task("等待扫码授权", probe):
            self._stop_authorization_probe()

    def _stop_authorization_probe(self) -> None:
        if self._probe_after_id is not None:
            self.root.after_cancel(self._probe_after_id)
            self._probe_after_id = None
        self._probe_deadline = None

    def _finish_authorization_wait(self) -> None:
        self._stop_authorization_probe()
        self._close_qr_window()
        self._set_authorization_url("")
        self._append_log(("程序的自动等待已结束，已清除此前二维码与链接。请点击“账号授权”重新获取二维码。",))

    def _show_qr_window(self, png_base64: str) -> None:
        self._close_qr_window()
        try:
            image = tk.PhotoImage(master=self.root, data=png_base64)
            factor = max(1, math.ceil(max(image.width(), image.height()) / 320))
            if factor > 1:
                image = image.subsample(factor)
            self._qr_image = image
            window = tk.Toplevel(self.root)
            self._qr_window = window
            window.title(f"网易云扫码授权（获取于 {datetime.now().strftime('%H:%M:%S')}）")
            window.transient(self.root)
            window.geometry(f"360x{max(420, image.height() + 130)}")
            window.resizable(False, False)
            ttk.Label(window, image=image).pack(padx=15, pady=(15, 8))
            ttk.Label(window, text="使用网易云 App 扫码并确认授权。\n授权状态与等待时间见主窗口。", anchor="center").pack(pady=(0, 12))
            refresh = ttk.Button(
                window, text="重新获取二维码",
                command=lambda: self._start_task("账号授权", self.controller.login),
                state="disabled" if self._busy else "normal",
            )
            refresh.pack(pady=(0, 12))
            self._qr_refresh_button = refresh
            self._buttons.append(refresh)
            window.protocol("WM_DELETE_WINDOW", self._close_qr_window)
            window.lift()
        except Exception:
            self._close_qr_window()
            self._append_log(("二维码未能显示，可复制授权链接完成授权。",))

    def _close_qr_window(self) -> None:
        if self._qr_refresh_button is not None:
            if self._qr_refresh_button in self._buttons:
                self._buttons.remove(self._qr_refresh_button)
            self._qr_refresh_button = None
        if self._qr_window is not None:
            try:
                self._qr_window.destroy()
            except tk.TclError:
                pass
            self._qr_window = None
        self._qr_image = None

    def _append_log(self, lines: Sequence[str]) -> None:
        timestamp = datetime.now().strftime("%H:%M:%S")
        self._log.configure(state="normal")
        for line in lines:
            self._log.insert("end", f"[{timestamp}] {line}\n")
        self._log.see("end")
        self._log.configure(state="disabled")

    def _close(self) -> None:
        if self._busy:
            self._close_after_task = True
            self._request_pause()
            self._stop_authorization_probe()
            self._status.set("正在等待当前请求完成并核对，随后关闭窗口。")
            return
        self._closed = True
        self._stop_ui_callbacks()
        self._close_qr_window()
        self._private_key.set("")
        self.root.destroy()

    def _stop_ui_callbacks(self) -> None:
        self._stop_authorization_probe()
        self._stop_elapsed_timer()
        for attribute in ("_poll_after_id", "_startup_after_id"):
            identifier = getattr(self, attribute, None)
            if identifier is not None:
                try:
                    self.root.after_cancel(identifier)
                except tk.TclError:
                    pass
                setattr(self, attribute, None)


def launch_gui(controller: OrganizerController | None = None, *, authorize_on_start: bool = False) -> None:
    """Construct the controller lazily so importing UI helpers has no side effects."""
    from .service import Organizer, OrganizerError

    if controller is None:
        controller = Organizer(Path(__file__).resolve().parents[1])
    root = tk.Tk()
    OrganizerWindow(root, controller, OrganizerError, authorize_on_start=authorize_on_start)
    root.mainloop()
