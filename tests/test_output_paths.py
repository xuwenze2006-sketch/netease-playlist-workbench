"""Output paths must protect client storage reached through a directory link."""

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from tests.fixtures import make_cache


class OutputPathTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.base = Path(self.directory.name).resolve()
        self.data_dir = self.base / "CloudMusic"
        self.storage = self.base / "MovedClientData"
        self.library_link = self.data_dir / "Library"
        self.library_target = self.storage / "Library"
        self.data_dir.mkdir()
        connection = make_cache(self.storage)
        connection.close()
        self.addCleanup(self._cleanup)

        if sys.platform == "win32":
            # Junctions work without the symbolic-link privilege on Windows.
            destination = str(self.library_link).replace("'", "''")
            source = str(self.library_target).replace("'", "''")
            command = (
                "$ErrorActionPreference = 'Stop'; "
                f"New-Item -ItemType Junction -Path '{destination}' "
                f"-Target '{source}' | Out-Null"
            )
            result = subprocess.run(
                ["powershell", "-NoProfile", "-NonInteractive", "-Command", command],
                capture_output=True,
                timeout=15,
            )
            if result.returncode:
                self.skipTest("This Windows environment cannot create a test junction")
        else:
            self.library_link.symlink_to(self.library_target, target_is_directory=True)

        self.assertEqual(self.library_link.resolve(), self.library_target.resolve())
        baseline = self.run_bridge("status")
        self.assertEqual(baseline.returncode, 0, baseline.stderr)

    def _cleanup(self):
        # Verify both fixture locations before removing anything. Remove only
        # the link itself, then recursively clean the known temporary root.
        if not self.library_link.parent.resolve().is_relative_to(self.base):
            raise RuntimeError("Test junction escaped its temporary fixture")
        if not self.library_target.resolve().is_relative_to(self.base):
            raise RuntimeError("Test storage escaped its temporary fixture")
        if os.path.lexists(self.library_link):
            if self.library_link.resolve() != self.library_target.resolve():
                raise RuntimeError("Test junction target changed unexpectedly")
            if sys.platform == "win32":
                os.rmdir(self.library_link)
            else:
                self.library_link.unlink()
            if os.path.lexists(self.library_link):
                raise RuntimeError("Test junction remains; refusing recursive cleanup")
            if not self.library_target.is_dir():
                raise RuntimeError("Removing test junction also removed its target")
        self.directory.cleanup()

    def run_bridge(self, *arguments):
        entrypoint = Path(__file__).resolve().parents[1] / "run_bridge.py"
        return subprocess.run(
            [sys.executable, "-X", "utf8", str(entrypoint), "--data-dir", str(self.data_dir), *arguments],
            capture_output=True,
            encoding="utf-8",
            timeout=15,
        )

    def test_output_cannot_overwrite_database_via_library_junction(self):
        database = self.library_target / "webdb.dat"
        before = database.read_bytes()
        result = self.run_bridge("--output", str(self.library_link / "webdb.dat"), "status")

        self.assertNotEqual(result.returncode, 0, "Source database output was accepted")
        self.assertEqual(json.loads(result.stdout)["error"]["code"], "invalid_arguments")
        self.assertEqual(database.read_bytes(), before)

    def test_output_cannot_overwrite_other_files_in_actual_library(self):
        output = self.library_target / "settings" / "client-state.dat"
        output.parent.mkdir()
        before = b"Existing client data must survive draft output."
        output.write_bytes(before)
        result = self.run_bridge("--output", str(output), "status")

        self.assertNotEqual(result.returncode, 0, "Output inside actual client storage was accepted")
        self.assertEqual(json.loads(result.stdout)["error"]["code"], "invalid_arguments")
        self.assertEqual(output.read_bytes(), before)


if __name__ == "__main__":
    unittest.main()
