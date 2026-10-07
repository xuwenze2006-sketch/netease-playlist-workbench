"""Public local records for the workbench; never call an account or expose raw DTOs."""

import copy
import re
from datetime import datetime, timezone
from pathlib import Path

from .service import _APPROVED_ARTIST_COUNTS
from .web_preview import build_local_preview, _load as _load_record


def _text(value, maximum=100):
    if (not isinstance(value, str) or not value.strip()
            or any(0xD800 <= ord(char) <= 0xDFFF for char in value)):
        return None
    return ''.join(char for char in value if ord(char) >= 32 and not 127 <= ord(char) <= 159)[:maximum]


def _decimal(value):
    if type(value) is int and value > 0:
        return str(value)
    return value if isinstance(value, str) and re.fullmatch(r'[1-9][0-9]{0,19}', value) else None


def _encrypted(value):
    return value.upper() if isinstance(value, str) and re.fullmatch(r'[0-9a-fA-F]{32}', value) else None


def _count(value, maximum=10000):
    return value if type(value) is int and 0 <= value <= maximum else None


def _load(project, name, maximum=256 * 1024):
    record = _load_record(project, name, maximum)
    return (record[0], record[1] / 10**9) if record is not None else (None, 0)


def _timestamp(value):
    return datetime.fromtimestamp(value, timezone.utc).isoformat() if value else None


def _row(raw, timestamp, *, liked=False):
    if not isinstance(raw, dict):
        return None
    ident, original = _encrypted(raw.get('id')), _decimal(raw.get('original_id'))
    name, count = _text(raw.get('name')), _count(raw.get('track_count'))
    if not ident or not original or not name or count is None:
        return None
    special = 5 if liked else raw.get('special_type')
    if _count(special) is None:
        return None
    return {'key': original, 'name': name, 'track_count': count,
            'category': 'liked' if special == 5 else ('artist' if name in _APPROVED_ARTIST_COUNTS else 'normal'),
            'source': 'local_record', 'updated_at': _timestamp(timestamp), '_id': ident}


def _snapshot(data, timestamp):
    if not isinstance(data, dict) or not isinstance(data.get('account'), dict):
        return None
    account = data['account']
    original, ident, nickname = _decimal(account.get('original_id')), _encrypted(account.get('id')), _text(account.get('nickname'))
    if not original or not ident or not nickname or not isinstance(data.get('playlists'), list) or len(data['playlists']) > 1000:
        return None
    rows, ids, originals = [], set(), set()
    for raw in data['playlists']:
        row = _row(raw, timestamp)
        if row is None or row['_id'] in ids or row['key'] in originals:
            return None
        rows.append(row)
        ids.add(row['_id'])
        originals.add(row['key'])
    liked = _row(data.get('liked'), timestamp, liked=True)
    if liked is not None:
        matching = next((row for row in rows if row['key'] == liked['key'] or row['_id'] == liked['_id']), None)
        if matching is not None:
            if matching['key'] != liked['key'] or matching['_id'] != liked['_id']:
                return None
        else:
            rows.insert(0, liked)
    return {'account': {'nickname': nickname}, '_owner': original, '_account_id': ident, 'playlists': rows,
            'source': 'local_record', 'updated_at': _timestamp(timestamp)}


def _identity_conflict(snapshots):
    """Equal timestamps cannot establish which conflicting identity is correct."""
    if len({(snapshot['_owner'], snapshot['_account_id']) for snapshot in snapshots}) > 1:
        return True
    ids, originals = {}, {}
    for snapshot in snapshots:
        for row in snapshot['playlists']:
            if (row['key'] in originals and originals[row['key']] != row['_id']
                    or row['_id'] in ids and ids[row['_id']] != row['key']):
                return True
            ids[row['_id']] = row['key']
            originals[row['key']] = row['_id']
    return False


def _history(record, operation):
    if (not isinstance(operation, str) or operation not in ('artists', 'renames', 'classification')
            or not isinstance(record, dict) or record.get('status') not in
            ('completed', 'paused', 'partial', 'uncertain', 'blocked')
            or _count(record.get('completed_count'), 1000) is None
            or not isinstance(record.get('items'), list)):
        return None
    items = []
    for item in record['items'][:64 if operation == 'classification' else 5]:
        if not isinstance(item, dict) or not _text(item.get('name')):
            continue
        safe = {'name': _text(item['name']), 'status': item.get('status') if item.get('status') in
                ('completed', 'pending', 'paused', 'partial', 'created', 'added', 'blocked', 'uncertain', 'skipped') else 'recorded'}
        if _count(item.get('count')) is not None:
            safe['count'] = item['count']
        if operation == 'classification':
            for key in ('expected_count', 'added_count'):
                if _count(item.get(key)) is not None:
                    safe[key] = item[key]
            if item.get('phase') in ('pending', 'created', 'adding', 'added', 'completed'):
                safe['phase'] = item['phase']
        items.append(safe)
    return {'operation': operation, 'status': record['status'], 'source': 'local_record',
            'completed_count': record['completed_count'], 'items': items, 'resumable': False}


def _confirmed_artists(receipt, journal, owner):
    if (not owner or not isinstance(receipt, dict) or not isinstance(journal, dict)
            or journal.get('kind') != 'artist_execution_journal'
            or journal.get('account_original_id') != owner
            or receipt.get('status') != 'completed' or type(receipt.get('completed_count')) is not int
            or receipt['completed_count'] != 5 or receipt.get('outcome_known') is not True
            or receipt.get('applied_to_account') is not True
            or not isinstance(receipt.get('items'), list) or len(receipt['items']) != 5):
        return []
    rows, names, ids, originals = [], set(), set(), set()
    for item in receipt['items']:
        if not isinstance(item, dict):
            return []
        name, count = item.get('name'), item.get('count')
        ident, original = _encrypted(item.get('playlist_id')), _decimal(item.get('original_playlist_id'))
        if (not isinstance(name, str) or name not in _APPROVED_ARTIST_COUNTS or name in names or item.get('status') != 'completed'
                or _count(count) != _APPROVED_ARTIST_COUNTS[name]
                or _count(item.get('expected_count')) != count or not ident or not original
                or ident in ids or original in originals):
            return []
        rows.append({'id': ident, 'original_id': original, 'name': name, 'track_count': count, 'special_type': 0})
        names.add(name)
        ids.add(ident)
        originals.add(original)
    return rows


def _load_directory(project):
    """Return the selected local directory with private identities for local mappers."""
    snapshots = []
    for name in ('在线整理快照.json', '在线名称整理快照.json'):
        record, timestamp = _load(project, name, maximum=16 * 1024 * 1024)
        snapshot = _snapshot(record, timestamp)
        if snapshot is not None:
            snapshots.append((timestamp, snapshot))
    if snapshots:
        snapshot_time, snapshot = max(snapshots, key=lambda pair: pair[0])
        latest = [candidate for timestamp, candidate in snapshots if timestamp == snapshot_time]
        if not _identity_conflict(latest):
            return snapshot_time, snapshot
    return 0, None


def build_local_state(project, *, organizer=None):
    """Read bounded project artifacts. Polling callers should cache this result."""
    project = Path(project).resolve()
    snapshot_time, snapshot = _load_directory(project)
    return _state_from_directory(project, snapshot_time, snapshot, organizer=organizer)


def directory_signatures(project):
    """Detect changes to every local source that may establish a playlist binding."""
    signatures = []
    for name in ('在线整理快照.json', '在线名称整理快照.json',
                 '歌手精选执行结果.json', '歌手精选执行进度.json'):
        try:
            info = (Path(project) / 'artifacts' / name).stat()
            signatures.append((info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns))
        except OSError:
            signatures.append(None)
    return tuple(signatures)


def load_playlist_binding(project, key):
    """Resolve only a visible, locally bound target. Private IDs stay in the backend."""
    if type(key) is not str or re.fullmatch(r'[1-9][0-9]{0,19}', key) is None:
        return None
    project = Path(project).resolve()
    signatures = directory_signatures(project)
    timestamp, snapshot = _load_directory(project)
    state = _project_directory(project, timestamp, snapshot)
    row = next((item for item in state['playlists'] if item['key'] == key), None)
    if (row is None or not state.get('_owner') or not state.get('_account_id')
            or signatures != directory_signatures(project)):
        return None
    return {'account': {'id': state['_account_id'], 'original_id': state['_owner']},
            'playlist': {'id': row['_id'], 'original_id': key}, 'signatures': signatures}


def _project_directory(project, snapshot_time, snapshot):
    """One private directory projection shared by display and explicit readers."""
    state = {'account': None, 'source': 'empty', 'updated_at': None, 'playlists': [],
             'history': None, 'artists_completed': False, 'preview': None}
    if snapshot is not None:
        state.update(copy.deepcopy(snapshot))
    artist_receipt, artist_time = _load(project, '歌手精选执行结果.json')
    journal, _ = _load(project, '歌手精选执行进度.json')
    completed = _confirmed_artists(artist_receipt, journal, state.get('_owner'))
    for raw in completed:
        matches = [row for row in state['playlists']
                   if row['key'] == raw['original_id'] or row['_id'] == raw['id']]
        if any(row['key'] != raw['original_id'] or row['_id'] != raw['id'] for row in matches):
            completed = []
            break
    state['artists_completed'] = bool(completed)
    if completed:
        for raw in completed:
            row = _row(raw, artist_time)
            matching = next((p for p in state['playlists'] if p['key'] == row['key'] or p['_id'] == row['_id']), None)
            # A receipt may fill a directory saved before execution. A newer
            # directory may already reflect a later deletion or other edits.
            if matching is None and artist_time > snapshot_time:
                state['playlists'].append(row)
            elif matching is not None and matching['key'] == row['key'] and matching['_id'] == row['_id'] and artist_time > snapshot_time:
                matching.update(row)
        state['updated_at'] = _timestamp(max(snapshot_time, artist_time))
    return state


def _state_from_directory(project, snapshot_time, snapshot, *, organizer=None):
    """Project one selected directory without changing its private identity evidence."""
    state = _project_directory(project, snapshot_time, snapshot)
    if organizer is not None:
        try:
            historical = organizer._last_result()
        except Exception:
            historical = None
        if isinstance(historical, dict):
            state['history'] = _history(historical, historical.get('operation'))
            if state['history'] is not None:
                state['history']['resumable'] = (state['history']['operation'] != 'classification' and state['history']['status'] == 'paused'
                                                and historical.get('resumable') is True)
    if state['history'] is None:
        histories = []
        from .web_classification import build_classification_history
        classification, timestamp = build_classification_history(project)
        if classification is not None:
            histories.append((timestamp, classification))
        for operation, name in (('artists', '歌手精选执行结果.json'), ('renames', '名称整理执行结果.json')):
            record, timestamp = _load(project, name)
            history = _history(record, operation)
            if history is not None:
                histories.append((timestamp, history))
        if histories:
            state['history'] = max(histories, key=lambda pair: pair[0])[1]
    if state.get('_owner') and state.get('_account_id'):
        state['preview'] = build_local_preview(project, expected_owner=state['_owner'],
                                             expected_account_id=state['_account_id'])
    state.pop('_owner', None)
    state.pop('_account_id', None)
    for row in state['playlists']:
        row.pop('_id', None)
    return state
