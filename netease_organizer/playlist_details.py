"""Validate and read private local playlist details without contacting an account."""

from __future__ import annotations

import json
import os
from pathlib import Path
import re
import stat
from typing import Any


MAX_RECORD_BYTES = 16 * 1024 * 1024
_HEX = re.compile(r"[0-9a-fA-F]{32}\Z")
_DECIMAL = re.compile(r"[1-9][0-9]{0,19}\Z")
_INVALID = "本地歌单明细记录无效。"


def _reject() -> None:
    raise ValueError(_INVALID)


def _mapping(value: Any) -> dict:
    if type(value) is not dict:
        _reject()
    return value


def _integer(value: Any, maximum: int) -> int:
    if type(value) is not int or not 0 <= value <= maximum:
        _reject()
    return value


def _boolean(value: Any) -> bool:
    if type(value) is not bool:
        _reject()
    return value


def _encrypted(value: Any) -> str:
    if type(value) is not str or _HEX.fullmatch(value) is None:
        _reject()
    return value.upper()


def _original(value: Any) -> str:
    if type(value) is int and 0 < value < 10**20:
        return str(value)
    if type(value) is not str or _DECIMAL.fullmatch(value) is None:
        _reject()
    return value


def _text(value: Any) -> str:
    if type(value) is not str or not value.strip() or len(value) > 512:
        _reject()
    if any(ord(char) < 32 or 127 <= ord(char) <= 159 or
           0xD800 <= ord(char) <= 0xDFFF for char in value):
        _reject()
    return value


def _sequence(value: Any, maximum: int) -> list:
    if type(value) is not list or len(value) > maximum:
        _reject()
    return value


def validate_record(raw: Any) -> dict:
    """Return a detached, normalized whitelist record, or raise a safe ValueError.

    Completeness describes this saved read, including known absent records and
    incomplete artist metadata. It does not establish a current online account.
    """
    raw = _mapping(raw)
    if raw.get("kind") != "playlist_details" or _integer(raw.get("version"), 1) != 1:
        _reject()
    read_at = _integer(raw.get("read_at"), 253402300799999)
    account_raw = _mapping(raw.get("account"))
    account = {"id": _encrypted(account_raw.get("id")),
               "original_id": _original(account_raw.get("original_id"))}
    source = _mapping(raw.get("playlist"))
    playlist = {
        "id": _encrypted(source.get("id")),
        "original_id": _original(source.get("original_id")),
        "name": _text(source.get("name")),
        "track_count": _integer(source.get("track_count"), 10000),
        "special_type": _integer(source.get("special_type"), 1000000),
        "track_update_time": _integer(source.get("track_update_time"), 10**18),
        "creator_id": _encrypted(source.get("creator_id")),
    }
    if playlist["creator_id"] != account["id"]:
        _reject()

    tracks = []
    track_ids: set[str] = set()
    track_originals: set[str] = set()
    artist_by_id: dict[str, tuple[str, str]] = {}
    artist_by_original: dict[str, str] = {}
    missing_metadata = []
    for track_raw in _sequence(source.get("tracks"), 10000):
        track_raw = _mapping(track_raw)
        track = {
            "id": _encrypted(track_raw.get("id")),
            "original_id": _original(track_raw.get("original_id")),
            "name": _text(track_raw.get("name")),
            "artists": [],
            "metadata_available": _boolean(track_raw.get("metadata_available")),
        }
        if track["id"] in track_ids or track["original_id"] in track_originals:
            _reject()
        track_ids.add(track["id"])
        track_originals.add(track["original_id"])
        local_ids: set[str] = set()
        local_originals: set[str] = set()
        for artist_raw in _sequence(track_raw.get("artists"), 100):
            artist_raw = _mapping(artist_raw)
            artist = {"id": _encrypted(artist_raw.get("id")),
                      "original_id": _original(artist_raw.get("original_id")),
                      "name": _text(artist_raw.get("name"))}
            encrypted, original, name = artist["id"], artist["original_id"], artist["name"]
            if encrypted in local_ids or original in local_originals:
                _reject()
            if (encrypted in artist_by_id and artist_by_id[encrypted] != (original, name)) or (
                    original in artist_by_original and artist_by_original[original] != encrypted):
                _reject()
            local_ids.add(encrypted)
            local_originals.add(original)
            artist_by_id[encrypted] = (original, name)
            artist_by_original[original] = encrypted
            track["artists"].append(artist)
        if track["metadata_available"] and not track["artists"]:
            _reject()
        if not track["metadata_available"]:
            missing_metadata.append(track["original_id"])
        tracks.append(track)

    missing_records = playlist["track_count"] - len(tracks)
    if missing_records < 0:
        _reject()
    membership_complete = _boolean(source.get("membership_complete"))
    metadata_complete = _boolean(source.get("metadata_complete"))
    supplied_missing = [_original(value) for value in
                        _sequence(source.get("missing_metadata_track_ids"), 10000)]
    if (membership_complete != (missing_records == 0) or
            metadata_complete != (not missing_metadata) or
            _integer(source.get("missing_record_count"), 10000) != missing_records or
            supplied_missing != missing_metadata):
        _reject()
    complete = _boolean(raw.get("complete"))
    if complete != (membership_complete and metadata_complete):
        _reject()
    playlist.update(tracks=tracks, membership_complete=membership_complete,
                    metadata_complete=metadata_complete, missing_record_count=missing_records,
                    missing_metadata_track_ids=missing_metadata)
    return {"kind": "playlist_details", "version": 1, "read_at": read_at,
            "account": account, "playlist": playlist, "complete": complete}


def _signature(info: os.stat_result) -> tuple[int, int, int, int]:
    # Windows pathname and open-handle ctime can differ for the same file.
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns


def _plain_path(path: Path, *, directory: bool = False) -> os.stat_result:
    info = path.lstat()
    if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & getattr(
            stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400):
        _reject()
    if not (stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode)):
        _reject()
    return info


def _parents(path: Path) -> tuple[tuple[Path, int, int], ...]:
    identities = []
    for parent in path.parents:
        info = _plain_path(parent, directory=True)
        identities.append((parent, info.st_dev, info.st_ino))
    return tuple(identities)


def _unique_object(pairs: list[tuple[str, Any]]) -> dict:
    value = {}
    for key, item in pairs:
        if key in value:
            _reject()
        value[key] = item
    return value


def _invalid_constant(_: str) -> None:
    _reject()


def load_record(project: Path, key: str) -> tuple[dict, int] | None:
    """Read one bounded stable record; missing and invalid files both return None."""
    if type(key) is not str or _DECIMAL.fullmatch(key) is None:
        return None
    try:
        base = Path(project).absolute()
        target = base / "artifacts" / "歌单明细" / f"{key}.json"
        parent_identities = _parents(target)
        before = _plain_path(target)
        if before.st_size > MAX_RECORD_BYTES or not target.resolve().is_relative_to(base.resolve()):
            return None
        with target.open("rb") as stream:
            opened = os.fstat(stream.fileno())
            if _signature(opened) != _signature(before) or not stat.S_ISREG(opened.st_mode):
                return None
            content = stream.read(MAX_RECORD_BYTES + 1)
            if len(content) > MAX_RECORD_BYTES or len(content) != opened.st_size:
                return None
            raw = json.loads(content.decode("utf-8"), object_pairs_hook=_unique_object,
                             parse_constant=_invalid_constant)
            normalized = validate_record(raw)
            if normalized["playlist"]["original_id"] != key:
                return None
            after_stream = os.fstat(stream.fileno())
            after_path = _plain_path(target)
            if (_signature(opened) != _signature(after_stream) or
                    _signature(before) != _signature(after_path) or
                    parent_identities != _parents(target) or
                    not target.resolve().is_relative_to(base.resolve())):
                return None
            return normalized, after_path.st_mtime_ns
    except (OSError, ValueError, TypeError, UnicodeError, RecursionError, OverflowError):
        return None
