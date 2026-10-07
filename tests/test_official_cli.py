import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from netease_organizer.official_cli import CliError, OfficialCli, flatten_manifest


class OfficialCliTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        script = self.root / ".tools/ncm-cli/node_modules/@music163/ncm-cli/dist/index.js"
        script.parent.mkdir(parents=True)
        script.write_text("// fixture", encoding="utf-8")
        (script.parent.parent / "package.json").write_text('{"version":"0.1.7"}')
        self.node = self.root / "node.exe"
        self.node.write_bytes(b"node fixture")
        self.calls = []
        self.runner = OfficialCli(self.root, node=self.node, run_process=self.fake_run)

    def tearDown(self):
        self.temp.cleanup()

    def fake_run(self, args, **kwargs):
        self.calls.append((args, kwargs))
        if args[-1] == "--version":
            return subprocess.CompletedProcess(args, 0, "0.1.7\n", "")
        if args[2:5] == ["config", "set", "privateKey"]:
            self.assertIn("SECRET-PRIVATE-KEY", Path(args[5]).read_text())
            target = self.runner.config_dir / "credentials.enc.json"
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text("encrypted fixture")
        return subprocess.CompletedProcess(args, 0, "configuration saved", "")

    def test_version_command_uses_isolated_home_without_shell(self):
        self.assertEqual(self.runner.version(), "0.1.7")
        args, settings = self.calls[-1]
        self.assertEqual(args[-1], "--version")
        self.assertEqual(settings["env"]["USERPROFILE"], str(self.runner.home))
        self.assertNotIn("shell", settings)

    def test_metrics_count_one_json_subprocess_without_exposing_arguments_or_output(self):
        events = []
        self.runner.set_observer(events.append)
        self.runner.run_process = lambda args, **kwargs: subprocess.CompletedProcess(
            args, 0, '{"code":200,"data":"SECRET-OUTPUT"}', "SECRET-STDERR")
        with patch("netease_organizer.official_cli.time.perf_counter", side_effect=[10.0, 12.5]):
            self.assertEqual(self.runner.run_json(["playlist", "tracks", "--playlistId", "SECRET-ID"])["code"], 200)
        self.assertEqual(events, [
            {"kind": "cli", "command": "playlist tracks", "phase": "started"},
            {"kind": "cli", "command": "playlist tracks", "phase": "finished",
             "elapsed_seconds": 2.5, "success": True},
        ])
        summary = self.runner.performance_summary()
        self.assertEqual(summary, {
            "call_count": 1, "elapsed_seconds": 2.5, "failure_count": 0,
            "commands": [{"command": "playlist tracks", "call_count": 1,
                          "elapsed_seconds": 2.5, "failure_count": 0}],
        })
        self.assertNotIn("SECRET", json.dumps([summary, events]))

    def test_nonzero_exit_is_counted_as_failed_without_changing_public_error(self):
        events = []
        self.runner.set_observer(events.append)
        self.runner.run_process = lambda args, **kwargs: subprocess.CompletedProcess(args, 1, "SECRET", "SECRET")
        with patch("netease_organizer.official_cli.time.perf_counter", side_effect=[0.0, 3.0]):
            with self.assertRaises(CliError) as error:
                self.runner.run_text(["playlist", "create", "--playlistName", "SECRET-NAME"])
        self.assertNotIn("SECRET", str(error.exception))
        self.assertEqual(events[-1]["success"], False)
        self.assertEqual(self.runner.performance_summary()["failure_count"], 1)
        self.assertEqual(self.runner.performance_summary()["elapsed_seconds"], 3.0)

    def test_timeout_and_start_failure_have_safe_finished_events_and_failure_metrics(self):
        for failure in (subprocess.TimeoutExpired("SECRET", 1, output="SECRET"), OSError("SECRET")):
            with self.subTest(failure=type(failure).__name__):
                self.runner.reset_metrics()
                events = []
                self.runner.set_observer(events.append)

                def failing(args, **kwargs):
                    raise failure

                self.runner.run_process = failing
                with patch("netease_organizer.official_cli.time.perf_counter", side_effect=[2.0, 3.0]):
                    with self.assertRaises(CliError) as error:
                        self.runner.run_text(["login", "--check"])
                self.assertNotIn("SECRET", str(error.exception))
                self.assertEqual(events[-1], {"kind": "cli", "command": "login check", "phase": "finished",
                                              "elapsed_seconds": 1.0, "success": False})
                self.assertEqual(self.runner.performance_summary()["call_count"], 1)
                self.assertEqual(self.runner.performance_summary()["failure_count"], 1)

    def test_observer_failures_do_not_change_success_or_failure(self):
        def broken_observer(event):
            raise RuntimeError("SECRET-OBSERVER")

        self.runner.set_observer(broken_observer)
        self.assertEqual(self.runner.run_text(["commands"]), "configuration saved")
        self.runner.run_process = lambda args, **kwargs: subprocess.CompletedProcess(args, 1, "SECRET", "")
        with self.assertRaises(CliError) as error:
            self.runner.run_text(["commands"])
        self.assertNotIn("OBSERVER", str(error.exception))
        summary = self.runner.performance_summary()
        self.assertEqual(summary["call_count"], 2)
        self.assertEqual(summary["failure_count"], 1)

    def test_unknown_command_and_config_values_are_never_metric_labels(self):
        events = []
        self.runner.set_observer(events.append)
        self.runner.run_text(["SECRET-COMMAND", "https://163cn.tv/SECRET-AUTHORIZATION"])
        self.runner.run_text(["config", "set", "appId", "SECRET-APP"])
        self.assertEqual([event["command"] for event in events if event["phase"] == "started"],
                         ["其他官方操作", "config set"])
        self.assertNotIn("SECRET", json.dumps([self.runner.performance_summary(), events]))

    def test_metric_reset_and_summary_mutation_do_not_change_future_counts(self):
        self.runner.run_text(["commands"])
        summary = self.runner.performance_summary()
        summary["commands"][0]["call_count"] = 999
        self.assertEqual(self.runner.performance_summary()["commands"][0]["call_count"], 1)
        self.runner.reset_metrics()
        self.assertEqual(self.runner.performance_summary(), {
            "call_count": 0, "elapsed_seconds": 0.0, "failure_count": 0, "commands": [],
        })

    def test_invalid_command_does_not_count_a_subprocess(self):
        with self.assertRaises(CliError):
            self.runner.run_text([None])
        self.assertEqual(self.calls, [])
        self.assertEqual(self.runner.performance_summary()["call_count"], 0)

    def test_version_cache_avoids_subprocess_until_node_or_script_metadata_changes(self):
        self.assertEqual(self.runner.version(), "0.1.7")
        self.assertEqual(self.runner.version(), "0.1.7")
        self.assertEqual(len(self.calls), 1)
        self.runner.script.write_text("// changed script fixture", encoding="utf-8")
        self.assertEqual(self.runner.version(), "0.1.7")
        self.assertEqual(len(self.calls), 2)
        self.node.write_bytes(b"changed node fixture")
        self.assertEqual(self.runner.version(), "0.1.7")
        self.assertEqual(len(self.calls), 3)
        self.assertEqual(self.runner.performance_summary()["call_count"], 3)

    def test_failed_or_malformed_version_is_not_cached(self):
        for returncode, output in ((1, "0.1.7"), (0, "SECRET-malformed")):
            with self.subTest(returncode=returncode):
                calls = []

                def failing(args, **kwargs):
                    calls.append(args)
                    return subprocess.CompletedProcess(args, returncode, output, "")

                self.runner.run_process = failing
                for _ in range(2):
                    with self.assertRaises(CliError):
                        self.runner.version()
                self.assertEqual(len(calls), 2)

    def test_token_probe_reads_only_file_metadata(self):
        from unittest.mock import patch
        token_file = self.runner.config_dir / "tokens.enc.json"
        token_file.parent.mkdir(parents=True)
        token_file.write_bytes(b"encrypted fixture")
        with patch.object(Path, "read_text", side_effect=AssertionError("Do not read token contents")):
            signature = self.runner.token_signature()
        self.assertIsInstance(signature, tuple)
        self.assertEqual(signature[1], len(b"encrypted fixture"))
        self.assertEqual(self.calls, [])

    def test_provider_environment_is_not_inherited(self):
        from unittest.mock import patch
        with patch.dict(os.environ, {"NETEASE_PRIVATE_KEY": "SECRET", "LANGBASE_TOKEN": "SECRET", "NODE_OPTIONS": "--require arbitrary.js",
                                    "NODE_PATH": "outside-project", "NODE_COMPILE_CACHE": "outside-project", "NODE_V8_COVERAGE": "outside-project"}):
            self.runner.version()
        settings = self.calls[-1][1]["env"]
        self.assertNotIn("NETEASE_PRIVATE_KEY", settings)
        self.assertNotIn("LANGBASE_TOKEN", settings)
        self.assertNotIn("NODE_OPTIONS", settings)
        self.assertNotIn("NODE_PATH", settings)
        self.assertNotIn("NODE_COMPILE_CACHE", settings)
        self.assertNotIn("NODE_V8_COVERAGE", settings)

    def test_symlinked_config_directory_cannot_write_outside_project(self):
        self.runner.version()
        self.calls.clear()
        external = self.root.parent / (self.root.name + "-external")
        external.mkdir()
        self.runner.config_dir.parent.mkdir(parents=True)
        try:
            if os.name == "nt":
                destination, source = str(self.runner.config_dir).replace("'", "''"), str(external).replace("'", "''")
                result = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command",
                                         f"New-Item -ItemType Junction -Path '{destination}' -Target '{source}' | Out-Null"],
                                        capture_output=True, timeout=15)
                if result.returncode:
                    self.skipTest("Creating test junctions is unavailable")
            else:
                self.runner.config_dir.symlink_to(external, target_is_directory=True)
            with self.assertRaises(CliError):
                self.runner.version()
            self.assertEqual(self.calls, [])
        finally:
            if os.path.lexists(self.runner.config_dir):
                if self.runner.config_dir.resolve() != external.resolve():
                    raise RuntimeError("Test junction target changed unexpectedly")
                if os.name == "nt":
                    os.rmdir(self.runner.config_dir)
                else:
                    self.runner.config_dir.unlink()
            external.rmdir()

    def test_key_uses_temporary_file_then_is_deleted(self):
        self.runner.save_credentials("app-1", "SECRET-PRIVATE-KEY")
        private_args = self.calls[-1][0]
        self.assertNotIn("SECRET-PRIVATE-KEY", json.dumps(self.calls))
        self.assertFalse(Path(private_args[5]).exists())
        self.assertTrue(self.runner.configured())
        self.assertNotIn("SECRET", self.runner.marker.read_text())

    def test_failed_config_does_not_leak_secret_or_claim_ready(self):
        def failing(args, **kwargs):
            return subprocess.CompletedProcess(args, 1, "SECRET-KEY", "SECRET-KEY")
        self.runner.run_process = failing
        with self.assertRaises(CliError) as context:
            self.runner.save_credentials("app", "SECRET-KEY")
        self.assertNotIn("SECRET", str(context.exception))
        self.assertFalse(self.runner.configured())
        self.assertFalse(list(self.runner.home.glob("private-key-*.tmp")))

    def test_timeout_has_safe_public_error(self):
        def timeout(args, **kwargs):
            raise subprocess.TimeoutExpired(args, 1, output="SECRET")
        self.runner.run_process = timeout
        with self.assertRaises(CliError) as context:
            self.runner.version()
        self.assertNotIn("SECRET", str(context.exception))

    def test_mixed_or_non_json_output_is_rejected(self):
        self.runner.run_process = lambda args, **kwargs: subprocess.CompletedProcess(args, 0, "log SECRET\n{}", "")
        with self.assertRaises(CliError):
            self.runner.run_json(["login", "--check"])

    def test_manifest_walks_resources_and_default_methods(self):
        manifest = {"manifests": {
            "root": {"sub_resources": [{"name": "playlist", "path": "/playlist"}]},
            "/playlist": {"methods": [{"name": "$default", "parameters": []},
                                        {"name": "create", "parameters": [{"name": "playlistName", "in": "body", "required": True, "type": "string"}]}]},
        }}
        commands = flatten_manifest(manifest)
        self.assertEqual([c["command"] for c in commands], [["playlist"], ["playlist", "create"]])
        self.assertEqual(commands[1]["parameters"][0]["name"], "playlistName")

    def test_malformed_or_cyclic_manifest_is_rejected(self):
        for manifest in ({"manifests": {}}, {"manifests": {"root": {"sub_resources": [{"name": "x", "path": "root"}]}}}):
            with self.subTest(manifest=manifest), self.assertRaises(CliError):
                flatten_manifest(manifest)

    def test_root_default_does_not_hide_child_resources(self):
        manifest = {"manifests": {
            "root": {"methods": [{"name": "$default"}], "sub_resources": [{"name": "playlist", "path": "/playlist"}]},
            "/playlist": {"methods": [{"name": "list"}]},
        }}
        self.assertEqual([c["command"] for c in flatten_manifest(manifest)], [["playlist", "list"]])

    def test_parameter_and_child_names_must_be_strings(self):
        for name in (True, None):
            manifest = {"manifests": {"root": {"methods": [
                {"name": "playlist", "parameters": [{"name": name}]}
            ]}}}
            with self.subTest(parameter=name), self.assertRaises(CliError):
                flatten_manifest(manifest)
            manifest = {"manifests": {
                "root": {"sub_resources": [{"name": name, "path": "/child"}]},
                "/child": {"methods": [{"name": "list"}]},
            }}
            with self.subTest(child=name), self.assertRaises(CliError):
                flatten_manifest(manifest)

    def test_single_resource_cannot_exceed_command_limit(self):
        manifest = {"manifests": {"root": {"methods": [{"name": f"command{i}"} for i in range(1001)]}}}
        with self.assertRaises(CliError):
            flatten_manifest(manifest)

    def test_empty_shared_resource_graph_cannot_expand_without_bound(self):
        resources = {}
        for depth in range(7):
            resources['root' if depth == 0 else str(depth)] = {
                'sub_resources': [{'name': f'branch{i}', 'path': str(depth + 1)} for i in range(5)]
            }
        resources['7'] = {'methods': []}
        with self.assertRaises(CliError):
            flatten_manifest({'manifests': resources})

    def test_small_shared_resource_graph_preserves_distinct_command_aliases(self):
        manifest = {'manifests': {
            'root': {'sub_resources': [{'name': 'first', 'path': '/shared'},
                                       {'name': 'second', 'path': '/shared'}]},
            '/shared': {'methods': [{'name': 'list'}]},
        }}
        self.assertEqual([row['command'] for row in flatten_manifest(manifest)],
                         [['first', 'list'], ['second', 'list']])

    def test_oversized_root_default_methods_are_bounded_even_without_commands(self):
        manifest = {'manifests': {'root': {'methods': [{'name': '$default'}] * 10001}}}
        with self.assertRaises(CliError):
            flatten_manifest(manifest)


if __name__ == "__main__":
    unittest.main()
