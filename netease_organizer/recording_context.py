"""Optional historical display metadata, never an account or audio identity claim."""

import copy
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import threading
from collections import OrderedDict
from datetime import datetime

from .classification_execution import ClassificationError, PLAN_FILE, _encoded, plan_digest, validate_plan
from .web_preview import _original, _snapshot


BASELINE_FILE = '全库分类-账号基线.json'
HISTORY_FILE = '网易云本地歌单快照.json'
REPORT_FILE = '全库分类-逐曲结果.json'
MAX_BASELINE_BYTES = 2 * 1024 * 1024
MAX_HISTORY_BYTES = 16 * 1024 * 1024
MAX_CORE_BYTES = 16 * 1024 * 1024
_CACHE = OrderedDict()
_LOCK = threading.Lock()
_REPARSE = getattr(stat, 'FILE_ATTRIBUTE_REPARSE_POINT', 0x400)


class _Unavailable(ValueError):
    pass


def _signature(info):
    return (info.st_dev, info.st_ino, info.st_mode, info.st_size, info.st_mtime_ns,
            info.st_ctime_ns, getattr(info, 'st_file_attributes', 0))


def _ancestors(folder):
    result = []
    for path in (folder, *folder.parents):
        info = path.lstat()
        if not stat.S_ISDIR(info.st_mode) or getattr(info, 'st_file_attributes', 0) & _REPARSE:
            raise _Unavailable()
        # Unrelated sibling writes may change directory mtime/size without
        # replacing this trusted directory. Reparse flags are checked above.
        result.append(_signature(info)[:3])
    return tuple(result)


def _read_files(project, sources):
    """Read bounded regular files and hash bytes even when stat fields repeat."""
    folder = project / 'artifacts'
    ancestors = _ancestors(folder)
    signatures = tuple(_signature((folder / name).lstat()) for name, _ in sources)
    data, digests = [], []
    for (name, maximum), before in zip(sources, signatures):
        if not stat.S_ISREG(before[2]) or before[6] & _REPARSE or not 0 < before[3] <= maximum:
            raise _Unavailable()
        path = folder / name
        with path.open('rb') as stream:
            # Windows lstat/fstat can expose different ctime semantics. Match
            # descriptor identity/size/mtime, then recheck lstat and SHA bytes.
            descriptor = _signature(os.fstat(stream.fileno()))
            if descriptor[:5] != before[:5]:
                raise _Unavailable()
            raw = stream.read(maximum + 1)
            if _signature(os.fstat(stream.fileno())) != descriptor:
                raise _Unavailable()
        if len(raw) != before[3] or len(raw) > maximum:
            raise _Unavailable()
        data.append(raw)
        digests.append(hashlib.sha256(raw).digest())
    if (ancestors != _ancestors(folder)
            or signatures != tuple(_signature((folder / name).lstat()) for name, _ in sources)):
        raise _Unavailable()
    return (ancestors, signatures, tuple(digests), tuple(sources)), data


def _unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise _Unavailable()
        result[key] = value
    return result


def _constant(_):
    raise _Unavailable()


def _json(raw):
    value = json.loads(raw.decode('utf-8'), object_pairs_hook=_unique, parse_constant=_constant)
    if type(value) is not dict:
        raise _Unavailable()
    return value


def _text(value, *, empty=False, maximum=512):
    return (type(value) is str and len(value) <= maximum and (empty or bool(value.strip()))
            and not any(ord(char) < 32 or 127 <= ord(char) <= 159 or 0xD800 <= ord(char) <= 0xDFFF
                        for char in value))


def _timestamp(value):
    if not _text(value, maximum=80):
        raise _Unavailable()
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise _Unavailable()
    return parsed.isoformat()


def _bound_baseline(raw, plan):
    if (raw.get('kind') != 'classification_source_snapshot'
            or type(raw.get('snapshot')) is not dict):
        raise _Unavailable()
    _timestamp(raw.get('created_at'))
    snapshot = _snapshot(raw.get('snapshot'), 'full')
    account, liked = snapshot['account'], snapshot['liked']
    tracks = liked['tracks']
    if ((account['id'], account['original_id']) != (plan['account_id'], plan['account_original_id'])
            or (liked['id'], liked['original_id']) != (plan['source_playlist_id'], plan['original_source_playlist_id'])
            or liked['track_count'] != plan['source_track_count']
            or len(tracks) != plan['source_track_count']
            or [track['id'] for track in tracks] != plan['source_track_ids']):
        raise _Unavailable()
    # _snapshot checks membership flags, ownership and bijective IDs. Its
    # complete flag may legitimately be false for incomplete artist metadata.
    return tracks


def _row_context(track, report, historical, generated_at):
    if type(report) is not dict or type(historical) is not dict:
        return None
    try:
        if _original(historical.get('id')) != track['original_id']:
            return None
    except ValueError:
        return None
    name, artists = historical.get('name'), historical.get('artists')
    if (not _text(name) or type(artists) is not list or len(artists) > 100
            or any(type(artist) is not dict or not _text(artist.get('name')) for artist in artists)):
        return None
    joined = ' / '.join(artist['name'] for artist in artists)
    # Preserve the builder's order and repeated names; metadata=false permits
    # a historical fallback rather than comparing incomplete current artists.
    fallback = historical if not track['metadata_available'] else {}
    expected_name = fallback.get('name') or track['name']
    expected_artists = ' / '.join(artist['name'] for artist in (fallback.get('artists') or track['artists']))
    if (report.get('name') != expected_name or report.get('artists') != expected_artists
            or name != report.get('name') or joined != report.get('artists')):
        return None
    if 'live_metadata_available' in report and (
            type(report['live_metadata_available']) is not bool
            or report['live_metadata_available'] != track['metadata_available']):
        return None
    album = historical.get('album')
    if type(album) is dict:
        album = album.get('name')
    if not _text(album):
        album = None
    duration = historical.get('duration_ms')
    if type(duration) is not int or not 0 < duration <= 86400000:
        duration = None
    return {'source': 'historical_snapshot', 'generated_at': generated_at,
            'album': album, 'duration_ms': duration}


def _build_contexts(project, source_version, optional_bytes):
    sources = ((PLAN_FILE, MAX_BASELINE_BYTES), (REPORT_FILE, MAX_CORE_BYTES))
    core_generation, core_bytes = _read_files(project, sources)
    plan = validate_plan(_json(core_bytes[0]))
    report = _json(core_bytes[1])
    actual_version = hashlib.sha256(_encoded({'plan_digest': plan_digest(plan), 'report': report})).hexdigest()
    if actual_version != source_version:
        raise _Unavailable()
    baseline, historical = (_json(raw) for raw in optional_bytes)
    tracks = _bound_baseline(baseline, plan)
    if historical.get('format') != 'netease-offline-playlist-snapshot-v1':
        raise _Unavailable()
    generated_at = _timestamp(historical.get('generated_at'))
    history = historical.get('tracks')
    rows = report.get('records')
    if (type(history) is not dict or len(history) > 10000 or type(rows) is not list
            or len(rows) != len(tracks) or report.get('kind') != 'classification_report'):
        raise _Unavailable()
    result = {}
    for position, (track, row) in enumerate(zip(tracks, rows), 1):
        if type(row) is not dict or type(row.get('position')) is not int or row['position'] != position:
            raise _Unavailable()
        value = _row_context(track, row, history.get(track['original_id']), generated_at)
        if value is not None:
            key = hashlib.sha256(_encoded({'account': plan['account_id'], 'track': track['id']})).hexdigest()[:32]
            result[key] = value
    if core_generation != _read_files(project, sources)[0]:
        raise _Unavailable()
    return result


def _forget(project):
    with _LOCK:
        _CACHE.pop(project, None)


def recording_contexts(project, source_version, record_keys):
    """Return independently copied page contexts; optional failures never escape."""
    if (type(source_version) is not str or re.fullmatch(r'[a-f0-9]{64}', source_version) is None
            or type(record_keys) not in (list, tuple) or len(record_keys) > 100
            or any(type(key) is not str or re.fullmatch(r'[a-f0-9]{32}', key) is None for key in record_keys)
            or not record_keys):
        return {}
    project_path = None
    try:
        project_path = Path(project).resolve()
        sources = ((BASELINE_FILE, MAX_BASELINE_BYTES), (HISTORY_FILE, MAX_HISTORY_BYTES))
        generation, optional_bytes = _read_files(project_path, sources)
        cache_key = source_version, generation
        with _LOCK:
            entry = _CACHE.get(project_path)
            values = entry[1] if entry is not None and entry[0] == cache_key else None
            if values is not None:
                _CACHE.move_to_end(project_path)
            else:
                _CACHE.pop(project_path, None)
        if values is None:
            values = _build_contexts(project_path, source_version, optional_bytes)
        selected = {key: copy.deepcopy(values[key]) for key in record_keys if key in values}
        if generation != _read_files(project_path, sources)[0]:
            raise _Unavailable()
        with _LOCK:
            _CACHE[project_path] = cache_key, values
            _CACHE.move_to_end(project_path)
            while len(_CACHE) > 2:
                _CACHE.popitem(last=False)
        return selected
    except (OSError, ValueError, TypeError, KeyError, AttributeError, OverflowError,
            UnicodeError, RecursionError, ClassificationError):
        if project_path is not None:
            _forget(project_path)
        return {}
