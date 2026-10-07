"""Run the local adapter directly, or expose its read tools over MCP stdio."""

import argparse
import json
import os
import sys
import tempfile
from pathlib import Path

from .protocol import McpServer
from .service import BridgeService


def _paging(parser):
    parser.add_argument("--offset", type=int, default=0)
    parser.add_argument("--limit", type=int, default=50)


def _parser():
    parser = argparse.ArgumentParser(description="网易云桌面版本地只读接入原型")
    parser.add_argument("--data-dir", type=Path, help="CloudMusic 数据目录，默认使用当前 Windows 用户的数据目录")
    parser.add_argument("--output", type=Path, help="保存本次命令的 JSON 结果；必须在客户端数据目录之外")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("status", help="检查接入能力和缓存状态")
    playlists = commands.add_parser("playlists", help="查询歌单列表")
    playlists.add_argument("--kind", choices=("all", "owned", "collected"), default="all")
    _paging(playlists)
    tracks = commands.add_parser("tracks", help="读取一个缓存歌单的歌曲")
    tracks.add_argument("--playlist-id", required=True)
    _paging(tracks)
    search = commands.add_parser("search", help="搜索指定缓存歌单中的歌曲，默认红心歌单")
    search.add_argument("--query", required=True)
    search.add_argument("--playlist-id")
    _paging(search)
    preview = commands.add_parser("preview-artist", help="生成本地歌手精选草案，不修改账号")
    artist = preview.add_mutually_exclusive_group(required=True)
    artist.add_argument("--artist-id")
    artist.add_argument("--artist-name")
    preview.add_argument("--playlist-id")
    preview.add_argument("--name")
    commands.add_parser("mcp", help="启动本地 MCP stdio 服务，无监听端口")
    return parser


def _write_json(path, text, data_dir):
    target = path.resolve()
    protected = (data_dir.absolute(), data_dir.resolve(), (data_dir / "Library").resolve())
    if any(target.is_relative_to(root) or path.absolute().is_relative_to(root) for root in protected):
        raise ValueError("输出不能位于网易云客户端数据目录中。")
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=target.parent, prefix=".netease-draft-", suffix=".tmp", delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(text + "\n")
        os.replace(temporary, target)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def main(argv=None):
    if hasattr(sys.stdin, "reconfigure"):
        # Preserve malformed input as surrogate code units so the transport
        # can reject its line and recover instead of failing during decoding.
        sys.stdin.reconfigure(encoding="utf-8", errors="surrogateescape")
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    parser = _parser()
    options = parser.parse_args(argv)
    service = BridgeService(options.data_dir)
    if options.command == "mcp":
        if options.output is not None:
            parser.error("MCP 模式不能使用 --output。")
        McpServer(service).serve(sys.stdin, sys.stdout)
        return 0
    tools = {"status": "desktop_status", "playlists": "list_playlists", "tracks": "get_playlist_tracks", "search": "search_tracks", "preview-artist": "preview_artist_playlist"}
    arguments = {key: value for key, value in vars(options).items() if key not in ("data_dir", "output", "command") and value is not None}
    try:
        result = service.call_tool(tools[options.command], arguments)
        data = result["structuredContent"]
        text = json.dumps(data, ensure_ascii=False, indent=2, allow_nan=False)
        if options.output is not None:
            _write_json(options.output, text, service.data_dir)
        print(text)
        return 1 if result.get("isError") else 0
    except ValueError as error:
        print(json.dumps({"error": {"code": "invalid_arguments", "message": str(error)}}, ensure_ascii=False))
        return 2
    except (OSError, UnicodeError):
        print(json.dumps({"error": {"code": "output_unavailable", "message": "本地输出文件或标准流不可写。"}}, ensure_ascii=False))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
