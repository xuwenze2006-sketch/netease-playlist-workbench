"""Verified name-only account writes through the official CLI boundary."""

import hashlib
import json
import re
import threading

from .runtime import OperationPaused
from .write_journal import MAX_INTENT_BYTES, MAX_JOBS, MAX_NAME_LENGTH


_EXECUTION_LOCK = threading.Lock()


def _emit_progress(listener, stage, *, name="", job_index=0, job_count=0, completed_count=0, status="running"):
    if callable(listener):
        try:
            listener({"stage": stage, "status": status, "name": name[:160],
                      "job_index": job_index, "job_count": job_count, "completed_count": completed_count})
        except Exception:
            pass


class RenameError(RuntimeError):
    """Only fixed public-safe explanations belong in this exception."""


def _decimal(value):
    if isinstance(value, int) and not isinstance(value, bool):
        value = str(value)
    return value if isinstance(value, str) and re.fullmatch(r"[1-9][0-9]{0,31}", value) else None


def _encrypted(value):
    return isinstance(value, str) and re.fullmatch(r"[0-9A-Fa-f]{32}", value) is not None


def _same_id(left, right):
    return _encrypted(left) and _encrypted(right) and left.upper() == right.upper()


def _name(value):
    return (isinstance(value, str) and bool(value.strip()) and len(value) <= MAX_NAME_LENGTH
            and not any(ord(character) < 32 or 127 <= ord(character) <= 159 for character in value))


def _data(response):
    if (not isinstance(response, dict) or type(response.get("code")) is not int
            or response["code"] != 200 or "data" not in response):
        raise RenameError("官方读取结果不兼容，不能确认歌单状态。")
    return response["data"]


class RenameExecutor:
    def __init__(self, cli, expected_owner_id, control=None, progress_listener=None, journal_writer=None):
        self.cli = cli
        self.expected_owner_id = _decimal(expected_owner_id)
        self._uncertain = False
        self.control = control
        self.progress_listener = progress_listener
        self.journal_writer = journal_writer

    def _journal(self, jobs, items, index, owner_id, before, tracks):
        if self.journal_writer is None:
            return
        normalized = [{**job, "playlist_id": job["playlist_id"].upper()} for job in jobs]
        state = {"phase": "rename_attempted", "job_index": index, "jobs": normalized,
                 "items": [{**item, "playlist_id": item["playlist_id"].upper()} for item in items],
                 "expected_owner_id": self.expected_owner_id, "owner_id": owner_id.upper(),
                 "before": {key: before[key] for key in ("name", "trackCount", "specialType", "trackUpdateTime")},
                 "tracks_sha256": hashlib.sha256("\n".join(tracks).encode("utf-8")).hexdigest()}
        if len(json.dumps(state, ensure_ascii=False, indent=2, allow_nan=False).encode("utf-8")) > MAX_INTENT_BYTES - 256:
            raise RenameError("名称写入意图超过本次保存范围。")
        self.journal_writer(state)

    def _checkpoint(self):
        if self.control is not None:
            self.control.checkpoint()

    def _progress(self, stage, items, index=0, status="running"):
        _emit_progress(self.progress_listener, stage, name=items[index]["name"] if items else "",
                       job_index=index + 1 if items else 0, job_count=len(items),
                       completed_count=sum(item["status"] == "completed" for item in items), status=status)

    def _pause(self, items, applied=False, write_attempted=False, outcome_known=True, index=0):
        for item in items:
            if item["status"] not in {"completed", "skipped"}:
                item.update(status="pending", message="已暂停，本项尚未写入；可在核验进度后继续。")
        self._progress("paused", items, index, "paused")
        return self._result("paused", items, applied, write_attempted, outcome_known)

    def _check_manifest(self):
        commands = self.cli.manifest()[1]
        if not isinstance(commands, list):
            raise RenameError("官方改名命令定义不兼容。")
        matches = [command for command in commands if isinstance(command, dict)
                   and command.get("command") == ["playlist", "updateName"]]
        if len(matches) != 1 or not isinstance(matches[0].get("parameters"), list):
            raise RenameError("未取得唯一且受支持的官方改名命令。")
        parameters = {}
        for parameter in matches[0]["parameters"]:
            if (not isinstance(parameter, dict) or not isinstance(parameter.get("name"), str)
                    or parameter["name"] in parameters
                    or ("required" in parameter and type(parameter["required"]) is not bool)):
                raise RenameError("官方改名参数定义不兼容。")
            parameters[parameter["name"]] = parameter
            if parameter.get("required") is True and parameter["name"] not in {"playlistId", "name"}:
                raise RenameError("官方改名命令新增必填参数，已停止执行。")
        for name in ("playlistId", "name"):
            parameter = parameters.get(name, {})
            if (parameter.get("type") != "string" or parameter.get("required") is not True
                    or (parameter.get("in") or parameter.get("location")) != "query"):
                raise RenameError("官方改名参数与已验证契约不一致。")

    def _account(self):
        account = _data(self.cli.run_json(["user", "info"]))
        if (not isinstance(account, dict) or _decimal(account.get("originalId")) != self.expected_owner_id
                or not _encrypted(account.get("id"))):
            raise RenameError("当前授权账户与待整理账户不一致。")
        return account["id"]

    @staticmethod
    def _detail(data, job, owner_id):
        if (not isinstance(data, dict) or not _same_id(data.get("id"), job["playlist_id"])
                or _decimal(data.get("originalId")) != job["original_playlist_id"]
                or not _same_id(data.get("creatorId"), owner_id)):
            raise RenameError("歌单身份或所有者不一致，已停止改名。")
        if (not _name(data.get("name")) or type(data.get("specialType")) is not int
                or data["specialType"] < 0 or type(data.get("trackCount")) is not int
                or not 0 <= data["trackCount"] <= 10000
                or type(data.get("trackUpdateTime")) is not int or data["trackUpdateTime"] < 0):
            raise RenameError("歌单资料或成员数量不兼容，不能确认完整状态。")
        if data["specialType"] == 5:
            raise RenameError("红心系统歌单不能执行名称整理。")
        return data

    def _get(self, job, owner_id):
        data = _data(self.cli.run_json(["playlist", "get", "--playlistId", job["playlist_id"]]))
        return self._detail(data, job, owner_id)

    def _tracks(self, job, expected_count):
        identifiers, seen = [], set()
        for offset in range(0, 10001, 500):
            page = _data(self.cli.run_json(["playlist", "tracks", "--playlistId", job["playlist_id"],
                                           "--limit", "500", "--offset", str(offset)]))
            if not isinstance(page, list) or len(page) > 500:
                raise RenameError("官方成员分页格式不兼容。")
            for track in page:
                ident = track.get("id") if isinstance(track, dict) else None
                if not _encrypted(ident) or ident.upper() in seen:
                    raise RenameError("歌单成员 ID 无效或分页重复，不能确认完整成员。")
                seen.add(ident.upper())
                identifiers.append(ident.upper())
            if len(identifiers) > 10000:
                raise RenameError("歌单成员超过本次完整校验范围。")
            if len(page) < 500:
                break
        if len(identifiers) != expected_count:
            raise RenameError("歌单成员数量与详情不一致，已停止改名。")
        return identifiers

    @staticmethod
    def _stable_detail(before, after):
        return all(before[field] == after[field]
                   for field in ("name", "trackCount", "specialType", "trackUpdateTime"))

    def _readback(self, job, owner_id, before, tracks):
        applied = False
        try:
            data = _data(self.cli.run_json(["playlist", "get", "--playlistId", job["playlist_id"]]))
            applied = (isinstance(data, dict) and _same_id(data.get("id"), job["playlist_id"])
                       and _decimal(data.get("originalId")) == job["original_playlist_id"]
                       and data.get("name") == job["name"])
            after = self._detail(data, job, owner_id)
            if (after["name"] not in {job["name"], job["old_name"]} or after["trackCount"] != before["trackCount"]
                    or after["specialType"] != before["specialType"]):
                return "unknown", applied
            if self._tracks(job, after["trackCount"]) != tracks:
                return "unknown", applied
            final = self._get(job, owner_id)
            if not self._stable_detail(after, final):
                return "unknown", applied
            return ("target" if after["name"] == job["name"] else "old"), applied
        except Exception:
            return "unknown", applied

    @staticmethod
    def _item(job):
        return {"kind": "rename_playlist", "playlist_id": job.get("playlist_id", ""),
                "original_playlist_id": job.get("original_playlist_id", ""),
                "name": job.get("name", ""), "old_name": job.get("old_name", ""),
                "status": "blocked", "message": "本项尚未执行。"}

    @staticmethod
    def _result(status, items, applied=False, write_attempted=False, outcome_known=True):
        return {"status": status, "completed_count": sum(item["status"] == "completed" for item in items),
                "applied_to_account": applied, "write_attempted": write_attempted,
                "outcome_known": outcome_known, "items": items}

    def execute(self, jobs):
        """Serialize this process; an uncertain executor cannot resend a write."""
        if self._uncertain or not _EXECUTION_LOCK.acquire(blocking=False):
            items = []
            if isinstance(jobs, list):
                for job in jobs[:MAX_JOBS]:
                    if isinstance(job, dict):
                        safe = {key: value for key, value in job.items()
                                if key in {"playlist_id", "original_playlist_id", "name", "old_name"}
                                and isinstance(value, str) and len(value) <= MAX_NAME_LENGTH}
                        item = self._item(safe)
                        item["message"] = "前次改名结果未确定或已有执行正在进行，本批未执行。"
                        items.append(item)
            return self._result("blocked", items, outcome_known=not self._uncertain)
        try:
            result = self._execute(jobs)
            self._uncertain = result["status"] == "uncertain"
            return result
        finally:
            _EXECUTION_LOCK.release()

    def _execute(self, jobs):
        """Send each accepted rename once; an uncertain write freezes the batch."""
        if not isinstance(jobs, list) or len(jobs) > MAX_JOBS:
            return self._result("blocked", [])
        items = []
        normalized = []
        encrypted_ids, original_ids = set(), set()
        for job in jobs:
            if (not isinstance(job, dict) or not _encrypted(job.get("playlist_id"))
                    or not isinstance(job.get("original_playlist_id"), str)
                    or _decimal(job["original_playlist_id"]) != job["original_playlist_id"]
                    or not _name(job.get("name")) or not _name(job.get("old_name"))
                    or job.get("kind", "rename_playlist") != "rename_playlist"):
                for item in items:
                    item["message"] = "改名任务格式不合法，整批未执行。"
                return self._result("blocked", items)
            normalized.append({key: job[key] for key in
                               ("playlist_id", "original_playlist_id", "name", "old_name")})
            items.append(self._item(normalized[-1]))
            if job["playlist_id"].upper() in encrypted_ids or job["original_playlist_id"] in original_ids:
                for item in items:
                    item["message"] = "改名任务包含重复歌单，整批未执行。"
                return self._result("blocked", items)
            encrypted_ids.add(job["playlist_id"].upper())
            original_ids.add(job["original_playlist_id"])
        if not normalized:
            return self._result("completed", [])
        try:
            self._checkpoint()
            self._progress("preflight", items)
            self._checkpoint()
            if self.expected_owner_id is None:
                raise RenameError("待整理账户 ID 不合法。")
            self._check_manifest()
        except OperationPaused:
            return self._pause(items)
        except Exception:
            for item in items:
                item["message"] = "账户或官方改名命令定义未通过校验，整批未执行。"
            return self._result("blocked", items)

        applied = False
        write_attempted, outcome_known = False, True
        for index, (job, item) in enumerate(zip(normalized, items)):
            stop_status = None
            try:
                self._checkpoint()
                self._progress("job_begin", items, index)
                self._checkpoint()
                owner_id = self._account()
                before = self._get(job, owner_id)
                if before["name"] == job["name"]:
                    item.update(status="skipped", message="歌单已是目标名称，本次未写入。")
                    self._progress("skipped", items, index)
                    continue
                if before["name"] != job["old_name"]:
                    raise RenameError("当前歌单名称与待整理旧名称不一致。")
                tracks = self._tracks(job, before["trackCount"])
                ready = self._get(job, owner_id)
                if not self._stable_detail(before, ready):
                    raise RenameError("完整成员读取期间歌单发生变化，已停止改名。")
            except OperationPaused:
                return self._pause(items, applied, write_attempted, outcome_known, index)
            except Exception as error:
                message = str(error) if isinstance(error, RenameError) else "改名前读取或身份校验失败，未发送改名。"
                item.update(status="blocked", message=message)
                stop_status = "blocked"

            if stop_status is None:
                try:
                    self._checkpoint()
                    self._progress("write", items, index)
                    self._checkpoint()
                except OperationPaused:
                    return self._pause(items, applied, write_attempted, outcome_known, index)
                try:
                    self._journal(normalized, items, index, owner_id, before, tracks)
                except Exception:
                    item.update(status="blocked", message="名称写入意图未能保存，本项未发送，已停止后续改名。")
                    stop_status = "blocked"

            if stop_status is None:
                response = None
                raised = False
                write_attempted = True
                try:
                    response = self.cli.run_json(["playlist", "updateName", "--playlistId", job["playlist_id"],
                                                  "--name", job["name"], "--userInput", "按用户要求规范歌单名称，保留歌单成员及顺序。"])
                except Exception:
                    raised = True
                known_code = isinstance(response, dict) and type(response.get("code")) is int
                state, observed = self._readback(job, owner_id, before, tracks)
                applied = applied or observed
                if known_code and response["code"] != 200 and state == "old":
                    item.update(status="blocked", message="官方未确认改名成功，读回仍是旧名称且成员一致，已停止且不会重发。")
                    stop_status = "blocked"
                elif state == "target" and (raised or (known_code and response["code"] == 200)):
                    item.update(status="completed", message="已读回目标名称，歌单身份、成员数量及顺序一致。")
                    self._progress("completed", items, index)
                else:
                    outcome_known = outcome_known and state == "target"
                    item.update(status="uncertain", message=("已读回目标名称，但接口响应或完整状态未通过确认；已停止后续改名。"
                                if observed else "改名结果未确定，已停止后续改名且不会重发，请核对后再执行。"))
                    stop_status = "uncertain"

            if stop_status is not None:
                for frozen in items[index + 1:]:
                    frozen.update(status="blocked", message="前项未通过确认，本项未执行。")
                completed = any(previous["status"] == "completed" for previous in items)
                status = "uncertain" if stop_status == "uncertain" else ("partial" if completed else "blocked")
                return self._result(status, items, applied, write_attempted, outcome_known)
        return self._result("completed", items, applied, write_attempted, outcome_known)
