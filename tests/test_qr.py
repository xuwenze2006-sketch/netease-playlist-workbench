"""Local QR generation validates input, subprocess boundaries and PNG pixels."""

import json
import os
import shutil
import struct
import subprocess
import unittest
import zlib
from pathlib import Path
from unittest.mock import patch

try:
    from netease_organizer.qr import QRError, render_authorization_qr
except ModuleNotFoundError as error:
    if error.name != "netease_organizer.qr":
        raise
    QRError = render_authorization_qr = None


PROJECT = Path(__file__).resolve().parents[1]
PUBLIC_URL = "https://music.163.com/oauth/authorize?state=public-local-qr-example"


def png_pixels(data):
    if data[:8] != b"\x89PNG\r\n\x1a\n":
        raise AssertionError("Not a PNG")
    offset, pixels, header = 8, bytearray(), None
    while offset < len(data):
        length = struct.unpack(">I", data[offset:offset + 4])[0]
        kind = data[offset + 4:offset + 8]
        payload = data[offset + 8:offset + 8 + length]
        crc = struct.unpack(">I", data[offset + 8 + length:offset + 12 + length])[0]
        if crc != zlib.crc32(kind + payload) & 0xFFFFFFFF:
            raise AssertionError("Invalid PNG CRC")
        if kind == b"IHDR":
            header = struct.unpack(">IIBBBBB", payload)
        elif kind == b"IDAT":
            pixels.extend(payload)
        offset += length + 12
    if header is None or header[2:] != (8, 0, 0, 0, 0):
        raise AssertionError("Expected noninterlaced 8-bit grayscale PNG")
    width, height = header[:2]
    raw = zlib.decompress(pixels)
    if len(raw) != (width + 1) * height:
        raise AssertionError("Unexpected PNG scanline size")
    rows = []
    for row in range(height):
        scanline = raw[row * (width + 1):(row + 1) * (width + 1)]
        if scanline[0] != 0:
            raise AssertionError("Expected unfiltered PNG scanlines")
        rows.append(scanline[1:])
    return width, height, rows


class QRTests(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(render_authorization_qr, "Local authorization QR renderer is missing")
        self.matrix = [[False] * 21 for _ in range(21)]
        self.matrix[0][0] = self.matrix[-1][-1] = True

    def render_with(self, matrix):
        completed = subprocess.CompletedProcess([], 0, json.dumps(matrix), "ignored-private-stderr")
        with patch("netease_organizer.qr.subprocess.run", return_value=completed):
            return render_authorization_qr(PROJECT, "fixture-node", PUBLIC_URL)

    def test_url_is_sent_only_through_stdin_and_provider_environment_is_removed(self):
        environment = {"PATH": "fixture-path", "NODE_OPTIONS": "--require PRIVATE.js",
                       "NODE_COMPILE_CACHE": "outside-project", "NODE_V8_COVERAGE": "outside-project",
                       "NETEASE_PRIVATE_KEY": "PRIVATE", "NCM_TOKEN": "PRIVATE", "LANGBASE_TOKEN": "PRIVATE"}
        completed = subprocess.CompletedProcess([], 0, json.dumps(self.matrix), "")
        with patch.dict(os.environ, environment, clear=True), patch("netease_organizer.qr.subprocess.run", return_value=completed) as run:
            result = render_authorization_qr(PROJECT, "fixture-node", PUBLIC_URL)
        self.assertIsInstance(result, bytes)
        arguments, settings = run.call_args
        self.assertEqual(arguments[0], ["fixture-node", str(PROJECT / "scripts" / "render-qr.cjs")])
        self.assertNotIn(PUBLIC_URL, arguments[0])
        self.assertEqual(settings["input"], PUBLIC_URL)
        self.assertEqual(settings["timeout"], 10)
        self.assertNotIn("shell", settings)
        for key in ("NODE_OPTIONS", "NODE_COMPILE_CACHE", "NODE_V8_COVERAGE",
                    "NETEASE_PRIVATE_KEY", "NCM_TOKEN", "LANGBASE_TOKEN"):
            self.assertNotIn(key, settings["env"])

    def test_invalid_urls_are_rejected_before_starting_node(self):
        invalid = [
            "http://music.163.com/oauth", "https://music.163.com.evil.test/", "https://evil.test/",
            "https://user:password@music.163.com/", "https://music.163.com:444/",
            "https://music.163.com/中文", "https://music.163.com/\nSECRET",
            "https://music.163.com/\x00SECRET", "https://evil.test\\.music.163.com/",
            "https://music.163.com/" + "x" * 4096, None,
        ]
        with patch("netease_organizer.qr.subprocess.run") as run:
            for url in invalid:
                with self.subTest(url_kind=type(url).__name__), self.assertRaises(QRError) as raised:
                    render_authorization_qr(PROJECT, "fixture-node", url)
                self.assertNotIn("SECRET", str(raised.exception))
            run.assert_not_called()

    def test_https_official_subdomain_and_normal_https_port_are_accepted(self):
        completed = subprocess.CompletedProcess([], 0, json.dumps(self.matrix), "")
        with patch("netease_organizer.qr.subprocess.run", return_value=completed):
            png_pixels(render_authorization_qr(PROJECT, "fixture-node", "https://developer.music.163.com:443/authorize?state=example"))

    def test_malformed_non_square_non_version_and_non_boolean_matrices_are_rejected(self):
        invalid = [None, {}, [], [[False] * 20 for _ in range(20)], [[False] * 22 for _ in range(22)],
                   [[False] * 181 for _ in range(181)], [[False] * 20 for _ in range(21)]]
        for value in (0, 1, None, "false", {}):
            matrix = [[False] * 21 for _ in range(21)]
            matrix[0][0] = value
            invalid.append(matrix)
        for matrix in invalid:
            with self.subTest(matrix_kind=type(matrix).__name__), self.assertRaises(QRError):
                self.render_with(matrix)

    def test_png_has_four_module_white_border_with_integer_scaling(self):
        width, height, rows = png_pixels(self.render_with(self.matrix))
        scale = 9  # floor(280 / (21 + eight quiet-zone modules))
        quiet = 4 * scale
        self.assertEqual((width, height), (261, 261))
        for row in rows[:quiet] + rows[-quiet:]:
            self.assertEqual(row, b"\xff" * width)
        for row in rows:
            self.assertEqual(row[:quiet], b"\xff" * quiet)
            self.assertEqual(row[-quiet:], b"\xff" * quiet)
        for row in rows[quiet:quiet + scale]:
            self.assertEqual(row[quiet:quiet + scale], b"\x00" * scale)
            self.assertEqual(row[quiet + scale:quiet + 2 * scale], b"\xff" * scale)

    def test_largest_legal_matrix_stays_under_320_pixels(self):
        width, height, rows = png_pixels(self.render_with([[False] * 177 for _ in range(177)]))
        self.assertEqual((width, height), (185, 185))
        self.assertEqual(rows[0], b"\xff" * width)

    def test_process_json_and_encoding_errors_have_public_messages(self):
        responses = [
            subprocess.CompletedProcess([], 1, PUBLIC_URL, "SECRET-NODE-ERROR"),
            subprocess.CompletedProcess([], 0, "SECRET-not-JSON", ""),
            subprocess.CompletedProcess([], 0, "x" * (1024 * 1024), ""),
        ]
        for completed in responses:
            with patch("netease_organizer.qr.subprocess.run", return_value=completed), self.assertRaises(QRError) as raised:
                render_authorization_qr(PROJECT, "fixture-node", PUBLIC_URL)
            self.assertNotIn(PUBLIC_URL, str(raised.exception))
            self.assertNotIn("SECRET", str(raised.exception))
        for error in (subprocess.TimeoutExpired(["node"], 10, output=PUBLIC_URL), OSError("SECRET-PROCESS-ERROR")):
            with patch("netease_organizer.qr.subprocess.run", side_effect=error), self.assertRaises(QRError) as raised:
                render_authorization_qr(PROJECT, "fixture-node", PUBLIC_URL)
            self.assertNotIn(PUBLIC_URL, str(raised.exception))
            self.assertNotIn("SECRET", str(raised.exception))

    def test_real_local_helper_renders_public_url_without_account_cli(self):
        bundled = Path.home() / ".cache/codex-runtimes/codex-primary-runtime/dependencies/node/bin/node.exe"
        node = shutil.which("node") or (str(bundled) if bundled.is_file() else None)
        vendor = PROJECT / ".tools/ncm-cli/node_modules/qrcode-terminal/vendor/QRCode/index.js"
        if node is None or not vendor.is_file():
            self.skipTest("Local Node and project QR vendor are needed for offline smoke test")
        width, height, _ = png_pixels(render_authorization_qr(PROJECT, node, PUBLIC_URL))
        self.assertEqual(width, height)
        self.assertLessEqual(width, 320)
        helper_env = {"PATH": os.environ.get("PATH", ""), "SystemRoot": os.environ.get("SystemRoot", "")}
        baseline = subprocess.run([node, str(PROJECT / "scripts/render-qr.cjs")], input=PUBLIC_URL,
                                  capture_output=True, text=True, timeout=10, env=helper_env)
        self.assertEqual(baseline.returncode, 0)
        for invalid_url in ("https://evil.test/SECRET-AUTH-URL", "https://@music.163.com/SECRET-AUTH-URL"):
            result = subprocess.run([node, str(PROJECT / "scripts/render-qr.cjs")], input=invalid_url,
                                    capture_output=True, text=True, timeout=10, env=helper_env)
            self.assertNotEqual(result.returncode, 0)
            self.assertNotIn("SECRET", result.stdout + result.stderr)

    def test_real_local_helper_accepts_official_short_host_only(self):
        node = shutil.which("node")
        if node is None or not (PROJECT / ".tools/ncm-cli/node_modules/qrcode-terminal/vendor/QRCode/index.js").is_file():
            self.skipTest("Project-local Node QR encoder required")
        width, height, _ = png_pixels(render_authorization_qr(PROJECT, node, "https://163cn.tv/local-example"))
        self.assertEqual(width, height)
        self.assertLessEqual(width, 320)
        for value in ("https://163cn.tv.evil.test/example", "https://evil.163cn.tv/example", "http://163cn.tv/example"):
            result = subprocess.run([node, str(PROJECT / "scripts/render-qr.cjs")], input=value,
                                    capture_output=True, text=True, timeout=10, env={"PATH": os.environ.get("PATH", "")})
            self.assertNotEqual(result.returncode, 0)


if __name__ == "__main__":
    unittest.main()
