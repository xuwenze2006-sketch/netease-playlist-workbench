"""A bounded content identity and immutable frontend snapshot for one launch."""

from dataclasses import dataclass
import hashlib
import importlib
from html.parser import HTMLParser
from pathlib import Path
from types import MappingProxyType
from typing import Mapping
from urllib.parse import unquote, urlsplit


MAX_ASSET_BYTES = 16 * 1024 * 1024
MAX_SOURCE_BYTES = 1024 * 1024
MAX_HELPER_BYTES = 8192
MAX_TOTAL_BYTES = 32 * 1024 * 1024
MAX_FILES = 512
_ASSET_SUFFIXES = {'.html', '.js', '.css', '.svg', '.png', '.jpg', '.jpeg',
                   '.webp', '.ico', '.woff', '.woff2'}
_ERRORS = {
    'frontend_missing': '现代工作台文件缺失，请按本地构建说明修复。',
    'build_changed': '程序文件正在变化或页面资源不完整，请完成本地构建后重新打开。',
    'local_permission_denied': '程序无法访问本地文件，请检查项目目录权限。',
    'browser_unavailable': '无法打开工作台，请检查本机浏览器。',
    'instance_busy': '工作台正在启动或退出，请稍后重新打开。',
    'legacy_instance': '旧版本工作台仍在运行，请关闭其窗口后稍后重新打开。',
    'startup_failed': '歌单工作台暂未能打开，请查看本地启动说明。',
}
_RUNTIME_MODULES = (
    'netease_bridge.cache', 'netease_organizer.authorization',
    'netease_organizer.runtime', 'netease_organizer.write_journal', 'netease_organizer.official_cli',
    'netease_organizer.planning', 'netease_organizer.online_planning',
    'netease_organizer.online', 'netease_organizer.rename',
    'netease_organizer.playlist_details',
    'netease_organizer.create', 'netease_organizer.qr',
    'netease_organizer.classification_execution', 'netease_organizer.classification_planning',
    'netease_organizer.lyric_features',
    'netease_organizer.service', 'netease_organizer.web_preview',
    'netease_organizer.web_state', 'netease_organizer.web_tracks', 'netease_organizer.web_server',
)


class LaunchError(RuntimeError):
    def __init__(self, *, code='startup_failed'):
        self.code = code if type(code) is str and code in _ERRORS else 'startup_failed'
        super().__init__(_ERRORS[self.code])


@dataclass(frozen=True)
class BuildSnapshot:
    revision: str
    assets: Mapping[str, bytes]
    helper_content: bytes | None = None


def _files(project):
    assets = project / 'frontend/dist'
    if not (assets / 'index.html').is_file():
        raise LaunchError(code='frontend_missing')
    files = []
    for package in ('netease_organizer', 'netease_bridge'):
        directory = project / package
        if directory.is_dir():
            files.extend(path for path in directory.rglob('*.py')
                         if not any(part.startswith('.') for part in path.relative_to(directory).parts))
    for name in ('run_organizer.py', 'scripts/render-qr.cjs'):
        path = project / name
        if path.is_file():
            files.append(path)
    files.extend(path for path in assets.rglob('*') if path.is_file()
                 and path.suffix.lower() in _ASSET_SUFFIXES
                 and not any(part.startswith('.') for part in path.relative_to(assets).parts))
    files.sort(key=lambda path: path.relative_to(project).as_posix())
    if len(files) > MAX_FILES:
        raise LaunchError(code='build_changed')
    for path in files:
        if path.is_symlink() or not path.resolve().is_relative_to(project):
            raise LaunchError(code='build_changed')
    return files


def _signature(path):
    stat = path.stat()
    return (stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns)


class _EntryResources(HTMLParser):
    def __init__(self):
        super().__init__()
        self.references = []

    def handle_starttag(self, tag, attrs):
        values = dict(attrs)
        if tag == 'base':
            raise LaunchError(code='build_changed')
        reference = values.get('src') if tag == 'script' else None
        if tag == 'link' and set((values.get('rel') or '').lower().split()) & {
                'stylesheet', 'icon', 'modulepreload', 'preload'}:
            reference = values.get('href')
        if reference is not None:
            self.references.append(reference)
            if len(self.references) > MAX_FILES:
                raise LaunchError(code='build_changed')


def _validate_entry(assets):
    try:
        parser = _EntryResources()
        parser.feed(assets['index.html'].decode('utf-8'))
        parser.close()
        for reference in parser.references:
            url = urlsplit(reference)
            path = unquote(url.path, errors='strict')
            if url.scheme or url.netloc or '\\' in path or any(ord(c) < 32 for c in reference):
                raise ValueError()
            path = path.lstrip('/')
            while path.startswith('./'):
                path = path[2:]
            if any(part in ('', '.', '..') for part in path.split('/')) or path not in assets:
                raise ValueError()
    except (ValueError, KeyError, UnicodeError):
        raise LaunchError(code='build_changed') from None


def load_build(project):
    """Read executable project sources and public assets, never user records."""
    try:
        project = Path(project).resolve()
        files = _files(project)
        original = {path: _signature(path) for path in files}
        assets = {}
        helper_content = None
        digest = hashlib.sha256()
        total = 0
        for path in files:
            relative = path.relative_to(project).as_posix()
            is_asset = relative.startswith('frontend/dist/')
            limit = MAX_ASSET_BYTES if is_asset else MAX_SOURCE_BYTES
            if relative == 'scripts/render-qr.cjs':
                limit = MAX_HELPER_BYTES
            if original[path][2] > limit:
                raise LaunchError(code='build_changed')
            with path.open('rb') as stream:
                content = stream.read(limit + 1)
            total += len(content)
            if len(content) > limit or total > MAX_TOTAL_BYTES or _signature(path) != original[path]:
                raise LaunchError(code='build_changed')
            name = relative.encode('utf-8')
            digest.update(len(name).to_bytes(4, 'big'))
            digest.update(name)
            digest.update(len(content).to_bytes(8, 'big'))
            digest.update(content)
            if is_asset:
                assets[relative[len('frontend/dist/'):]] = content
            elif relative == 'scripts/render-qr.cjs':
                helper_content = content
        if _files(project) != files or any(_signature(path) != original[path] for path in files):
            raise LaunchError(code='build_changed')
        _validate_entry(assets)
        return BuildSnapshot(digest.hexdigest(), MappingProxyType(assets), helper_content)
    except PermissionError:
        raise LaunchError(code='local_permission_denied') from None
    except OSError:
        raise LaunchError(code='build_changed') from None


def preload_runtime():
    """Load lazy Python execution paths at birth without contacting any account."""
    for module in _RUNTIME_MODULES:
        importlib.import_module(module)
