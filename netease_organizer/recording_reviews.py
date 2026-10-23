"""Source-bound recording evidence and conservative, offline scene previews.

A stored review is an immutable assertion by its author, not proof that audio
was heard. Explicit audio evidence is required before scene changes can appear.
Neither reviews nor previews authorize or execute account mutations.
"""

import copy
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import re
import stat
from urllib.parse import urlsplit

from .classification_planning import SCENES


MAX_REVIEW_BYTES = 16 * 1024 * 1024
_SCENES = frozenset(SCENES.values())
_KIND = 'classification_recording_review'
_SOURCE_FILE = '全库分类-逐曲结果.json'
_REPARSE = getattr(stat, 'FILE_ATTRIBUTE_REPARSE_POINT', 0x400)
_CONTROLS = re.compile(r'[\x00-\x09\x0b-\x1f\x7f-\x9f\ud800-\udfff]')
_SAFE_NAME = re.compile(r'[A-Za-z0-9][A-Za-z0-9_-]{0,63}\Z')
_RESERVED = re.compile(r'(?:CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])\Z', re.I)


class ReviewError(ValueError):
    """The review, source binding, or local storage cannot be trusted."""


def _fail(message='录音核验格式不兼容或本地文件不可用。'):
    raise ReviewError(message)


def _unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            _fail()
        result[key] = value
    return result


def _constant(_):
    _fail()


def _float(text):
    number = float(text)
    if not math.isfinite(number):
        _fail()
    return number


def decode_review(data):
    """Decode bounded, strict JSON; validate_review performs schema checks."""
    if type(data) is not bytes or not 0 < len(data) <= MAX_REVIEW_BYTES:
        _fail()
    try:
        result = json.loads(data.decode('utf-8'), object_pairs_hook=_unique,
                            parse_constant=_constant, parse_float=_float)
    except (ValueError, UnicodeError, RecursionError):
        _fail()
    if type(result) is not dict:
        _fail()
    return result


def _object(value, fields):
    if type(value) is not dict or set(value) != set(fields.split()):
        _fail()


def _text(value, *, empty=False, maximum=2000):
    if (type(value) is not str or len(value) > maximum or _CONTROLS.search(value)
            or not empty and not value.strip()):
        _fail()
    return value


def _url(value):
    _text(value)
    if any(character.isspace() for character in value) or '\\' in value:
        _fail()
    try:
        parsed = urlsplit(value)
        if (parsed.scheme not in ('https', 'http') or not parsed.hostname
                or parsed.username is not None or parsed.password is not None):
            _fail()
        parsed.port
    except ValueError:
        _fail()
    return value


def _scenes(value):
    if (type(value) is not list or len(value) > 3
            or any(type(item) is not str or item not in _SCENES for item in value)
            or len(set(value)) != len(value)):
        _fail()
    return value


def _source_records(source_bytes):
    source = decode_review(source_bytes)
    rows = source.get('records')
    if source.get('kind') != 'classification_report' or type(rows) is not list or not 1 <= len(rows) <= 10000:
        _fail()
    records = {}
    for index, row in enumerate(rows, 1):
        if type(row) is not dict or type(row.get('position')) is not int or row['position'] != index:
            _fail()
        _text(row.get('name'))
        _text(row.get('artists'))
        _scenes(row.get('scenes'))
        records[index] = row
    return records


def create_review(source_bytes, positions):
    """Create a detached template; every initial decision remains uncertain."""
    source = _source_records(source_bytes)
    if (type(positions) is not list or not 1 <= len(positions) <= 50
            or any(type(position) is not int or position not in source for position in positions)
            or len(set(positions)) != len(positions)):
        _fail()
    rows = []
    for position in positions:
        original = source[position]
        rows.append({'position': position, 'name': original['name'], 'artists': original['artists'],
                     'original_scenes': list(original['scenes']),
                     'recording': {'status': 'unresolved', 'version': '', 'note': '',
                                   'evidence_sources': [], 'listened_segments': []},
                     'scene_decisions': [{'scene': scene, 'decision': 'uncertain',
                                          'reason': '尚未完成具体录音试听核验'} for scene in original['scenes']]})
    return {'kind': _KIND, 'version': 1, 'source_sha256': hashlib.sha256(source_bytes).hexdigest(),
            'source_count': len(source), 'created_at': datetime.now(timezone.utc).isoformat(), 'records': rows}


def _recording(value):
    _object(value, 'status version note evidence_sources listened_segments')
    status = value['status']
    if type(status) is not str or status not in ('unresolved', 'metadata_matched', 'audio_verified'):
        _fail()
    _text(value['version'], empty=status == 'unresolved')
    _text(value['note'], empty=status == 'unresolved', maximum=8000)
    evidence, segments = value['evidence_sources'], value['listened_segments']
    if type(evidence) is not list or len(evidence) > 20 or type(segments) is not list or len(segments) > 100:
        _fail()
    audio_urls, metadata_urls, all_urls = set(), set(), set()
    for item in evidence:
        _object(item, 'url title kind note')
        url = _url(item['url'])
        _text(item['title'])
        _text(item['note'])
        if type(item['kind']) is not str or item['kind'] not in ('catalog', 'lyrics', 'audio'):
            _fail()
        identity = (url, item['kind'])
        if identity in all_urls:
            _fail()
        all_urls.add(identity)
        (audio_urls if item['kind'] == 'audio' else metadata_urls).add(url)
    for segment in segments:
        _object(segment, 'start_seconds end_seconds source_url note')
        start, end = segment['start_seconds'], segment['end_seconds']
        if (type(start) not in (int, float) or type(end) not in (int, float)
                or not 0 <= start < end <= 86400 or not math.isfinite(start) or not math.isfinite(end)):
            _fail()
        if _url(segment['source_url']) not in audio_urls:
            _fail()
        _text(segment['note'])
    if (status == 'audio_verified' and (not audio_urls or not segments)
            or status != 'audio_verified' and segments
            or status == 'metadata_matched' and not metadata_urls):
        _fail()


def _after_scenes(row):
    after = list(row['original_scenes'])
    for decision in row['scene_decisions']:
        scene = decision['scene']
        if decision['decision'] == 'unsuitable' and scene in after:
            after.remove(scene)
        elif decision['decision'] == 'suitable' and scene not in after:
            after.append(scene)
    _scenes(after)
    return after


def validate_review(review, source_bytes):
    """Validate exact source identity and evidence requirements, without mutation."""
    source = _source_records(source_bytes)
    _object(review, 'kind version source_sha256 source_count created_at records')
    if (review['kind'] != _KIND or type(review['version']) is not int or review['version'] != 1
            or type(review['source_count']) is not int or review['source_count'] != len(source)
            or type(review['source_sha256']) is not str
            or review['source_sha256'] != hashlib.sha256(source_bytes).hexdigest()):
        _fail('核验记录与当前分类来源不一致。')
    _text(review['created_at'], maximum=64)
    try:
        timestamp = datetime.fromisoformat(review['created_at'])
        if timestamp.tzinfo is None:
            _fail()
    except ValueError:
        _fail()
    rows = review['records']
    if type(rows) is not list or not 1 <= len(rows) <= 50:
        _fail()
    selected = set()
    for row in rows:
        _object(row, 'position name artists original_scenes recording scene_decisions')
        position = row['position']
        if type(position) is not int or position not in source or position in selected:
            _fail()
        selected.add(position)
        original = source[position]
        if (row['name'] != original['name'] or row['artists'] != original['artists']
                or _scenes(row['original_scenes']) != original['scenes']):
            _fail('歌曲身份或原场景与分类来源不一致。')
        _recording(row['recording'])
        decisions = row['scene_decisions']
        if type(decisions) is not list or len(decisions) > len(_SCENES):
            _fail()
        seen = set()
        for decision in decisions:
            _object(decision, 'scene decision reason')
            scene, verdict = decision['scene'], decision['decision']
            if (type(scene) is not str or scene not in _SCENES or scene in seen
                    or type(verdict) is not str or verdict not in ('suitable', 'unsuitable', 'uncertain')):
                _fail()
            seen.add(scene)
            _text(decision['reason'])
            if verdict != 'uncertain' and row['recording']['status'] != 'audio_verified':
                _fail('场景确认需要具体录音来源和试听区间。')
        _after_scenes(row)
    try:
        encoded = json.dumps(review, ensure_ascii=False, allow_nan=False).encode('utf-8')
    except (ValueError, TypeError, UnicodeError, RecursionError):
        _fail()
    if len(encoded) > MAX_REVIEW_BYTES:
        _fail()
    return copy.deepcopy(review)


def build_preview(review, source_bytes):
    """Unknown or unmentioned decisions preserve existing membership."""
    validated = validate_review(review, source_bytes)
    summary = dict.fromkeys(('reviewed_records', 'metadata_matched_records', 'audio_verified_records',
                             'scene_verified_records', 'uncertain_records', 'partial_scene_records',
                             'added_count', 'removed_count'), 0)
    rows = []
    for row in validated['records']:
        before, after = row['original_scenes'], _after_scenes(row)
        added = [scene for scene in after if scene not in before]
        removed = [scene for scene in before if scene not in after]
        decisions = row['scene_decisions']
        known = sum(item['decision'] != 'uncertain' for item in decisions)
        scope = set(before) | {item['scene'] for item in decisions}
        verified = bool(scope) and known == len(scope)
        summary['reviewed_records'] += 1
        summary['metadata_matched_records'] += row['recording']['status'] == 'metadata_matched'
        summary['audio_verified_records'] += row['recording']['status'] == 'audio_verified'
        summary['scene_verified_records'] += verified
        summary['uncertain_records'] += not verified
        summary['partial_scene_records'] += 0 < known < len(scope)
        summary['added_count'] += len(added)
        summary['removed_count'] += len(removed)
        rows.append({key: row[key] for key in ('position', 'name', 'artists', 'scene_decisions', 'recording')})
        rows[-1].update(before_scenes=before, after_scenes=after, added_scenes=added, removed_scenes=removed)
    return {'kind': 'classification_scene_review_preview', 'source_sha256': validated['source_sha256'],
            'summary': summary, 'records': rows}


def _ordinary(info, *, directory):
    if (stat.S_ISLNK(info.st_mode) or getattr(info, 'st_file_attributes', 0) & _REPARSE
            or not (stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode))):
        _fail()


def _signature(info):
    return (info.st_dev, info.st_ino, info.st_mode, info.st_size, info.st_mtime_ns,
            info.st_ctime_ns, getattr(info, 'st_file_attributes', 0))


def _opened_signature(info):
    # Windows lstat and fstat disagree about ctime; compare shared fields.
    signature = _signature(info)
    return signature[:5], signature[6]


def _directories(folder):
    result = []
    for directory in (folder, *folder.parents):
        info = directory.lstat()
        _ordinary(info, directory=True)
        result.append((str(directory), info.st_dev, info.st_ino, info.st_mode,
                       getattr(info, 'st_file_attributes', 0)))
    return tuple(result)


def _read_file(path):
    directories = _directories(path.parent)
    before = path.lstat()
    _ordinary(before, directory=False)
    if not 0 < before.st_size <= MAX_REVIEW_BYTES:
        _fail()
    flags = os.O_RDONLY | getattr(os, 'O_BINARY', 0) | getattr(os, 'O_NOFOLLOW', 0)
    with os.fdopen(os.open(path, flags), 'rb') as stream:
        opened = os.fstat(stream.fileno())
        _ordinary(opened, directory=False)
        if _opened_signature(opened) != _opened_signature(before):
            _fail()
        data = stream.read(MAX_REVIEW_BYTES + 1)
    if (len(data) != before.st_size or len(data) > MAX_REVIEW_BYTES
            or _signature(path.lstat()) != _signature(before) or _directories(path.parent) != directories):
        _fail()
    return data, (_signature(before), hashlib.sha256(data).digest(), directories)


def _source(project):
    project = Path(project).absolute()
    _directories(project)
    data, identity = _read_file(project / 'artifacts' / _SOURCE_FILE)
    _source_records(data)
    return data, identity


def load_source(project):
    """Read only the fixed, bounded, ordinary classification report file."""
    try:
        return _source(project)[0]
    except OSError:
        _fail()


def _bundle(project, name):
    if (type(name) is not str or _SAFE_NAME.fullmatch(name) is None
            or _RESERVED.fullmatch(name)):
        _fail('核验包名称须为安全的字母、数字、连字符或下划线。')
    return Path(project).absolute() / 'artifacts' / 'recording-reviews' / name


def save_review(project, review, bundle_name):
    """Create an immutable bundle; collisions and failed attempts are retained."""
    folder = _bundle(project, bundle_name)
    try:
        source, identity = _source(project)
        validated = validate_review(review, source)
        data = (json.dumps(validated, ensure_ascii=False, allow_nan=False, indent=2) + '\n').encode('utf-8')
        if len(data) > MAX_REVIEW_BYTES:
            _fail()
        parent_identity = _directories(folder.parent.parent)
        try:
            folder.parent.mkdir()
        except FileExistsError:
            pass
        _directories(folder.parent)
        if _directories(folder.parent.parent) != parent_identity:
            _fail()
        folder.mkdir()  # Exclusive even if an earlier attempt was incomplete.
        directory_identity = _directories(folder)
        if _source(project)[1] != identity:
            _fail('分类来源在保存前发生变化。')
        target = folder / 'review.json'
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, 'O_BINARY', 0) | getattr(os, 'O_NOFOLLOW', 0)
        with os.fdopen(os.open(target, flags, 0o600), 'wb') as stream:
            _ordinary(os.fstat(stream.fileno()), directory=False)
            if _directories(folder) != directory_identity:
                _fail()
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        reread, _ = _read_file(target)
        if (reread != data or _directories(folder) != directory_identity
                or _source(project)[1] != identity):
            _fail('核验包或分类来源在保存期间发生变化。')
        return target
    except OSError:
        _fail('核验包已存在或本地路径不可用；未覆盖已有记录。')


def read_review(project, bundle_name):
    """Reload and validate an immutable bundle against the current source."""
    folder = _bundle(project, bundle_name)
    try:
        source, identity = _source(project)
        data, review_identity = _read_file(folder / 'review.json')
        validated = validate_review(decode_review(data), source)
        if _source(project)[1] != identity or _read_file(folder / 'review.json')[1] != review_identity:
            _fail('核验包或分类来源在读取期间发生变化。')
        return validated
    except OSError:
        _fail()
