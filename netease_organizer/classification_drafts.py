"""Version-bound local classification corrections, separate from source reports.

The process-wide lock serializes threads; an OS file lock protects writes across
independent workbench processes. The lock file remains in place after release.
This file is a review draft; it never authorizes or performs an account mutation.
"""

import copy
import errno
import hashlib
import json
import math
import os
import re
import stat
import tempfile
import threading
from contextlib import contextmanager
from pathlib import Path

from .classification_planning import LANGUAGES, SCENES, STYLES


MAX_DRAFT_BYTES = 16 * 1024 * 1024
MAX_RECORDS = 10000
MAX_REVISION = 2**53 - 1
DRAFT_FILE = 'classification-draft.json'
LOCK_FILE = 'classification-draft.lock'
_KIND = 'classification_correction_draft'
_LOCK = threading.Lock()
_HEX32 = re.compile(r'[0-9a-fA-F]{32}\Z')
_HEX64 = re.compile(r'[0-9a-fA-F]{64}\Z')
_LABEL_FIELDS = {'styles', 'scenes', 'language'}
_EDIT_FIELDS = {'before', 'after', 'reason', 'recording_note'}
_ROOT_FIELDS = {'kind', 'version', 'source_version', 'revision', 'edits'}
_STYLE_LABELS = frozenset((*STYLES.values(), '待辨识'))
_SCENE_LABELS = frozenset(SCENES.values())
_LANGUAGE_LABELS = frozenset((*LANGUAGES.values(), '器乐或配乐录音', '待辨识'))
_REPARSE = getattr(stat, 'FILE_ATTRIBUTE_REPARSE_POINT', 0x400)


class DraftError(ValueError):
    """Invalid draft/request or an unsafe/unavailable local storage path."""

    def __init__(self, message, *, code='invalid_request'):
        super().__init__(message)
        self.code = code


class DraftConflict(DraftError):
    """The source identity or revision no longer matches the submitted draft."""


def _fail():
    raise DraftError('分类修正草稿格式不兼容或本地文件不可用。')


def _hash(value, expression):
    if type(value) is not str or expression.fullmatch(value) is None:
        _fail()
    return value


def _revision(value):
    if type(value) is not int or not 0 <= value <= MAX_REVISION:
        _fail()
    return value


def _text(value, *, empty=False):
    """Validate evidence text, preserving LF while rejecting other controls."""
    if (type(value) is not str or len(value) > 1000 or not empty and not value.strip()
            or any((ord(char) < 32 and char != '\n')
                   or 127 <= ord(char) <= 159 or 0xD800 <= ord(char) <= 0xDFFF
                   for char in value)):
        _fail()
    return value


def _tags(value, labels, minimum, maximum):
    if (type(value) is not list or not minimum <= len(value) <= maximum
            or any(type(label) is not str or label not in labels for label in value)
            or len(set(value)) != len(value)):
        _fail()
    return list(value)


def _labels(value, *, exact=True):
    if type(value) is not dict or not _LABEL_FIELDS <= value.keys() or exact and set(value) != _LABEL_FIELDS:
        _fail()
    styles = _tags(value['styles'], _STYLE_LABELS, 1, 2)
    if '待辨识' in styles and styles != ['待辨识']:
        _fail()
    scenes = _tags(value['scenes'], _SCENE_LABELS, 0, 3)
    language = value['language']
    if type(language) is not str or language not in _LANGUAGE_LABELS:
        _fail()
    return {'styles': styles, 'scenes': scenes, 'language': language}


def _records(records):
    if type(records) is not list or len(records) > MAX_RECORDS:
        _fail()
    result = {}
    for row in records:
        if type(row) is not dict:
            _fail()
        key = _hash(row.get('record_key'), _HEX32)
        if key in result:
            _fail()
        result[key] = _labels(row, exact=False)
    return result


def _unique(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            _fail()
        value[key] = item
    return value


def _constant(_):
    _fail()


def _finite_float(value):
    parsed = float(value)
    if not math.isfinite(parsed):
        _fail()
    return parsed


def _signature(info):
    return (info.st_dev, info.st_ino, info.st_mode, info.st_size,
            info.st_mtime_ns, info.st_ctime_ns, getattr(info, 'st_file_attributes', 0))


def _opened_signature(info):
    # Windows Python 3.12 fstat reports ctime differently from path lstat.
    # Identity, mode, size, mtime and reparse attributes remain comparable.
    signature = _signature(info)
    return signature[:5], signature[6]


def _ordinary(info, *, directory):
    if (stat.S_ISLNK(info.st_mode) or getattr(info, 'st_file_attributes', 0) & _REPARSE
            or not (stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode))):
        _fail()


def _paths(project, *, create=False):
    project = Path(project).absolute()
    # Check the unresolved spelling first: resolving a junction would hide it.
    for ancestor in (project, *project.parents):
        _ordinary(ancestor.lstat(), directory=True)
    resolved = project.resolve(strict=True)
    folder = project / '.organizer'
    try:
        info = folder.lstat()
    except FileNotFoundError:
        if not create:
            return resolved, folder, folder / DRAFT_FILE, False
        try:
            folder.mkdir()
        except FileExistsError:
            pass
        info = folder.lstat()
    _ordinary(info, directory=True)
    if not folder.resolve(strict=True).is_relative_to(resolved):
        _fail()
    return resolved, folder, folder / DRAFT_FILE, True


def _lock_file_info(path):
    info = path.lstat()
    _ordinary(info, directory=False)
    if info.st_size not in (0, 1):
        _fail()
    return info


@contextmanager
def _process_lock(project):
    _, folder, _, _ = _paths(project, create=True)
    target = folder / LOCK_FILE
    try:
        _lock_file_info(target)
    except FileNotFoundError:
        pass
    flags = os.O_RDWR | getattr(os, 'O_BINARY', 0) | getattr(os, 'O_NOFOLLOW', 0)
    created = False
    descriptor = None
    locked = False
    try:
        try:
            descriptor = os.open(target, flags | os.O_CREAT | os.O_EXCL, 0o600)
            created = True
        except FileExistsError:
            descriptor = os.open(target, flags)
        opened = os.fstat(descriptor)
        _ordinary(opened, directory=False)
        if _opened_signature(opened) != _opened_signature(_lock_file_info(target)):
            _fail()
        if created:
            os.write(descriptor, b'\0')
        os.lseek(descriptor, 0, os.SEEK_SET)
        try:
            if os.name == 'nt':
                import msvcrt
                msvcrt.locking(descriptor, msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            locked = True
        except OSError as error:
            if (error.errno in (errno.EACCES, errno.EAGAIN, errno.EWOULDBLOCK)
                    or getattr(error, 'winerror', None) == 33):
                raise DraftConflict('另一窗口正在保存分类修正，请重新载入后重试。') from None
            raise
        _paths(project)
        if _opened_signature(os.fstat(descriptor)) != _opened_signature(_lock_file_info(target)):
            _fail()
        yield
    finally:
        if descriptor is not None:
            try:
                if locked:
                    os.lseek(descriptor, 0, os.SEEK_SET)
                    if os.name == 'nt':
                        import msvcrt
                        msvcrt.locking(descriptor, msvcrt.LK_UNLCK, 1)
                    else:
                        import fcntl
                        fcntl.flock(descriptor, fcntl.LOCK_UN)
            finally:
                os.close(descriptor)


def _empty(source_version):
    return {'kind': _KIND, 'version': 1, 'source_version': source_version, 'revision': 0, 'edits': {}}


def _validate(raw, source_version, records):
    if (type(raw) is not dict or set(raw) != _ROOT_FIELDS or raw.get('kind') != _KIND
            or type(raw.get('version')) is not int or raw['version'] != 1):
        _fail()
    _hash(raw['source_version'], _HEX64)
    _revision(raw['revision'])
    edits = raw['edits']
    if type(edits) is not dict or len(edits) > MAX_RECORDS or raw['revision'] == 0 and edits:
        _fail()
    same_source = raw['source_version'] == source_version
    for key, edit in edits.items():
        _hash(key, _HEX32)
        if type(edit) is not dict or set(edit) != _EDIT_FIELDS:
            _fail()
        before = _labels(edit['before'])
        _labels(edit['after'])
        _text(edit['reason'])
        _text(edit['recording_note'], empty=True)
        # A stale file still receives full structural validation, but its old
        # recording keys/labels need not exist in the current source report.
        if same_source and (key not in records or before != records[key]):
            _fail()
    return raw


def _content(target):
    before = target.lstat()
    _ordinary(before, directory=False)
    if not 0 < before.st_size <= MAX_DRAFT_BYTES:
        _fail()
    with target.open('rb') as stream:
        opened = os.fstat(stream.fileno())
        _ordinary(opened, directory=False)
        if _opened_signature(opened) != _opened_signature(before):
            _fail()
        content = stream.read(MAX_DRAFT_BYTES + 1)
    if (len(content) > MAX_DRAFT_BYTES or len(content) != before.st_size
            or _signature(target.lstat()) != _signature(before)):
        _fail()
    return content, (_signature(before), hashlib.sha256(content).digest())


def _read(project, source_version, records):
    _, folder, target, exists = _paths(project)
    if not exists:
        return _empty(source_version), None
    try:
        _lock_file_info(folder / LOCK_FILE)
    except FileNotFoundError:
        pass
    try:
        content, identity = _content(target)
    except FileNotFoundError:
        return _empty(source_version), None
    _paths(project)
    raw = json.loads(content.decode('utf-8'), object_pairs_hook=_unique,
                     parse_constant=_constant, parse_float=_finite_float)
    return _validate(raw, source_version, records), identity


def _view(raw, source_version):
    stale = raw['source_version'] != source_version
    return {'status': 'stale' if stale else 'ready', 'revision': raw['revision'],
            'changed_count': len(raw['edits']), 'edits': {} if stale else copy.deepcopy(raw['edits'])}


def _unavailable():
    return {'status': 'unavailable', 'revision': 0, 'changed_count': 0, 'edits': {}}


def _request(request, source_version, records):
    common = {'action', 'source_version', 'revision', 'record_key'}
    if type(request) is not dict or request.get('action') not in ('save', 'remove'):
        _fail()
    fields = common | (_LABEL_FIELDS | {'reason', 'recording_note'} if request['action'] == 'save' else set())
    if set(request) != fields:
        _fail()
    _hash(request['source_version'], _HEX64)
    _revision(request['revision'])
    key = _hash(request['record_key'], _HEX32)
    if key not in records:
        _fail()
    edit = None
    if request['action'] == 'save':
        edit = {'before': copy.deepcopy(records[key]), 'after': _labels(request, exact=False),
                'reason': _text(request['reason']), 'recording_note': _text(request['recording_note'], empty=True)}
    if request['source_version'] != source_version:
        raise DraftConflict('分类来源已更新，请重新载入后修正。')
    return edit


def _write(project, raw, signature):
    content = (json.dumps(raw, ensure_ascii=False, allow_nan=False, separators=(',', ':')) + '\n').encode('utf-8')
    if len(content) > MAX_DRAFT_BYTES:
        _fail()
    _, folder, target, _ = _paths(project, create=True)
    temporary = None
    descriptor = None
    try:
        descriptor, name = tempfile.mkstemp(prefix='.classification-draft-', suffix='.tmp', dir=folder)
        temporary = Path(name)
        with os.fdopen(descriptor, 'wb') as stream:
            descriptor = None
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        _paths(project)
        try:
            _, current = _content(target)
        except FileNotFoundError:
            current = None
        _paths(project)
        if current != signature:
            raise DraftConflict('分类修正草稿在保存前已更新，请重新载入。')
        _ordinary(temporary.lstat(), directory=False)
        os.replace(temporary, target)
        temporary = None
    finally:
        if descriptor is not None:
            os.close(descriptor)
        if temporary is not None:
            try:
                temporary.unlink()
            except OSError:
                pass


def load_draft(project, source_version, records):
    """Return ready/stale/unavailable without creating local folders.

    Invalid API arguments raise DraftError. Invalid files and unsafe paths return
    unavailable, so the caller can continue displaying the original report.
    Stale files retain their revision/count but expose no applicable edits.
    """
    _hash(source_version, _HEX64)
    baseline = _records(records)
    with _LOCK:
        try:
            raw, _ = _read(project, source_version, baseline)
            return _view(raw, source_version)
        except (DraftError, OSError, ValueError, TypeError, RecursionError):
            return _unavailable()


def mutate_draft(project, source_version, records, request):
    """Persist one correction/removal with a source/revision compare-and-swap.

    Invalid files are never reset or replaced. Removing an edit retains the
    draft file and advances its revision, including after the final removal.
    """
    _hash(source_version, _HEX64)
    baseline = _records(records)
    edit = _request(request, source_version, baseline)
    with _LOCK:
        try:
            with _process_lock(project):
                raw, signature = _read(project, source_version, baseline)
                if raw['source_version'] != source_version or raw['revision'] != request['revision']:
                    raise DraftConflict('分类来源或草稿修订号已更新，请重新载入后修正。')
                if raw['revision'] == MAX_REVISION:
                    _fail()
                updated = copy.deepcopy(raw)
                if edit is None:
                    updated['edits'].pop(request['record_key'], None)
                else:
                    updated['edits'][request['record_key']] = edit
                updated['revision'] += 1
                _write(project, updated, signature)
                return _view(updated, source_version)
        except DraftConflict:
            raise
        except (DraftError, OSError, ValueError, TypeError, RecursionError):
            raise DraftError('分类修正草稿保存失败，请检查本地文件后重试。', code='unavailable') from None
