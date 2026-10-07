"""Read verified account metadata and report filtered songs without inventing data."""

import re

from .official_cli import CliError, public_error_code


class OnlineError(RuntimeError):
    """Public-safe failure: no raw response, credential, or CLI error text."""

    def __init__(self, *args, code="operation_failed"):
        super().__init__(*args)
        self.code = public_error_code(code)


_PAGE_SIZE = 500
_MAX_PLAYLISTS = 1000
_MAX_TRACKS = 10000
_READ_COMMANDS = frozenset({
    ("user", "info"), ("user", "favorite"), ("playlist", "created"),
    ("playlist", "get"), ("playlist", "tracks"),
})


def _read_arguments_allowed(arguments):
    if not isinstance(arguments, list) or not all(isinstance(value, str) for value in arguments):
        return False
    prefix = tuple(arguments[:2])
    if prefix not in _READ_COMMANDS:
        return False
    if prefix[0] == "user":
        return len(arguments) == 2
    if prefix == ("playlist", "get"):
        return (len(arguments) == 4 and arguments[2] == "--playlistId"
                and re.fullmatch(r"[0-9A-Fa-f]{32}", arguments[3]) is not None)
    if prefix == ("playlist", "created"):
        shape = len(arguments) == 6 and arguments[2:5] == ["--limit", "500", "--offset"]
        maximum = _MAX_PLAYLISTS
    else:
        shape = (len(arguments) == 8 and arguments[2] == "--playlistId"
                 and re.fullmatch(r"[0-9A-Fa-f]{32}", arguments[3]) is not None
                 and arguments[4:7] == ["--limit", "500", "--offset"])
        maximum = _MAX_TRACKS
    return (shape and re.fullmatch(r"(?:0|[1-9][0-9]{0,4})", arguments[-1]) is not None
            and int(arguments[-1]) < maximum
            and (prefix != ("playlist", "tracks") or int(arguments[-1]) % _PAGE_SIZE == 0))


def _original_id(value):
    if type(value) is int and value > 0:
        return str(value)
    if isinstance(value, str) and re.fullmatch(r"[1-9][0-9]*", value):
        return value
    raise OnlineError("官方原始 ID 资料不完整，不能确认在线快照。")


def _encrypted_id(value):
    if isinstance(value, str) and re.fullmatch(r"[0-9A-Fa-f]{32}", value):
        return value.upper()
    raise OnlineError("官方接口 ID 格式不兼容，不能确认在线快照。")


def _integer(value, *, maximum=None):
    if type(value) is not int or value < 0 or (maximum is not None and value > maximum):
        raise OnlineError("在线数量或更新时间不兼容，或已超出本程序的读取上限。")
    return value


def _name(value):
    if not isinstance(value, str) or not value.strip():
        raise OnlineError("在线名称资料缺失，不能确认完整快照。")
    return value


def _identity(value):
    if not isinstance(value, dict):
        raise OnlineError("官方在线资料缺少对象字段。")
    return {"id": _encrypted_id(value.get("id")), "original_id": _original_id(value.get("originalId"))}


def _playlist(value):
    return {
        **_identity(value), "name": _name(value.get("name")),
        "track_count": _integer(value.get("trackCount"), maximum=_MAX_TRACKS),
        "special_type": _integer(value.get("specialType")),
        "track_update_time": _integer(value.get("trackUpdateTime")),
    }


def _same_playlist_overview(left, right):
    # Timestamp fields have endpoint-specific semantics, including a zero
    # placeholder in favorite. Each endpoint is rechecked against itself.
    return all(left[key] == right[key] for key in ("id", "original_id", "name", "track_count", "special_type"))


def _unique(item, encrypted_ids, original_ids):
    if item["id"] in encrypted_ids or item["original_id"] in original_ids:
        raise OnlineError("在线分页出现重复或冲突 ID，不能确认完整快照。")
    encrypted_ids.add(item["id"])
    original_ids.add(item["original_id"])


def _artists(value):
    if not isinstance(value, list):
        raise OnlineError("在线歌曲歌手资料的格式不兼容。")
    result, ids, original_ids = [], set(), set()
    unmapped = False
    for raw in value:
        if (isinstance(raw, dict) and type(raw.get("originalId")) is int
                and raw["originalId"] == 0 and "id" in raw and raw["id"] is None):
            # Known cloud-song artist placeholder: keep no invented identity.
            _name(raw.get("name"))
            unmapped = True
            continue
        artist = {**_identity(raw), "name": _name(raw.get("name"))}
        _unique(artist, ids, original_ids)
        result.append(artist)
    return result, unmapped


def _track(value):
    identity = _identity(value)
    artists, unmapped = _artists(value.get("artists"))
    if "fullArtists" in value:
        full, full_unmapped = _artists(value["fullArtists"])
        unmapped = unmapped or full_unmapped
        if full:
            identities = {(artist["id"], artist["original_id"]) for artist in full}
            if any((artist["id"], artist["original_id"]) not in identities for artist in artists):
                raise OnlineError("在线歌曲的完整歌手资料存在冲突。")
            artists = full
    return {**identity, "name": _name(value.get("name")), "artists": artists,
            "metadata_available": bool(artists) and not unmapped}


class OnlineReader:
    """Reject changing/foreign data and report song gaps; never issue mutations."""

    def __init__(self, cli, expected_owner_id, *, control=None):
        self.control = control
        self.cli = cli
        self.expected_owner_id = _original_id(expected_owner_id)

    def _read(self, arguments, data_type):
        if self.control is not None:
            self.control.checkpoint()
        if not _read_arguments_allowed(arguments):
            raise OnlineError("在线快照读取器仅允许固定的只读命令。")
        try:
            response = self.cli.run_json(arguments)
        except CliError as error:
            raise OnlineError("官方在线读取未完成，请检查账号授权和接口状态后重试。",
                              code=error.code) from None
        except Exception:
            raise OnlineError("官方在线读取未完成，请检查账号授权和接口状态后重试。") from None
        if (not isinstance(response, dict) or type(response.get("code")) is not int
                or response["code"] != 200 or not isinstance(response.get("data"), data_type)):
            raise OnlineError("官方在线响应未确认成功，或资料格式不完整。")
        return response["data"]

    def _account(self):
        raw = self._read(["user", "info"], dict)
        account = {**_identity(raw), "nickname": _name(raw.get("nickname"))}
        if account["original_id"] != self.expected_owner_id:
            raise OnlineError("在线账号与指定账号不一致，已停止读取。", code="account_mismatch")
        return account

    def _created(self):
        result, ids, original_ids = [], set(), set()
        total = None
        while total is None or len(result) < total:
            data = self._read(["playlist", "created", "--limit", str(_PAGE_SIZE), "--offset", str(len(result))], dict)
            count = _integer(data.get("recordCount"), maximum=_MAX_PLAYLISTS)
            if total is not None and count != total:
                raise OnlineError("读取期间歌单数量发生变化，请重新读取。")
            total = count
            page = data.get("records")
            if (not isinstance(page, list) or len(page) > _PAGE_SIZE
                    or len(result) + len(page) > total or (not page and len(result) < total)):
                raise OnlineError("在线歌单分页缺失或数量不一致，不能确认完整快照。")
            for raw in page:
                item = _playlist(raw)
                _unique(item, ids, original_ids)
                result.append(item)
        return result

    def _favorite(self):
        item = _playlist(self._read(["user", "favorite"], dict))
        if item["special_type"] != 5:
            raise OnlineError("官方返回的歌单不能确认为红心歌曲歌单。")
        return item

    def _owned_detail(self, favorite, account):
        raw = self._read(["playlist", "get", "--playlistId", favorite["id"]], dict)
        detail = _playlist(raw)
        creator = _encrypted_id(raw.get("creatorId"))
        if creator != account["id"]:
            raise OnlineError("红心歌单归属与在线账号不一致，已停止读取。")
        if not _same_playlist_overview(detail, favorite):
            raise OnlineError("红心歌单的身份、数量或资料发生变化，请重新读取。")
        return {**detail, "creator_id": creator}

    def _tracks(self, favorite, *, progress=None):
        result, ids, original_ids = [], set(), set()
        total = favorite["track_count"]
        # The official API filters unavailable slots. Its offset addresses the
        # advertised membership, rather than the number of returned records.
        for offset in range(0, total, _PAGE_SIZE):
            page = self._read([
                "playlist", "tracks", "--playlistId", favorite["id"],
                "--limit", str(_PAGE_SIZE), "--offset", str(offset),
            ], list)
            if len(page) > min(_PAGE_SIZE, total - offset):
                raise OnlineError("在线歌曲分页数量不一致，不能确认快照。")
            for raw in page:
                item = _track(raw)
                _unique(item, ids, original_ids)
                result.append(item)
            if progress is not None:
                progress({"stage": "reading", "completed_count": min(offset + _PAGE_SIZE, total),
                          "total_count": total})
        return result

    def read_playlist(self, playlist_id, original_playlist_id, *, expected_account_id, progress=None):
        """Read one owned playlist; local bindings contain identities, never stale headers."""
        playlist_id = _encrypted_id(playlist_id)
        expected_account_id = _encrypted_id(expected_account_id)
        if (not isinstance(original_playlist_id, str)
                or re.fullmatch(r"[1-9][0-9]{0,19}", original_playlist_id) is None
                or progress is not None and not callable(progress)):
            raise OnlineError("歌单读取参数不兼容，未开始在线读取。")
        account = self._account()
        if account["id"] != expected_account_id:
            raise OnlineError("在线账号与指定账号不一致，已停止读取。", code="account_mismatch")

        def fresh_header():
            raw = self._read(["playlist", "get", "--playlistId", playlist_id], dict)
            detail = _playlist(raw)
            creator = _encrypted_id(raw.get("creatorId"))
            if (detail["id"], detail["original_id"]) != (playlist_id, original_playlist_id):
                raise OnlineError("歌单的双 ID 与指定歌单不一致，已停止读取。")
            if creator != account["id"]:
                raise OnlineError("歌单归属与在线账号不一致，已停止读取。")
            return {**detail, "creator_id": creator}

        before = fresh_header()
        tracks = self._tracks(before, progress=progress)
        if fresh_header() != before:
            raise OnlineError("读取期间歌单资料或成员更新时间发生变化，请重新读取。")
        latest_account = self._account()
        if (latest_account["id"], latest_account["original_id"]) != (account["id"], account["original_id"]):
            raise OnlineError("读取期间在线账号身份发生变化，请重新读取。", code="account_mismatch")
        missing_records = before["track_count"] - len(tracks)
        missing_metadata = [track["original_id"] for track in tracks if not track["metadata_available"]]
        result = {
            "account": latest_account,
            "playlist": {**before, "tracks": tracks, "membership_complete": missing_records == 0,
                         "metadata_complete": not missing_metadata, "missing_record_count": missing_records,
                         "missing_metadata_track_ids": missing_metadata},
            "complete": missing_records == 0 and not missing_metadata,
        }
        if self.control is not None:
            self.control.checkpoint()
        return result

    def read_snapshot(self, *, include_tracks=True):
        account = self._account()
        playlists = self._created()
        favorite = self._favorite()
        for item in playlists:
            if item["id"] == favorite["id"] or item["original_id"] == favorite["original_id"]:
                if not _same_playlist_overview(item, favorite):
                    raise OnlineError("歌单目录与红心歌单资料存在冲突，请重新读取。")
        detail = self._owned_detail(favorite, account)
        tracks = self._tracks(favorite) if include_tracks else []
        if self._favorite() != favorite or self._owned_detail(favorite, account) != detail:
            raise OnlineError("读取期间红心歌单发生变化，请重新读取。")
        if self._created() != playlists:
            raise OnlineError("读取期间歌单目录发生变化，请重新读取。")
        latest_account = self._account()
        if (latest_account["id"], latest_account["original_id"]) != (account["id"], account["original_id"]):
            raise OnlineError("读取期间在线账号身份发生变化，请重新读取。")
        missing_records = favorite["track_count"] - len(tracks)
        missing_metadata = [track["original_id"] for track in tracks if not track["metadata_available"]]
        membership_complete = include_tracks and missing_records == 0
        metadata_complete = include_tracks and not missing_metadata
        return {
            "account": latest_account, "playlists": playlists,
            "liked": {**detail, "tracks": tracks, "membership_complete": membership_complete,
                      "metadata_complete": metadata_complete, "missing_record_count": missing_records,
                      "missing_metadata_track_ids": missing_metadata},
            "overview_complete": True, "tracks_loaded": include_tracks,
            "complete": membership_complete and metadata_complete,
        }
