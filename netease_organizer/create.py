"""Create reviewed artist playlists once and verify every account mutation."""

import copy
import json

from .rename import _EXECUTION_LOCK, _decimal, _emit_progress, _encrypted, _name, _same_id
from .runtime import OperationPaused


class ArtistError(RuntimeError):
    """A fixed public-safe explanation, never an upstream response or error."""


class _JournalError(ArtistError):
    pass


_INTENT = "用户已同意按网易云官方默认可见性（可能公开）创建歌手精选，使用已审核歌曲列表并保持原顺序。"
_CLASSIFICATION_INTENT = "用户已同意按网易云官方默认可见性（可能公开）创建场景、风格及语言分类歌单，使用已审核的红心歌曲列表。"
_CONTRACTS = {
    "create": {"playlistName": ("query", "string")},
    "add": {"playlistId": ("query", "string"), "songIdList": ("query", "array")},
    "reorder": {"playlistId": ("body", "string"), "trackIds": ("body", "array")},
}


def _response_data(response, kind):
    if (not isinstance(response, dict) or type(response.get("code")) is not int
            or response["code"] != 200 or not isinstance(response.get("data"), kind)):
        raise ArtistError("官方只读响应不兼容，不能确认精选状态。")
    return response["data"]


def _integer(value, maximum):
    if type(value) is not int or not 0 <= value <= maximum:
        raise ArtistError("官方数量或时间字段不兼容。")
    return value


def _header(raw):
    if (not isinstance(raw, dict) or not _encrypted(raw.get("id"))
            or _decimal(raw.get("originalId")) is None or not _name(raw.get("name"))):
        raise ArtistError("官方歌单身份或名称字段不兼容。")
    return {"id": raw["id"].upper(), "original_id": _decimal(raw["originalId"]), "name": raw["name"],
            "count": _integer(raw.get("trackCount"), 10000),
            "special_type": _integer(raw.get("specialType"), 10000),
            "track_update_time": _integer(raw.get("trackUpdateTime"), 2**63 - 1)}


class ArtistExecutor:
    def __init__(self, cli, expected_owner_id, journal_writer, control=None, progress_listener=None,
                 *, job_kind="create_artist_playlist", batch_add_size=None, preserve_order=True,
                 expected_account_id=None):
        if (job_kind not in {"create_artist_playlist", "create_classification_playlist"}
                or type(preserve_order) is not bool
                or expected_account_id is not None and not _encrypted(expected_account_id)
                or job_kind == "create_artist_playlist" and (batch_add_size is not None or not preserve_order)
                or job_kind == "create_classification_playlist" and
                   (type(batch_add_size) is not int or not 1 <= batch_add_size <= 300 or preserve_order)):
            raise ArtistError("歌单执行方式或分批设置不兼容。")
        self.cli = cli
        self.expected_owner_id = _decimal(expected_owner_id)
        self.journal_writer = journal_writer
        self._frozen = False
        self._last_result = None
        self.control = control
        self.progress_listener = progress_listener
        self.job_kind, self.batch_add_size, self.preserve_order = job_kind, batch_add_size, preserve_order
        self.expected_account_id = expected_account_id.upper() if expected_account_id is not None else None

    def _checkpoint(self):
        if self.control is not None:
            self.control.checkpoint()

    def _progress(self, stage, status="running"):
        _emit_progress(self.progress_listener, stage,
                       name=self.items[self.index]["name"] if self.items else "",
                       job_index=self.index + 1 if self.items else 0, job_count=len(self.items),
                       completed_count=sum(item["status"] == "completed" for item in self.items), status=status)

    def _pause(self):
        if self.batch_add_size is not None and self.pending:
            self.outcome_known = False
            return self._stop("uncertain", "分批写入仍未完成确认，暂停不会覆盖原写入意图；不会重发。")
        if self.batch_add_size is not None and self.items:
            item = self.items[self.index]
            verified = item.get("added_count", 0)
            if item["phase"] == "adding" and verified and item["count"] == verified:
                self.add_count = min(self.batch_add_size, verified)
                self.add_offset = verified - self.add_count
        for item in self.items:
            if item["status"] != "completed":
                item.update(status="pending", message="已暂停，已核验的进度保留；继续前会重新确认歌单状态。")
        try:
            self._journal("paused")
        except _JournalError:
            return self._stop("partial" if self.applied else "blocked",
                              "暂停记录未能保存；已确认效果保留，已停止后续写入。")
        self._progress("paused", "paused")
        return self._report("paused")

    def _manifest(self):
        commands = self.cli.manifest()[1]
        if not isinstance(commands, list):
            raise ArtistError("官方精选写入命令定义不兼容。")
        for method, contract in _CONTRACTS.items():
            if method == "reorder" and not self.preserve_order:
                continue
            matches = [entry for entry in commands if isinstance(entry, dict)
                       and entry.get("command") == ["playlist", method]]
            if len(matches) != 1 or not isinstance(matches[0].get("parameters"), list):
                raise ArtistError("未取得唯一且受支持的官方精选写入命令。")
            parameters = {}
            for parameter in matches[0]["parameters"]:
                if (not isinstance(parameter, dict) or not isinstance(parameter.get("name"), str)
                        or parameter["name"] in parameters
                        or ("required" in parameter and type(parameter["required"]) is not bool)):
                    raise ArtistError("官方精选写入参数定义不兼容。")
                parameters[parameter["name"]] = parameter
                if parameter.get("required") is True and parameter["name"] not in contract:
                    raise ArtistError("官方精选命令出现未知必填参数，不能猜测执行。")
            for name, (location, kind) in contract.items():
                parameter = parameters.get(name, {})
                if (parameter.get("required") is not True or parameter.get("type") != kind
                        or (parameter.get("in") or parameter.get("location")) != location
                        or any(parameter[field] != location for field in ("in", "location")
                               if field in parameter)):
                    raise ArtistError("官方精选写入参数与已核实契约不一致。")

    def _read(self, arguments, kind):
        return _response_data(self.cli.run_json(arguments), kind)

    def _account(self):
        account = self._read(["user", "info"], dict)
        if (_decimal(account.get("originalId")) != self.expected_owner_id
                or not _encrypted(account.get("id"))
                or self.expected_account_id is not None and account["id"].upper() != self.expected_account_id):
            raise ArtistError("当前官方账号与已批准账户不一致。")
        return account["id"].upper()

    def _created(self):
        result, original_ids = {}, set()
        total, offset = None, 0
        while total is None or offset < total:
            page = self._read(["playlist", "created", "--limit", "500", "--offset", str(offset)], dict)
            count = _integer(page.get("recordCount"), 1000)
            if total is not None and count != total:
                raise ArtistError("读取期间歌单目录数量发生变化。")
            total = count
            records = page.get("records")
            if not isinstance(records, list) or len(records) != min(500, total - offset):
                raise ArtistError("官方歌单目录分页不完整。")
            for raw in records:
                header = _header(raw)
                if header["id"] in result or header["original_id"] in original_ids:
                    raise ArtistError("官方歌单目录出现重复或冲突 ID。")
                result[header["id"]] = header
                original_ids.add(header["original_id"])
            offset += 500
        return result

    def _get(self, playlist, owner_id):
        raw = self._read(["playlist", "get", "--playlistId", playlist["id"]], dict)
        detail = _header(raw)
        if (detail["id"] != playlist["id"] or detail["original_id"] != playlist["original_id"]
                or detail["name"] != playlist["name"] or not _same_id(raw.get("creatorId"), owner_id)
                or detail["special_type"] == 5):
            raise ArtistError("新歌单身份、名称或所有者未通过核对。")
        return detail

    def _snapshot(self, playlist, owner_id, stabilize_empty=False):
        attempts = 3 if stabilize_empty else 1
        for attempt in range(attempts):
            detail, tracks, after = self._snapshot_once(playlist, owner_id)
            if after == detail:
                return detail, tracks
            if (detail["count"] != 0 or tracks or after["count"] != 0
                    or any(after[key] != value for key, value in detail.items()
                           if key != "track_update_time") or attempt == attempts - 1):
                raise ArtistError("新歌单读取期间状态发生变化。")
        raise ArtistError("新歌单空成员状态未能稳定确认。")

    def _snapshot_once(self, playlist, owner_id):
        detail = self._get(playlist, owner_id)
        tracks, seen = [], set()
        for offset in range(0, max(detail["count"], 1), 500):
            page = self._read(["playlist", "tracks", "--playlistId", playlist["id"],
                               "--limit", "500", "--offset", str(offset)], list)
            if len(page) > min(500, max(detail["count"] - offset, 0)):
                raise ArtistError("新歌单成员分页数量不兼容。")
            for raw in page:
                ident = raw.get("id") if isinstance(raw, dict) else None
                if not _encrypted(ident) or ident.upper() in seen:
                    raise ArtistError("新歌单成员 ID 无效或重复。")
                tracks.append(ident.upper())
                seen.add(ident.upper())
        if len(tracks) != detail["count"]:
            raise ArtistError("新歌单成员不完整。")
        return detail, tracks, self._get(playlist, owner_id)

    def _snapshot_verified_batch(self, playlist, owner_id, expected):
        """Only exact classification members permit timestamp-only read retries."""
        baseline = None
        for attempt in range(3):
            before, actual, after = self._snapshot_once(playlist, owner_id)
            fields = {key: value for key, value in before.items() if key != "track_update_time"}
            if baseline is None:
                baseline = fields
            if fields != baseline:
                raise ArtistError("分类加歌读回期间身份、数量或类型发生变化。")
            if before == after:
                # The caller retains its existing stable-but-incomplete partial
                # result. Such a mismatch never permits a second read or add.
                return before, actual
            exact = (before["count"] == len(expected) and len(actual) == len(expected)
                     and set(actual) == set(expected))
            if (not exact or any(after[key] != value for key, value in baseline.items())
                    or attempt == 2):
                raise ArtistError("分类加歌后的完整成员或读取稳定性未通过核对。")
            if self._account() != owner_id:
                raise ArtistError("分类加歌读回期间当前官方账号发生变化。")
        raise ArtistError("分类加歌后的读取状态未能稳定确认。")

    def _report(self, status):
        return {"status": status, "completed_count": sum(item["status"] == "completed" for item in self.items),
                "applied_to_account": self.applied, "write_attempted": self.write_attempted,
                "outcome_known": self.outcome_known, "items": copy.deepcopy(self.items)}

    def _journal(self, phase):
        record = {**self._report("paused" if phase == "paused" else "running"), "phase": phase, "job_index": self.index,
                  "expected_owner_id": self.expected_owner_id, "jobs": copy.deepcopy(self.jobs)}
        if self.recovery is not None:
            record["recovery"] = copy.deepcopy(self.recovery)
        if self.batch_add_size is not None:
            record.update(add_offset=self.add_offset, add_count=self.add_count)
        try:
            self.journal_writer(record)
        except Exception:
            raise _JournalError("执行记录未能可靠保存，已停止后续写入。") from None

    def _write(self, method, fields):
        self._checkpoint()
        self._progress(method)
        self._checkpoint()
        self._journal(method + "_attempted")
        self._checkpoint()
        self.write_attempted, self.outcome_known, self.pending = True, False, True
        try:
            intent = _INTENT if self.job_kind == "create_artist_playlist" else _CLASSIFICATION_INTENT
            response = self.cli.run_json(["playlist", method, *fields, "--userInput", intent])
        except Exception:
            return "exception"
        if isinstance(response, dict) and type(response.get("code")) is int:
            return "accepted" if response["code"] == 200 else "rejected"
        return "unknown"

    def _stop(self, status, message):
        item = self.items[self.index]
        if item["status"] != "completed":
            item["status"] = status
        item["message"] = message
        for frozen in self.items[self.index + 1:]:
            frozen.update(status="blocked", message="前项未完成确认，本项未执行。")
        if status == "blocked" and self.applied:
            status = "partial"
        return self._report(status)

    def _add_batches(self, playlist, owner_id, detail, expected, *, start_offset=0):
        """Verify every cumulative set; an abnormal batch is never resent."""
        if (type(start_offset) is not int or not 0 <= start_offset < len(expected)
                or start_offset % self.batch_add_size or detail["count"] != start_offset):
            raise ArtistError("分批添加的已核验边界不兼容，不能继续提交。")
        actual = list(expected[:start_offset])
        for offset in range(start_offset, len(expected), self.batch_add_size):
            self._checkpoint()
            if self._account() != owner_id or self._get(playlist, owner_id) != detail:
                raise ArtistError("分批添加前账号或歌单成员状态发生变化。")
            chunk = expected[offset:offset + self.batch_add_size]
            self.add_offset, self.add_count = offset, len(chunk)
            state = self._write("add", ["--playlistId", playlist["id"], "--songIdList",
                                        json.dumps(chunk, separators=(",", ":"))])
            # No pause checkpoint inside this readback: the in-flight write is verified first.
            prefix = expected[:offset + len(chunk)]
            detail, actual = self._snapshot_verified_batch(playlist, owner_id, prefix)
            self.items[self.index]["count"] = detail["count"]
            if self._account() != owner_id:
                raise ArtistError("分批添加后的当前官方账号发生变化。")
            exact = len(actual) == len(prefix) and set(actual) == set(prefix)
            self.pending, self.outcome_known = False, exact
            if state != "accepted":
                return detail, actual, self._stop("uncertain", "本批添加响应未确认成功，已只读核对并停止，不会重发或继续后续批次。")
            if not exact:
                self.outcome_known = True
                return detail, actual, self._stop("partial", "本批添加后的累计歌曲集合或数量不一致，已停止，不会重发或继续后续批次。")
            self.items[self.index].update(phase="adding", added_count=len(prefix))
            self._journal("add_verified")
            self._progress("added")
        return detail, actual, None

    def execute(self, jobs):
        return self._run(jobs, None, False)

    def resume_created(self, jobs, recovery):
        """Continue only a verified create-only attempt, never reuse an arbitrary list."""
        return self._run(jobs, recovery, True)

    def resume_checkpoint(self, jobs, checkpoint_items):
        """Resume explicit, verified phase evidence; ordinary execution never reuses lists."""
        return self._run(jobs, None, False, checkpoint_items)

    def resume_verified_batches(self, jobs, recovery):
        """Classification only: freshly verify the exact completed batch prefix."""
        if self.job_kind != "create_classification_playlist" or type(recovery) is not dict or not recovery:
            raise ArtistError("已核验分批恢复仅适用于独立分类批次。")
        return self._run(jobs, None, False, batch_recovery=recovery)

    def _run(self, jobs, recovery, resume, checkpoint_items=None, batch_recovery=None):
        if self._frozen:
            return {**copy.deepcopy(self._last_result), "status": "blocked",
                    "message": "前次精选创建未完成确认，不能重复写入。"}
        if not _EXECUTION_LOCK.acquire(blocking=False):
            return {"status": "blocked", "completed_count": 0, "applied_to_account": False,
                    "write_attempted": False, "outcome_known": True, "items": []}
        try:
            result = self._execute(jobs, recovery, resume, checkpoint_items, batch_recovery)
            self._last_result = copy.deepcopy(result)
            self._frozen = result["status"] in {"partial", "uncertain"}
            return result
        finally:
            _EXECUTION_LOCK.release()

    def _bind_checkpoint(self, checkpoint_items):
        if not isinstance(checkpoint_items, list) or len(checkpoint_items) != len(self.jobs):
            raise ArtistError("暂停进度与精选任务数量不一致。")
        seen_ids, seen_originals = set(), set()
        for item, job, raw in zip(self.items, self.jobs, checkpoint_items):
            if (not isinstance(raw, dict) or raw.get("kind") != "create_artist_playlist"
                    or raw.get("name") != job["name"]
                    or type(raw.get("expected_count")) is not int
                    or raw["expected_count"] != len(job["candidate_track_ids"])
                    or raw.get("phase") not in {"pending", "created", "added", "completed"}
                    or raw.get("status") != ("completed" if raw["phase"] == "completed" else "pending")
                    or type(raw.get("count")) is not int):
                raise ArtistError("暂停进度的任务、阶段或数量绑定不合法。")
            phase = raw["phase"]
            if phase == "pending":
                if raw.get("playlist_id") is not None or raw.get("original_playlist_id") is not None or raw["count"] != 0:
                    raise ArtistError("未执行任务不能绑定既有歌单。")
            else:
                ident, original = raw.get("playlist_id"), raw.get("original_playlist_id")
                if (not _encrypted(ident) or not isinstance(original, str) or _decimal(original) is None
                        or ident.upper() in seen_ids or original in seen_originals
                        or raw["count"] != (0 if phase == "created" else len(job["candidate_track_ids"]))):
                    raise ArtistError("暂停进度的已创建歌单身份或成员数量不合法。")
                seen_ids.add(ident.upper())
                seen_originals.add(original)
                item.update(playlist_id=ident.upper(), original_playlist_id=original, count=raw["count"])
                # These are effects recorded by the previous verified attempt. Live checks
                # below authorize continuation, while a new pause preserves that history.
                self.applied, self.write_attempted = True, True
            item.update(phase=phase, status=raw["status"], message="暂停进度已绑定，等待重新核验。")
            if raw.get("creation_response_warning") is True:
                item["creation_response_warning"] = True

    def _verify_checkpoint(self, initial, owner_id):
        for self.index, (job, item) in enumerate(zip(self.jobs, self.items)):
            self._checkpoint()
            matches = [header for header in initial.values() if header["name"] == job["name"]]
            if item["phase"] == "pending":
                if matches:
                    raise ArtistError("未执行精选的名称已被占用，不能复用或重复创建。")
                continue
            if (len(matches) != 1 or matches[0]["id"] != item["playlist_id"]
                    or matches[0]["original_id"] != item["original_playlist_id"]):
                raise ArtistError("暂停精选的目录绑定不一致。")
            self._get(matches[0], owner_id)
            self.applied, self.write_attempted = True, True
            detail, actual = self._snapshot(matches[0], owner_id, stabilize_empty=item["phase"] == "created")
            item["count"] = detail["count"]
            expected = job["candidate_track_ids"]
            if (item["phase"] == "created" and actual
                    or item["phase"] == "completed" and actual != expected
                    or item["phase"] == "added" and (len(actual) != len(expected) or set(actual) != set(expected))):
                raise ArtistError("暂停精选的完整成员或顺序与已确认阶段不一致。")
            if item["phase"] in {"added", "completed"} and actual == expected:
                item.update(phase="completed", status="completed", message="已重新核验完整歌曲 ID、数量和顺序，无需重复写入。")
            if self._account() != owner_id:
                raise ArtistError("暂停进度核验期间官方账号发生变化。")
        self.index = 0
        self._journal("checkpoint_identified")

    def _execute(self, jobs, recovery=None, resume=False, checkpoint_items=None, batch_recovery=None):
        self.jobs, self.items, self.index = [], [], 0
        self.recovery = None
        self.batch_recovery = None
        self.applied, self.write_attempted, self.outcome_known, self.pending = False, False, True, False
        self.add_offset, self.add_count = 0, 0
        try:
            if not isinstance(jobs, list) or self.expected_owner_id is None or not callable(self.journal_writer):
                raise ArtistError("精选任务或账号配置不合法。")
            if self.batch_add_size is not None and (len(jobs) > 64 or resume or checkpoint_items is not None):
                raise ArtistError("分类批次过大或缺少受支持的恢复契约。")
            names = set()
            for raw in jobs:
                if (not isinstance(raw, dict) or raw.get("kind") != self.job_kind
                        or not _name(raw.get("name")) or raw["name"] in names
                        or raw.get("intended_visibility") != "provider_default"
                        or not isinstance(raw.get("candidate_track_ids"), list)
                        or not 1 <= len(raw["candidate_track_ids"]) <= 10000
                        or any(not _encrypted(ident) for ident in raw["candidate_track_ids"])):
                    raise ArtistError("精选名称、可见性或候选歌曲格式不合法。")
                tracks = [ident.upper() for ident in raw["candidate_track_ids"]]
                if len(set(tracks)) != len(tracks):
                    raise ArtistError("同一精选的候选歌曲重复，整批未执行。")
                names.add(raw["name"])
                self.jobs.append({"kind": self.job_kind, "name": raw["name"],
                                  "candidate_track_ids": tracks, "intended_visibility": "provider_default"})
                self.items.append({"kind": self.job_kind, "name": raw["name"], "playlist_id": None,
                                   "original_playlist_id": None, "count": 0, "expected_count": len(tracks),
                                   "phase": "pending", "status": "blocked", "message": "本项尚未执行。"})
            if batch_recovery is not None:
                if (self.batch_add_size is None or not self.jobs or type(batch_recovery) is not dict
                        or set(batch_recovery) != {"playlist_id", "original_playlist_id", "name", "verified_add_offset"}
                        or not _encrypted(batch_recovery.get("playlist_id"))
                        or type(batch_recovery.get("original_playlist_id")) is not str
                        or _decimal(batch_recovery["original_playlist_id"]) is None
                        or batch_recovery.get("name") != self.jobs[0]["name"]
                        or type(batch_recovery.get("verified_add_offset")) is not int
                        or not 0 < batch_recovery["verified_add_offset"] < len(self.jobs[0]["candidate_track_ids"])
                        or batch_recovery["verified_add_offset"] % self.batch_add_size):
                    raise ArtistError("分类已核验批次的恢复绑定或边界不兼容。")
                self.batch_recovery = {**batch_recovery, "playlist_id": batch_recovery["playlist_id"].upper()}
                boundary = batch_recovery["verified_add_offset"]
                self.items[0].update(playlist_id=self.batch_recovery["playlist_id"],
                                     original_playlist_id=batch_recovery["original_playlist_id"],
                                     count=boundary, added_count=boundary, phase="adding", status="pending")
                self.applied, self.write_attempted = True, True
                self.add_offset, self.add_count = boundary - self.batch_add_size, self.batch_add_size
            if checkpoint_items is not None:
                self._bind_checkpoint(checkpoint_items)
            if resume:
                if (not self.jobs or not isinstance(recovery, dict)
                        or type(recovery.get("job_index")) is not int or recovery["job_index"] != 0
                        or recovery.get("create_only_verified") is not True
                        or not _encrypted(recovery.get("playlist_id"))
                        or not isinstance(recovery.get("original_playlist_id"), str)
                        or _decimal(recovery["original_playlist_id"]) is None
                        or recovery.get("name") != self.jobs[0]["name"]):
                    raise ArtistError("已创建精选的恢复证据或绑定字段不合法。")
                self.recovery = {"job_index": 0, "playlist_id": recovery["playlist_id"].upper(),
                                 "original_playlist_id": _decimal(recovery["original_playlist_id"]),
                                 "name": recovery["name"], "create_only_verified": True}
                # Preserve the prior verified create effect even if the user pauses
                # before fresh reads. These reads still gate every resumed mutation.
                self.items[0].update(playlist_id=self.recovery["playlist_id"],
                                     original_playlist_id=self.recovery["original_playlist_id"],
                                     count=0, phase="created", status="pending")
                self.applied, self.write_attempted = True, True
            if not self.jobs:
                return self._report("completed")
            self._checkpoint()
            self._progress("preflight")
            self._checkpoint()
            self._manifest()
            owner_id = self._account()
            initial = self._created()
            if checkpoint_items is not None:
                self._verify_checkpoint(initial, owner_id)
            elif self.batch_recovery is not None:
                bound = {"id": self.batch_recovery["playlist_id"],
                         "original_id": self.batch_recovery["original_playlist_id"], "name": self.batch_recovery["name"]}
                matches = [header for header in initial.values() if header["name"] == bound["name"]]
                if (len(matches) != 1 or matches[0]["id"] != bound["id"]
                        or matches[0]["original_id"] != bound["original_id"]
                        or any(header["name"] in names - {bound["name"]} for header in initial.values())):
                    raise ArtistError("分类恢复目标或未触及名称出现目录冲突。")
                self.pending, self.outcome_known = True, False
                detail, actual = self._snapshot(bound, owner_id)
                boundary = self.batch_recovery["verified_add_offset"]
                if (detail["count"] != boundary or len(actual) != boundary
                        or set(actual) != set(self.jobs[0]["candidate_track_ids"][:boundary])
                        or self._account() != owner_id):
                    raise ArtistError("分类已核验批次的完整成员集合发生变化。")
                self.pending, self.outcome_known = False, True
                self._journal("add_verified")
            elif self.recovery is None:
                if any(header["name"] in names for header in initial.values()):
                    raise ArtistError("账号已存在同名精选，整批未创建，也不会向已有歌单加歌。")
            else:
                bound = {"id": self.recovery["playlist_id"],
                         "original_id": self.recovery["original_playlist_id"], "name": self.recovery["name"]}
                confirmed = self._get(bound, owner_id)
                self.items[0].update(playlist_id=confirmed["id"], original_playlist_id=confirmed["original_id"],
                                     count=confirmed["count"])
                self.applied, self.write_attempted = True, True
                matches = [header for header in initial.values() if header["name"] == bound["name"]]
                if (len(matches) != 1 or matches[0]["id"] != bound["id"]
                        or matches[0]["original_id"] != bound["original_id"]
                        or any(header["name"] in names - {bound["name"]} for header in initial.values())):
                    raise ArtistError("恢复歌单或其他待创建名称出现目录冲突。")
                self.pending, self.outcome_known = True, False
                detail, actual = self._snapshot(bound, owner_id, stabilize_empty=True)
                self.items[0]["count"] = detail["count"]
                if self._account() != owner_id:
                    raise ArtistError("恢复核验后官方账号发生变化。")
                self.pending, self.outcome_known = False, True
                if actual:
                    return self._stop("partial", "已绑定歌单已有成员，未添加或重排任何歌曲。")
                self.items[0]["phase"] = "created"
                self._journal("recovery_identified")
        except OperationPaused:
            return self._pause()
        except _JournalError:
            return self._stop("partial" if self.applied else "blocked",
                              "恢复记录保存失败；已确认创建效果保留，未继续写入。")
        except Exception:
            for item in self.items:
                item["message"] = "精选任务、账号、同名检查或官方契约未通过确认，整批未执行。"
            if self.pending:
                self.outcome_known = False
                return self._stop("uncertain", "已确认创建效果，但恢复只读核验失败，未继续写入。")
            return self._report("partial" if self.applied else "blocked")

        for self.index, job in enumerate(self.jobs):
            item = self.items[self.index]
            batch_current = self.batch_recovery is not None and self.index == 0
            if not batch_current:
                self.add_offset, self.add_count = 0, 0
            try:
                if item["status"] == "completed":
                    self._progress("skipped")
                    continue
                self._checkpoint()
                self._progress("job_begin")
                self._checkpoint()
                if self._account() != owner_id:
                    raise ArtistError("当前官方账号发生变化。")
                before = self._created()
                resumed_phase = item["phase"] if checkpoint_items is not None else None
                if batch_current:
                    resumed_phase = "adding"
                if resumed_phase in {"created", "added", "adding"}:
                    matches = [header for header in before.values() if header["name"] == job["name"]]
                    if (len(matches) != 1 or matches[0]["id"] != item["playlist_id"]
                            or matches[0]["original_id"] != item["original_playlist_id"]):
                        raise ArtistError("继续前已绑定精选目录发生变化。")
                    playlist, create_state = matches[0], "accepted"
                elif self.recovery is not None and self.index == 0:
                    matches = [header for header in before.values() if header["name"] == job["name"]]
                    if (len(matches) != 1 or matches[0]["id"] != self.recovery["playlist_id"]
                            or matches[0]["original_id"] != self.recovery["original_playlist_id"]):
                        raise ArtistError("恢复前已绑定歌单目录发生变化。")
                    playlist, create_state = matches[0], "accepted"
                else:
                    if any(header["name"] == job["name"] for header in before.values()):
                        raise ArtistError("创建前发现同名歌单，不能占用或重复创建。")
                    create_state = self._write("create", ["--playlistName", job["name"]])
                    after = self._created()
                    if self._account() != owner_id:
                        raise ArtistError("创建后官方账号发生变化。")
                    matches = [header for header in after.values() if header["name"] == job["name"]]
                    old_originals = {header["original_id"] for header in before.values()}
                    if (len(matches) != 1 or matches[0]["id"] in before
                            or matches[0]["original_id"] in old_originals):
                        if create_state == "rejected" and not matches:
                            self.pending, self.outcome_known = False, True
                            return self._stop("blocked", "官方未确认创建成功，完整目录未发现新同名歌单，已停止且不会重发。")
                        return self._stop("uncertain", "不能唯一确认新歌单，已停止且不会重复创建或添加歌曲。")
                    playlist = matches[0]
                    confirmed = self._get(playlist, owner_id)
                    item.update(playlist_id=confirmed["id"], original_playlist_id=confirmed["original_id"],
                                count=confirmed["count"])
                    self.applied = True
                detail, actual = self._snapshot(playlist, owner_id, stabilize_empty=resumed_phase != "added")
                item.update(playlist_id=detail["id"], original_playlist_id=detail["original_id"], count=detail["count"])
                self.applied, self.pending, self.outcome_known = True, False, True
                if actual and resumed_phase not in {"added", "adding"}:
                    return self._stop("partial", "新歌单已有成员，未添加或重排任何歌曲。")
                if create_state != "accepted":
                    item["creation_response_warning"] = True
                expected = job["candidate_track_ids"]
                if resumed_phase == "adding":
                    boundary = self.batch_recovery["verified_add_offset"]
                    if (len(actual) != boundary or detail["count"] != boundary
                            or set(actual) != set(expected[:boundary])):
                        raise ArtistError("提交未尝试批次前，已确认歌曲集合发生变化。")
                    detail, actual, stopped = self._add_batches(playlist, owner_id, detail, expected,
                                                               start_offset=boundary)
                    if stopped is not None:
                        return stopped
                elif resumed_phase != "added":
                    item["phase"] = "created"
                    self._journal("playlist_identified")
                    self._progress("created")
                    self._checkpoint()
                    # A stable empty snapshot already read every member immediately above.
                    # Keep a fresh owner/header check; repeating its members adds no invariant.
                    if self._account() != owner_id or self._get(playlist, owner_id) != detail:
                        raise ArtistError("添加前账号或新歌单空成员状态发生变化。")
                    if self.batch_add_size is not None:
                        detail, actual, stopped = self._add_batches(playlist, owner_id, detail, expected)
                        if stopped is not None:
                            return stopped
                    else:
                        add_state = self._write("add", ["--playlistId", playlist["id"], "--songIdList",
                                                         json.dumps(expected, ensure_ascii=False, separators=(",", ":"))])
                        detail, actual = self._snapshot(playlist, owner_id)
                        item["count"] = detail["count"]
                        if self._account() != owner_id:
                            raise ArtistError("添加后的当前官方账号发生变化。")
                        exact_set = len(actual) == len(expected) and set(actual) == set(expected)
                        self.pending = False
                        self.outcome_known = add_state != "exception" or exact_set
                        if add_state not in {"accepted", "exception"}:
                            return self._stop("uncertain", "已读回添加后的歌单，但接口响应未确认成功，停止且不会重发。")
                        if not exact_set:
                            return self._stop("uncertain" if add_state == "exception" else "partial",
                                              "添加后的歌曲数量或成员集合与审核清单不一致，停止且不会重发或重排。")
                elif len(actual) != len(expected) or set(actual) != set(expected):
                    raise ArtistError("继续排序前完整成员集合发生变化，未添加或重排歌曲。")
                item["phase"] = "added"
                if self.preserve_order and actual != expected:
                    if self._account() != owner_id:
                        raise ArtistError("排序前官方账号发生变化。")
                    detail, current = self._snapshot(playlist, owner_id)
                    if len(current) != len(expected) or set(current) != set(expected):
                        raise ArtistError("排序前歌单成员发生变化。")
                    reorder_state = self._write("reorder", ["--playlistId", playlist["id"], "--trackIds",
                                                               json.dumps(expected, separators=(",", ":"))])
                    detail, actual = self._snapshot(playlist, owner_id)
                    item["count"] = detail["count"]
                    if self._account() != owner_id:
                        raise ArtistError("排序后的当前官方账号发生变化。")
                    self.pending = False
                    self.outcome_known = reorder_state != "exception" or actual == expected
                    if actual != expected or reorder_state not in {"accepted", "exception"}:
                        return self._stop("uncertain" if reorder_state != "accepted" else "partial",
                                          "完整曲目顺序未通过最终确认，已停止且不会再次排序。")
                message = ("已创建精选，完整歌曲 ID、数量及顺序均与审核清单一致。" if self.preserve_order
                           else "已创建分类歌单，完整歌曲 ID 集合及数量均与审核清单一致，保留平台歌曲顺序。")
                item.update(phase="completed", status="completed", message=message)
                self._journal("completed")
                self._progress("completed")
            except OperationPaused:
                return self._pause()
            except _JournalError:
                return self._stop("partial" if self.applied else "blocked",
                                  "执行记录保存失败；已确认的账号效果保留，已停止后续写入。")
            except Exception:
                if self.pending:
                    self.outcome_known = False
                    return self._stop("uncertain", "写入后的只读确认失败，结果未确定；已停止且不会重发。")
                return self._stop("partial" if self.applied else "blocked",
                                  "后续身份或歌单状态校验失败，已停止写入；已确认效果保留。")
        return self._report("completed")
