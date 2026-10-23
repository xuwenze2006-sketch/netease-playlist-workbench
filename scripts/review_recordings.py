"""Create, save, read and preview immutable local recording-review bundles."""

import argparse
import json
import os
import stat
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from netease_organizer.recording_reviews import (
    MAX_REVIEW_BYTES, build_preview, create_review, decode_review,
    load_source, read_review, save_review,
)
from netease_organizer.recording_review_preview import render_preview


def _directory(path):
    identities = []
    for item in (path, *path.parents):
        info = item.lstat()
        if (not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode)
                or getattr(info, 'st_file_attributes', 0) & 0x400):
            raise ValueError('预览目录必须是普通本地目录。')
        identities.append((str(item), info.st_dev, info.st_ino, info.st_mode,
                           getattr(info, 'st_file_attributes', 0)))
    return tuple(identities)


def _input(path):
    before = path.lstat()
    if (not stat.S_ISREG(before.st_mode) or stat.S_ISLNK(before.st_mode)
            or getattr(before, 'st_file_attributes', 0) & 0x400
            or not 0 < before.st_size <= MAX_REVIEW_BYTES):
        raise ValueError('核验输入不是兼容的普通文件。')
    with path.open('rb') as stream:
        opened = os.fstat(stream.fileno())
        if (opened.st_dev, opened.st_ino, opened.st_size, opened.st_mtime_ns) != (
                before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns):
            raise ValueError('核验输入读取期间发生变化。')
        data = stream.read(MAX_REVIEW_BYTES + 1)
    after = path.lstat()
    if (len(data) != before.st_size or (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
            != (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)):
        raise ValueError('核验输入读取期间发生变化。')
    return decode_review(data)


def _publish_preview(project, bundle, review, source):
    # read_review already validates bundle spelling, source and stored schema.
    preview = build_preview(review, source)
    parent = project / 'artifacts/recording-previews'
    _directory(parent.parent)
    parent.mkdir(exist_ok=True)
    _directory(parent)
    target = parent / bundle
    if load_source(project) != source:
        raise ValueError('分类来源已改变，未生成预览。')
    target.mkdir()  # Exclusive: retain any previous or incomplete export.
    identity = _directory(target)
    payloads = (
        ('preview.json', json.dumps(preview, ensure_ascii=False, indent=2, allow_nan=False) + '\n'),
        ('index.html', render_preview(preview)),
    )
    files = []
    for name, text in payloads:
        if _directory(target) != identity:
            raise ValueError('预览目录在导出期间发生变化。')
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, 'O_BINARY', 0) | getattr(os, 'O_NOFOLLOW', 0)
        with os.fdopen(os.open(target / name, flags, 0o600), 'wb') as stream:
            if _directory(target) != identity:
                raise ValueError('预览目录在导出期间发生变化。')
            stream.write(text.encode('utf-8'))
            stream.flush()
            os.fsync(stream.fileno())
            info = os.fstat(stream.fileno())
            files.append((name, text.encode('utf-8'), info.st_dev, info.st_ino))
    if load_source(project) != source:
        raise ValueError('分类来源在导出期间改变；本次预览不再适用，请保留记录并重新核验。')
    for name, expected, device, inode in files:
        if _directory(target) != identity:
            raise ValueError('预览目录在导出期间发生变化。')
        path = target / name
        before = path.lstat()
        if (not stat.S_ISREG(before.st_mode) or stat.S_ISLNK(before.st_mode)
                or getattr(before, 'st_file_attributes', 0) & 0x400
                or (before.st_dev, before.st_ino, before.st_size) != (device, inode, len(expected))):
            raise ValueError('预览输出读回校验失败。')
        flags = os.O_RDONLY | getattr(os, 'O_BINARY', 0) | getattr(os, 'O_NOFOLLOW', 0)
        with os.fdopen(os.open(path, flags), 'rb') as stream:
            opened = os.fstat(stream.fileno())
            if ((opened.st_dev, opened.st_ino) != (device, inode)
                    or stream.read(len(expected) + 1) != expected):
                raise ValueError('预览输出读回校验失败。')
    if _directory(target) != identity or load_source(project) != source:
        raise ValueError('预览来源或目录在读回期间改变。')
    return {'preview_directory': str(target), 'summary': preview['summary']}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--project', type=Path, default=ROOT)
    commands = parser.add_subparsers(dest='command', required=True)
    for name in ('init', 'save', 'read', 'preview'):
        command = commands.add_parser(name)
        command.add_argument('--bundle', required=True, help='A new safe local bundle name for init/save')
        if name == 'init':
            command.add_argument('--positions', required=True, help='Comma-separated original report positions (1–50 songs)')
        if name == 'save':
            command.add_argument('--input', required=True, type=Path)
    args = parser.parse_args(argv)
    project = args.project.absolute()
    try:
        if args.command == 'init':
            positions = [int(value.strip()) for value in args.positions.split(',')]
            review = create_review(load_source(project), positions)
            path = save_review(project, review, args.bundle)
            result = {'saved': str(path), 'records': len(review['records']), 'audio_verified': False}
        elif args.command == 'save':
            review = _input(args.input)
            path = save_review(project, review, args.bundle)
            reloaded = read_review(project, args.bundle)
            if reloaded != review:
                raise ValueError('保存后的核验记录与提交内容不一致。')
            result = {'saved': str(path), 'records': len(review['records']), 'readback_verified': True}
        else:
            review = read_review(project, args.bundle)
            source = load_source(project)
            result = (_publish_preview(project, args.bundle, review, source)
                      if args.command == 'preview' else build_preview(review, source))
        print(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False))
        return 0
    except (ValueError, OSError, TypeError, KeyError, RecursionError):
        print('核验操作未完成：请检查来源是否变化、输入格式、普通本地路径以及输出是否已存在。已有文件不会被覆盖。', file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
