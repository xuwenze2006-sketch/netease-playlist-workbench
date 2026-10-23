"""Bounded, read-only workbench views of saved recording review bundles."""

import os
from pathlib import Path

from .classification_generation import generation_incomplete
from . import recording_reviews as reviews


CATALOG_LIMIT = 100
_CATALOG_MESSAGE = '核验目录暂时无法安全读取，请检查本地记录后重试；已有记录未修改。'
_DETAIL_MESSAGE = '核验记录损坏、来源已变化或读取期间被修改，暂时无法核对；请检查本地记录后重试，原文件已保留。'


class RecordingReviewReadError(Exception):
    """A fixed public diagnosis, never a filesystem or parser exception string."""

    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


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
                'created_at': review['created_at'], 'preview': preview}
    except (OSError, ValueError, TypeError):
        raise RecordingReviewReadError('recording_review_unavailable', _DETAIL_MESSAGE) from None
