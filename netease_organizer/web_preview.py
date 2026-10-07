"""Bounded, public-only previews of paired local records; never an execution permit."""

import json
import re
from datetime import datetime, timezone
from pathlib import Path

from .online_planning import build_online_plan


_REFERENCE = '本地记录，仅供预览；执行前仍需重新核验。'
_UNREAD = '本次未读取红心歌曲明细。'
_MISSING = '红心记录存在缺失，候选只包含已读取歌曲。'
_METADATA = '部分歌曲歌手资料不完整。'
_LIMIT = '精选候选超出展示上限，本次仅展示名称整理。'
_ORDER = '官方接口未提供歌单列表排列能力。'
_VISIBILITY = '精选候选为展示参考，创建时仍需确认官方默认可见性。'


class _InvalidRecord(ValueError):
    pass


def _original(value):
    if type(value) is int and 0 < value < 10 ** 20:
        return str(value)
    if isinstance(value, str) and re.fullmatch(r'[1-9][0-9]{0,19}', value):
        return value
    raise _InvalidRecord()


def _encrypted(value):
    if isinstance(value, str) and re.fullmatch(r'[0-9a-fA-F]{32}', value):
        return value.upper()
    raise _InvalidRecord()


def _text(value):
    if (not isinstance(value, str) or not value.strip() or len(value) > 512
            or any(ord(char) < 32 or 127 <= ord(char) <= 159 for char in value)):
        raise _InvalidRecord()
    return value


def _integer(value, maximum=10000):
    if type(value) is not int or not 0 <= value <= maximum:
        raise _InvalidRecord()
    return value


def _object(value):
    if not isinstance(value, dict):
        raise _InvalidRecord()
    return value


def _list(value, maximum):
    if not isinstance(value, list) or len(value) > maximum:
        raise _InvalidRecord()
    return value


def _unique_object(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise _InvalidRecord()
        value[key] = item
    return value


def _bad_constant(value):
    raise _InvalidRecord()


def _load(project, name, maximum):
    path = project / 'artifacts' / name
    try:
        if path.is_symlink() or not path.resolve().is_relative_to(project):
            return None
        before = path.stat()
        if not 0 < before.st_size <= maximum:
            return None
        with path.open('rb') as stream:
            raw = stream.read(maximum + 1)
        after = path.stat()
        if (len(raw) > maximum or before.st_size != after.st_size
                or before.st_mtime_ns != after.st_mtime_ns):
            return None
        data = json.loads(raw.decode('utf-8'), object_pairs_hook=_unique_object,
                          parse_constant=_bad_constant)
        return _object(data), after.st_mtime_ns
    except (OSError, UnicodeError, ValueError, RecursionError):
        return None


def _identity(raw):
    raw = _object(raw)
    return {'id': _encrypted(raw.get('id')), 'original_id': _original(raw.get('original_id'))}


def _playlist(raw):
    return {**_identity(raw), 'name': _text(raw.get('name')),
            'track_count': _integer(raw.get('track_count')),
            'special_type': _integer(raw.get('special_type'), 1000000)}


def _bind(item, encrypted, originals, *, unique=False):
    ident, original = item['id'], item['original_id']
    if (unique and (ident in encrypted or original in originals)
            or ident in encrypted and encrypted[ident] != original
            or original in originals and originals[original] != ident):
        raise _InvalidRecord()
    encrypted[ident] = original
    originals[original] = ident


def _snapshot(raw, scope):
    account_raw = _object(raw.get('account'))
    account = {**_identity(account_raw), 'nickname': _text(account_raw.get('nickname'))}
    if raw.get('overview_complete') is not True or type(raw.get('complete')) is not bool:
        raise _InvalidRecord()
    loaded = raw.get('tracks_loaded')
    if type(loaded) is not bool or loaded != (scope == 'full'):
        raise _InvalidRecord()
    playlists, ids, originals = [], {}, {}
    for record in _list(raw.get('playlists'), 1000):
        item = _playlist(_object(record))
        _bind(item, ids, originals, unique=True)
        playlists.append(item)
    liked_raw = _object(raw.get('liked'))
    liked = _playlist(liked_raw)
    if liked['special_type'] != 5 or _encrypted(liked_raw.get('creator_id')) != account['id']:
        raise _InvalidRecord()
    _bind(liked, ids, originals)
    matching = [item for item in playlists if item['id'] == liked['id']]
    if matching and matching[0] != liked:
        raise _InvalidRecord()
    if any(item['special_type'] == 5 and item['id'] != liked['id'] for item in playlists):
        raise _InvalidRecord()
    tracks, track_ids, track_originals, artist_ids, artist_originals = [], {}, {}, {}, {}
    artist_names = {}
    for record in _list(liked_raw.get('tracks'), 10000):
        track = {**_identity(record), 'name': _text(record.get('name'))}
        _bind(track, track_ids, track_originals, unique=True)
        artists, ids_in_track, originals_in_track = [], {}, {}
        for raw_artist in _list(record.get('artists'), 100):
            artist = {**_identity(raw_artist), 'name': _text(raw_artist.get('name'))}
            _bind(artist, ids_in_track, originals_in_track, unique=True)
            _bind(artist, artist_ids, artist_originals)
            name = artist['name'].strip()
            if artist['id'] in artist_names and artist_names[artist['id']] != name:
                raise _InvalidRecord()
            artist_names[artist['id']] = name
            artists.append(artist)
        available = record.get('metadata_available')
        if type(available) is not bool or available and not artists:
            raise _InvalidRecord()
        track.update(artists=artists, metadata_available=available)
        tracks.append(track)
    if len(tracks) > liked['track_count'] or not loaded and tracks:
        raise _InvalidRecord()
    missing = liked['track_count'] - len(tracks)
    metadata_missing = [track['original_id'] for track in tracks if not track['metadata_available']]
    membership_complete = loaded and missing == 0
    metadata_complete = loaded and not metadata_missing
    if (type(liked_raw.get('membership_complete')) is not bool
            or liked_raw['membership_complete'] != membership_complete
            or type(liked_raw.get('metadata_complete')) is not bool
            or liked_raw['metadata_complete'] != metadata_complete
            or _integer(liked_raw.get('missing_record_count')) != missing
            or [_original(item) for item in _list(liked_raw.get('missing_metadata_track_ids'), 10000)] != metadata_missing
            or raw['complete'] != (membership_complete and metadata_complete)):
        raise _InvalidRecord()
    liked.update(tracks=tracks)
    return {'account': account, 'playlists': playlists, 'liked': liked,
            'overview_complete': True, 'tracks_loaded': loaded, 'complete': raw['complete']}


def _projection(plan):
    renames, artists = [], []
    for job in _list(plan.get('jobs'), 1006):
        job = _object(job)
        kind = job.get('kind')
        if kind == 'rename_playlist':
            if job.get('status') != 'ready':
                raise _InvalidRecord()
            renames.append({'playlist_id': _encrypted(job.get('playlist_id')),
                            'original_playlist_id': _original(job.get('original_playlist_id')),
                            'old_name': _text(job.get('old_name')), 'name': _text(job.get('name'))})
        elif kind == 'create_artist_playlist':
            artist = _object(job.get('artist'))
            candidates = [_encrypted(item) for item in _list(job.get('candidate_track_ids'), 10000)]
            originals = [_original(item) for item in _list(job.get('original_candidate_track_ids'), 10000)]
            if (job.get('status') != 'blocked' or not candidates or len(candidates) != len(originals)
                    or len(set(candidates)) != len(candidates) or len(set(originals)) != len(originals)):
                raise _InvalidRecord()
            artists.append({'name': _text(job.get('name')), 'artist_id': _original(artist.get('id')),
                            'artist_name': _text(artist.get('name')),
                            'source_playlist_id': _encrypted(job.get('source_playlist_id')),
                            'candidates': candidates, 'original_candidates': originals})
        elif kind != 'reorder_playlists':
            raise _InvalidRecord()
    if len(renames) > 1000 or len(artists) > 5:
        raise _InvalidRecord()
    return renames, artists


def _pair(raw_snapshot, plan, scope, timestamp, expected):
    live = _snapshot(raw_snapshot, scope)
    account = live['account']
    source = _object(plan.get('source'))
    if (plan.get('kind') != 'official_online_organizing_plan' or type(plan.get('schema_version')) is not int
            or plan['schema_version'] != 1 or plan.get('online_account_verified') is not True
            or source.get('provider') != 'official_ncm_cli' or source.get('online_account_verified') is not True
            or plan.get('applied_to_account') is not False
            or _original(plan.get('owner_id')) != account['original_id']
            or _encrypted(plan.get('account_id')) != account['id']
            or expected is not None and expected != (account['original_id'], account['id'])):
        raise _InvalidRecord()
    expected_plan = build_online_plan(live)
    renames, artists = _projection(plan)
    if (renames, artists) != _projection(expected_plan):
        raise _InvalidRecord()
    summary = _object(plan.get('summary'))
    for field in ('rename_count', 'artist_playlist_count', 'online_liked_expected_count',
                  'online_liked_observed_count', 'online_missing_record_count'):
        if _integer(summary.get(field)) != expected_plan['summary'][field]:
            raise _InvalidRecord()
    limitations = [_REFERENCE]
    observed = len(live['liked']['tracks'])
    missing = live['liked']['track_count'] - observed
    if not live['tracks_loaded']:
        limitations.append(_UNREAD)
    elif missing:
        limitations.append(_MISSING)
    if any(not track['metadata_available'] for track in live['liked']['tracks']):
        limitations.append(_METADATA)
    if any(job.get('kind') == 'reorder_playlists' for job in plan['jobs']):
        limitations.append(_ORDER)
    if artists:
        limitations.append(_VISIBILITY)
    public_artists = []
    if any(len(artist['candidates']) > 500 for artist in artists):
        scope = 'names'
        limitations.append(_LIMIT)
    else:
        tracks = {track['id']: track for track in live['liked']['tracks']}
        for artist in artists:
            public_artists.append({'name': artist['name'], 'count': len(artist['candidates']),
                                   'tracks': [{'name': tracks[ident]['name'],
                                               'artists': [item['name'].strip() for item in tracks[ident]['artists']]}
                                              for ident in artist['candidates']]})
    preview = {'source': 'local_record', 'scope': scope,
               'updated_at': datetime.fromtimestamp(timestamp / 10 ** 9, timezone.utc).isoformat(),
               'rename_count': len(renames),
               'renames': [{'key': f'rename-{index}', 'old_name': job['old_name'], 'name': job['name']}
                           for index, job in enumerate(renames, 1)],
               'artists': public_artists, 'limitations': limitations,
               'liked': {'expected': live['liked']['track_count'],
                         'observed': observed if live['tracks_loaded'] else None,
                         'missing': missing if live['tracks_loaded'] else None}}
    identities = {'playlist': {}, 'track': {}, 'artist': {}}
    for item in live['playlists'] + [live['liked']]:
        identities['playlist'][item['original_id']] = item['id']
    for track in live['liked']['tracks']:
        identities['track'][track['original_id']] = track['id']
        for artist in track['artists']:
            identities['artist'][artist['original_id']] = artist['id']
    return preview, (account['original_id'], account['id']), identities


def _conflict(records):
    if len({record[2] for record in records}) > 1:
        return True
    for domain in ('playlist', 'track', 'artist'):
        encrypted, originals = {}, {}
        for record in records:
            for original, ident in record[3][domain].items():
                if (original in originals and originals[original] != ident
                        or ident in encrypted and encrypted[ident] != original):
                    return True
                encrypted[ident] = original
                originals[original] = ident
    return False


def build_local_preview(project, *, expected_owner=None, expected_account_id=None):
    """Return the latest paired display record, or None. No account calls occur.

    Supply both private account identities to bind a preview to the directory
    currently displayed. The returned object contains neither identity.
    """
    try:
        project = Path(project).resolve()
        expected = None
        if expected_owner is not None or expected_account_id is not None:
            expected = (_original(expected_owner), _encrypted(expected_account_id))
    except (OSError, ValueError, TypeError):
        return None
    records = []
    for scope, prefix in (('full', '在线整理'), ('names', '在线名称整理')):
        snapshot = _load(project, prefix + '快照.json', 16 * 1024 * 1024)
        plan = _load(project, prefix + '清单.json', 2 * 1024 * 1024)
        if snapshot is None or plan is None or snapshot[1] > plan[1]:
            continue
        try:
            preview, account, identities = _pair(snapshot[0], plan[0], scope, plan[1], expected)
            records.append((plan[1], preview, account, identities))
        except (ValueError, TypeError, KeyError, RecursionError, OverflowError, OSError):
            continue
    if not records:
        return None
    timestamp = max(record[0] for record in records)
    latest = [record for record in records if record[0] == timestamp]
    return None if _conflict(latest) else latest[0][1]
