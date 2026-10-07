import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from tests.fixtures import make_cache


class CliTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name) / "CloudMusic"
        self.db = make_cache(self.root)

    def tearDown(self):
        self.db.close()
        self.directory.cleanup()

    def run_bridge(self, *arguments, input=None):
        return subprocess.run([sys.executable, "-X", "utf8", "-m", "netease_bridge", "--data-dir", str(self.root), *arguments], input=input, capture_output=True, encoding="utf-8", timeout=15)

    def test_status_cli_reports_counts_without_credentials(self):
        result = self.run_bridge("status")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["playlist_counts"]["all"], 4)
        self.assertNotIn("SECRET-", result.stdout + result.stderr)

    def test_unicode_artist_preview_writes_local_json_only(self):
        output = self.root.parent / "输出" / "精选.json"
        result = self.run_bridge("--output", str(output), "preview-artist", "--artist-name", "歌手甲")
        self.assertEqual(result.returncode, 0, result.stderr)
        preview = json.loads(output.read_text(encoding="utf-8"))
        self.assertEqual(preview["track_ids"], ["1", "3"])
        self.assertFalse(preview["applied_to_account"])

    def test_output_cannot_overwrite_any_client_data(self):
        output = self.root / "Library" / "webdb.dat"
        before = output.read_bytes()
        result = self.run_bridge("--output", str(output), "status")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(output.read_bytes(), before)

    def test_missing_source_is_json_error_with_nonzero_exit(self):
        result = self.run_bridge("tracks", "--playlist-id", "102")
        self.assertEqual(result.returncode, 1)
        self.assertEqual(json.loads(result.stdout)["error"]["code"], "membership_unavailable")
        self.assertNotIn("Traceback", result.stderr)

    def test_mcp_child_process_works_with_real_service(self):
        messages = [
            {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-11-25", "capabilities": {}, "clientInfo": {"name": "integration-test", "version": "1"}}},
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
            {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "get_playlist_tracks", "arguments": {"playlist_id": "100", "offset": 1, "limit": 2}}},
        ]
        result = self.run_bridge("mcp", input="\n".join(json.dumps(message, ensure_ascii=False) for message in messages) + "\n")
        self.assertEqual(result.returncode, 0, result.stderr)
        replies = [json.loads(line) for line in result.stdout.splitlines()]
        self.assertEqual([reply["id"] for reply in replies], [1, 2, 3])
        self.assertEqual(len(replies[1]["result"]["tools"]), 5)
        self.assertEqual([song["id"] for song in replies[2]["result"]["structuredContent"]["items"]], ["2", "3"])

    def test_stdout_has_only_json_in_mcp_mode_after_bad_request(self):
        result = self.run_bridge("mcp", input="not-json\n" + json.dumps({"jsonrpc": "2.0", "id": 9, "method": "ping"}) + "\n")
        self.assertEqual(result.returncode, 0, result.stderr)
        replies = [json.loads(line) for line in result.stdout.splitlines()]
        self.assertEqual(replies[0]["error"]["code"], -32700)
        self.assertEqual(replies[1]["id"], 9)

    def test_invalid_utf8_request_does_not_kill_mcp_process(self):
        payload = b"\xff\n" + json.dumps({"jsonrpc": "2.0", "id": 9, "method": "ping"}).encode() + b"\n"
        result = subprocess.run([sys.executable, "-X", "utf8", "-m", "netease_bridge", "--data-dir", str(self.root), "mcp"],
                                input=payload, capture_output=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)
        replies = [json.loads(line) for line in result.stdout.splitlines()]
        self.assertEqual(replies[0]["error"]["code"], -32700)
        self.assertEqual(replies[1]["id"], 9)


if __name__ == "__main__":
    unittest.main()
