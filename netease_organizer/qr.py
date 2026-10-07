"""Generate authorization QR PNGs in memory using the project's local vendor."""

import base64
import json
import os
import struct
import subprocess
import threading
import zlib
from pathlib import Path
from .authorization import safe_authorization_url


class QRError(RuntimeError):
    """A public explanation that contains no URL, process output or credential."""


_PROVIDER_PREFIXES = (
    "NETEASE_", "NCM_", "LANGBASE_", "OPENAI_", "DEEPSEEK_",
    "SILICONFLOW_", "ANTHROPIC_", "GEMINI_", "GOOGLE_API_",
)

_PINNED_HELPERS = {}
_PIN_LOCK = threading.Lock()
_MAX_HELPER_BYTES = 8192
_PINNED_WRAPPER = """'use strict';
try {
  const Module = require('node:module');
  const path = require('node:path');
  const filename = process.argv[1];
  const source = Buffer.from(process.argv[2], 'base64').toString('utf8');
  const helper = new Module(filename, module);
  helper.filename = filename;
  helper.paths = Module._nodeModulePaths(path.dirname(filename));
  helper._compile(source, filename);
} catch (_) {
  process.stderr.write('Local authorization QR generation failed.\\n');
  process.exit(1);
}
"""


def pin_authorization_helper(project: Path, content: bytes) -> None:
    """Keep one immutable birth helper per project, without writing any files."""
    try:
        if not isinstance(content, bytes) or not 1 <= len(content) <= _MAX_HELPER_BYTES:
            raise ValueError
        content.decode("utf-8")
        project = Path(project).resolve()
    except (OSError, ValueError, TypeError, UnicodeError):
        raise QRError("本地二维码脚本不可用，请使用授权链接。") from None
    with _PIN_LOCK:
        previous = _PINNED_HELPERS.get(project)
        if previous is not None and previous != content:
            raise QRError("本地二维码脚本版本已固定，请重新打开程序。") from None
        _PINNED_HELPERS[project] = bytes(content)


def _validate_url(url):
    if not safe_authorization_url(url):
        raise QRError("授权链接格式不可用，请使用网易云官方 HTTPS 授权链接。") from None


def _validate_matrix(matrix):
    if not isinstance(matrix, list):
        raise QRError("本地二维码内容不可用，请使用授权链接。")
    size = len(matrix)
    if size < 21 or size > 177 or (size - 21) % 4:
        raise QRError("本地二维码内容不可用，请使用授权链接。")
    if any(not isinstance(row, list) or len(row) != size or any(type(cell) is not bool for cell in row)
           for row in matrix):
        raise QRError("本地二维码内容不可用，请使用授权链接。")
    return size


def _png_chunk(kind, content):
    return (struct.pack(">I", len(content)) + kind + content
            + struct.pack(">I", zlib.crc32(kind + content) & 0xFFFFFFFF))


def _png_from_matrix(matrix):
    size = _validate_matrix(matrix)
    # Integer scaling preserves modules. Four white modules surround every side.
    scale = max(1, 280 // (size + 8))
    quiet = 4 * scale
    width = (size + 8) * scale
    white_scanline = b"\x00" + b"\xff" * width
    raw = bytearray(white_scanline * quiet)
    for row in matrix:
        pixels = b"".join((b"\x00" if cell else b"\xff") * scale for cell in row)
        raw.extend((b"\x00" + b"\xff" * quiet + pixels + b"\xff" * quiet) * scale)
    raw.extend(white_scanline * quiet)
    header = struct.pack(">IIBBBBB", width, width, 8, 0, 0, 0, 0)
    return (b"\x89PNG\r\n\x1a\n" + _png_chunk(b"IHDR", header)
            + _png_chunk(b"IDAT", zlib.compress(raw, level=9)) + _png_chunk(b"IEND", b""))


def render_authorization_qr(project: Path, node: str, url: str) -> bytes:
    """Return a local PNG; the authorization URL enters Node only via stdin."""
    _validate_url(url)
    if not isinstance(node, str) or not node.strip():
        raise QRError("本地 Node.js 不可用，请使用授权链接。")
    try:
        project = Path(project).resolve()
        helper = project / "scripts" / "render-qr.cjs"
        with _PIN_LOCK:
            pinned = _PINNED_HELPERS.get(project)
        command = [node, str(helper)] if pinned is None else [
            node, "-e", _PINNED_WRAPPER, str(helper), base64.b64encode(pinned).decode("ascii"),
        ]
        env = {key: value for key, value in os.environ.items()
               if not key.upper().startswith(("NODE_", *_PROVIDER_PREFIXES))}
        settings = {
            "input": url, "capture_output": True, "text": True, "encoding": "utf-8",
            "errors": "strict", "timeout": 10, "env": env, "cwd": str(project),
        }
        if os.name == "nt":
            settings["creationflags"] = subprocess.CREATE_NO_WINDOW
        result = subprocess.run(command, **settings)
        if result.returncode != 0 or not isinstance(result.stdout, str) or len(result.stdout) > 262144:
            raise QRError("无法本地生成授权二维码，请使用授权链接。")
        matrix = json.loads(result.stdout)
        return _png_from_matrix(matrix)
    except QRError:
        raise
    except (OSError, ValueError, UnicodeError, RecursionError, subprocess.TimeoutExpired):
        raise QRError("无法本地生成授权二维码，请使用授权链接。") from None
