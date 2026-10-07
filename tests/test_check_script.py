"""The read-only bridge helper can run without a Codex installation."""

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest


@unittest.skipUnless(os.name == 'nt' and shutil.which('powershell.exe'), 'Windows PowerShell helper')
class CheckScriptTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix='bridge-check-fixture-')
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        (self.root / 'scripts').mkdir()
        source = Path(__file__).resolve().parents[1] / 'scripts/check.ps1'
        (self.root / 'scripts/check.ps1').write_text(source.read_text(encoding='utf-8-sig'), encoding='utf-8-sig')
        (self.root / 'run_bridge.py').write_text(
            'import json,os,sys\nprint(json.dumps(sys.argv[1:]))\n'
            'raise SystemExit(int(os.environ.get("TEST_BRIDGE_EXIT", "0")))\n', encoding='utf-8')

    def invoke(self, *, explicit=False, exit_code=0):
        environment = dict(os.environ, USERPROFILE=str(self.root / 'empty-profile'),
                           TEST_PYTHON=sys.executable, TEST_ROOT=str(self.root), TEST_BRIDGE_EXIT=str(exit_code))
        command = (
            'function Get-Command { param($Name, $CommandType, $ErrorAction) '
            'if ($Name -eq "python.exe") { [pscustomobject]@{Source=$env:TEST_PYTHON} } }; '
            '& (Join-Path $env:TEST_ROOT "scripts/check.ps1") '
            '-DataDirectory (Join-Path $env:TEST_ROOT "cache with spaces")'
        )
        if explicit:
            command += ' -PythonExecutable $env:TEST_PYTHON'
        command += '; exit $LASTEXITCODE'
        return subprocess.run(['powershell.exe', '-NoProfile', '-NonInteractive', '-ExecutionPolicy', 'Bypass',
                               '-Command', command], env=environment, capture_output=True, text=True,
                              encoding='utf-8', errors='replace', timeout=15)

    def test_path_python_works_with_no_codex_runtime_and_preserves_data_path(self):
        result = self.invoke()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout), ['--data-dir', str(self.root / 'cache with spaces'), 'status'])

    def test_explicit_python_preserves_bridge_exit_code(self):
        result = self.invoke(explicit=True, exit_code=7)
        self.assertEqual(result.returncode, 7, result.stderr)
        self.assertEqual(json.loads(result.stdout)[-1], 'status')


if __name__ == '__main__':
    unittest.main()
