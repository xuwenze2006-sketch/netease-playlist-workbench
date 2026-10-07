"""Bounded local classification display; these projections never grant a write permit."""

import copy
import hashlib
import json
import re
import stat
import threading
import unicodedata
from collections import OrderedDict
from datetime import datetime, timezone
from pathlib import Path

from .classification_execution import (
    ClassificationError, INTENT_FILE, PLAN_FILE, RECEIPT_FILE,
    _encoded, _journal, _result, plan_digest, validate_plan,
)
from .classification_planning import LANGUAGES, SCENES, STYLES


MAX_REPORT_BYTES = 16 * 1024 * 1024
MAX_PRIVATE_BYTES = 2 * 1024 * 1024
REPORT_FILE = '全库分类-逐曲结果.json'
FINAL_FILE = '分类整理最终核验.json'
_DIRECTORIES = ('在线整理快照.json', '在线名称整理快照.json')
_FILES = (PLAN_FILE, REPORT_FILE, INTENT_FILE, RECEIPT_FILE, FINAL_FILE, *_DIRECTORIES)
_SUMMARY_FIELDS = ('source_count', 'covered_count', 'playlist_count', 'pending_count',
                   'unknown_style_count', 'unknown_language_count')
_DIMENSIONS = ('scene', 'style', 'language')
_LABELS = {'scene': tuple(SCENES.values()), 'style': (*STYLES.values(), '待辨识'),
           'language': (*LANGUAGES.values(), '器乐或配乐录音', '待辨识')}
_MISSING = object()
# Only normalized public display fields are retained, never plans or write permits.
_PROJECTION_CACHE_LIMIT = 2
_PROJECTION_CACHE = OrderedDict()
_PROJECTION_CACHE_LOCK = threading.Lock()


class _Invalid(ValueError):
    pass


def _load_directory(project):
    # The workbench imports this mapper too, so defer its directory dependency.
    from .web_state import _load_directory as load
    return load(project)


def _integer(value, maximum=10000):
    if type(value) is not int or not 0 <= value <= maximum:
        raise _Invalid()
    return value


def _text(value, maximum=512, *, empty=False):
    if (type(value) is not str or len(value) > maximum or not empty and not value.strip()
            or any(ord(char) < 32 or 127 <= ord(char) <= 159 or 0xD800 <= ord(char) <= 0xDFFF
                   for char in value)):
        raise _Invalid()
    return value


def _time(value):
    _text(value, 80)
    try:
        parsed = datetime.fromisoformat(value)
        if parsed.tzinfo is None:
            raise _Invalid()
        return parsed.astimezone(timezone.utc)
    except (ValueError, OverflowError):
        raise _Invalid() from None


def _unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise _Invalid()
        result[key] = value
    return result


def _bad_constant(_):
    raise _Invalid()


def _signature(path):
    try:
        info = path.lstat()
        return (info.st_dev, info.st_ino, info.st_mode, info.st_size, info.st_mtime_ns,
                info.st_ctime_ns, getattr(info, 'st_file_attributes', 0))
    except FileNotFoundError:
        return None


def _signatures(project):
    return tuple(_signature(project / 'artifacts' / name) for name in _FILES)


def _cache_generation(project):
    """Hash bounded sources: Windows ctime does not identify in-place edits."""
    ancestors = []
    folder = project / 'artifacts'
    reparse = getattr(stat, 'FILE_ATTRIBUTE_REPARSE_POINT', 0x400)
    for path in (folder, *folder.parents):
        try:
            info = path.lstat()
        except FileNotFoundError:
            ancestors.append(None)
            continue
        if not stat.S_ISDIR(info.st_mode) or getattr(info, 'st_file_attributes', 0) & reparse:
            raise _Invalid()
        ancestors.append((info.st_dev, info.st_ino, info.st_mode))
    signatures = _signatures(project)
    if any(info is not None and (not stat.S_ISREG(info[2]) or info[6] & reparse)
           for info in signatures):
        raise _Invalid()
    digests = []
    for name, info in zip(_FILES, signatures):
        if info is None:
            digests.append(None)
            continue
        maximum = MAX_REPORT_BYTES if name == REPORT_FILE or name in _DIRECTORIES else MAX_PRIVATE_BYTES
        if info[3] > maximum:
            raise _Invalid()
        with (folder / name).open('rb') as stream:
            raw = stream.read(maximum + 1)
        if len(raw) > maximum or len(raw) != info[3]:
            raise _Invalid()
        digests.append(hashlib.sha256(raw).digest())
    if signatures != _signatures(project):
        raise _Invalid()
    return tuple(ancestors), signatures, tuple(digests), MAX_REPORT_BYTES, MAX_PRIVATE_BYTES


def _cached_projection(project, generation):
    with _PROJECTION_CACHE_LOCK:
        entry = _PROJECTION_CACHE.get(project)
        if entry is not None and entry[0] == generation:
            _PROJECTION_CACHE.move_to_end(project)
            return entry[1]
        _PROJECTION_CACHE.pop(project, None)
    return None


def _forget_projection(project):
    with _PROJECTION_CACHE_LOCK:
        _PROJECTION_CACHE.pop(project, None)


def _remember_projection(project, generation, projection):
    with _PROJECTION_CACHE_LOCK:
        _PROJECTION_CACHE[project] = generation, projection
        _PROJECTION_CACHE.move_to_end(project)
        while len(_PROJECTION_CACHE) > _PROJECTION_CACHE_LIMIT:
            _PROJECTION_CACHE.popitem(last=False)


def _load(project, name, maximum):
    path = project / 'artifacts' / name
    before = _signature(path)
    if before is None:
        return _MISSING
    if (path.is_symlink() or not path.resolve().is_relative_to(project)
            or not stat.S_ISREG(before[2]) or not 0 < before[3] <= maximum):
        raise _Invalid()
    with path.open('rb') as stream:
        raw = stream.read(maximum + 1)
    if len(raw) > maximum or before != _signature(path):
        raise _Invalid()
    value = json.loads(raw.decode('utf-8'), object_pairs_hook=_unique, parse_constant=_bad_constant)
    if type(value) is not dict:
        raise _Invalid()
    return value, before[4] / 10**9


def _parameters(offset, limit, query, dimension, tag, review):
    if (type(offset) is not int or not 0 <= offset <= 10000
            or type(limit) is not int or not 1 <= limit <= 100
            or type(dimension) is not str or dimension not in ('all', *_DIMENSIONS)
            or type(review) is not str or review not in ('all', 'pending')):
        raise ValueError('分类筛选参数不兼容。')
    try:
        _text(query, 160, empty=True)
        _text(tag, 160, empty=True)
    except _Invalid:
        raise ValueError('分类筛选参数不兼容。') from None
    return {'dimension': dimension, 'tag': tag, 'review': review, 'query': query}


def _empty(status, filters, offset, limit):
    return {'status': status, 'source': 'local_record', 'verification': 'local_only',
            'updated_at': None, 'verified_at': None,
            'summary': None,
            'options': {key: [] for key in _DIMENSIONS}, 'playlists': [], 'filters': filters,
            'pagination': {'offset': offset, 'limit': limit, 'total': 0, 'next_offset': None}, 'records': []}


def _bound_directory(project, plan):
    timestamp, directory = _load_directory(project)
    if (directory is None or directory.get('_owner') != plan['account_original_id']
            or directory.get('_account_id') != plan['account_id']):
        raise _Invalid()
    source = [row for row in directory['playlists']
              if row['_id'] == plan['source_playlist_id'] or row['key'] == plan['original_source_playlist_id']]
    if (len(source) != 1 or source[0]['_id'] != plan['source_playlist_id']
            or source[0]['key'] != plan['original_source_playlist_id']
            or source[0]['category'] != 'liked' or source[0]['track_count'] != plan['source_track_count']):
        raise _Invalid()
    return timestamp, directory


def _tags(values, dimension, maximum, *, nonempty=False):
    if type(values) is not list or not int(nonempty) <= len(values) <= maximum:
        raise _Invalid()
    if any(type(value) is not str or value not in _LABELS[dimension] for value in values):
        raise _Invalid()
    if len(set(values)) != len(values):
        raise _Invalid()
    return list(values)


def _report(raw, plan):
    if (raw.get('kind') != 'classification_report' or type(raw.get('records')) is not list
            or len(raw['records']) != len(plan['source_track_ids']) or type(raw.get('summary')) is not dict):
        raise _Invalid()
    created = _time(raw.get('created_at'))
    records, members, pending = [], {}, []
    for position, (raw_record, ident) in enumerate(zip(raw['records'], plan['source_track_ids']), 1):
        if (type(raw_record) is not dict or type(raw_record.get('position')) is not int
                or raw_record['position'] != position):
            raise _Invalid()
        styles = _tags(raw_record.get('styles'), 'style', 2, nonempty=True)
        scenes = _tags(raw_record.get('scenes'), 'scene', 3)
        language = raw_record.get('language')
        if ('待辨识' in styles and styles != ['待辨识'] or type(language) is not str
                or language not in _LABELS['language']):
            raise _Invalid()
        reasons = (['风格待辨识'] if styles == ['待辨识'] else []) + (['语言待辨识'] if language == '待辨识' else [])
        if raw_record.get('pending_reasons') != reasons:
            raise _Invalid()
        records.append({'position': position, 'name': _text(raw_record.get('name')),
                        'artists': _text(raw_record.get('artists'), 2048, empty=True),
                        'styles': styles, 'scenes': scenes, 'language': language,
                        'pending_reasons': reasons, 'evidence_note': _text(raw_record.get('evidence_note'), 2048),
                        'language_evidence_note': _text(raw_record.get('language_evidence_note'), 2048)})
        for dimension, labels in (('scene', scenes), ('style', styles), ('language', [language])):
            for label in labels:
                if label in ('待辨识', '器乐或配乐录音'):
                    continue
                members.setdefault((dimension, label), []).append(ident)
        if reasons:
            pending.append(ident)
    expected = {(dimension, {'scene': '场景', 'style': '风格', 'language': '语言'}[dimension] + ' · ' + label): ids
                for (dimension, label), ids in members.items()}
    if pending:
        expected['style', '分类 · 待辨识'] = pending
    if (len(expected) != len(plan['jobs']) or any(
            expected.get((job['dimension'], job['name'])) != job['candidate_track_ids'] for job in plan['jobs'])):
        raise _Invalid()
    summary = {'source_count': len(records), 'covered_count': len(set(ident for ids in expected.values() for ident in ids)),
               'playlist_count': len(plan['jobs']), 'pending_count': len(pending),
               'unknown_style_count': sum(row['styles'] == ['待辨识'] for row in records),
               'unknown_language_count': sum(row['language'] == '待辨识' for row in records)}
    for key, value in summary.items():
        if _integer(raw['summary'].get(key)) != value:
            raise _Invalid()
    supplied_playlists = raw['summary'].get('playlists')
    if type(supplied_playlists) is not list or len(supplied_playlists) != len(plan['jobs']):
        raise _Invalid()
    for supplied, job in zip(supplied_playlists, plan['jobs']):
        if (type(supplied) is not dict or supplied.get('name') != job['name']
                or _integer(supplied.get('count')) != len(job['candidate_track_ids'])):
            raise _Invalid()
    if 'dimensions' in raw['summary']:
        dimensions = {dim: sum(job['dimension'] == dim for job in plan['jobs']) for dim in _DIMENSIONS}
        dimensions = {dim: count for dim, count in dimensions.items() if count}
        supplied = raw['summary']['dimensions']
        if (type(supplied) is not dict or set(supplied) != set(dimensions)
                or any(_integer(supplied[dim], 64) != value for dim, value in dimensions.items())):
            raise _Invalid()
    return records, summary, created


def _receipt(project, plan):
    """Bind local history, including abnormal outcomes; never clear a recovery latch."""
    intent_record = _load(project, INTENT_FILE, MAX_PRIVATE_BYTES)
    receipt_record = _load(project, RECEIPT_FILE, MAX_PRIVATE_BYTES)
    if intent_record is _MISSING or receipt_record is _MISSING:
        return None
    intent = _journal(intent_record[0])
    receipt, receipt_time = receipt_record
    result = _result(receipt, plan)
    if (intent['plan'] != plan or receipt.get('kind') != 'classification_execution_receipt'
            or type(receipt.get('version')) is not int or receipt['version'] != 1
            or receipt.get('run_id') != intent['run_id'] or receipt.get('plan_digest') != plan_digest(plan)
            or receipt.get('intent_digest') != hashlib.sha256(_encoded(intent)).hexdigest()
            or receipt.get('record_saved') is not True or intent_record[1] > receipt_time
            or result['status'] not in ('completed', 'paused', 'partial', 'blocked', 'uncertain')):
        raise _Invalid()
    for key, value in (('source_expected_count', plan['source_track_count']),
                       ('source_observed_count', len(plan['source_track_ids'])),
                       ('source_missing_count', plan['source_track_count'] - len(plan['source_track_ids']))):
        if _integer(receipt.get(key)) != value:
            raise _Invalid()
    for index, (old, item) in enumerate(zip(intent['items'], result['items'])):
        if item['count'] > item['expected_count'] or item.get('added_count', 0) > item['count']:
            raise _Invalid()
        if old['playlist_id'] is not None:
            if (old['playlist_id'], old['original_playlist_id']) != (item['playlist_id'], item['original_playlist_id']):
                raise _Invalid()
        elif item['playlist_id'] is not None and index != intent['job_index']:
            raise _Invalid()
        if old['status'] == 'completed' and item != old:
            raise _Invalid()
    return intent, result, receipt_time


def _optional_receipt(project, plan):
    try:
        return _receipt(project, plan)
    except (OSError, ValueError, TypeError, UnicodeError, RecursionError, ClassificationError):
        return None


def _playlist_key(job, item, directory):
    if item is None or item['playlist_id'] is None:
        return None
    matches = [row for row in directory['playlists']
               if row['_id'] == item['playlist_id'] or row['key'] == item['original_playlist_id']]
    if (len(matches) != 1 or matches[0]['_id'] != item['playlist_id']
            or matches[0]['key'] != item['original_playlist_id'] or matches[0]['name'] != job['name']
            or matches[0]['track_count'] != len(job['candidate_track_ids']) or matches[0]['category'] == 'liked'):
        return None
    return item['original_playlist_id']


def _verified(project, plan, summary, created, report_time, directory_time, evidence, playlists):
    try:
        if evidence is None:
            return None
        intent, result, receipt_time = evidence
        if (result['status'] != 'completed' or result['outcome_known'] is not True
                or result['completed_count'] != len(plan['jobs']) or result['items'] != intent['items']
                or intent['phase'] != 'completed' or intent['job_index'] != len(plan['jobs']) - 1
                or any(result[key] != intent[key] for key in ('completed_count', 'applied_to_account', 'write_attempted'))
                or any(row['key'] is None for row in playlists)):
            return None
        record = _load(project, FINAL_FILE, MAX_PRIVATE_BYTES)
        if record is _MISSING:
            return None
        raw, final_time = record
        verified = _time(raw.get('verified_at'))
        if (raw.get('kind') != 'classification_final_readback' or raw.get('status') != 'verified'
                or raw.get('original_favorites_membership_and_order_unchanged') is not True
                or not created.timestamp() <= report_time <= receipt_time <= verified.timestamp() <= final_time <= directory_time
                or type(raw.get('playlists')) is not list or len(raw['playlists']) != len(plan['jobs'])):
            return None
        if any(_integer(raw.get(key)) != value for key, value in summary.items()):
            return None
        for job, item, row in zip(plan['jobs'], result['items'], raw['playlists']):
            if (type(row) is not dict or row.get('name') != job['name'] or row.get('dimension') != job['dimension']
                    or _integer(row.get('count')) != len(job['candidate_track_ids'])
                    or row.get('original_playlist_id') != item['original_playlist_id']):
                return None
            _integer(row.get('track_update_time'), 2**63 - 1)
        return verified.isoformat()
    except (OSError, ValueError, TypeError, UnicodeError, RecursionError):
        return None


def _fold(value):
    return unicodedata.normalize('NFKC', value).casefold()


def _matches(row, filters):
    if filters['review'] == 'pending' and not row['pending_reasons']:
        return False
    tag, dimension = filters['tag'], filters['dimension']
    if tag:
        labels = {'scene': row['scenes'], 'style': row['styles'], 'language': [row['language']]}
        dimensions = _DIMENSIONS if dimension == 'all' else (dimension,)
        if not any(tag in labels[dim] for dim in dimensions):
            return False
    return _fold(filters['query']) in _fold(row['name'] + ' ' + row['artists'])


def _build_projection(project):
    plan_record = _load(project, PLAN_FILE, MAX_PRIVATE_BYTES)
    report_record = _load(project, REPORT_FILE, MAX_REPORT_BYTES)
    if plan_record is _MISSING or report_record is _MISSING:
        return None
    plan = validate_plan(plan_record[0])
    directory_time, directory = _bound_directory(project, plan)
    records, summary, created = _report(report_record[0], plan)
    evidence = _optional_receipt(project, plan)
    items = evidence[1]['items'] if evidence else [None] * len(plan['jobs'])
    playlists = [{'name': job['name'], 'dimension': 'review' if job['name'] == '分类 · 待辨识' else job['dimension'],
                  'count': len(job['candidate_track_ids']),
                  'key': _playlist_key(job, item, directory)} for job, item in zip(plan['jobs'], items)]
    verified = _verified(project, plan, summary, created, report_record[1], directory_time, evidence, playlists)
    options = {dim: [label for label in _LABELS[dim] if any(
        label in row['scenes'] if dim == 'scene' else label in row['styles'] if dim == 'style'
        else label == row['language'] for row in records)] for dim in _DIMENSIONS}
    return {'status': 'available', 'source': 'local_record',
            'verification': 'verified' if verified else 'local_only', 'updated_at': created.isoformat(),
            'verified_at': verified, 'summary': summary, 'options': options, 'playlists': playlists,
            'records': records}


def build_local_classification(project, *, offset=0, limit=50, query='', dimension='all', tag='', review='all'):
    """Display paired local classification records, with no account or CLI access."""
    filters = _parameters(offset, limit, query, dimension, tag, review)
    fallback = _empty('unavailable', filters, offset, limit)
    cache_project = None
    try:
        cache_project = Path(project).resolve()
        generation = _cache_generation(cache_project)
        projection = _cached_projection(cache_project, generation)
        cached = projection is not None
        if not cached:
            projection = _build_projection(cache_project)
        if projection is None:
            result = _empty('not_loaded', filters, offset, limit)
        else:
            selected = [row for row in projection['records'] if _matches(row, filters)]
            total = len(selected)
            result = copy.deepcopy({**projection, 'filters': filters,
                                    'pagination': {'offset': offset, 'limit': limit, 'total': total,
                                                   'next_offset': offset + limit if offset + limit < total else None},
                                    'records': selected[offset:offset + limit]})
        if generation != _cache_generation(cache_project):
            raise _Invalid()
        if not cached and projection is not None:
            _remember_projection(cache_project, generation, projection)
        return result
    except (OSError, ValueError, TypeError, KeyError, UnicodeError, RecursionError, ClassificationError):
        if cache_project is not None:
            _forget_projection(cache_project)
        return fallback


def build_classification_history(project):
    """Return trusted local history and receipt mtime; classification has no generic resume."""
    try:
        project = Path(project).resolve()
        signatures = _signatures(project)
        record = _load(project, PLAN_FILE, MAX_PRIVATE_BYTES)
        if record is _MISSING:
            return None, 0
        plan = validate_plan(record[0])
        _bound_directory(project, plan)
        evidence = _optional_receipt(project, plan)
        if evidence is None or signatures != _signatures(project):
            return None, 0
        _, result, timestamp = evidence
        items = [{key: item[key] for key in ('name', 'status', 'count', 'expected_count', 'added_count', 'phase')
                  if key in item} for item in result['items']]
        return {'operation': 'classification', 'status': result['status'], 'source': 'local_record',
                'completed_count': result['completed_count'], 'items': items, 'resumable': False}, timestamp
    except (OSError, ValueError, TypeError, KeyError, UnicodeError, RecursionError, ClassificationError):
        return None, 0
