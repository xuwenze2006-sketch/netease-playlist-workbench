"""Loopback-only workbench API; polling reads memory, never the account."""

from __future__ import annotations

import base64
import binascii
import copy
from collections.abc import Mapping
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import inspect
import io
import math
import mimetypes
from pathlib import Path
import re
import secrets
import threading
import time
from types import MappingProxyType
from urllib.parse import parse_qsl, unquote, urlsplit

from .authorization import safe_authorization_url
from .official_cli import CliError
from .service import OrganizerError
from .web_tracks import build_local_tracks
from .web_classification import build_local_classification, _parameters as _classification_parameters


_MAX_BODY = 65536
_LABELS = {
    "preview_names": "预览名称整理", "preview_full": "读取完整在线清单", "rename": "执行名称整理",
    "artists": "创建歌手精选", "resume": "继续暂停任务", "check": "检查本地接入",
    "login": "账号授权", "login_status": "检查授权状态", "authorization_probe": "等待扫码授权",
    "discover": "发现官方命令", "local_plan": "生成本地清单", "save_credentials": "保存接入凭证",
    "reconcile_renames": "只读核对上次改名", "read_playlist": "读取单个歌单明细",
}
_PHASES = {
    "checking": "核对账号与歌单", "preflight": "核对执行条件", "job_begin": "开始下一项",
    "create": "创建歌单", "creating": "创建歌单", "create_attempted": "创建歌单",
    "add": "添加歌曲", "adding": "添加歌曲", "add_attempted": "添加歌曲",
    "reorder": "排列歌曲", "reordering": "核对歌曲顺序", "reorder_attempted": "排列歌曲",
    "rename": "修改名称", "rename_attempted": "修改名称", "created": "空歌单已核对",
    "completed": "歌单已核对", "skipped": "已完成，跳过", "paused": "已暂停",
    "reading": "正在读取歌曲明细", "saving": "正在保存本地明细",
}
_STATUSES = {
    "completed", "paused", "partial", "uncertain", "blocked", "failed", "recorded", "pending",
    "created", "added", "not_attempted", "skipped", "authorization_pending", "authorization_required",
    "authorization_expired", "authorized", "credentials_saved", "first_setup_required",
    "schema_available", "offline_plan_ready", "online_plan_ready", "discovery_failed", "pause_requested",
}
_BOOL_FIELDS = {"authorized", "installed", "configured", "applied_to_account", "write_attempted",
                "outcome_known", "record_saved", "resumable", "account_writeback_available",
                "creation_response_warning"}
_COUNT_FIELDS = {
    "completed_count", "total_count", "job_count", "blocked_count", "command_count", "count",
    "expected_count", "rename_count", "reorder_count", "artist_playlist_count", "ready_count",
    "cached_liked_unique_track_count", "online_liked_expected_count", "online_liked_observed_count",
    "online_missing_record_count", "missing_metadata_count", "missing_count",
}
_MESSAGES = {
    "completed": "任务已完成，结果已核对。", "paused": "已暂停；已确认结果保留，后续步骤未执行。",
    "partial": "部分步骤已确认，后续步骤已停止，请查看本地记录。",
    "uncertain": "操作结果未能完整确认，已停止后续步骤，请勿重复提交。",
    "blocked": "执行条件未通过核对，本任务已停止。", "failed": "任务未能确认完成，请查看接入状态或本地记录。",
    "authorization_pending": "授权已开始，请用网易云手机应用扫码。",
    "authorized": "官方授权已确认，账号与歌单归属仍需核对。",
    "authorization_required": "尚未确认账号授权，请进行扫码授权。",
    "authorization_expired": "本次扫码等待已结束，请重新发起授权。",
    "credentials_saved": "接入凭证已交由官方 CLI 保存。", "first_setup_required": "请先配置官方开放平台凭证。",
    "schema_available": "已取得官方命令定义。", "offline_plan_ready": "本地候选清单已生成，仍待在线核对。",
    "online_plan_ready": "在线清单已核对，可查看本地预览记录。",
}
_ARTIFACTS = {"自动整理清单.md", "在线名称整理清单.md", "在线整理清单.md", "名称整理执行结果.json",
              "歌手精选执行结果.json", "official-commands.json"}
_ASSET_TYPES = {".html", ".js", ".css", ".svg", ".png", ".jpg", ".jpeg", ".webp", ".ico", ".woff", ".woff2"}
_WRITE_ACTIONS = {"rename", "artists", "resume"}
_CLEAR_RECOVERY = {"status": "clear", "operation": "unknown", "can_reconcile": False}
_UNKNOWN_RECOVERY = {"status": "review_required", "operation": "unknown", "can_reconcile": False}
_CLASSIFICATION_STEPS = {
    'source_start': '只读核验：核对原红心歌单与账号',
    'playlist_start': '只读核验：正在核对已创建的分类歌单',
    'playlist_verified': '只读核验：已确认该歌单此前尝试的范围',
    'finished': '只读核验完成；后续仅处理未尝试的步骤',
}
_DIAGNOSTICS = {
    "node_missing": ("尚未找到 Node.js，请按接入设置中的本地安装步骤完成准备。", "install_cli"),
    "cli_missing": ("尚未找到官方 CLI，请运行项目安装脚本，再检查本地接入。", "install_cli"),
    "credentials_required": ("尚未配置开放平台凭证，请在接入设置中保存 App ID 和私钥文件。", "configure_credentials"),
    "cli_timeout": ("官方 CLI 请求超时，结果尚未核对；请先查看任务记录。", "inspect_records"),
    "invalid_app_id": ("App ID 格式无效：仅支持128字符以内的字母、数字、下划线和连字符。", "correct_credentials"),
    "invalid_private_key": ("私钥内容不可用，请选择有效的 UTF-8 私钥文件，再保存凭证。", "correct_credentials"),
    "local_permission_denied": ("本地文件访问被拒绝，请检查项目目录及所选文件的访问权限。", "check_local_access"),
    "local_snapshot_unavailable": ("本地歌单资料不可用，请在桌面网易云登录目标账号并加载歌单，再主动更新清单。", "load_desktop_playlists"),
    "account_mismatch": ("授权账号与本地歌单账号不一致，请核对桌面账号，再用目标账号重新扫码。", "authorize_correct_account"),
    "operation_failed": (_MESSAGES["failed"], "inspect_records"),
}


def _diagnostic_fields(code, *, uncertain=False):
    if not isinstance(code, str) or code not in _DIAGNOSTICS:
        return {}
    message, step = _DIAGNOSTICS[code]
    return {"error_code": code,
            "message": _MESSAGES["uncertain"] if uncertain else message,
            "next_step": "inspect_records" if uncertain else step}


def _exception_result(error, action):
    # Only our typed branches are diagnostic evidence. Never inspect exception
    # text, third-party attributes, CLI output, or arguments.
    code = getattr(error, "code", "operation_failed") if isinstance(error, (CliError, OrganizerError)) else "operation_failed"
    if not isinstance(code, str) or code not in _DIAGNOSTICS:
        code = "operation_failed"
    uncertain = action in _WRITE_ACTIONS
    status = "uncertain" if uncertain else "failed" if code in {"operation_failed", "cli_timeout"} else "blocked"
    return {"status": status, "outcome_known": False, **_diagnostic_fields(code, uncertain=uncertain)}


def _count(value):
    return type(value) is int and 0 <= value <= 1000000


def _playlist_key(value):
    return type(value) is str and re.fullmatch(r"[1-9][0-9]{0,19}", value) is not None


def _text(value, limit=160):
    return (isinstance(value, str) and len(value) <= limit
            and not any(ord(c) < 32 or ord(c) == 127 or 0xD800 <= ord(c) <= 0xDFFF for c in value))


def _counts(raw):
    return {key: raw[key] for key in _COUNT_FIELDS if key in raw and _count(raw[key])}


def _items(raw, sensitive=()):
    if not isinstance(raw, list):
        return []
    result = []
    for item in raw[:1000]:
        if not isinstance(item, dict):
            continue
        safe = _counts(item)
        for key in ("name", "old_name"):
            if _text(item.get(key)) and not any(value and value in item[key] for value in sensitive):
                safe[key] = item[key]
        if isinstance(item.get("status"), str) and item["status"] in _STATUSES:
            safe["status"] = item["status"]
        if type(item.get("creation_response_warning")) is bool:
            safe["creation_response_warning"] = item["creation_response_warning"]
        if safe:
            result.append(safe)
    return result


def _safe_result(raw, action, project, sensitive=()):
    if not isinstance(raw, dict):
        return _exception_result(None, action)
    status = raw.get("status")
    status = status if isinstance(status, str) and status in _STATUSES else "failed"
    safe = {"status": status, "message": _MESSAGES.get(status, "操作已结束，请查看本地任务记录。"), **_counts(raw)}
    safe.update({key: raw[key] for key in _BOOL_FIELDS if key in raw and type(raw[key]) is bool})
    if "applied_to_account" in raw and raw["applied_to_account"] is None:
        safe["applied_to_account"] = None
    if action in _WRITE_ACTIONS and status == "failed" and raw.get("outcome_known") is not True:
        safe.update(status="uncertain", outcome_known=False, message=_MESSAGES["uncertain"], next_step="inspect_records")
    safe.update(_diagnostic_fields(raw.get("error_code"), uncertain=safe["status"] == "uncertain"))
    if safe["status"] == "uncertain":
        safe["next_step"] = "inspect_records"
    if action == "reconcile_renames" and safe["status"] == "completed" and safe.get("outcome_known") is True:
        safe["message"] = "上次改名结果已只读核对；后续整理仍暂停。"
    if action == "read_playlist":
        if _playlist_key(raw.get("playlist_key")):
            safe["playlist_key"] = raw["playlist_key"]
        if safe.get("record_saved") is True and safe["status"] in {"completed", "partial"}:
            safe["message"] = ("歌曲明细已读取并保存到本地。" if safe["status"] == "completed"
                               else "歌曲明细已保存到本地，部分歌曲或资料仍有缺口。")
        elif safe["status"] == "paused":
            safe["message"] = "已暂停，本地明细保留原来的记录。"
    if safe.get("record_saved") is False:
        if action == "read_playlist":
            if safe["status"] != "paused":
                safe["message"] += " 新明细保存状态未确认，可以查看本地记录后主动重试读取。"
        else:
            safe["message"] += " 本地结果记录未能保存，请勿重复提交。"
        safe["next_step"] = "inspect_records"
    if isinstance(raw.get("summary"), dict):
        safe["summary"] = _counts(raw["summary"])
    if isinstance(raw.get("performance"), dict):
        metrics = raw["performance"]
        safe["performance"] = {key: metrics[key] for key in ("call_count", "failure_count")
                               if _count(metrics.get(key))}
        elapsed = metrics.get("elapsed_seconds")
        if type(elapsed) in (int, float) and math.isfinite(elapsed) and 0 <= elapsed <= 86400:
            safe["performance"]["elapsed_seconds"] = elapsed
    if isinstance(raw.get("items"), list):
        safe["items"] = _items(raw["items"], sensitive)
    historical = raw.get("last_result")
    if isinstance(historical, dict) and historical.get("source") == "local_record":
        historical_status = historical.get("status")
        if isinstance(historical_status, str) and historical_status in _STATUSES and historical.get("operation") in ("artists", "renames", "classification"):
            safe["last_result"] = {"source": "local_record", "operation": historical["operation"],
                                   "status": historical_status, **_counts(historical),
                                   "items": _items(historical.get("items"), sensitive)}
            if type(historical.get("resumable")) is bool:
                safe["last_result"]["resumable"] = historical["resumable"]
    if project is not None and _text(raw.get("path"), 1024) and not any(value and value in raw["path"] for value in sensitive):
        try:
            path = Path(raw["path"]).resolve()
            if path.name in _ARTIFACTS and path.parent == (project / "artifacts").resolve():
                safe["path"] = str(path)
        except (OSError, ValueError):
            pass
    if action == "login":
        url = safe_authorization_url(raw.get("url"))
        if url:
            safe["url"] = url
            encoded = raw.get("qr_png_base64")
            if isinstance(encoded, str) and len(encoded) <= 256 * 1024:
                try:
                    png = base64.b64decode(encoded, validate=True)
                    if png.startswith(b"\x89PNG\r\n\x1a\n") and len(png) <= 192 * 1024:
                        safe["qr_png_base64"] = encoded
                except (ValueError, binascii.Error):
                    pass
    return safe


class WorkbenchApplication:
    def __init__(self, controller, state_provider=None, *, revision=None, control_key=None):
        self.controller, self.state_provider = controller, state_provider
        self._revision = revision
        self._control_key = control_key
        self.update_status = "none"
        self._review_required = False
        self._review_reasons = set()
        self.recovery = dict(_CLEAR_RECOVERY)
        self.session = secrets.token_hex(32)
        self.instance_id = secrets.token_hex(16)
        self.last_activity_at = time.monotonic()
        self.lock = threading.RLock()
        # Serialize local refresh with job submission without blocking status,
        # page attachment or shutdown signals during bounded disk reads.
        # Always acquire this gate before self.lock when both are needed.
        self._records_gate = threading.RLock()
        self.worker = None
        self.job = None
        self.busy = False
        self.stopping = False
        self.stop_requested = False
        self.pause_requested = False
        self.page_closing = False
        self.page_attached = False
        self.job_session = None
        self.login_started = None
        self.startup_login_job = None
        self.startup_login_at = None
        self.started = None
        project = getattr(controller, "project", None)
        self.project = Path(project).resolve() if isinstance(project, (str, Path)) else None
        self.connection = {"installed": False, "configured": False, "authorized": None}
        try:
            doctor = _safe_result(controller.doctor(), "check", self.project)
            self._update_connection({key: doctor[key] for key in ("installed", "configured") if key in doctor})
        except Exception:
            pass
        self.data = self._local_data()
        self._refresh_local_recovery()
        controller.set_progress_listener(self._progress)

    @property
    def revision(self):
        return self._revision

    def valid_control(self, control):
        return (self._control_key is not None and type(control) is str
                and re.fullmatch(r"[a-fA-F0-9]{64}", control) is not None
                and secrets.compare_digest(control.lower(), self._control_key))

    @staticmethod
    def _needs_review(job):
        if not isinstance(job, dict):
            return False
        result = job.get("result")
        result = result if isinstance(result, dict) else {}
        return (job.get("status") == "uncertain" or result.get("status") == "uncertain"
                or (result.get("write_attempted") is True and result.get("outcome_known") is False)
                or (result.get("record_saved") is False and result.get("applied_to_account") is True))

    def _refresh_local_recovery(self):
        """Read only the controller's declared local contract, never Mock-created attributes."""
        try:
            declared = inspect.getattr_static(self.controller, "write_recovery", None)
            if declared is None:
                recovery = dict(_CLEAR_RECOVERY)
            else:
                method = getattr(self.controller, "write_recovery")
                raw = method() if callable(method) else None
                if (not isinstance(raw, dict) or type(raw.get("status")) is not str
                        or raw["status"] not in {"clear", "review_required"}
                        or type(raw.get("operation")) is not str
                        or raw["operation"] not in {"renames", "artists", "classification", "unknown"}
                        or type(raw.get("can_reconcile")) is not bool
                        or (raw["can_reconcile"] and (raw["status"] != "review_required" or raw["operation"] != "renames"))):
                    recovery = dict(_UNKNOWN_RECOVERY)
                else:
                    recovery = {key: raw[key] for key in _CLEAR_RECOVERY}
        except Exception:
            recovery = dict(_UNKNOWN_RECOVERY)
        self.recovery = recovery
        if recovery["status"] == "review_required":
            self._review_required = True
            self._review_reasons.add(recovery["operation"])
        if self._review_required and not self.stopping:
            self.update_status = "review_required"
        return recovery

    def request_update(self, instance, revision):
        """Claim an idle handoff; never pause work or queue an automatic retry."""
        with self.lock:
            if instance.lower() != self.instance_id:
                return 409, {"accepted": False, "reason": "instance_mismatch"}
            if self.revision is None:
                return 409, {"accepted": False, "reason": "unsupported"}
            self._refresh_local_recovery()
            if revision.lower() == self.revision:
                return 200, {"accepted": False, "reason": "current"}
            if self.busy:
                self.update_status = "busy"
                return 409, {"accepted": False, "reason": "busy"}
            if self._review_required or self._needs_review(self.job):
                self.update_status = "review_required"
                return 409, {"accepted": False, "reason": "review_required"}
            self.update_status = "stopping"
            self.stopping = self.stop_requested = True
            return 202, {"accepted": True, "reason": "stopping"}

    def health(self):
        with self.lock:
            status = 503 if self.stopping else 200
            result = {"kind": "netease-organizer-stopping" if self.stopping else "netease-organizer-local"}
            if not self.stopping:
                result["instance"] = self.instance_id
            if self.revision is not None:
                result.update(revision=self.revision, update_status=self.update_status)
            return status, result

    def _local_data(self):
        fallback = {"account": None, "playlists": [], "history": None, "source": "unavailable",
                    "updated_at": None, "artists_completed": False}
        try:
            raw = self.state_provider() if callable(self.state_provider) else fallback
            if isinstance(raw, dict):
                return json.loads(json.dumps(raw, ensure_ascii=False, allow_nan=False))
        except Exception:
            pass
        return fallback

    def _strict_local_data(self):
        """Explicit local refresh must report failure instead of publishing an empty fallback."""
        if callable(self.state_provider):
            raw = self.state_provider()
        elif self.state_provider is None and self.project is not None:
            from .web_state import build_local_state
            declared = inspect.getattr_static(self.controller, '_last_result', None)
            raw = build_local_state(self.project, organizer=self.controller if callable(declared) else None)
        else:
            raise ValueError('local state unavailable')
        if (not isinstance(raw, dict) or raw.get('source') not in ('local_record', 'empty')
                or not isinstance(raw.get('playlists'), list) or len(raw['playlists']) > 1000
                or any(not isinstance(row, dict) for row in raw['playlists'])
                or raw.get('account') is not None and not isinstance(raw['account'], dict)
                or raw.get('history') is not None and not isinstance(raw['history'], dict)
                or type(raw.get('artists_completed')) is not bool
                or raw.get('updated_at') is not None and not _text(raw['updated_at'], 80)
                or raw.get('preview') is not None and not isinstance(raw['preview'], dict)):
            raise ValueError('local state unavailable')
        if raw.get('account') is not None and (not _text(raw['account'].get('nickname'), 128)
                                               or not raw['account']['nickname'].strip()):
            raise ValueError('local account display unavailable')
        for row in raw['playlists']:
            if (not _playlist_key(row.get('key')) or not _text(row.get('name')) or not row['name'].strip()
                    or not _count(row.get('track_count')) or row['track_count'] > 10000
                    or row.get('category') not in ('liked', 'artist', 'normal')
                    or row.get('source') != 'local_record'):
                raise ValueError('local playlist display unavailable')
        return json.loads(json.dumps(raw, ensure_ascii=False, allow_nan=False))

    def _local_record_signatures(self):
        """Only fixed local display evidence; never inspect credential or CLI directories."""
        if self.project is None:
            raise ValueError('local project unavailable')
        from .web_classification import _signatures
        from .web_state import directory_signatures
        extra = []
        for name in ('名称整理执行结果.json', '名称整理执行进度.json',
                     '在线名称整理清单.json', '在线整理清单.json'):
            try:
                info = (self.project / 'artifacts' / name).lstat()
                extra.append((info.st_dev, info.st_ino, info.st_mode, info.st_size, info.st_mtime_ns, info.st_ctime_ns))
            except FileNotFoundError:
                extra.append(None)
        return _signatures(self.project), directory_signatures(self.project), tuple(extra)

    def refresh_local_records(self, *, offset=0, limit=50, query='', dimension='all', tag='', review='all', session=None):
        """Publish one idle local generation; a display refresh cannot resolve a write outcome."""
        _classification_parameters(offset, limit, query, dimension, tag, review)
        with self._records_gate:
            with self.lock:
                if session is not None and not self.valid_session(session):
                    return 403, {'accepted': False, 'message': '页面会话已更新，本次本地刷新未执行。'}
                if self.busy or self.stopping or self.stop_requested:
                    return 409, {'accepted': False, 'message': '已有任务正在运行或工作台正在关闭，请稍后刷新本地记录。'}
                generation = self.session
            try:
                signatures = self._local_record_signatures()
                candidate = self._strict_local_data()
                classification = build_local_classification(self.project, offset=offset, limit=limit, query=query,
                                                             dimension=dimension, tag=tag, review=review)
                if (not isinstance(classification, dict) or classification.get('status') not in ('available', 'not_loaded')
                        or signatures != self._local_record_signatures()):
                    raise ValueError('local records changed or unavailable')
                with self.lock:
                    if generation != self.session:
                        return 403, {'accepted': False, 'message': '页面会话已更新，本次本地刷新未执行。'}
                    if self.busy or self.stopping or self.stop_requested or self.page_closing:
                        return 409, {'accepted': False, 'message': '已有任务正在运行或工作台正在关闭，请稍后刷新本地记录。'}
                    self.data = candidate
                    return 200, classification
            except Exception:
                return 503, {'accepted': False, 'message': '本地记录暂时无法完整刷新，原页面资料已保留，请稍后重试。'}

    def _update_connection(self, result):
        for field in self.connection:
            if type(result.get(field)) is bool:
                self.connection[field] = result[field]
        if result.get("status") == "credentials_saved":
            self.connection["configured"] = True
        if result.get("status") == "authorization_pending":
            self.connection["authorized"] = False

    def _log(self, message, level="info"):
        self.job["logs"].append({"time": datetime.now(timezone.utc).isoformat(), "level": level, "message": message})
        del self.job["logs"][:-100]

    def _progress(self, event):
        if not isinstance(event, dict):
            return
        with self.lock:
            if not self.busy or self.job is None:
                return
            phase = event.get("phase", event.get("stage"))
            label = _PHASES.get(phase, "正在核对") if isinstance(phase, str) else "正在核对"
            progress = self.job["progress"]
            step = event.get('step')
            if event.get('stage') == 'classification_preflight' and isinstance(step, str) and step in _CLASSIFICATION_STEPS:
                label = _CLASSIFICATION_STEPS[step]
                progress['stage'] = 'classification_preflight'
            else:
                progress.pop('stage', None)
            if not self.pause_requested:
                progress["label"] = label
            for key in ("completed_count", "total_count"):
                if _count(event.get(key)):
                    progress[key] = event[key]
            if progress["total_count"]:
                progress["completed_count"] = min(progress["completed_count"], progress["total_count"])
            if not self.job["logs"] or self.job["logs"][-1]["message"] != label:
                self._log(label)

    def state(self):
        with self.lock:
            if self.job is not None and self.busy:
                self.job["progress"]["elapsed_seconds"] = round(max(0, time.monotonic() - self.started), 2)
            return copy.deepcopy({"connection": self.connection, "data": self.data, "job": self.job,
                                  "update": {"status": self.update_status}, "recovery": self.recovery})

    def touch(self):
        with self.lock:
            self.last_activity_at = time.monotonic()

    def attach_page(self):
        with self.lock:
            if self.stopping:
                return None
            self.session = secrets.token_hex(32)
            self.page_closing = False
            self.page_attached = True
            self.last_activity_at = time.monotonic()
            return self.session

    def claim_idle_stop(self, idle_seconds, *, now=None):
        """Sleeping documents keep their backend; expiry claims the stop atomically."""
        with self.lock:
            if self.stopping or (self.page_attached and not self.page_closing):
                return False
            current = time.monotonic() if now is None else now
            if current - self.last_activity_at < idle_seconds:
                return False
            self.stopping = self.stop_requested = True
            return True

    def bootstrap_page(self):
        """Pass an explicit CLI login to one document; never replay old login jobs."""
        with self.lock:
            session = self.attach_page()
            if session is None:
                return None, ""
            login_job = ""
            if (self.startup_login_job is not None and self.startup_login_at is not None
                    and time.monotonic() - self.startup_login_at < 300
                    and self.job is not None and self.job["action"] == "login"
                    and self.job["id"] == self.startup_login_job and not self.stopping):
                login_job = self.startup_login_job
            self.startup_login_job = self.startup_login_at = None
            return session, login_job

    def submit_startup_login(self):
        with self._records_gate, self.lock:
            status, result = self.submit("login", {})
            if status == 202:
                self.startup_login_job = result["job_id"]
                self.startup_login_at = time.monotonic()
            return status, result

    def valid_session(self, session, *, allow_job=False):
        with self.lock:
            if not isinstance(session, str) or re.fullmatch(r"[a-f0-9]{64}", session) is None:
                return False
            return (secrets.compare_digest(session, self.session)
                    or (allow_job and self.busy and session == self.job_session))

    def submit(self, action, payload, *, session=None):
        with self._records_gate, self.lock:
            if action == "read_playlist" and _valid_action({"action": action, "payload": payload}) is None:
                return 400, {"accepted": False, "message": "本地请求未通过检查。"}
            if self.page_closing or (session is not None and not self.valid_session(session)):
                return 409, {"accepted": False, "message": "页面会话已结束或更新，请重新加载工作台。"}
            if self.stopping or self.busy:
                return 409, {"accepted": False, "message": "已有任务正在运行或工作台正在关闭。"}
            self._refresh_local_recovery()
            if action in _WRITE_ACTIONS and (self._review_required or (
                    self.job is not None and self.job.get("action") in _WRITE_ACTIONS
                    and self._needs_review(self.job))):
                return 409, {"accepted": False, "message": "上次账号操作仍需核对，请先查看本地记录，勿重复提交。"}
            if action == "reconcile_renames" and not self.recovery["can_reconcile"]:
                return 409, {"accepted": False, "message": "没有可安全只读核对的改名意图，请先查看本地记录。"}
            if action == "authorization_probe" and (self.pause_requested or self.login_started is None
                                                     or time.monotonic() - self.login_started >= 300):
                return 409, {"accepted": False, "message": "没有正在等待的扫码授权，请主动发起授权。"}
            self.started, self.pause_requested = time.monotonic(), False
            self._operation_recovery = dict(self.recovery)
            self.job_session = self.session
            self.job = {"id": secrets.token_hex(12), "action": action, "label": _LABELS[action], "status": "queued",
                        "progress": {"label": _LABELS[action], "completed_count": 0, "total_count": 0, "elapsed_seconds": 0},
                        "result": None, "logs": []}
            if action == "read_playlist":
                self.job["playlist_key"] = payload["key"]
            self.busy = True
            try:
                if action != "authorization_probe":
                    self.controller.prepare_operation(_LABELS[action])
                self._log("任务已开始。")
                self.worker = threading.Thread(target=self._work, args=(action, payload),
                                               name="organizer-job", daemon=False)
                self.worker.start()
            except Exception:
                self.busy = False
                self.job.update(status="failed", result={"status": "failed", "message": _MESSAGES["failed"]})
                return 500, {"accepted": False, "message": "任务未能启动。"}
            return 202, {"accepted": True, "job_id": self.job["id"]}

    def _invoke(self, action, payload):
        if action in ("preview_names", "preview_full"):
            return self.controller.online_preview(names_only=action == "preview_names")
        if action == "artists":
            return self.controller.execute_artist_playlists(accept_default_visibility=True)
        if action == "save_credentials":
            return self.controller.save_credentials(payload["app_id"], payload["private_key"])
        if action == "read_playlist":
            return self.controller.read_playlist(payload["key"])
        method = {"rename": "execute_renames", "resume": "resume_last_operation", "check": "doctor",
                  "login": "login", "login_status": "login_status", "authorization_probe": "authorization_probe",
                  "discover": "discover", "local_plan": "prepare", "reconcile_renames": "reconcile_renames"}[action]
        return getattr(self.controller, method)()

    def _work(self, action, payload):
        try:
            with self.lock:
                paused = self.pause_requested
                if not paused:
                    self.job["status"] = "running"
            raw = {"status": "paused"} if paused else self._invoke(action, payload)
            sensitive = (payload["app_id"], payload["private_key"]) if action == "save_credentials" else ()
            result = _safe_result(raw, action, self.project, sensitive)
        except Exception as error:
            result = _exception_result(error, action)
        finally:
            if action == "read_playlist":
                result["playlist_key"] = payload["key"]
                result["write_attempted"] = False
                result["applied_to_account"] = False
                result["outcome_known"] = True
                if result["status"] not in {"completed", "partial"}:
                    result["record_saved"] = False
            payload.clear()
        data = self._local_data()
        with self.lock:
            self._update_connection(result)
            if action == "save_credentials" and result.get("status") == "credentials_saved":
                self.connection["authorized"] = None
                self.login_started = None
            if action == "login":
                self.login_started = time.monotonic() if result.get("url") and not self.pause_requested else None
            elif result.get("authorized") is True or result.get("status") == "authorization_expired":
                self.login_started = None
            status = result["status"]
            self.job["status"] = status if status in {"paused", "partial", "uncertain", "blocked", "failed"} else "completed"
            self.job["result"] = result
            self.job["progress"]["elapsed_seconds"] = round(max(0, time.monotonic() - self.started), 2)
            self._log(result["message"], "error" if status in {"failed", "uncertain", "blocked"} else "info")
            self.data = data
            self.busy = False
            if action in _WRITE_ACTIONS and self._needs_review(self.job):
                self._review_required = True
                reason = {"rename": "renames", "artists": "artists"}.get(action)
                if reason is None:
                    reason = self._refresh_local_recovery()["operation"]
                self._review_reasons.add(reason)
            recovery = self._refresh_local_recovery()
            if (action == "reconcile_renames" and result.get("status") == "completed"
                    and result.get("outcome_known") is True and result.get("record_saved") is True
                    and recovery["status"] == "clear"
                    and self._operation_recovery["operation"] == "renames"
                    and self._operation_recovery["can_reconcile"]):
                self._review_reasons.discard("renames")
                if not self._review_reasons:
                    self._review_required = False
            if not self.stopping:
                self.update_status = "review_required" if self._review_required else "none"

    def request_pause(self, *, session=None):
        with self.lock:
            if session is not None and not self.valid_session(session):
                return {"status": "blocked", "message": "页面会话已更新，本次暂停请求未执行。"}
            self.pause_requested = True
            self.login_started = None
            if self.busy:
                self.job["status"] = "pause_requested"
                self.job["progress"]["label"] = "正在暂停，等待当前请求核对完成"
                self._log("已请求暂停；当前请求完成核对后停止。")
            try:
                self.controller.request_pause()
            except Exception:
                return {"status": "failed", "message": "暂停请求未能确认，当前任务仍需完成核对。"}
        return {"status": "pause_requested", "message": "已请求暂停；当前请求完成核对后停止。"}

    def close_page(self, session):
        with self.lock:
            if not self.valid_session(session, allow_job=True):
                return 403, {"accepted": False, "message": "页面会话已更新，本次关闭请求未执行。"}
            if self.valid_session(session):
                self.page_closing = True
            result = self.request_pause()
            return 200, {**result, "message": "页面已离开，当前任务会完成核对后暂停。"}

    def request_stop(self):
        with self.lock:
            self.stopping = True
            self.stop_requested = True
        self.request_pause()

    def wait_for_idle(self, timeout=None):
        with self.lock:
            worker = self.worker
        if worker is not None and worker is not threading.current_thread():
            worker.join(timeout)
        return worker is None or not worker.is_alive()


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate key")
        result[key] = value
    return result


def _reject_constant(value):
    raise ValueError("nonfinite JSON")


def _valid_action(raw):
    if not isinstance(raw, dict) or set(raw) - {"action", "payload"}:
        return None
    action, payload = raw.get("action"), raw.get("payload", {})
    if not isinstance(action, str) or action not in _LABELS or not isinstance(payload, dict):
        return None
    if action == "artists":
        if set(payload) != {"accept_default_visibility"} or payload["accept_default_visibility"] is not True:
            return None
    elif action == "save_credentials":
        if (set(payload) != {"app_id", "private_key"} or not isinstance(payload["app_id"], str)
                or re.fullmatch(r"[A-Za-z0-9_-]{1,128}", payload["app_id"].strip()) is None
                or not isinstance(payload["private_key"], str) or not payload["private_key"].strip()
                or len(payload["private_key"].encode("utf-8")) > 65536):
            return None
    elif action == "read_playlist":
        if set(payload) != {"key"} or not _playlist_key(payload["key"]):
            return None
    elif payload:
        return None
    return action, payload


class _RequestInput(io.RawIOBase):
    """A receive deadline shared by request line, headers, and JSON body."""

    def __init__(self, connection, timeout):
        super().__init__()
        self.connection = connection
        self.deadline = time.monotonic() + timeout

    def readable(self):
        return True

    def readinto(self, buffer):
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError('local request receive deadline')
        current = self.connection.gettimeout()
        self.connection.settimeout(remaining if current is None else min(current, remaining))
        return self.connection.recv_into(buffer)


class _WorkbenchHandler(BaseHTTPRequestHandler):
    def setup(self):
        super().setup()
        self.connection.settimeout(3)
        self.rfile.close()
        self.rfile = io.BufferedReader(_RequestInput(self.connection, self.server.request_timeout))

    def log_message(self, format, *args):
        pass

    def send_error(self, code, message=None, explain=None):
        self._json(code, {"accepted": False, "message": "本地请求未通过检查。"})

    def _discard_rejected_body(self):
        """Avoid a Windows reset for small rejected bodies; never trust a huge length."""
        if getattr(self, "command", "") != "POST" or getattr(self, "_body_read", False):
            return
        headers = getattr(self, "headers", None)
        if headers is None or headers.get_all("Transfer-Encoding"):
            return
        lengths = headers.get_all("Content-Length", [])
        if len(lengths) != 1 or re.fullmatch(r"[0-9]{1,8}", lengths[0]) is None:
            return
        remaining = int(lengths[0])
        if remaining > _MAX_BODY + 1024:
            return
        self._body_read = True
        self.connection.settimeout(0.2)
        deadline = time.monotonic() + 0.2
        try:
            while remaining:
                left = deadline - time.monotonic()
                if left <= 0:
                    break
                self.connection.settimeout(left)
                chunk = self.rfile.read1(min(4096, remaining))
                if not chunk:
                    break
                remaining -= len(chunk)
        except OSError:
            pass
        finally:
            self.connection.settimeout(3)

    def _reply(self, status, body, content_type):
        if status >= 400:
            self._discard_rejected_body()
        # The receive deadline must not shorten completed response writes.
        self.connection.settimeout(3)
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy", "default-src 'self'; img-src 'self' data:; style-src 'self' 'unsafe-inline'; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'")
        self.send_header("Connection", "close")
        self.end_headers()
        self.close_connection = True
        self.wfile.write(body)

    def _json(self, status, data):
        self._reply(status, json.dumps(data, ensure_ascii=False, allow_nan=False).encode("utf-8"), "application/json; charset=utf-8")

    def _guard(self):
        expected = f"127.0.0.1:{self.server.server_address[1]}"
        host, origins = self.headers.get_all("Host", []), self.headers.get_all("Origin", [])
        session = self.headers.get_all("X-Organizer-Session", [])
        control = self.headers.get_all("X-Organizer-Control", [])
        try:
            api = unquote(urlsplit(self.path).path, errors="strict").startswith("/api/")
        except (ValueError, UnicodeError):
            self._json(400, {"accepted": False, "message": "请求路径无效。"})
            return False
        update = self.path == "/api/update"
        authenticated = (len(control) == 1 and self.server.application.valid_control(control[0])) if update else (
            len(session) == 1 and self.server.application.valid_session(session[0], allow_job=self.path == "/api/close"))
        if (host != [expected] or len(origins) > 1 or (origins and origins[0] != "http://" + expected)
                or self.headers.get("Sec-Fetch-Site") not in (None, "same-origin", "none")
                or len(self.headers.get_all("Sec-Fetch-Site", [])) > 1
                or (api and not authenticated)):
            self._json(403, {"accepted": False, "message": "请求来源或工作台会话无效。"})
            return False
        self._request_session = session[0] if len(session) == 1 else None
        return True

    def do_GET(self):
        if not self._guard():
            return
        try:
            url = urlsplit(self.path)
            if url.scheme or url.netloc or url.fragment:
                raise ValueError
            path = unquote(url.path, errors="strict")
            if not path.startswith("/") or "\\" in path or ":" in path or any(ord(c) < 32 for c in path):
                raise ValueError
            if any(part in (".", "..") for part in path.split("/")):
                raise ValueError
        except (ValueError, UnicodeError):
            self._json(400, {"accepted": False, "message": "请求路径无效。"})
            return
        if path == "/api/state":
            self.server.application.touch()
            self._json(200, self.server.application.state())
            return
        if path == '/api/classification':
            try:
                if len(url.query) > 4096:
                    raise ValueError()
                pairs = parse_qsl(url.query, keep_blank_values=True, strict_parsing=True,
                                  encoding='utf-8', errors='strict', max_num_fields=7)
                parameters = dict(pairs)
                if (len(parameters) != len(pairs)
                        or set(parameters) - {'offset', 'limit', 'q', 'dimension', 'tag', 'review', 'refresh'}
                        or 'refresh' in parameters and parameters['refresh'] != 'local'):
                    raise ValueError()
                offset, limit = parameters.get('offset', '0'), parameters.get('limit', '50')
                if (re.fullmatch(r'0|[1-9][0-9]{0,4}', offset) is None
                        or re.fullmatch(r'[1-9][0-9]{0,2}', limit) is None):
                    raise ValueError()
                arguments = {'offset': int(offset), 'limit': int(limit), 'query': parameters.get('q', ''),
                             'dimension': parameters.get('dimension', 'all'), 'tag': parameters.get('tag', ''),
                             'review': parameters.get('review', 'all')}
                _classification_parameters(**arguments)
                if parameters.get('refresh') == 'local':
                    status, data = self.server.application.refresh_local_records(**arguments, session=self._request_session)
                    if status != 200:
                        self._json(status, data)
                        return
                else:
                    data = build_local_classification(self.server.application.project, **arguments)
            except (ValueError, UnicodeError):
                self._json(400, {'accepted': False, 'message': '分类查询参数无效。'})
                return
            except Exception:
                self._json(503, {'accepted': False, 'message': '本地分类资料暂时无法读取，请稍后重试。'})
                return
            self.server.application.touch()
            self._json(200, data)
            return
        if path.startswith('/api/playlists/') and path.endswith('/tracks'):
            try:
                match = re.fullmatch(r'/api/playlists/([1-9][0-9]{0,19})/tracks', path)
                if match is None or len(url.query) > 2048:
                    raise ValueError()
                pairs = parse_qsl(url.query, keep_blank_values=True, strict_parsing=True,
                                  encoding='utf-8', errors='strict', max_num_fields=4)
                parameters = dict(pairs)
                if len(parameters) != len(pairs) or set(parameters) - {'offset', 'limit', 'q', 'metadata'}:
                    raise ValueError()
                offset, limit = parameters.get('offset', '0'), parameters.get('limit', '50')
                if (re.fullmatch(r'0|[1-9][0-9]{0,4}', offset) is None
                        or re.fullmatch(r'[1-9][0-9]{0,2}', limit) is None):
                    raise ValueError()
                if self.server.application.project is None:
                    raise RuntimeError()
                data = build_local_tracks(self.server.application.project, match[1],
                                          offset=int(offset), limit=int(limit), query=parameters.get('q', ''),
                                          metadata=parameters.get('metadata', 'all'))
            except (ValueError, UnicodeError):
                self._json(400, {'accepted': False, 'message': '歌曲明细请求参数无效。'})
                return
            except Exception:
                self._json(503, {'accepted': False, 'message': '本地歌曲明细暂时无法读取，请稍后重试。'})
                return
            self.server.application.touch()
            if data['status'] == 'missing_playlist':
                self._json(404, {'accepted': False, 'message': '该歌单已不在本地目录中，请更新页面后重试。'})
                return
            self._json(200, data)
            return
        if path == "/health":
            status, result = self.server.application.health()
            self._json(status, result)
            return
        if path.startswith("/api/"):
            self._json(404, {"accepted": False, "message": "没有此本地 API。"})
            return
        relative = "index.html" if path == "/" else path.lstrip("/")
        target = self.server.assets / relative
        try:
            if self.server.asset_snapshot is not None:
                if target.suffix.lower() not in _ASSET_TYPES or relative not in self.server.asset_snapshot:
                    raise OSError
                body = self.server.asset_snapshot[relative]
                index = relative == "index.html"
            else:
                target = target.resolve()
                if (not target.is_relative_to(self.server.assets) or target.suffix.lower() not in _ASSET_TYPES
                        or not target.is_file() or target.stat().st_size > 16 * 1024 * 1024):
                    raise OSError
                body = target.read_bytes()
                index = target == self.server.assets / "index.html"
            if index:
                session, login_job = self.server.application.bootstrap_page()
                if session is None:
                    self._json(503, {"accepted": False, "message": "工作台正在关闭，请稍后重新打开。"})
                    return
                body = body.replace(b"__ORGANIZER_SESSION__", session.encode("ascii"))
                body = body.replace(b"__ORGANIZER_LOGIN_JOB__", login_job.encode("ascii"))
            content_type = mimetypes.guess_type(str(target))[0] or "application/octet-stream"
            if target.suffix.lower() in {".html", ".js", ".css", ".svg"}:
                content_type += "; charset=utf-8"
            self._reply(200, body, content_type)
        except (OSError, ValueError):
            self._json(404, {"accepted": False, "message": "没有此本地资源。"})

    def do_POST(self):
        if not self._guard():
            return
        if self.path not in ("/api/actions", "/api/pause", "/api/close", "/api/update"):
            self._json(404, {"accepted": False, "message": "没有此本地 API。"})
            return
        lengths = self.headers.get_all("Content-Length", [])
        types = self.headers.get_all("Content-Type", [])
        if (len(lengths) != 1 or re.fullmatch(r"[0-9]{1,8}", lengths[0]) is None
                or self.headers.get_all("Transfer-Encoding") or len(types) != 1
                or types[0].split(";", 1)[0].strip().lower() != "application/json"):
            self._json(400, {"accepted": False, "message": "请求需使用有限长度的 JSON。"})
            return
        length = int(lengths[0])
        if length > _MAX_BODY:
            self._json(413, {"accepted": False, "message": "请求内容超过 64 KiB。"})
            return
        try:
            self._body_read = True
            body = self.rfile.read(length)
            if len(body) != length:
                raise ValueError
            raw = json.loads(body.decode("utf-8"), object_pairs_hook=_unique_object, parse_constant=_reject_constant)
            if self.path == "/api/update":
                if (not isinstance(raw, dict) or set(raw) != {"instance", "revision"}
                        or type(raw["instance"]) is not str or re.fullmatch(r"[a-fA-F0-9]{32}", raw["instance"]) is None
                        or type(raw["revision"]) is not str or re.fullmatch(r"[a-fA-F0-9]{64}", raw["revision"]) is None):
                    raise ValueError
                status, result = self.server.application.request_update(raw["instance"], raw["revision"])
                self._json(status, result)
                return
            if self.path in ("/api/pause", "/api/close"):
                if raw != {}:
                    raise ValueError
                self.server.application.touch()
                if self.path == "/api/close":
                    status, result = self.server.application.close_page(self._request_session)
                else:
                    result = self.server.application.request_pause(session=self._request_session)
                    status = 409 if result["status"] == "blocked" else 200
                self._json(status, result)
                return
            validated = _valid_action(raw)
            if validated is None:
                raise ValueError
        except (ValueError, UnicodeError, RecursionError, OSError):
            self._json(400, {"accepted": False, "message": "任务参数或 JSON 内容无效。"})
            return
        self.server.application.touch()
        status, result = self.server.application.submit(*validated, session=self._request_session)
        self._json(status, result)

    def _unsupported(self):
        if self._guard():
            self._json(405, {"accepted": False, "message": "此本地请求方法不受支持。"})

    do_HEAD = do_OPTIONS = do_PUT = do_PATCH = do_DELETE = _unsupported


class _WorkbenchServer(ThreadingHTTPServer):
    daemon_threads = False
    request_timeout = 3
    request_limit = 16

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._serving = threading.Event()
        self._lifecycle_lock = threading.Lock()
        self._request_slots = threading.BoundedSemaphore(self.request_limit)

    def process_request(self, request, client_address):
        if not self._request_slots.acquire(blocking=False):
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except BaseException:
            self._request_slots.release()
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._request_slots.release()

    def serve_forever(self, poll_interval=0.1):
        with self._lifecycle_lock:
            if self.application.stop_requested:
                return
            self._serving.set()
        try:
            super().serve_forever(poll_interval)
        finally:
            with self._lifecycle_lock:
                self._serving.clear()

    def shutdown(self):
        self.application.request_stop()
        self.application.wait_for_idle()
        with self._lifecycle_lock:
            serving = self._serving.is_set()
        if serving:
            super().shutdown()

    def server_close(self):
        application = getattr(self, "application", None)
        if application is not None:
            application.request_stop()
            application.wait_for_idle()
        super().server_close()

    def handle_error(self, request, client_address):
        pass


def _update_hex(value):
    if value is None:
        return None
    if type(value) is not str or re.fullmatch(r"[a-fA-F0-9]{64}", value) is None:
        raise ValueError("本地更新配置无效。")
    return value.lower()


def _snapshot_assets(raw):
    if raw is None:
        return None
    if not isinstance(raw, Mapping) or len(raw) > 1000:
        raise ValueError("本地前端快照无效。")
    snapshot, total = {}, 0
    for name, body in raw.items():
        if (type(name) is not str or not name or len(name) > 1024
                or "\\" in name or ":" in name or any(ord(c) < 32 or ord(c) == 127 for c in name)
                or any(part in ("", ".", "..") for part in name.split("/"))
                or Path(name).suffix.lower() not in _ASSET_TYPES
                or type(body) is not bytes or len(body) > 16 * 1024 * 1024):
            raise ValueError("本地前端快照无效。")
        total += len(body)
        if total > 64 * 1024 * 1024:
            raise ValueError("本地前端快照超过上限。")
        snapshot[name] = body
    return MappingProxyType(snapshot)


def create_server(controller, *, assets, state_provider=None, host="127.0.0.1", port=0,
                  revision=None, control_key=None, asset_snapshot=None):
    """Create without starting serving or any automatic account operation."""
    if host != "127.0.0.1" or type(port) is not int or not 0 <= port <= 65535:
        raise ValueError("工作台只能绑定本机 IPv4 地址及合法端口。")
    revision, control_key = _update_hex(revision), _update_hex(control_key)
    snapshot = _snapshot_assets(asset_snapshot)
    assets = Path(assets).resolve()
    if snapshot is None and not assets.is_dir():
        raise ValueError("本地前端构建目录不存在。")
    server = _WorkbenchServer((host, port), _WorkbenchHandler)
    try:
        server.assets = assets
        server.asset_snapshot = snapshot
        server.application = WorkbenchApplication(controller, state_provider, revision=revision, control_key=control_key)
    except Exception:
        server.server_close()
        raise RuntimeError("本地工作台初始化失败。") from None
    return server


def shutdown_server(server, *, timeout=None):
    """Call outside serve_forever's thread; finish readback before closing HTTP."""
    server.application.request_stop()
    if not server.application.wait_for_idle(timeout):
        return False
    server.shutdown()
    server.server_close()
    return True
