"""A backend keeps its birth QR helper without copying files or URL arguments."""

import base64
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from netease_organizer import qr


PUBLIC_URL = "https://music.163.com/oauth/authorize?state=fixture-public-state"


class QRPinnedHelperTests(unittest.TestCase):
    def setUp(self):
        self.assertTrue(callable(getattr(qr, "pin_authorization_helper", None)),
                        "Birth QR helper pinning is missing")
        self.temp = tempfile.TemporaryDirectory(prefix="organizer-qr-pinning-")
        self.addCleanup(self.temp.cleanup)
        self.project = Path(self.temp.name).resolve()
        self.projects = [self.project]
        self.addCleanup(self.clean_own_pins)
        self.source = b"'use strict'; // birth-helper fixture\n"
        self.completed = subprocess.CompletedProcess([], 0, json.dumps([[False] * 21 for _ in range(21)]), "")

    def clean_own_pins(self):
        for project in self.projects:
            qr._PINNED_HELPERS.pop(project, None)

    def test_pinned_birth_source_uses_fixed_wrapper_and_url_only_stdin(self):
        before = list(self.project.rglob("*"))
        qr.pin_authorization_helper(self.project, self.source)
        self.assertEqual(list(self.project.rglob("*")), before)
        environment = {"PATH": "fixture-path", "NODE_OPTIONS": "PRIVATE",
                       "NODE_V8_COVERAGE": "PRIVATE", "NCM_TOKEN": "PRIVATE"}
        with patch.dict(os.environ, environment, clear=True), \
                patch.object(qr.subprocess, "run", return_value=self.completed) as run:
            png = qr.render_authorization_qr(self.project, "fixture-node", PUBLIC_URL)
        self.assertTrue(png.startswith(b"\x89PNG\r\n\x1a\n"))
        args, settings = run.call_args
        command = args[0]
        self.assertEqual(command[:2], ["fixture-node", "-e"])
        self.assertIn("._compile(", command[2])
        self.assertEqual(command[-2], str(self.project / "scripts/render-qr.cjs"))
        self.assertEqual(base64.b64decode(command[-1], validate=True), self.source)
        self.assertFalse(any(PUBLIC_URL in part or "fixture-public-state" in part for part in command))
        self.assertEqual(settings["input"], PUBLIC_URL)
        self.assertEqual(settings["timeout"], 10)
        self.assertEqual(settings["cwd"], str(self.project))
        self.assertNotIn("shell", settings)
        for key in ("NODE_OPTIONS", "NODE_V8_COVERAGE", "NCM_TOKEN"):
            self.assertNotIn(key, settings["env"])

    def test_disk_replacement_or_removal_cannot_change_pinned_source(self):
        helper = self.project / "scripts/render-qr.cjs"
        helper.parent.mkdir()
        helper.write_bytes(self.source)
        qr.pin_authorization_helper(self.project, self.source)
        helper.write_bytes(b"throw Error('new-disk-helper-must-not-run');\n")
        with patch.object(qr.subprocess, "run", return_value=self.completed) as run:
            qr.render_authorization_qr(self.project, "fixture-node", PUBLIC_URL)
            self.assertEqual(base64.b64decode(run.call_args.args[0][-1]), self.source)
            helper.unlink()
            qr.render_authorization_qr(self.project, "fixture-node", PUBLIC_URL)
            self.assertEqual(base64.b64decode(run.call_args.args[0][-1]), self.source)

    def test_pin_is_project_specific_and_unpinned_projects_keep_legacy_command(self):
        qr.pin_authorization_helper(self.project, self.source)
        second = self.project / "second"
        self.projects.append(second)
        qr.pin_authorization_helper(second, b"// second birth\n")
        third = self.project / "third"
        with patch.object(qr.subprocess, "run", return_value=self.completed) as run:
            qr.render_authorization_qr(second, "fixture-node", PUBLIC_URL)
            self.assertEqual(base64.b64decode(run.call_args.args[0][-1]), b"// second birth\n")
            qr.render_authorization_qr(third, "fixture-node", PUBLIC_URL)
            self.assertEqual(run.call_args.args[0], ["fixture-node", str(third / "scripts/render-qr.cjs")])

    def test_same_pin_is_idempotent_but_replacement_is_rejected(self):
        qr.pin_authorization_helper(self.project, self.source)
        qr.pin_authorization_helper(self.project, self.source)
        with self.assertRaises(qr.QRError) as error:
            qr.pin_authorization_helper(self.project, b"// PRIVATE replacement source\n")
        self.assertNotIn("PRIVATE", str(error.exception))
        with patch.object(qr.subprocess, "run", return_value=self.completed) as run:
            qr.render_authorization_qr(self.project, "fixture-node", PUBLIC_URL)
        self.assertEqual(base64.b64decode(run.call_args.args[0][-1]), self.source)

    def test_invalid_or_oversized_helper_is_rejected_without_a_pin_or_process(self):
        with patch.object(qr.subprocess, "run") as run:
            for source in (None, "PRIVATE", bytearray(b"code"), b"", b"x" * 8193, b"\xff"):
                with self.subTest(kind=type(source).__name__), self.assertRaises(qr.QRError) as error:
                    qr.pin_authorization_helper(self.project, source)
                self.assertNotIn("PRIVATE", str(error.exception))
                self.assertNotIn(self.project, qr._PINNED_HELPERS)
            run.assert_not_called()

    def test_eight_kib_helper_is_accepted_at_the_command_line_bound(self):
        source = b"/*" + b"x" * 8188 + b"*/"
        qr.pin_authorization_helper(self.project, source)
        with patch.object(qr.subprocess, "run", return_value=self.completed) as run:
            qr.render_authorization_qr(self.project, "fixture-node", PUBLIC_URL)
        self.assertEqual(base64.b64decode(run.call_args.args[0][-1]), source)

    def test_local_node_compiles_birth_source_with_original_relative_vendor_path(self):
        node = shutil.which("node")
        if node is None:
            self.skipTest("Local Node is needed for the offline fake helper smoke test")
        helper = self.project / "scripts/render-qr.cjs"
        helper.parent.mkdir()
        matrix = [[False] * 21 for _ in range(21)]
        (self.project / "fixture-vendor.cjs").write_text(
            "module.exports=" + json.dumps(matrix) + ";", encoding="utf-8")
        source = b"""'use strict';
const path = require('node:path');
const matrix = require(path.join(__dirname, '..', 'fixture-vendor.cjs'));
if (__filename !== path.join(process.cwd(), 'scripts', 'render-qr.cjs')) throw Error('filename');
process.stdin.resume();
process.stdin.on('end', () => process.stdout.write(JSON.stringify(matrix)));
"""
        qr.pin_authorization_helper(self.project, source)
        replacement = b"throw Error('disk replacement must not run');"
        helper.write_bytes(replacement)
        png = qr.render_authorization_qr(self.project, node, PUBLIC_URL)
        self.assertTrue(png.startswith(b"\x89PNG\r\n\x1a\n"))
        self.assertEqual(helper.read_bytes(), replacement)


if __name__ == "__main__":
    unittest.main()
