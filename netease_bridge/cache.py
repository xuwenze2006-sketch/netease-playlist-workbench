"""Read a verified temporary copy of the client's local SQLite cache."""

import json
import hashlib
import os
import re
import sqlite3
import tempfile
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path


class CacheError(RuntimeError):
    """An actionable cache error whose text contains no raw source records."""


def default_data_dir() -> Path:
    return Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local")) / "NetEase" / "CloudMusic"


def _number(value):
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


def _id(value):
    if isinstance(value, bool):
        return None
    text = str(value)
    return text if text.isascii() and text.isdecimal() else None


def _json(raw):
    if not isinstance(raw, str) or len(raw) > 32 * 1024 * 1024:
        return None
    try:
        return json.loads(raw)
    except (ValueError, RecursionError):
        return None


def _text(value):
    return value if isinstance(value, str) else ""


def _track(record):
    if not isinstance(record, dict) or not (ident := _id(record.get("id"))):
        return None
    name = _text(record.get("name"))
    artists = record.get("artists", record.get("ar", []))
    normalized_artists = [
        {"id": _id(artist.get("id")), "name": _text(artist.get("name"))}
        for artist in (artists if isinstance(artists, list) else [])
        if isinstance(artist, dict)
    ]
    metadata_available = (
        bool(name.strip())
        and isinstance(artists, list)
        and bool(artists)
        and len(normalized_artists) == len(artists)
        and all(
            artist["id"] is not None and bool(artist["name"].strip())
            for artist in normalized_artists
        )
    )
    album = record.get("album", record.get("al", {}))
    album = album if isinstance(album, dict) else {}
    return {
        "id": ident,
        "name": name,
        "artists": normalized_artists,
        "album": {"id": _id(album.get("id")), "name": _text(album.get("name"))},
        "duration_ms": _number(record.get("duration", record.get("dt"))),
        "metadata_available": metadata_available,
    }


def source_signature(data_dir: Path):
    database = data_dir / "Library" / "webdb.dat"
    result = []
    for path in (database, Path(str(database) + "-wal"), Path(str(database) + "-journal")):
        try:
            stat = path.stat()
            result.append((stat.st_mtime_ns, stat.st_size, stat.st_ctime_ns, stat.st_ino, stat.st_dev))
        except OSError:
            result.append(None)
    return tuple(result)


def _file_hash(path):
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while block := stream.read(1024 * 1024):
            digest.update(block)
    return digest.digest()


def _copy_and_hash(source, destination):
    digest = hashlib.sha256()
    with source.open("rb") as reader, destination.open("wb") as writer:
        while block := reader.read(1024 * 1024):
            writer.write(block)
            digest.update(block)
    return digest.digest()


class CacheReader:
    def __init__(self, data_dir: Path):
        self.data_dir = Path(data_dir).resolve()
        self.database = self.data_dir / "Library" / "webdb.dat"

    def load(self) -> dict:
        if not self.database.is_file():
            raise CacheError("找不到 Library/webdb.dat，请确认已使用网易云桌面版，或指定其数据目录。")
        try:
            scratch_root = Path(tempfile.gettempdir()).resolve()
            if any(scratch_root.is_relative_to(root) for root in (self.data_dir, self.database.parent.resolve())):
                raise CacheError("临时目录位于客户端数据目录内，请为 TEMP 设置独立目录。")
            # SQLite's mode=ro may create WAL/SHM files. Never open the source
            # with SQLite: copy its DB/WAL bytes, then use only the temp copy.
            for _ in range(3):
                with tempfile.TemporaryDirectory(prefix="netease-bridge-", dir=scratch_root) as directory:
                    copied = Path(directory) / "webdb.dat"
                    before = source_signature(self.data_dir)
                    if before[0] is None:
                        continue
                    if before[2] is not None and before[2][1] > 0:
                        raise CacheError("缓存存在未完成的回滚日志，请待客户端写入结束后重试。")
                    files = [(self.database, copied)]
                    if before[1] is not None:
                        files.append((Path(str(self.database) + "-wal"), Path(str(copied) + "-wal")))
                    digests = [_copy_and_hash(source, destination) for source, destination in files]
                    if before != source_signature(self.data_dir):
                        continue
                    # Compare bytes as well as metadata; this catches writes
                    # that preserve a file's size or timestamp.
                    if any(_file_hash(source) != digest for (source, _), digest in zip(files, digests)):
                        continue
                    if before != source_signature(self.data_dir):
                        continue
                    connection = sqlite3.connect(copied.as_uri() + "?mode=ro", uri=True, timeout=3)
                    try:
                        connection.execute("PRAGMA query_only=ON")
                        connection.execute("BEGIN")
                        if connection.execute("PRAGMA quick_check").fetchall() != [("ok",)]:
                            raise CacheError("临时缓存副本未通过完整性检查，请稍后重试。")
                        snapshot = self._read(connection)
                        snapshot["source"]["database_modified_at"] = datetime.fromtimestamp(before[0][0] / 1e9, timezone.utc).isoformat()
                        snapshot["source"]["read_strategy"] = "verified_temporary_copy"
                        connection.rollback()
                        return snapshot
                    finally:
                        connection.close()
            raise CacheError("缓存文件持续变化，无法取得稳定副本，请待客户端空闲后重试。")
        except (sqlite3.Error, OSError) as error:
            raise CacheError("网易云本地缓存暂时不可读或格式不兼容，请稍后重试并检查数据目录。") from error

    def _read(self, connection):
        tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if "requestCache" not in tables:
            raise CacheError("缓存中缺少歌单响应表，当前客户端数据格式暂不支持。")
        tracks, overview, details = {}, None, defaultdict(list)
        ignored = 0
        if "dbTrack" in tables:
            for (raw,) in connection.execute("SELECT jsonStr FROM dbTrack"):
                if song := _track(_json(raw)):
                    tracks[song["id"]] = song
        # Select only playlist-related cache rows. The opaque request ID/body is
        # used solely to recognize an allowlisted response route and never saved.
        for rowid, key, raw in connection.execute(
            "SELECT rowid,id,jsonStr FROM requestCache WHERE id LIKE '%playlist%' ORDER BY rowid"
        ):
            match = re.search(r'"url"\s*:\s*"([^"?]+)', key) if isinstance(key, str) else None
            route = match.group(1) if match else ""
            user_list = route in ("/eapi/user/playlist", "/xeapi/user/playlist")
            detail = route in (
                "/eapi/playlist/v4/detail", "/xeapi/playlist/v4/detail",
                "/eapi/v6/playlist/detail", "/xeapi/v6/playlist/detail",
                "/eapi/playlist/detail", "/xeapi/playlist/detail",
            )
            if not (user_list or detail):
                continue
            wrapper = _json(raw)
            payload = wrapper.get("cache") if isinstance(wrapper, dict) else None
            payload = _json(payload) if isinstance(payload, str) else payload
            if not isinstance(payload, dict) or payload.get("code", 200) != 200:
                ignored += 1
                continue
            playlist = payload.get("playlist")
            if user_list and isinstance(playlist, list):
                overview = payload
            elif detail and isinstance(playlist, dict) and (ident := _id(playlist.get("id"))):
                if isinstance(playlist.get("trackIds"), list):
                    details[ident].append((playlist.get("trackUpdateTime"), rowid, "cached_playlist_detail", playlist["trackIds"]))
                inline_tracks = playlist.get("tracks", [])
                if not isinstance(inline_tracks, list):
                    ignored += 1
                    inline_tracks = []
                for song_record in inline_tracks:
                    if song := _track(song_record):
                        existing = tracks.get(song["id"])
                        if existing is None or (not existing["metadata_available"] and song["metadata_available"]):
                            tracks[song["id"]] = song
        if overview is None:
            raise CacheError("未找到已缓存的账号歌单列表；本地接入不能代替在线获取。")
        raw_playlists = [record for record in overview["playlist"] if isinstance(record, dict) and _id(record.get("id"))]
        invalid_overview_records = len(overview["playlist"]) - len(raw_playlists)
        owners = {_id(record.get("userId")) for record in raw_playlists if record.get("specialType") == 5 and record.get("subscribed") is False}
        owners.discard(None)
        if len(owners) != 1:
            raise CacheError("缓存中的账号归属不明确，无法可靠区分自建和收藏歌单。")
        owner = owners.pop()
        if "playlistTrackIds" in tables:
            for ident, raw in connection.execute("SELECT id,jsonStr FROM playlistTrackIds"):
                record = _json(raw)
                if _id(ident) and isinstance(record, dict) and isinstance(record.get("trackIds"), list):
                    details[str(ident)].append((record.get("updateTime"), 0, "cached_track_ids", record["trackIds"]))
        playlists, used = [], set()
        for record in raw_playlists:
            ident = _id(record["id"])
            expected = _number(record.get("trackCount"))
            updated = _number(record.get("trackUpdateTime"))
            candidates = details.get(ident, [])
            issues, members, member_time, member_source = [], [], None, None
            if candidates:
                timestamp, _, member_source, raw_members = max(candidates, key=lambda item: (_number(item[0]) or 0, item[1]))
                member_time = _number(timestamp)
                for value in raw_members:
                    tid = _id(value.get("id")) if isinstance(value, dict) else _id(value)
                    if tid:
                        members.append(tid)
                    elif "invalid_membership_entry" not in issues:
                        issues.append("invalid_membership_entry")
                if expected != len(members):
                    issues.append("membership_count_mismatch")
                if updated is None or member_time is None:
                    issues.append("membership_timestamp_unknown")
                elif updated != member_time:
                    issues.append("membership_timestamp_mismatch")
                if len(set(members)) != len(members):
                    issues.append("duplicate_membership")
                if any(not tracks.get(tid, {}).get("metadata_available", False) for tid in members):
                    issues.append("missing_song_metadata")
            else:
                issues.append("missing_membership")
            used.update(members)
            playlists.append({
                "id": ident, "name": _text(record.get("name")),
                "owned": _id(record.get("userId")) == owner,
                "special_type": _number(record.get("specialType")),
                "tags": [tag for tag in record.get("tags", []) if isinstance(tag, str)] if isinstance(record.get("tags"), list) else [],
                "overview_track_count": expected, "overview_update_ms": updated,
                "membership_source": member_source, "membership_update_ms": member_time,
                "membership_available": bool(candidates), "members": members,
                "cached_track_count": len(members),
                "resolved_track_count": sum(bool(tracks.get(tid, {}).get("metadata_available")) for tid in members),
                "snapshot_complete": bool(candidates) and not issues,
                "issues": issues,
            })
        return {
            "source": {
                "kind": "local_desktop_cache", "database_path": str(self.database),
                "overview_selection": "last_inserted_successful_cached_response",
                "overview_complete": overview.get("more") is False and not invalid_overview_records,
                "invalid_overview_records": invalid_overview_records,
                "ignored_malformed_responses": ignored,
                "online_account_verified": False,
            },
            "owner_id": owner, "playlists": playlists,
            "tracks": {tid: tracks[tid] for tid in sorted(used) if tid in tracks},
        }
