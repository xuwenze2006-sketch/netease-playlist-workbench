"""Bounded workbench views and immutable saves of recording review bundles."""

import copy
import hashlib
import os
from pathlib import Path
import re

from .classification_generation import generation_incomplete
from . import recording_reviews as reviews


CATALOG_LIMIT = 100
_CATALOG_MESSAGE = '核验目录暂时无法安全读取，请检查本地记录后重试；已有记录未修改。'
_DETAIL_MESSAGE = '核验记录损坏、来源已变化或读取期间被修改，暂时无法核对；请检查本地记录后重试，原文件已保留。'
_SAVE_UNKNOWN = '本次核验保存结果尚未确认；请保留当前内容并使用同一请求核对重试，勿另建保存目标。'


class RecordingReviewReadError(Exception):
    """A fixed public diagnosis, never a filesystem or parser exception string."""

    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


class RecordingReviewSaveError(Exception):
    """A public, phase-aware save failure with no private exception details."""

    def __init__(self, status, code, message):
        super().__init__(message)
        self.status, self.code = status, code


def _save_unknown():
    return RecordingReviewSaveError(503, 'recording_review_save_unconfirmed', _SAVE_UNKNOWN)


def validate_bundle_name(name):
    """Validate only a name, using the same allowlist as the immutable writer."""
    try:
        reviews._bundle(Path('.'), name)
    except reviews.ReviewError:
        raise ValueError('核验包名称无效。') from None


def _catalog_snapshot(project):
    ancestors = reviews._directories(project)
    signatures = []
    for folder in (project / 'artifacts', project / 'artifacts/recording-reviews'):
        try:
            info = folder.lstat()
        except FileNotFoundError:
            signatures.append(None)
            break
        reviews._ordinary(info, directory=True)
        signatures.append(reviews._signature(info))
    return ancestors, tuple(signatures)


def list_recording_reviews(project):
    """List names only. Unexpected entries and oversized catalogs are visible failures."""
    try:
        project = Path(project).absolute()
        folder = project / 'artifacts/recording-reviews'
        before = _catalog_snapshot(project)
        names, entries = [], []
        if before[1][-1] is not None:
            with os.scandir(folder) as iterator:
                for entry in iterator:
                    if len(names) >= CATALOG_LIMIT:
                        raise RecordingReviewReadError(
                            'recording_review_catalog_limit',
                            '核验目录超过 100 项，无法完整列出；请先整理本地核验目录后重试，已有记录未修改。')
                    validate_bundle_name(entry.name)
                    # Windows DirEntry.stat reports zero device/inode; use lstat on both reads.
                    info = Path(entry.path).lstat()
                    reviews._ordinary(info, directory=True)
                    names.append(entry.name)
                    entries.append((Path(entry.path), reviews._signature(info)))
        if (_catalog_snapshot(project) != before
                or any(reviews._signature(path.lstat()) != signature for path, signature in entries)):
            raise reviews.ReviewError()
        names.sort(key=lambda name: (name.casefold(), name))
        return {'status': 'available' if names else 'empty',
                'bundles': [{'name': name} for name in names], 'limit': CATALOG_LIMIT, 'truncated': False}
    except RecordingReviewReadError:
        raise
    except (OSError, ValueError, TypeError):
        raise RecordingReviewReadError('recording_review_catalog_unavailable', _CATALOG_MESSAGE) from None


def read_recording_review(project, bundle_name):
    """Build one source-bound preview and recheck both read identities before returning."""
    validate_bundle_name(bundle_name)
    try:
        project = Path(project).absolute()
        if generation_incomplete(project):
            raise reviews.ReviewError()
        source, source_identity = reviews._source(project)
        target = reviews._bundle(project, bundle_name) / 'review.json'
        data, review_identity = reviews._read_file(target)
        review = reviews.decode_review(data)
        preview = reviews.build_preview(review, source)
        if (reviews._source(project)[1] != source_identity
                or reviews._read_file(target)[1] != review_identity or generation_incomplete(project)):
            raise reviews.ReviewError()
        return {'status': 'available', 'bundle': bundle_name,
                'created_at': review['created_at'], 'review_sha256': hashlib.sha256(data).hexdigest(),
                'preview': preview}
    except (OSError, ValueError, TypeError):
        raise RecordingReviewReadError('recording_review_unavailable', _DETAIL_MESSAGE) from None


def _save_request(raw):
    try:
        reviews._object(raw, 'base_bundle base_sha256 target_bundle created_at position recording scene_decisions')
        validate_bundle_name(raw['base_bundle'])
        if (type(raw['target_bundle']) is not str
                or re.fullmatch(r'review-[a-f0-9]{32}', raw['target_bundle']) is None
                or raw['target_bundle'] == raw['base_bundle']
                or type(raw['base_sha256']) is not str
                or re.fullmatch(r'[a-f0-9]{64}', raw['base_sha256']) is None
                or type(raw['position']) is not int or raw['position'] < 1):
            raise ValueError()
        return copy.deepcopy(raw)
    except (ValueError, TypeError, RecursionError):
        raise RecordingReviewSaveError(400, 'recording_review_invalid_request',
                                       '核验保存参数无效，请检查歌曲、录音证据和场景结论。') from None


def _check_binding(project, base, source_identity, base_identity, *, writing=False):
    try:
        if (generation_incomplete(project) or reviews._source(project)[1] != source_identity
                or reviews._read_file(base)[1] != base_identity):
            raise reviews.ReviewError()
    except (OSError, ValueError, TypeError):
        if writing:
            raise _save_unknown() from None
        raise RecordingReviewSaveError(409, 'recording_review_source_conflict',
                                       '来源或原核验包已变化，请重新读取并核对；本次另存未执行。') from None


def _read_saved_target(project, name):
    """Retain the complete reader's validation and bind the result to file identity."""
    target = reviews._bundle(project, name) / 'review.json'
    data, identity = reviews._read_file(target)
    saved = reviews.read_review(project, name)
    if reviews.decode_review(data) != saved or reviews._read_file(target)[1] != identity:
        raise reviews.ReviewError()
    return saved, identity


def _confirmed_save(project, raw, expected, preview, source_identity, base_identity, *, replayed):
    """Confirm all records and the final projection, not only the selected song."""
    try:
        base = reviews._bundle(project, raw['base_bundle']) / 'review.json'
        _check_binding(project, base, source_identity, base_identity, writing=True)
        saved, identity = _read_saved_target(project, raw['target_bundle'])
        if saved != expected:
            raise reviews.ReviewError()
        detail = read_recording_review(project, raw['target_bundle'])
        if (detail['created_at'] != expected['created_at'] or detail['preview'] != preview
                or detail['review_sha256'] != identity[1].hex()
                or reviews._read_file(reviews._bundle(project, raw['target_bundle']) / 'review.json')[1] != identity):
            raise reviews.ReviewError()
        _check_binding(project, base, source_identity, base_identity, writing=True)
        return {'status': 'saved', 'bundle': raw['target_bundle'], 'replayed': replayed, 'detail': detail}
    except Exception:
        raise _save_unknown() from None


def save_recording_review(project, raw, *, before_commit=None):
    """Save one edited song to one fixed target; failed attempts are never replaced.

    The HTTP application serializes this operation with its records gate. A
    callback rechecks its session immediately before invoking the immutable
    writer, while all filesystem work remains outside the application's lock.
    """
    raw = _save_request(raw)
    try:
        project = Path(project).absolute()
        if generation_incomplete(project):
            raise reviews.ReviewError()
        source, source_identity = reviews._source(project)
        base = reviews._bundle(project, raw['base_bundle']) / 'review.json'
        data, base_identity = reviews._read_file(base)
        if hashlib.sha256(data).hexdigest() != raw['base_sha256']:
            raise reviews.ReviewError()
        candidate = reviews.read_review(project, raw['base_bundle'])
        if reviews.decode_review(data) != candidate:
            raise reviews.ReviewError()
        _check_binding(project, base, source_identity, base_identity)
    except (OSError, ValueError, TypeError):
        raise RecordingReviewSaveError(409, 'recording_review_source_conflict',
                                       '来源或原核验包已变化，请重新读取并核对；本次另存未执行。') from None

    try:
        row = next((row for row in candidate['records'] if row['position'] == raw['position']), None)
        if row is None:
            raise reviews.ReviewError()
        row['recording'], row['scene_decisions'] = raw['recording'], raw['scene_decisions']
        candidate['created_at'] = raw['created_at']
        preview = reviews.build_preview(candidate, source)
    except (ValueError, TypeError, RecursionError):
        raise RecordingReviewSaveError(400, 'recording_review_invalid_request',
                                       '核验保存参数无效，请检查歌曲、录音证据和场景结论。') from None
    _check_binding(project, base, source_identity, base_identity)

    # A previous write must remain recoverable even at capacity or if unrelated
    # catalog entries have become unreadable. Never scan first in this branch.
    folder = reviews._bundle(project, raw['target_bundle'])
    try:
        folder.lstat()
    except FileNotFoundError:
        exists = False
    except (OSError, ValueError):
        raise _save_unknown() from None
    else:
        exists = True
    if exists:
        try:
            saved, _ = _read_saved_target(project, raw['target_bundle'])
        except Exception:
            raise _save_unknown() from None
        _check_binding(project, base, source_identity, base_identity)
        if saved != candidate:
            raise RecordingReviewSaveError(409, 'recording_review_target_conflict',
                                           '保存目标已包含不同核验内容；已有包未覆盖，请核对保存记录。')
        return _confirmed_save(project, raw, candidate, preview, source_identity, base_identity, replayed=True)

    try:
        catalog = list_recording_reviews(project)
    except RecordingReviewReadError as error:
        if error.code == 'recording_review_catalog_limit':
            raise RecordingReviewSaveError(409, error.code, '核验目录已达到 100 包上限，本次另存未执行。') from None
        raise _save_unknown() from None
    if len(catalog['bundles']) >= CATALOG_LIMIT:
        raise RecordingReviewSaveError(409, 'recording_review_catalog_limit',
                                       '核验目录已达到 100 包上限，本次另存未执行。')
    _check_binding(project, base, source_identity, base_identity)
    if before_commit is not None:
        before_commit()
    _check_binding(project, base, source_identity, base_identity)
    if before_commit is not None:
        before_commit()
    # After this point even an exception may follow a complete successful write.
    # Only a full target readback may turn the result into confirmed success.
    try:
        reviews.save_review(project, candidate, raw['target_bundle'])
    except Exception:
        pass
    return _confirmed_save(project, raw, candidate, preview, source_identity, base_identity, replayed=False)
