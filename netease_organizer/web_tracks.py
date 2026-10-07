"""Paged display of saved songs. Reading this module never contacts an account."""

from datetime import datetime, timezone
from pathlib import Path
import re
import unicodedata

from .web_preview import _load, _snapshot
from .web_state import _load_directory, _project_directory, directory_signatures
from .playlist_details import load_record


MAX_PAGE = 100
MAX_QUERY = 160


def _safe_text(value):
    return (isinstance(value, str) and bool(value.strip()) and len(value) <= 512
            and not any(ord(char) < 32 or 127 <= ord(char) <= 159 or 0xD800 <= ord(char) <= 0xDFFF
                        for char in value))


def _search_text(value):
    return unicodedata.normalize('NFKC', value).casefold().strip()


def _source_signatures(project, key):
    try:
        stat = (project / 'artifacts/歌单明细' / f'{key}.json').stat()
        detail = (stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns)
    except OSError:
        detail = None
    return directory_signatures(project) + (detail,)


def _unavailable(result):
    result.update(status='unavailable', updated_at=None, tracks=[])
    result['counts'].update(observed=None, missing=None, metadata_missing=None)
    result['pagination'].update(total=0, next_offset=None)
    return result


def build_local_tracks(project, key, *, offset=0, limit=50, query='', metadata='all'):
    """Return a bounded public page; saved details are historical, never a write permit."""
    if (type(key) is not str or re.fullmatch(r'[1-9][0-9]{0,19}', key) is None
            or type(offset) is not int or not 0 <= offset <= 10000
            or type(limit) is not int or not 1 <= limit <= MAX_PAGE
            or type(metadata) is not str or metadata not in ('all', 'incomplete')
            or type(query) is not str or len(query) > MAX_QUERY
            or any(ord(char) < 32 or 127 <= ord(char) <= 159 or 0xD800 <= ord(char) <= 0xDFFF
                   for char in query)):
        raise ValueError('Invalid local song request')
    project = Path(project).resolve()
    before = _source_signatures(project, key)
    snapshot_time, directory = _load_directory(project)
    private = _project_directory(project, snapshot_time, directory)
    private_row = next((item for item in private['playlists'] if item['key'] == key), None)
    row = {field: value for field, value in private_row.items() if field != '_id'} if private_row else None
    result = {'status': 'missing_playlist', 'source': 'local_record', 'playlist': row,
              'metadata_filter': metadata,
              'updated_at': None,
              'counts': {'expected': row['track_count'] if row else None, 'observed': None,
                         'missing': None, 'metadata_missing': None},
              'pagination': {'offset': offset, 'limit': limit, 'total': 0, 'next_offset': None},
              'tracks': []}
    if before != _source_signatures(project, key):
        return _unavailable(result)
    if row is None or directory is None:
        return result
    result['status'] = 'not_loaded'
    try:
        sources = []
        saved = load_record(project, key)
        path = project / 'artifacts/歌单明细' / f'{key}.json'
        if saved is None and (path.exists() or path.is_symlink() or path.parent.is_symlink()):
            return _unavailable(result)
        if saved is not None:
            record = saved[0]
            account, detail = record['account'], record['playlist']
            if ((account['original_id'], account['id']) == (private['_owner'], private['_account_id'])
                    and (detail['original_id'], detail['id']) == (key, private_row['_id'])):
                sources.append((record['read_at'] / 1000, detail))
        if row['category'] == 'liked':
            loaded = _load(project, '在线整理快照.json', 16 * 1024 * 1024)
            if loaded is None:
                if not sources and (project / 'artifacts/在线整理快照.json').exists():
                    return _unavailable(result)
            else:
                raw, timestamp = loaded
                if raw.get('tracks_loaded') is True:
                    try:
                        snapshot = _snapshot(raw, 'full')
                        account, liked = snapshot['account'], snapshot['liked']
                        if ((account['original_id'], account['id']) == (private['_owner'], private['_account_id'])
                                and (liked['original_id'], liked['id']) == (key, private_row['_id'])):
                            sources.append((timestamp / 10**9, liked))
                    except (ValueError, TypeError, KeyError, RecursionError):
                        if not sources:
                            return _unavailable(result)
        if before != _source_signatures(project, key):
            return _unavailable(result)
        if not sources:
            return result
        saved_time, liked = max(sources, key=lambda item: item[0])
        if (not _safe_text(row['name']) or not _safe_text(liked['name'])
                or any(not _safe_text(track['name']) or any(not _safe_text(artist['name'])
                           for artist in track['artists']) for track in liked['tracks'])):
            raise ValueError()
        all_tracks = liked['tracks']
        search = _search_text(query)
        selected = [(position, track) for position, track in enumerate(all_tracks, 1)
                    if (metadata == 'all' or track['metadata_available'] is False)
                    and (not search or search in _search_text(track['name'] + ' ' +
                                                           ' '.join(a['name'] for a in track['artists'])))]
        result.update(status='available', updated_at=datetime.fromtimestamp(saved_time, timezone.utc).isoformat())
        result['counts'] = {'expected': liked['track_count'], 'observed': len(all_tracks),
                            'missing': liked['track_count'] - len(all_tracks),
                            'metadata_missing': sum(not track['metadata_available'] for track in all_tracks)}
        result['pagination'].update(total=len(selected),
                                    next_offset=offset + limit if offset + limit < len(selected) else None)
        result['tracks'] = [{'key': f'track-{position}', 'position': position, 'name': track['name'],
                             'artists': [artist['name'].strip()[:160] for artist in track['artists'][:8]],
                             'artist_count': len(track['artists']),
                             'metadata_available': track['metadata_available']}
                            for position, track in selected[offset:offset+limit]]
        if before != _source_signatures(project, key):
            return _unavailable(result)
    except (ValueError, TypeError, KeyError, RecursionError, OverflowError, OSError):
        return _unavailable(result)
    return result
