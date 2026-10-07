"""Bounded rename evidence and an OS-owned lease; no account operations."""

from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import re
import stat


MAX_INTENT_BYTES = 1024 * 1024
MAX_JOBS = 1000
MAX_NAME_LENGTH = 160
_JOB_FIELDS = {"playlist_id", "original_playlist_id", "old_name", "name"}
_ITEM_FIELDS = _JOB_FIELDS | {"kind", "status", "message"}
_INTENT_FIELDS = {"kind", "version", "run_id", "phase", "job_index", "jobs", "items",
                  "expected_owner_id", "owner_id", "before", "tracks_sha256"}
_BEFORE_FIELDS = {"name", "trackCount", "specialType", "trackUpdateTime"}
_KNOWN_STATUSES = {"completed", "paused", "partial", "blocked"}
_ITEM_STATUSES = {"completed", "skipped", "blocked", "pending"}
_MISSING = object()


class JournalError(RuntimeError):
    """Fixed safe journal errors only; never expose local data or paths."""


class WriteLeaseBusy(JournalError):
    """Another process owns this account-write lease."""


def _invalid():
    raise JournalError("名称写入记录不可用，需先核对，不能继续提交账号修改。")


def _text(value, limit=MAX_NAME_LENGTH):
    return (type(value) is str and bool(value.strip()) and len(value) <= limit
            and not any(ord(c) < 32 or 127 <= ord(c) <= 159 or 0xD800 <= ord(c) <= 0xDFFF for c in value))


def _hex(value, size, *, upper=False):
    if type(value) is not str or re.fullmatch(rf"[0-9a-fA-F]{{{size}}}", value) is None:
        _invalid()
    return value.upper() if upper else value.lower()


def _decimal(value):
    if type(value) is not str or re.fullmatch(r"[1-9][0-9]{0,31}", value) is None:
        _invalid()
    return value


def _integer(value, maximum):
    if type(value) is not int or not 0 <= value <= maximum:
        _invalid()
    return value


def _job(raw):
    if type(raw) is not dict or set(raw) != _JOB_FIELDS or not _text(raw.get("name")) or not _text(raw.get("old_name")):
        _invalid()
    return {"playlist_id": _hex(raw["playlist_id"], 32, upper=True),
            "original_playlist_id": _decimal(raw["original_playlist_id"]),
            "old_name": raw["old_name"], "name": raw["name"]}


def _encoded(value):
    try:
        content = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
                             allow_nan=False).encode("utf-8")
    except (ValueError, TypeError, UnicodeError, RecursionError):
        _invalid()
    if len(content) > MAX_INTENT_BYTES:
        _invalid()
    return content


def _validate_intent(raw):
    if (type(raw) is not dict or set(raw) != _INTENT_FIELDS
            or raw.get("kind") != "rename_execution_journal" or type(raw.get("version")) is not int
            or raw["version"] != 1 or raw.get("phase") != "rename_attempted"
            or type(raw.get("jobs")) is not list or not 1 <= len(raw["jobs"]) <= MAX_JOBS
            or type(raw.get("items")) is not list or len(raw["items"]) != len(raw["jobs"])):
        _invalid()
    index = _integer(raw.get("job_index"), len(raw["jobs"]) - 1)
    jobs = [_job(job) for job in raw["jobs"]]
    if len({job["playlist_id"] for job in jobs}) != len(jobs) or len({job["original_playlist_id"] for job in jobs}) != len(jobs):
        _invalid()
    items = []
    for position, item in enumerate(raw["items"]):
        if (type(item) is not dict or set(item) != _ITEM_FIELDS or item.get("kind") != "rename_playlist"
                or type(item.get("status")) is not str or not _text(item.get("message"), 512)):
            _invalid()
        identity = _job({key: item[key] for key in _JOB_FIELDS})
        if identity != jobs[position] or item["status"] not in ({"completed", "skipped"} if position < index else {"blocked"}):
            _invalid()
        items.append({**identity, "kind": "rename_playlist", "status": item["status"], "message": item["message"]})
    before = raw.get("before")
    if (type(before) is not dict or set(before) != _BEFORE_FIELDS
            or before.get("name") != jobs[index]["old_name"]):
        _invalid()
    before = {"name": before["name"], "trackCount": _integer(before.get("trackCount"), 10000),
              "specialType": _integer(before.get("specialType"), 2 ** 31 - 1),
              "trackUpdateTime": _integer(before.get("trackUpdateTime"), 2 ** 63 - 1)}
    if before["specialType"] == 5:
        _invalid()
    normalized = {"kind": "rename_execution_journal", "version": 1, "run_id": _hex(raw["run_id"], 32),
                  "phase": "rename_attempted", "job_index": index, "jobs": jobs, "items": items,
                  "expected_owner_id": _decimal(raw["expected_owner_id"]),
                  "owner_id": _hex(raw["owner_id"], 32, upper=True), "before": before,
                  "tracks_sha256": _hex(raw["tracks_sha256"], 64)}
    _encoded(normalized)
    return normalized


def _unique_object(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            _invalid()
        value[key] = item
    return value


def _reject_constant(value):
    _invalid()


def _signature(info):
    # Windows lstat/fstat can report different ctime meanings for the same
    # regular file. Identity, size and modification time remain comparable.
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns


def _regular(info):
    if not stat.S_ISREG(info.st_mode):
        _invalid()


def _read_json(path):
    descriptor = None
    try:
        path = Path(path)
        try:
            original = path.lstat()
        except FileNotFoundError:
            return _MISSING
        _regular(original)  # lstat rejects the link itself, including dangling links.
        if original.st_size > MAX_INTENT_BYTES:
            _invalid()
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0))
        opened = os.fstat(descriptor)
        _regular(opened)
        if _signature(opened) != _signature(original) or _signature(path.lstat()) != _signature(original):
            _invalid()
        with os.fdopen(descriptor, "rb") as stream:
            descriptor = None
            content = stream.read(MAX_INTENT_BYTES + 1)
        if len(content) > MAX_INTENT_BYTES or _signature(path.lstat()) != _signature(original):
            _invalid()
        return json.loads(content.decode("utf-8"), object_pairs_hook=_unique_object, parse_constant=_reject_constant)
    except JournalError:
        raise
    except (OSError, ValueError, TypeError, UnicodeError, RecursionError):
        _invalid()
    finally:
        if descriptor is not None:
            os.close(descriptor)


def read_rename_intent(path):
    """Missing is distinct from malformed; normalize only the strict safe schema."""
    value = _read_json(path)
    return None if value is _MISSING else _validate_intent(value)


def intent_digest(intent):
    return hashlib.sha256(_encoded(_validate_intent(intent))).hexdigest()


def receipt_resolves_intent(intent, receipt_path):
    """A different or incomplete receipt cannot release a pending attempt."""
    try:
        bound = _validate_intent(intent)
        receipt = _read_json(receipt_path)
        if (type(receipt) is not dict or _hex(receipt.get("run_id"), 32) != bound["run_id"]
                or _hex(receipt.get("intent_digest"), 64) != intent_digest(bound)
                or receipt.get("outcome_known") is not True or receipt.get("record_saved") is False
                or type(receipt.get("status")) is not str or receipt["status"] not in _KNOWN_STATUSES
                or type(receipt.get("items")) is not list or len(receipt["items"]) != len(bound["jobs"])):
            return False
        statuses = []
        for index, item in enumerate(receipt["items"]):
            if (type(item) is not dict or not _ITEM_FIELDS.issubset(item) or item.get("kind") != "rename_playlist"
                    or type(item.get("status")) is not str or item["status"] not in _ITEM_STATUSES
                    or _job({key: item[key] for key in _JOB_FIELDS}) != bound["jobs"][index]):
                return False
            if index < bound["job_index"] and item["status"] != bound["items"][index]["status"]:
                return False
            statuses.append(item["status"])
        completed = sum(status == "completed" for status in statuses)
        if (type(receipt.get("completed_count")) is not int or receipt["completed_count"] != completed
                or statuses[bound["job_index"]] not in {"completed", "blocked"}
                or (receipt["status"] == "completed" and any(status not in {"completed", "skipped"} for status in statuses))
                or (receipt["status"] == "blocked" and completed != 0)):
            return False
        return True
    except (JournalError, OSError, ValueError, TypeError, UnicodeError, RecursionError):
        return False


@contextmanager
def write_lease(path):
    """Nonblocking file lease: the OS releases ownership even on process death."""
    descriptor, stream = None, None
    acquired = False
    try:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.is_symlink():
            _invalid()
        descriptor = os.open(path, os.O_CREAT | os.O_RDWR | getattr(os, "O_BINARY", 0)
                             | getattr(os, "O_NOFOLLOW", 0), 0o600)
        opened = os.fstat(descriptor)
        _regular(opened)
        current = path.lstat()
        _regular(current)
        if (opened.st_dev, opened.st_ino) != (current.st_dev, current.st_ino):
            _invalid()
        stream = os.fdopen(descriptor, "r+b")
        descriptor = None
        if opened.st_size == 0:
            stream.write(b"\0")
            stream.flush()
        stream.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            raise WriteLeaseBusy("已有账号操作正在执行，请等待当前请求核对完成。") from None
        acquired = True
    except JournalError:
        if stream is not None:
            stream.close()
        if descriptor is not None:
            os.close(descriptor)
        raise
    except (OSError, ValueError, TypeError):
        if stream is not None:
            stream.close()
        if descriptor is not None:
            os.close(descriptor)
        raise JournalError("账号写入租约不可用，不能提交账号修改。") from None
    try:
        yield
    finally:
        try:
            if acquired:
                stream.seek(0)
                if os.name == "nt":
                    msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
        finally:
            stream.close()
