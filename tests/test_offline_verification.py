import ctypes
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from scripts import verify_offline


class OfflineVerificationTests(unittest.TestCase):
    def test_success_and_failure_exit_codes_are_preserved_in_step_logs(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            for expected in (0, 7):
                log = root / f'{expected}.log'
                code = verify_offline.run_step(
                    [sys.executable, '-c', f'print("local-step"); raise SystemExit({expected})'],
                    root, log, timeout=5)
                self.assertEqual(code, expected)
                self.assertIn('local-step', log.read_text())

    @unittest.skipUnless(sys.platform == 'win32', 'Windows process-tree ownership')
    def test_timeout_terminates_the_owned_child_process_as_well_as_its_parent(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            script = root / 'owned_tree.py'
            script.write_text(
                'import json,subprocess,sys,time\n'
                'from pathlib import Path\n'
                'child=subprocess.Popen([sys.executable,"-c","import time; time.sleep(60)"])\n'
                'Path("child.json").write_text(json.dumps(child.pid))\n'
                'time.sleep(60)\n', encoding='utf-8')
            child_pid = None
            try:
                code = verify_offline.run_step(
                    [sys.executable, str(script)], root, root / 'timeout.log', timeout=2)
                child_pid = json.loads((root / 'child.json').read_text())
                self.assertEqual(code, 2)
                kernel = ctypes.WinDLL('kernel32', use_last_error=True)
                kernel.OpenProcess.argtypes = [ctypes.c_ulong, ctypes.c_int, ctypes.c_ulong]
                kernel.OpenProcess.restype = ctypes.c_void_p
                kernel.WaitForSingleObject.argtypes = [ctypes.c_void_p, ctypes.c_ulong]
                kernel.CloseHandle.argtypes = [ctypes.c_void_p]
                handle = kernel.OpenProcess(0x00100000, False, child_pid)
                if handle:
                    try:
                        self.assertEqual(kernel.WaitForSingleObject(handle, 1000), 0,
                                         'the timed-out step left its child running')
                    finally:
                        kernel.CloseHandle(handle)
                else:
                    self.assertEqual(ctypes.get_last_error(), 87)
                child_pid = None
            finally:
                # If a regression leaves this test's own child alive, clean it.
                if child_pid is not None:
                    subprocess.run(['taskkill', '/PID', str(child_pid), '/T', '/F'],
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                   creationflags=subprocess.CREATE_NO_WINDOW, timeout=5)
