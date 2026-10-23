"""Bind classification generation to preserved source bytes and track identity.

Text identity detects source drift; it is not an audio or recording fingerprint.
This module deliberately leaves label and review-adoption policy to the builder.
"""

import hashlib
import json
from pathlib import Path
import re


_ALLOWED_NAMES = frozenset({
    '全库分类-账号基线.json', '网易云本地歌单快照.json', '全库分类-歌词特征.json',
    *(f'分类判断-分块{i:02}.json' for i in range(1, 4)),
    *(f'分类输入-分块{i:02}.jsonl' for i in range(1, 4)),
    *(f'分类语言核验{i:02}.json' for i in range(1, 4)),
})
_DIGEST = re.compile(r'[0-9a-fA-F]{64}\Z')


def _identity(track, historical):
    if type(track) is not dict or type(track.get('metadata_available')) is not bool:
        raise ValueError('分类歌曲资料格式不兼容。')
    old = historical.get(str(track.get('original_id')), {})
    if type(old) is not dict:
        raise ValueError('历史歌曲资料格式不兼容。')
    fallback = old if not track['metadata_available'] else {}
    title = fallback.get('name') or track.get('name')
    artists = fallback.get('artists') or track.get('artists')
    if (type(title) is not str or type(artists) is not list
            or any(type(artist) is not dict or type(artist.get('name')) is not str
                   for artist in artists)):
        raise ValueError('分类歌曲身份格式不兼容。')
    return title, list(dict.fromkeys(artist['name'] for artist in artists)), old


class GenerationInputs:
    """Read each allowed source once, then verify the consumed snapshot."""

    def __init__(self, folder):
        self.folder = Path(folder)
        self._raw = {}

    def _read(self, name, optional=False):
        if type(name) is not str or name not in _ALLOWED_NAMES:
            raise ValueError('分类资料文件名不兼容。')
        if name not in self._raw:
            try:
                raw = (self.folder / name).read_bytes()
            except FileNotFoundError:
                if not optional:
                    raise ValueError('缺少原始分类资料。') from None
                raw = None
            except OSError:
                raise ValueError('分类资料无法读取。') from None
            self._raw[name] = raw
        raw = self._raw[name]
        if raw is None and not optional:
            raise ValueError('缺少原始分类资料。')
        return raw

    def json(self, name, optional=False):
        raw = self._read(name, optional)
        if raw is None:
            return None
        try:
            data = json.loads(raw)
        except (ValueError, UnicodeError):
            raise ValueError('分类资料格式不兼容。') from None
        if data is None:
            raise ValueError('分类资料不能为空值。')
        return data

    def validate_tracks(self, tracks, historical):
        if type(tracks) is not list or type(historical) is not dict:
            raise ValueError('分类歌曲资料格式不兼容。')
        rows = []
        for index in range(1, 4):
            raw = self._read(f'分类输入-分块{index:02}.jsonl')
            try:
                rows.extend(json.loads(line) for line in raw.splitlines() if line.strip())
            except (ValueError, UnicodeError):
                raise ValueError('原始分类输入格式不兼容。') from None
        if len(rows) != len(tracks):
            raise ValueError('原始分类输入未完整覆盖歌曲。')
        for expected, (row, track) in enumerate(zip(rows, tracks), 1):
            if (type(row) is not list or len(row) != 6
                    or type(row[0]) is not int or row[0] != expected
                    or type(row[1]) is not str or type(row[2]) is not list
                    or any(type(name) is not str for name in row[2])
                    or type(row[3]) is not str
                    or row[4] is not None and (type(row[4]) is not int or row[4] < 0)
                    or type(row[5]) is not bool):
                raise ValueError('原始分类输入位置或字段不兼容。')
            title, artists, old = _identity(track, historical)
            if row[1] != title or row[2] != artists or row[5] != track['metadata_available']:
                raise ValueError('原始分类输入与当前歌曲身份不一致。')
            if 'album' in old:
                album = old['album']
                if type(album) is dict:
                    album = album.get('name', '')
                if type(album) is not str or row[3] != album:
                    raise ValueError('原始分类输入与历史专辑不一致。')
            if 'duration_ms' in old:
                duration = old['duration_ms']
                if (duration is not None and (type(duration) is not int or duration < 0)
                        or row[4] != duration):
                    raise ValueError('原始分类输入与历史时长不一致。')

    def validate_review(self, data, tracks, historical):
        if type(tracks) is not list or type(historical) is not dict:
            raise ValueError('分类歌曲资料格式不兼容。')
        start, end = 1, len(tracks)
        if type(data) is dict:
            if 'sources' in data:
                sources = data['sources']
                if type(sources) is not dict or not sources:
                    raise ValueError('语言复核来源声明不兼容。')
                for name, digest in sources.items():
                    if (type(name) is not str or name not in _ALLOWED_NAMES
                            or type(digest) is not str or not _DIGEST.fullmatch(digest)):
                        raise ValueError('语言复核来源声明不兼容。')
                    if hashlib.sha256(self._read(name)).hexdigest() != digest.lower():
                        raise ValueError('语言复核来源与原始资料不一致。')
            if 'scope' in data:
                scope = data['scope']
                if (type(scope) is not dict
                        or any(type(scope.get(key)) is not int for key in ('start', 'end', 'count'))):
                    raise ValueError('语言复核范围声明不兼容。')
                start, end = scope['start'], scope['end']
                if not 1 <= start <= end <= len(tracks) or scope['count'] != end - start + 1:
                    raise ValueError('语言复核范围声明不兼容。')
            sections = [data.get('recommendations', []), data.get('conflicts', [])]
        elif type(data) is list:
            sections = [data]
        else:
            raise ValueError('语言复核身份资料格式不兼容。')
        for entries in sections:
            if type(entries) is not list:
                raise ValueError('语言复核身份资料格式不兼容。')
            for entry in entries:
                if (type(entry) is not dict or type(entry.get('position')) is not int
                        or not start <= entry['position'] <= end):
                    raise ValueError('语言复核位置与声明范围不兼容。')
                if 'name' in entry:
                    name = entry['name']
                    title, _, _ = _identity(tracks[entry['position'] - 1], historical)
                    if type(name) is not str or not name.strip() or name != title:
                        raise ValueError('语言复核名称与当前歌曲不一致。')

    def verify_unchanged(self):
        for name, original in self._raw.items():
            try:
                current = (self.folder / name).read_bytes()
            except FileNotFoundError:
                current = None
            except OSError:
                raise ValueError('分类资料无法重新核对。') from None
            if current != original:
                raise ValueError('分类资料在生成期间发生变化。')
