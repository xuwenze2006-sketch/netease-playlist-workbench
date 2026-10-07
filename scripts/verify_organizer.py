"""Local-only acceptance. It never configures credentials or requests login."""

import hashlib
import json
import subprocess
import sys
import tempfile
import time
import tkinter as tk
from pathlib import Path
from unittest.mock import patch

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT))

from netease_bridge.cache import default_data_dir
from netease_organizer.service import Organizer, OrganizerError
from netease_organizer.ui import OrganizerWindow
from netease_organizer.qr import render_authorization_qr


def verify_onboarding_flow(node):
    """Exercise real Tk/PNG/threads with a local account-service substitute."""
    class LocalCli:
        def __init__(self):
            self.node, self.ready, self.revision = node, False, 0
            self.operations = []

        def installed(self):
            return True

        def configured(self):
            return self.ready

        def version(self):
            self.operations.append("offline_version")
            return "0.1.7"

        def save_credentials(self, app_id, key):
            self.operations.append("save_local_fixture")
            self.ready = True

        def token_signature(self):
            return (self.revision, 128)

        def run_json(self, arguments):
            if arguments == ["login", "--background"]:
                self.operations.append("background_login_fixture")
                self.revision = 1
                return {"success": True, "clickableUrl": "https://music.163.com/?local-onboarding-example"}
            assert arguments == ["login", "--check"]
            self.operations.append("authoritative_check_fixture")
            return {"success": self.revision == 2}

        def run_text(self, arguments):
            assert arguments == ["commands"]
            self.operations.append("discover_commands_fixture")
            return "not displayed"

        def manifest(self):
            return {}, [{"command": ["playlist", "create"], "description": "本地验收替身",
                         "parameters": [{"name": "playlistName", "in": "body", "type": "string"}]}]

    with tempfile.TemporaryDirectory(prefix="organizer-flow-") as folder:
        temporary = Path(folder).resolve()
        assert temporary.is_relative_to(Path(tempfile.gettempdir()).resolve())
        cli = LocalCli()
        controller = Organizer(temporary, cli=cli, data_dir=temporary / "fixture-cache",
                               qr_renderer=lambda _, executable, url: render_authorization_qr(PROJECT, executable, url))
        root = tk.Tk()
        root.withdraw()
        real_toplevel = tk.Toplevel

        def hidden_toplevel(*args, **kwargs):
            window = real_toplevel(*args, **kwargs)
            window.withdraw()
            return window

        def spin_until(predicate):
            deadline = time.monotonic() + 10
            while True:
                root.update()
                if predicate():
                    return
                assert time.monotonic() < deadline, "Local GUI onboarding did not advance."
                time.sleep(0.02)

        window = None
        try:
            with patch("netease_organizer.ui.tk.Toplevel", side_effect=hidden_toplevel):
                window = OrganizerWindow(root, controller, OrganizerError)
                spin_until(lambda: "offline_version" in cli.operations and not window._busy)
                window._app_id.set("local-fixture-app")
                window._private_key.set("LOCAL-FIXTURE-PRIVATE-KEY")
                window._save_credentials()
                spin_until(lambda: window._qr_image is not None and not window._busy)
                pixels = [window._qr_image.width(), window._qr_image.height()]
                assert max(pixels) <= 320 and window._probe_deadline is not None
                assert window._private_key.get() == ""
                cli.revision = 2
                if window._probe_after_id is not None:
                    root.after_cancel(window._probe_after_id)
                    window._probe_after_id = None
                window._probe_authorization_once()
                spin_until(lambda: "discover_commands_fixture" in cli.operations and not window._busy)
                assert cli.operations == ["offline_version", "save_local_fixture", "background_login_fixture",
                                          "authoritative_check_fixture", "discover_commands_fixture"]
                assert window._qr_image is None and window._qr_window is None and window._probe_deadline is None
                public_log = window._log.get("1.0", "end")
                assert "LOCAL-FIXTURE-PRIVATE-KEY" not in public_log and "local-onboarding-example" not in public_log
                assert not controller.execute()["applied_to_account"]
                return {"verified": True, "account_service_simulated": True, "real_tk_threads_and_png": True,
                        "qr_pixels": pixels, "save_to_login_to_check_to_discovery": True,
                        "authorization_image_cleared_after_confirmation": True,
                        "online_authorization_verified": False}
        finally:
            if window is not None:
                window._closed = True
                window._stop_ui_callbacks()
                window._close_qr_window()
            root.destroy()


def library_hashes(data_dir):
    values = {}
    for path in sorted((data_dir / "Library").iterdir()):
        if path.is_file():
            digest = hashlib.sha256()
            with path.open("rb") as stream:
                while block := stream.read(1024 * 1024):
                    digest.update(block)
            values[path.name] = digest.hexdigest()
    return values


def main():
    controller = Organizer(PROJECT)
    before = library_hashes(default_data_dir())
    doctor = controller.doctor()
    prepared = controller.prepare()
    blocked = controller.execute()
    after = library_hashes(default_data_dir())
    assert before == after, "Active client cache changed during verification; rerun after it settles."
    assert doctor["installed"] and doctor["version"] == "0.1.7"
    assert blocked["status"] == "blocked" and not blocked["applied_to_account"]
    result = subprocess.run([controller.cli.node, "-e", "process.stdout.write(require('os').homedir())"],
                            env=controller.cli._environment(), capture_output=True, encoding="utf-8", timeout=10,
                            creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0)
    assert result.returncode == 0 and Path(result.stdout).resolve() == controller.cli.home.resolve()
    root = tk.Tk()
    root.withdraw()
    window = None
    try:
        window = OrganizerWindow(root, controller, OrganizerError)
        root.update_idletasks()
        labels = [button.cget("text") for button in window._buttons]
        assert "选择私钥文件" in labels and "检查执行条件" in labels
        window._start_task("检查接入", controller.doctor)
        # Join the bounded subprocess work while pumping the real Tk event loop.
        deadline = time.monotonic() + 15
        while window._busy and time.monotonic() < deadline:
            root.update()
            time.sleep(0.02)
        assert not window._busy, "GUI worker did not publish its result."
        assert "0.1.7" in window._log.get("1.0", "end")
        gui = {"tk_version": root.tk.call("info", "patchlevel"), "constructed": True,
               "worker_result_delivered": True, "button_labels": labels}
    finally:
        if window is not None:
            window._closed = True
            window._stop_ui_callbacks()
        root.destroy()
    onboarding = verify_onboarding_flow(controller.cli.node)
    plan = json.loads((PROJECT / "artifacts/自动整理清单.json").read_text(encoding="utf-8"))
    report = {"kind": "automatic_organizer_local_verification", "official_cli_version": doctor["version"],
              "credentials_configured": doctor["configured"], "isolated_node_home_verified": True,
              "source_library_unchanged": before == after, "source_library_sha256": after,
              "plan_summary": plan["summary"], "gui": gui,
              "onboarding_flow": onboarding,
              "online_authorization_verified": False, "dynamic_account_schema_verified": False,
              "account_name_write_implemented": True,
              "account_artist_write_implemented": True,
              "account_bulk_write_implemented": False, "account_bulk_write_verified": False,
              "plan_path": prepared["path"]}
    path = controller._write_artifact("organizer-verification.json", json.dumps(report, ensure_ascii=False, indent=2))
    print(json.dumps({"report": str(path), "cache_unchanged": True, "gui_worker_verified": True,
                      "onboarding_flow_simulation_verified": onboarding["verified"],
                      "job_count": plan["summary"]["job_count"], "account_bulk_write_verified": False}, ensure_ascii=False))


if __name__ == "__main__":
    main()
