"""Offline entry and Windows runtime-discovery regressions; never launch an account job."""

import builtins
import contextlib
import importlib.util
import io
import json
import os
import py_compile
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import run_organizer
from netease_organizer.web_build import LaunchError

PROJECT = Path(__file__).resolve().parents[1]


class StartupRecoveryTests(unittest.TestCase):
    def setUp(self):
        # Startup error routing must reach its injected failure even before the
        # first frontend build. Actual build validation has separate fixtures.
        self.enterContext(patch('netease_organizer.web_build.load_build', return_value=object()))

    def test_actual_service_import_failure_is_caught_after_entry_module_loads(self):
        original_import = builtins.__import__

        def without_service(name, *args, **kwargs):
            if name == 'netease_organizer.service':
                raise ImportError('SECRET-SERVICE-IMPORT')
            return original_import(name, *args, **kwargs)

        spec = importlib.util.spec_from_file_location('startup_import_fixture', PROJECT / 'run_organizer.py')
        module = importlib.util.module_from_spec(spec)
        with patch('builtins.__import__', side_effect=without_service):
            spec.loader.exec_module(module)
            with patch.object(module, 'show_gui_startup_error') as visible:
                self.assertEqual(module.main(['gui']), 1)
                visible.assert_called_once_with()

    def test_temporary_cache_creation_failure_has_a_fixed_visible_gui_fallback(self):
        with patch('run_organizer.TemporaryDirectory', side_effect=PermissionError('SECRET-TEMP-PATH')), \
                patch('run_organizer.show_gui_startup_error') as visible:
            self.assertEqual(run_organizer.main(['gui']), 1)
            visible.assert_called_once_with()

    def test_typed_launch_failures_forward_only_whitelisted_reason_codes(self):
        for code in ('frontend_missing', 'build_changed', 'local_permission_denied',
                     'browser_unavailable', 'instance_busy', 'legacy_instance'):
            with self.subTest(code=code):
                error = LaunchError(code=code)
                error.args = ('SECRET-EXCEPTION',)
                with patch('run_organizer.Organizer') as controller, \
                        patch('netease_organizer.web_launcher.launch_web', side_effect=error), \
                        patch('run_organizer.show_gui_startup_error') as visible:
                    self.assertEqual(run_organizer.main(['gui']), 1)
                    visible.assert_called_once_with(error_code=code)
                    controller.return_value.login.assert_not_called()

    def test_unknown_exception_codes_keep_legacy_no_argument_error_call(self):
        for code in ('frontend_missing', 'SECRET-CODE', None, ['frontend_missing']):
            with self.subTest(code=code):
                error = RuntimeError('SECRET-EXCEPTION')
                error.code = code
                with patch('run_organizer.Organizer', side_effect=error), \
                        patch('run_organizer.show_gui_startup_error') as visible:
                    self.assertEqual(run_organizer.main(['gui']), 1)
                    visible.assert_called_once_with()

    def test_visible_errors_are_fixed_actionable_and_never_include_unknown_details(self):
        expected = {
            'frontend_missing': 'npm run build',
            'build_changed': 'npm run build',
            'local_permission_denied': '权限',
            'browser_unavailable': '浏览器',
            'instance_busy': '正在启动',
            'legacy_instance': '旧版',
            'startup_failed': '本地验证',
            'SECRET-CODE': '本地验证',
        }
        for code, phrase in expected.items():
            with self.subTest(code=code), patch('run_organizer.os.name', 'posix'), \
                    contextlib.redirect_stderr(io.StringIO()) as output:
                run_organizer.show_gui_startup_error(error_code=code)
                self.assertIn(phrase, output.getvalue())
                self.assertIn('不会自动重发', output.getvalue())
                self.assertNotIn('SECRET', output.getvalue())

    def test_equal_timestamp_and_size_source_update_ignores_stale_project_pyc(self):
        with tempfile.TemporaryDirectory() as folder:
            project = Path(folder)
            shutil.copyfile(PROJECT / 'run_organizer.py', project / 'run_organizer.py')
            package = project / 'netease_organizer'
            package.mkdir()
            (package / '__init__.py').write_text('', encoding='utf-8')
            service = package / 'service.py'
            old = "class Organizer:\n    def __init__(self,*args,**kwargs): self.marker='OLD'\n"
            service.write_text(old, encoding='utf-8')
            original = service.stat()
            py_compile.compile(str(service), doraise=True)
            service.write_text(old.replace('OLD', 'NEW'), encoding='utf-8')
            os.utime(service, ns=(original.st_atime_ns, original.st_mtime_ns))
            self.assertEqual(service.stat().st_size, original.st_size)
            control = subprocess.run([sys.executable, '-c',
                "from netease_organizer.service import Organizer; print(Organizer().marker)"],
                cwd=project, capture_output=True, text=True, timeout=10)
            self.assertEqual(control.stdout.strip(), 'OLD', control.stderr)
            (package / 'web_build.py').write_text(
                "class LaunchError(RuntimeError): pass\ndef load_build(project): return object()\n", encoding='utf-8')
            (package / 'web_launcher.py').write_text(
                "import json,sys\nfrom pathlib import Path\n"
                "def launch_web(controller, **kwargs):\n"
                "    print(json.dumps({'marker': controller.marker, 'prefix': sys.pycache_prefix, "
                "'exists_during_launch': Path(sys.pycache_prefix).is_dir()}))\n", encoding='utf-8')
            launched = subprocess.run([sys.executable, '-X', 'utf8', 'run_organizer.py', 'gui'],
                cwd=project, capture_output=True, text=True, timeout=15)
            self.assertEqual(launched.returncode, 0, launched.stderr)
            result = json.loads(launched.stdout)
            self.assertEqual(result['marker'], 'NEW')
            self.assertTrue(result['exists_during_launch'])
            self.assertFalse(Path(result['prefix']).exists())


@unittest.skipUnless(os.name == 'nt' and shutil.which('powershell.exe'), 'Windows PowerShell discovery checks')
class WindowsRuntimeDiscoveryTests(unittest.TestCase):
    def run_helper(self, command):
        contents = (PROJECT / '启动歌单整理.cmd').read_text(encoding='utf-8-sig')
        self.assertIn('\n# ORGANIZER_POWERSHELL\n', contents)
        source = contents.split('\n# ORGANIZER_POWERSHELL\n', 1)[1]
        with tempfile.TemporaryDirectory() as folder:
            helper = Path(folder) / 'runtime-helper.ps1'
            helper.write_text(source, encoding='utf-8-sig')
            environment = dict(os.environ, ORGANIZER_TEST_HELPER=str(helper), ORGANIZER_TEST_PROJECT=folder)
            (Path(folder) / 'run_organizer.py').write_text('# fixture only\n', encoding='utf-8')
            result = subprocess.run(
                ['powershell.exe', '-NoProfile', '-NonInteractive', '-Command',
                 ". $env:ORGANIZER_TEST_HELPER; " + command],
                capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=10,
                creationflags=subprocess.CREATE_NO_WINDOW, env=environment,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            return json.loads(result.stdout.lstrip('\ufeff'))

    def test_codex_runtime_wins_over_an_available_local_python(self):
        value = self.run_helper("Find-OrganizerPython -ProfilePath 'C:\\FixtureProfile' "
            "-LocalCandidates @('C:\\LocalPython\\python.exe') -PathExists { $true } "
            "-VersionProbe { $true } | ConvertTo-Json -Compress")
        self.assertEqual(value, r'C:\FixtureProfile\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\pythonw.exe')

    def test_local_installation_listing_is_skipped_when_codex_runtime_is_ready(self):
        value = self.run_helper("function Get-OrganizerLocalCandidates { throw 'unnecessary discovery' }; "
            "Find-OrganizerPython -ProfilePath 'C:\\FixtureProfile' "
            "-PathExists { $true } -VersionProbe { $true } | ConvertTo-Json -Compress")
        self.assertEqual(value, r'C:\FixtureProfile\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\pythonw.exe')

    def test_old_codex_python_is_skipped_for_a_local_311_runtime(self):
        value = self.run_helper("Find-OrganizerPython -ProfilePath 'C:\\FixtureProfile' "
            "-LocalCandidates @('C:\\LocalPython\\python.exe') -PathExists { $true } "
            "-VersionProbe { param($path) $path -notlike '*codex-runtimes*' } | ConvertTo-Json -Compress")
        self.assertEqual(value, r'C:\LocalPython\pythonw.exe')

    def test_store_installation_alias_is_not_probed_or_selected(self):
        value = self.run_helper("Find-OrganizerPython -ProfilePath 'C:\\FixtureProfile' "
            "-LocalCandidates @('C:\\FixtureProfile\\AppData\\Local\\Microsoft\\WindowsApps\\python.exe', 'C:\\LocalPython\\python.exe') "
            "-PathExists { param($path) $path -notlike '*codex-runtimes*' } "
            "-VersionProbe { param($path) if ($path -like '*WindowsApps*') { throw 'alias invoked' }; $true } "
            "| ConvertTo-Json -Compress")
        self.assertEqual(value, r'C:\LocalPython\pythonw.exe')

    def test_missing_runtime_notifies_once_without_launching_anything(self):
        value = self.run_helper("Start-OrganizerWorkbench -ProjectDirectory $env:ORGANIZER_TEST_PROJECT "
            "-ResolveRuntime { $null } -Launch { throw 'unexpected launch' } "
            "-Notify { param($code) @{code=$code} | ConvertTo-Json -Compress }")
        self.assertEqual(value, {'code': 'runtime_missing'})

    def test_runtime_launch_is_windowless_gui_with_no_authorization_flag(self):
        value = self.run_helper("Start-OrganizerWorkbench -ProjectDirectory $env:ORGANIZER_TEST_PROJECT "
            "-ResolveRuntime { 'C:\\LocalPython\\pythonw.exe' } "
            "-Launch { param($executable, $arguments, $directory) "
            "@{executable=$executable; arguments=$arguments; directory=$directory} | ConvertTo-Json -Compress } "
            "-Notify { throw 'unexpected notification' }")
        self.assertEqual(value['executable'], r'C:\LocalPython\pythonw.exe')
        self.assertEqual(value['arguments'][0:2], ['-X', 'utf8'])
        self.assertEqual(value['arguments'][-1], 'gui')
        self.assertNotIn('--authorize', value['arguments'])
        self.assertTrue(value['arguments'][2].startswith('"'))
        self.assertTrue(value['arguments'][2].endswith('run_organizer.py"'))

    def test_real_cmd_wrapper_launches_only_a_fake_unicode_project_entry(self):
        with tempfile.TemporaryDirectory(prefix="organizer-启动 & '") as folder:
            project = Path(folder)
            launcher = project / '启动测试.cmd'
            shutil.copyfile(PROJECT / '启动歌单整理.cmd', launcher)
            marker = project / 'received.json'
            (project / 'run_organizer.py').write_text(
                "import json,sys\nfrom pathlib import Path\n"
                "Path(__file__).with_name('received.json').write_text(json.dumps(sys.argv[1:]), encoding='utf-8')\n",
                encoding='utf-8')
            result = subprocess.run(['cmd.exe', '/d', '/c', launcher.name], cwd=project,
                capture_output=True, timeout=15, creationflags=subprocess.CREATE_NO_WINDOW)
            self.assertEqual(result.returncode, 0, (result.stdout + result.stderr).decode('gb18030', errors='replace'))
            deadline = time.monotonic() + 8
            while not marker.exists() and time.monotonic() < deadline:
                time.sleep(0.05)
            self.assertTrue(marker.exists(), 'The hidden wrapper did not reach the fake project entry')
            self.assertEqual(json.loads(marker.read_text(encoding='utf-8')), ['gui'])


if __name__ == '__main__':
    unittest.main()
