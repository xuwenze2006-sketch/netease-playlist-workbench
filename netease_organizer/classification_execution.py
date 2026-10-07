"""One approved classification batch, with independent durable write evidence."""

import copy
import hashlib
import json
import re
import stat
import uuid
from pathlib import Path

from .create import ArtistExecutor
from .online import OnlineReader
from .rename import _name
from .runtime import OperationPaused
from .write_journal import write_lease, WriteLeaseBusy


MAX_RECORD_BYTES = 2 * 1024 * 1024
PLAN_FILE = "分类整理计划.json"
INTENT_FILE = "分类整理执行进度.json"
RECEIPT_FILE = "分类整理执行结果.json"
_MISSING = object()
_PLAN_FIELDS = {"kind", "version", "account_original_id", "account_id", "source_playlist_id",
                "original_source_playlist_id", "source_track_count", "source_track_ids", "jobs"}
_JOB_FIELDS = {"kind", "name", "dimension", "candidate_track_ids", "intended_visibility"}
_ITEM_FIELDS = {"kind", "name", "playlist_id", "original_playlist_id", "count", "expected_count",
                "phase", "status", "message", "creation_response_warning", "added_count"}
_PHASES = {"create_attempted", "playlist_identified", "add_attempted", "add_verified", "completed", "paused"}


class ClassificationError(RuntimeError):
    """Fixed safe explanations only; account responses are never included."""


def _invalid():
    raise ClassificationError("分类范围或执行记录不兼容，需先核对，不能继续提交账号修改。")


def _hex(value, size=32):
    if type(value) is not str or re.fullmatch(rf"[0-9a-fA-F]{{{size}}}", value) is None:
        _invalid()
    return value.upper() if size == 32 else value.lower()


def _decimal(value):
    if type(value) is not str or re.fullmatch(r"[1-9][0-9]{0,19}", value) is None:
        _invalid()
    return value


def _integer(value, maximum=10000):
    if type(value) is not int or not 0 <= value <= maximum:
        _invalid()
    return value


def _encoded(value):
    try:
        result = json.dumps(value, ensure_ascii=False, sort_keys=True,
                            separators=(",", ":"), allow_nan=False).encode("utf-8")
        if len(result) + 1 > MAX_RECORD_BYTES:
            _invalid()
        return result
    except (ValueError, TypeError, UnicodeError, RecursionError):
        _invalid()


def _ids(values):
    if type(values) is not list or not 1 <= len(values) <= 10000:
        _invalid()
    normalized = [_hex(value) for value in values]
    if len(set(normalized)) != len(normalized):
        _invalid()
    return normalized


def validate_plan(plan):
    """Bind exact readable source IDs; missing provider slots remain explicit."""
    if (type(plan) is not dict or set(plan) != _PLAN_FIELDS
            or plan.get("kind") != "approved_classification_plan" or type(plan.get("version")) is not int
            or plan["version"] != 1 or type(plan.get("jobs")) is not list or not 1 <= len(plan["jobs"]) <= 64):
        _invalid()
    source = _ids(plan["source_track_ids"])
    count = _integer(plan["source_track_count"])
    if count < len(source):
        _invalid()
    jobs, names, covered = [], set(), set()
    for job in plan["jobs"]:
        if (type(job) is not dict or set(job) != _JOB_FIELDS
                or job.get("kind") != "create_classification_playlist" or not _name(job.get("name"))
                or job["name"] in names or type(job.get("dimension")) is not str
                or job["dimension"] not in {"scene", "style", "language"}
                or job.get("intended_visibility") != "provider_default"):
            _invalid()
        tracks = _ids(job["candidate_track_ids"])
        if not set(tracks).issubset(source):
            _invalid()
        names.add(job["name"])
        covered.update(tracks)
        jobs.append({**job, "candidate_track_ids": tracks})
    if covered != set(source):
        _invalid()
    normalized = {**plan, "account_original_id": _decimal(plan["account_original_id"]),
                  "account_id": _hex(plan["account_id"]), "source_playlist_id": _hex(plan["source_playlist_id"]),
                  "original_source_playlist_id": _decimal(plan["original_source_playlist_id"]),
                  "source_track_ids": source, "jobs": jobs}
    _encoded(normalized)
    return normalized


def plan_digest(plan):
    return hashlib.sha256(_encoded(validate_plan(plan))).hexdigest()


def _jobs(plan):
    return [{key: value for key, value in job.items() if key != "dimension"} for job in plan["jobs"]]


def _result(raw, plan):
    if (type(raw) is not dict or raw.get("status") not in
            {"running", "completed", "paused", "partial", "blocked", "uncertain"}
            or type(raw.get("items")) is not list or len(raw["items"]) != len(plan["jobs"])):
        _invalid()
    result = {"status": raw["status"], "completed_count": _integer(raw.get("completed_count"), 64), "items": []}
    for key in ("applied_to_account", "write_attempted", "outcome_known"):
        if type(raw.get(key)) is not bool:
            _invalid()
        result[key] = raw[key]
    identities, originals = set(), set()
    required_item_fields = _ITEM_FIELDS - {"creation_response_warning", "added_count"}
    for item, job in zip(raw["items"], plan["jobs"]):
        if (type(item) is not dict or not set(item).issubset(_ITEM_FIELDS)
                or not required_item_fields.issubset(item)
                or item.get("kind") != "create_classification_playlist" or item.get("name") != job["name"]
                or type(item.get("status")) is not str
                or item["status"] not in {"completed", "pending", "blocked", "partial", "uncertain"}
                or type(item.get("phase")) is not str
                or item["phase"] not in {"pending", "created", "adding", "added", "completed"}
                or item.get("expected_count") != len(job["candidate_track_ids"])
                or type(item.get("expected_count")) is not int
                or type(item.get("message")) is not str or len(item["message"]) > 512):
            _invalid()
        bound = dict(item)
        bound["count"] = _integer(item.get("count"))
        if item.get("playlist_id") is None:
            if item.get("original_playlist_id") is not None or item["phase"] != "pending" or bound["count"] != 0:
                _invalid()
        else:
            bound["playlist_id"] = _hex(item["playlist_id"])
            bound["original_playlist_id"] = _decimal(item.get("original_playlist_id"))
            if bound["playlist_id"] in identities or bound["original_playlist_id"] in originals:
                _invalid()
            identities.add(bound["playlist_id"])
            originals.add(bound["original_playlist_id"])
        if "added_count" in item:
            bound["added_count"] = _integer(item["added_count"], len(job["candidate_track_ids"]))
        if "creation_response_warning" in item and type(item["creation_response_warning"]) is not bool:
            _invalid()
        if (item["status"] == "completed" or item["phase"] == "completed") and (
                item["status"] != "completed" or item["phase"] != "completed" or item.get("playlist_id") is None
                or bound["count"] != item["expected_count"] or bound.get("added_count") != item["expected_count"]):
            _invalid()
        result["items"].append(bound)
    if result["completed_count"] != sum(item["status"] == "completed" for item in result["items"]):
        _invalid()
    if identities and (not result["applied_to_account"] or not result["write_attempted"]):
        _invalid()
    return result


def _journal(raw):
    if (type(raw) is not dict or raw.get("kind") != "classification_execution_journal"
            or type(raw.get("version")) is not int or raw["version"] != 1
            or type(raw.get("phase")) is not str or raw["phase"] not in _PHASES):
        _invalid()
    plan = validate_plan(raw.get("plan"))
    result = _result(raw, plan)
    index = _integer(raw.get("job_index"), len(plan["jobs"]) - 1)
    if (raw.get("jobs") != _jobs(plan) or raw.get("expected_owner_id") != plan["account_original_id"]
            or _hex(raw.get("plan_digest"), 64) != plan_digest(plan)):
        _invalid()
    offset, count = _integer(raw.get("add_offset")), _integer(raw.get("add_count"), 300)
    if offset + count > len(plan["jobs"][index]["candidate_track_ids"]):
        _invalid()
    normalized = {**result, "kind": "classification_execution_journal", "version": 1,
                  "run_id": _hex(raw.get("run_id")).lower(), "plan_digest": plan_digest(plan), "plan": plan,
                  "phase": raw["phase"], "job_index": index, "jobs": _jobs(plan),
                  "expected_owner_id": plan["account_original_id"], "add_offset": offset, "add_count": count}
    if set(raw) != set(normalized):
        _invalid()
    _encoded(normalized)
    return normalized


def _unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            _invalid()
        result[key] = value
    return result


def _read(controller, filename):
    relative = Path("artifacts") / filename
    path = controller.project / relative
    if path.is_symlink():
        _invalid()
    path = controller._workspace_target(relative)
    try:
        before = path.stat()
    except FileNotFoundError:
        return _MISSING
    if not stat.S_ISREG(before.st_mode) or before.st_size > MAX_RECORD_BYTES:
        _invalid()
    with path.open("rb") as stream:
        content = stream.read(MAX_RECORD_BYTES + 1)
    after = path.stat()
    if (len(content) > MAX_RECORD_BYTES or (before.st_ino, before.st_size, before.st_mtime_ns)
            != (after.st_ino, after.st_size, after.st_mtime_ns)):
        _invalid()
    value = json.loads(content.decode("utf-8"), object_pairs_hook=_unique,
                       parse_constant=lambda _: _invalid())
    if type(value) is not dict:
        _invalid()
    return value


def classification_pending(controller):
    """No CLI access: any unmatched attempted write survives Python restarts."""
    try:
        intent, receipt = _read(controller, INTENT_FILE), _read(controller, RECEIPT_FILE)
        if intent is _MISSING and receipt is _MISSING:
            return False
        if intent is _MISSING or receipt is _MISSING:
            return True
        intent = _journal(intent)
        result = _result(receipt, intent["plan"])
        return not (receipt.get("kind") == "classification_execution_receipt"
                    and type(receipt.get("version")) is int and receipt["version"] == 1
                    and _hex(receipt.get("run_id")).lower() == intent["run_id"]
                    and _hex(receipt.get("plan_digest"), 64) == intent["plan_digest"]
                    and _hex(receipt.get("intent_digest"), 64) == hashlib.sha256(_encoded(intent)).hexdigest()
                    and receipt.get("record_saved") is True and result["outcome_known"] is True
                    and result["status"] in {"completed", "paused", "partial", "blocked"}
                    and result["items"] == intent["items"]
                    and result["completed_count"] == intent["completed_count"]
                    and result["applied_to_account"] == intent["applied_to_account"]
                    and result["write_attempted"] == intent["write_attempted"]
                    and (result["status"] != "completed" or all(i["status"] == "completed" for i in result["items"])))
    except Exception:
        # Workspace boundary and parsing failures preserve protection, never break startup.
        return True


def _blocked(message, status="blocked"):
    return {"status": status, "completed_count": 0, "applied_to_account": False,
            "write_attempted": False, "outcome_known": True, "record_saved": False,
            "items": [], "message": message}


def execute_classification(controller, plan, *, accept_default_visibility=True):
    """Explicit new batch only; known or unknown prior batches are never replayed."""
    executor, confirmed_result, final_result = None, None, None
    try:
        plan = validate_plan(plan)
        if accept_default_visibility is not True:
            return _blocked("创建分类歌单需要接受官方默认可见性（可能公开）。")
        relative = Path(".organizer/account-write.lock")
        if (controller.project / relative).is_symlink():
            _invalid()
        with write_lease(controller._workspace_target(relative)):
            if controller.write_recovery()["status"] == "review_required":
                return {**_blocked("上次账号修改仍待核对，已保留记录；本次不会提交分类修改。"), "outcome_known": False}
            if _read(controller, INTENT_FILE) is not _MISSING or _read(controller, RECEIPT_FILE) is not _MISSING:
                return _blocked("分类批次已有执行记录，不会重复创建或添加；请先查看原记录。")
            previous_plan = _read(controller, PLAN_FILE)
            if previous_plan is not _MISSING and validate_plan(previous_plan) != plan:
                return _blocked("分类批准范围与已保存计划不一致，未提交账号修改。")
            if str(controller.reader.load()["owner_id"]) != plan["account_original_id"]:
                return _blocked("分类计划账号与本地账号不一致，未提交账号修改。")
            controller._require_credentials()
            controller.control.checkpoint()
            live = (controller.online_reader or OnlineReader(controller.cli, plan["account_original_id"],
                                                             control=controller.control)).read_snapshot()
            if ((live["account"]["original_id"], live["account"]["id"]) !=
                    (plan["account_original_id"], plan["account_id"])
                    or (live["liked"]["original_id"], live["liked"]["id"], live["liked"]["track_count"]) !=
                    (plan["original_source_playlist_id"], plan["source_playlist_id"], plan["source_track_count"])
                    or [track["id"] for track in live["liked"]["tracks"]] != plan["source_track_ids"]):
                return _blocked("新鲜在线账号、红心歌单或歌曲全集与批准范围不一致，未提交账号修改。")
            controller.control.checkpoint()
            controller._write_artifact(PLAN_FILE, _encoded(plan).decode("utf-8"))
            run_id, digest, last_intent = uuid.uuid4().hex, plan_digest(plan), None

            def journal(state):
                nonlocal last_intent
                record = _journal({**state, "kind": "classification_execution_journal", "version": 1,
                                   "run_id": run_id, "plan_digest": digest, "plan": plan})
                controller._write_artifact(INTENT_FILE, _encoded(record).decode("utf-8"))
                last_intent = record

            executor = ArtistExecutor(controller.cli, plan["account_original_id"], journal,
                                      control=controller.control, progress_listener=controller._progress,
                                      job_kind="create_classification_playlist", batch_add_size=300,
                                      preserve_order=False, expected_account_id=plan["account_id"])
            result = executor.execute(_jobs(plan))
            result.update(source_expected_count=plan["source_track_count"],
                          source_observed_count=len(plan["source_track_ids"]),
                          source_missing_count=plan["source_track_count"] - len(plan["source_track_ids"]))
            confirmed_result = copy.deepcopy(result)
            if last_intent is None:
                return {**result, "record_saved": False,
                        "message": "分类写入前检查未通过或已暂停，没有发送账号修改。"}
            receipt = {**result, "kind": "classification_execution_receipt", "version": 1,
                       "run_id": run_id, "plan_digest": digest,
                       "intent_digest": hashlib.sha256(_encoded(last_intent)).hexdigest(), "record_saved": True}
            try:
                path = controller._write_artifact(RECEIPT_FILE, _encoded(receipt).decode("utf-8"))
            except Exception:
                final_result = {**result, "record_saved": False,
                                "message": "已确认的分类账号效果保留，但结果记录保存失败；请勿重复提交。"}
                return final_result
            final_result = {**result, "record_saved": True, "path": str(path),
                            "message": "分类执行结果已保存；已确认效果与未完成原因以独立记录为准。"}
            return final_result
    except OperationPaused:
        return _blocked("已暂停分类核对，没有发送账号修改。", "paused")
    except WriteLeaseBusy:
        return _blocked("另一个程序正在核对或修改账号，请等待当前操作完成。")
    except Exception:
        if final_result is not None:
            return {**final_result, "message": "本地执行收尾未完成，已确认的分类账号结果保留；请勿重复提交。"}
        if confirmed_result is not None:
            return {**confirmed_result, "record_saved": False,
                    "message": "分类结果记录未完成，已确认的账号效果保留；请勿重复提交。"}
        if executor is not None and getattr(executor, "write_attempted", False):
            result = executor._report("uncertain")
            return {**result, "outcome_known": False, "record_saved": False,
                    "message": "分类写入后未完成确认，已保留执行意图；不会重发或继续后续操作。"}
        return _blocked("分类计划、官方读取或本地执行保护未通过核对，未提交账号修改。")


def _resume_binding(plan, intent, receipt):
    """Bind the recorded created prefix before verifying songs or batch ends."""
    result = _result(receipt, plan)
    if (intent["plan"] != plan or receipt.get("kind") != "classification_execution_receipt"
            or type(receipt.get("version")) is not int or receipt["version"] != 1
            or _hex(receipt.get("run_id")).lower() != intent["run_id"]
            or _hex(receipt.get("plan_digest"), 64) != intent["plan_digest"]
            or _hex(receipt.get("intent_digest"), 64) != hashlib.sha256(_encoded(intent)).hexdigest()
            or receipt.get("record_saved") is not True or result["status"] == "completed"
            or not result["applied_to_account"] or not result["write_attempted"]):
        _invalid()
    prefix_count = next((index for index, item in enumerate(result["items"])
                         if item["playlist_id"] is None), len(result["items"]))
    if prefix_count == 0:
        _invalid()
    if intent["job_index"] != prefix_count - 1:
        if (intent["job_index"] != prefix_count or intent["phase"] != "paused"
                or result["status"] != "paused" or not result["outcome_known"]
                or not intent["outcome_known"] or result["items"] != intent["items"]):
            _invalid()
    for index, (before, after) in enumerate(zip(intent["items"], result["items"])):
        if any(before[key] != after[key] for key in
               ("kind", "name", "playlist_id", "original_playlist_id", "expected_count")):
            _invalid()
        if index < prefix_count:
            if after["playlist_id"] is None:
                _invalid()
            if index < prefix_count - 1 and (before["status"] != "completed" or after["status"] != "completed"):
                _invalid()
        elif (after["playlist_id"] is not None or after["phase"] != "pending"
              or after["count"] != 0 or after.get("added_count", 0) != 0):
            _invalid()
    return result, prefix_count


def _verified_batch_end(intent, prior, index, total):
    """Derive a boundary from durable batch coordinates, never observed length."""
    if intent["job_index"] != index or intent["phase"] not in {"add_attempted", "add_verified", "paused"}:
        return None
    offset, count = intent["add_offset"], intent["add_count"]
    if offset % 300 or count != min(300, total - offset) or count <= 0:
        _invalid()
    before = intent["items"][index]
    end = offset + count
    if intent["phase"] == "add_attempted":
        if before["count"] != offset or before.get("added_count", 0) != offset:
            _invalid()
    elif (before["count"] != end or before.get("added_count") != end or not intent["outcome_known"]
          or prior["items"][index]["count"] != end or prior["items"][index].get("added_count") != end):
        _invalid()
    if intent["phase"] == "paused" and not prior["outcome_known"]:
        _invalid()
    return end


def _resume_backup(controller, filename, raw, run_id, label):
    """Preserve the original bytes under a unique old-run name, before replacement."""
    name = f"分类整理恢复备份-{run_id}-{label}.json"
    existing = _read(controller, name)
    if _read(controller, filename) != raw:
        _invalid()
    path = controller._workspace_target(Path("artifacts") / filename)
    with path.open("rb") as stream:
        content = stream.read(MAX_RECORD_BYTES + 1)
    if len(content) > MAX_RECORD_BYTES:
        _invalid()
    if json.loads(content.decode("utf-8"), object_pairs_hook=_unique) != raw:
        _invalid()
    # The project's text writer appends one platform newline. Remove precisely
    # that newline, including CR on Windows, before passing back to the writer.
    if not content.endswith(b"\n"):
        _invalid()
    if existing is not _MISSING:
        backup = controller._workspace_target(Path("artifacts") / name)
        with backup.open("rb") as stream:
            old_content = stream.read(MAX_RECORD_BYTES + 1)
        if existing != raw or old_content != content:
            _invalid()
        return
    end = 2 if content.endswith(b"\r\n") else 1
    controller._write_artifact(name, content[:-end].decode("utf-8"))


def _preflight_progress(controller, step, completed, total, *, index=None, name=None):
    """Report only the bound read preflight, never account-write completion."""
    phase, label = {
        "source_start": ("checking", "正在只读核对账号与红心来源"),
        "playlist_start": ("reading", "正在只读核验已绑定分类歌单"),
        "playlist_verified": ("preflight", "已核验该分类歌单的已尝试范围"),
        "finished": ("preflight", "分类只读预检完成"),
    }[step]
    event = {"stage": "classification_preflight", "phase": phase, "step": step, "label": label,
             "completed_count": completed, "total_count": total}
    if index is not None:
        event.update(job_index=index, name=name)
    try:
        controller._progress(event)
    except Exception:
        # Display callbacks cannot change mutation eligibility or persistence.
        pass


def resume_verified_classification(controller, *, accept_default_visibility=True):
    """Verify recorded batch ends; continue only never-attempted song slices.

    Missing members from an attempted batch or an unbound same-name playlist
    can never be repaired by this entry point.
    """
    preserved, executor, merge, final_result = None, None, None, None
    try:
        if accept_default_visibility is not True:
            return _blocked("继续分类创建需要接受官方默认可见性（可能公开）。")
        relative = Path(".organizer/account-write.lock")
        if (controller.project / relative).is_symlink():
            _invalid()
        with write_lease(controller._workspace_target(relative)):
            recovery = controller.write_recovery()
            raw_intent = _read(controller, INTENT_FILE)
            intent = _journal(raw_intent)
            preserved = {**_result(intent, intent["plan"]), "status": "uncertain",
                         "outcome_known": False, "record_saved": False}
            plan = validate_plan(_read(controller, PLAN_FILE))
            raw_receipt = _read(controller, RECEIPT_FILE)
            prior, prefix_count = _resume_binding(plan, intent, raw_receipt)
            preserved = {**prior, "status": "uncertain", "outcome_known": False, "record_saved": False}
            allowed = recovery.get("status") == "review_required" and recovery.get("operation") == "classification"
            if not allowed and not (recovery.get("status") == "clear" and prior["status"] == "paused"
                                    and prior["outcome_known"] and intent["outcome_known"]
                                    and prior["items"] == intent["items"]):
                return {**preserved,
                        "message": "没有唯一可核对的分类批次，或其他账号修改仍待核对；已保留分类效果，不会继续写入。"}
            if str(controller.reader.load()["owner_id"]) != plan["account_original_id"]:
                _invalid()
            controller._require_credentials()
            _preflight_progress(controller, "source_start", 0, prefix_count)
            controller.control.checkpoint()
            live = (controller.online_reader or OnlineReader(controller.cli, plan["account_original_id"],
                                                             control=controller.control)).read_snapshot()
            if ((live["account"]["original_id"], live["account"]["id"]) !=
                    (plan["account_original_id"], plan["account_id"])
                    or (live["liked"]["original_id"], live["liked"]["id"], live["liked"]["track_count"]) !=
                    (plan["original_source_playlist_id"], plan["source_playlist_id"], plan["source_track_count"])
                    or len(plan["source_track_ids"]) != plan["source_track_count"]
                    or [track["id"] for track in live["liked"]["tracks"]] != plan["source_track_ids"]):
                _invalid()
            verifier = ArtistExecutor(controller.cli, plan["account_original_id"], lambda _: None,
                                      job_kind="create_classification_playlist", batch_add_size=300,
                                      preserve_order=False, expected_account_id=plan["account_id"])
            owner = verifier._account()
            directory = verifier._created()
            prefix = copy.deepcopy(prior["items"][:prefix_count])
            continuation = None
            for index, (job, item) in enumerate(zip(plan["jobs"], prior["items"])):
                controller.control.checkpoint()
                matches = [header for header in directory.values() if header["name"] == job["name"]]
                if index >= prefix_count:
                    if matches:
                        _invalid()
                    continue
                if (len(matches) != 1 or matches[0]["id"] != item["playlist_id"]
                        or matches[0]["original_id"] != item["original_playlist_id"]):
                    _invalid()
                _preflight_progress(controller, "playlist_start", index, prefix_count,
                                    index=index, name=job["name"])
                detail, members = verifier._snapshot(matches[0], owner)
                expected = job["candidate_track_ids"]
                boundary = _verified_batch_end(intent, prior, index, len(expected)) if index == prefix_count - 1 else None
                complete = detail["count"] == len(expected) and len(members) == len(expected) and set(members) == set(expected)
                if not complete:
                    if (index != prefix_count - 1 or boundary is None or not 0 < boundary < len(expected)
                            or detail["count"] != boundary or len(members) != boundary
                            or set(members) != set(expected[:boundary])):
                        _invalid()
                    continuation = {"playlist_id": item["playlist_id"],
                                    "original_playlist_id": item["original_playlist_id"],
                                    "name": item["name"], "verified_add_offset": boundary}
                    prefix[index].update(count=boundary, added_count=boundary, phase="adding", status="pending",
                                         message="已只读核验此前完整批次；仅可继续从未尝试的歌曲后缀。")
                if verifier._account() != owner:
                    _invalid()
                if complete:
                    prefix[index].update(count=len(expected), added_count=len(expected), phase="completed",
                                         status="completed", message="已只读核验完整分类歌曲集合及数量，无需重复创建或添加。")
                _preflight_progress(controller, "playlist_verified", index + 1, prefix_count,
                                    index=index, name=job["name"])
            if verifier._account() != owner:
                _invalid()
            _preflight_progress(controller, "finished", prefix_count, prefix_count)
            controller.control.checkpoint()
            _resume_backup(controller, INTENT_FILE, raw_intent, intent["run_id"], "执行进度")
            _resume_backup(controller, RECEIPT_FILE, raw_receipt, intent["run_id"], "执行结果")
            run_id, digest, last_intent = uuid.uuid4().hex, plan_digest(plan), None
            verified_items = prefix + prior["items"][prefix_count:]
            if continuation is not None:
                prefix_count -= 1
                prefix = prefix[:prefix_count]

            def combine(state):
                result = {**state, "items": copy.deepcopy(prefix) + copy.deepcopy(state["items"]),
                          "completed_count": prefix_count + state["completed_count"],
                          "applied_to_account": True, "write_attempted": True}
                if result["status"] == "blocked":
                    result["status"] = "partial"
                return result

            merge = combine

            def journal(state):
                nonlocal last_intent
                record = _journal({**combine(state), "jobs": _jobs(plan),
                                   "job_index": prefix_count + state["job_index"],
                                   "kind": "classification_execution_journal", "version": 1,
                                   "run_id": run_id, "plan_digest": digest, "plan": plan})
                controller._write_artifact(INTENT_FILE, _encoded(record).decode("utf-8"))
                last_intent = record

            # Keep a full-plan durable checkpoint before entering the suffix.
            # Its new run_id cannot match the old receipt, so protection remains.
            checkpoint = _journal({**prior, "status": "running", "items": verified_items,
                                   "completed_count": prefix_count, "outcome_known": True,
                                   "phase": "add_verified" if continuation is not None else "completed",
                                   "job_index": prefix_count if continuation is not None else prefix_count - 1,
                                   "jobs": _jobs(plan), "expected_owner_id": plan["account_original_id"],
                                   "add_offset": continuation["verified_add_offset"] - 300 if continuation is not None else 0,
                                   "add_count": 300 if continuation is not None else 0, "kind": "classification_execution_journal",
                                   "version": 1, "run_id": run_id, "plan_digest": digest, "plan": plan})
            controller._write_artifact(INTENT_FILE, _encoded(checkpoint).decode("utf-8"))
            last_intent = checkpoint
            if prefix_count == len(plan["jobs"]):
                result = _result({**checkpoint, "status": "completed"}, plan)
            else:
                def progress(state):
                    controller._progress({**state, "job_index": prefix_count + state.get("job_index", 0),
                                          "job_count": len(plan["jobs"]),
                                          "completed_count": prefix_count + state.get("completed_count", 0)})

                executor = ArtistExecutor(controller.cli, plan["account_original_id"], journal,
                                          control=controller.control, progress_listener=progress,
                                          job_kind="create_classification_playlist", batch_add_size=300,
                                          preserve_order=False, expected_account_id=plan["account_id"])
                jobs = _jobs(plan)[prefix_count:]
                raw_result = (executor.resume_verified_batches(jobs, continuation) if continuation is not None
                              else executor.execute(jobs))
                result = _result(combine(raw_result), plan)
            result.update(source_expected_count=plan["source_track_count"],
                          source_observed_count=len(plan["source_track_ids"]), source_missing_count=0)
            preserved = copy.deepcopy(result)
            receipt = {**result, "kind": "classification_execution_receipt", "version": 1,
                       "run_id": run_id, "plan_digest": digest,
                       "intent_digest": hashlib.sha256(_encoded(last_intent)).hexdigest(), "record_saved": True}
            try:
                path = controller._write_artifact(RECEIPT_FILE, _encoded(receipt).decode("utf-8"))
            except Exception:
                final_result = {**result, "record_saved": False,
                                "message": "已确认的分类账号效果保留，但恢复结果保存失败；请勿重复提交。"}
                return final_result
            final_result = {**result, "record_saved": True, "path": str(path),
                            "message": "已核对完整歌曲或已尝试批次成员，仅继续未尝试后缀；独立恢复结果已保存。"}
            return final_result
    except (Exception, OperationPaused):
        if final_result is not None:
            return {**final_result, "message": "恢复本地收尾未完成，已确认效果保留；请勿重复提交。"}
        if executor is not None and getattr(executor, "write_attempted", False) and merge is not None:
            preserved = merge(executor._report("uncertain"))
            preserved.update(outcome_known=False, record_saved=False)
        if preserved is not None:
            return {**preserved, "message": "分类原记录或批次成员未通过恢复核对，已保留效果并停止；不会补回已尝试批次缺失成员或重发。"}
        return {**_blocked("分类恢复记录或执行保护未通过核对；不会继续账号修改。", "uncertain"),
                "outcome_known": False}
