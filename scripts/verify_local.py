"""Verify the bridge against this computer's cache; write only local artifacts."""

import hashlib
import json
import subprocess
import sys
from datetime import datetime
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT))

from netease_bridge.service import BridgeService


def fingerprints(folder):
    result = {}
    for path in sorted(folder.iterdir()):
        if path.is_file():
            with path.open("rb") as stream:
                result[path.name] = hashlib.file_digest(stream, "sha256").hexdigest()
    return result


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    service = BridgeService()
    before = fingerprints(service.data_dir / "Library")

    def call(name, arguments):
        response = service.call_tool(name, arguments)
        if response.get("isError"):
            raise RuntimeError(response["structuredContent"]["error"]["message"])
        return response["structuredContent"]

    status = call("desktop_status", {})
    playlists = call("list_playlists", {"limit": 200})
    liked = next(p for p in playlists["items"] if p["owned"] and p["special_type"] == 5)
    page = call("get_playlist_tracks", {"playlist_id": liked["id"], "limit": 200})
    artist_search = call("search_tracks", {"query": "林俊杰", "limit": 200})
    draft_response = service.call_tool("preview_artist_playlist", {"artist_name": "林俊杰"})
    draft = draft_response["structuredContent"]
    if draft_response.get("isError"):
        if draft["error"]["code"] != "incomplete_source":
            raise RuntimeError("歌手精选未通过验收。")
        draft = {"kind": "local_playlist_draft_unavailable", "name": "林俊杰 · 收藏精选",
                 **draft, "source_playlist": liked,
                 "note": "此前草案的完整性判断已被本次验证纠正；请使用搜索结果查看已知匹配，不能视为完整精选。"}
    complete_playlist = next(p for p in playlists["items"] if p["snapshot_complete"] and p["cached_track_count"] > 0)
    complete_tracks = call("get_playlist_tracks", {"playlist_id": complete_playlist["id"], "limit": 200})
    artist_id = complete_tracks["items"][0]["artists"][0]["id"]
    verified_preview = call("preview_artist_playlist", {"playlist_id": complete_playlist["id"], "artist_id": artist_id})
    requests = [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-11-25", "capabilities": {}, "clientInfo": {"name": "local-verification", "version": "1"}}},
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
        {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "desktop_status", "arguments": {}}},
    ]
    child = subprocess.run([sys.executable, "-X", "utf8", str(PROJECT / "run_bridge.py"), "mcp"],
                           input="\n".join(json.dumps(r) for r in requests) + "\n",
                           capture_output=True, encoding="utf-8", timeout=45)
    if child.returncode != 0 or child.stderr:
        raise RuntimeError("MCP 子进程验收失败。")
    replies = [json.loads(line) for line in child.stdout.splitlines()]
    if [reply["id"] for reply in replies] != [1, 2, 3] or any("error" in reply for reply in replies):
        raise RuntimeError("MCP 返回未通过验收。")
    if replies[2]["result"]["structuredContent"] != status:
        raise RuntimeError("MCP 和直接调用的缓存状态不一致。")
    after = fingerprints(service.data_dir / "Library")
    if before != after:
        raise RuntimeError("验收期间源目录内容发生变化，不能确认源目录保持不变。")
    report = {
        "verified_at": datetime.now().astimezone().isoformat(),
        "status": status, "liked_playlist": liked,
        "first_page_count": len(page["items"]),
        "artist_search_match_count": artist_search["total"],
        "liked_artist_preview_available": not draft_response.get("isError", False),
        "liked_artist_preview_error": draft.get("error"),
        "complete_playlist_preview": {"playlist_id": complete_playlist["id"], "artist_id": artist_id,
                                      "track_count": verified_preview["track_count"], "applied_to_account": False},
        "mcp_child_process": {"exit_code": child.returncode, "response_ids": [r["id"] for r in replies], "tool_count": len(replies[1]["result"]["tools"])},
        "source_library_sha256_before": before, "source_library_sha256_after": after,
        "source_library_files_and_contents_unchanged": True,
        "online_changes_applied": False, "global_mcp_configuration_changed": False,
    }
    artifacts = PROJECT / "artifacts"
    artifacts.mkdir(exist_ok=True)
    for name, data in (("bridge-verification.json", report), ("林俊杰精选草案.json", draft)):
        (artifacts / name).write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"playlist_counts": status["playlist_counts"], "liked_tracks": liked["cached_track_count"],
                      "liked_resolved_tracks": liked["resolved_track_count"],
                      "liked_snapshot_complete": liked["snapshot_complete"],
                      "artist_search_match_count": artist_search["total"],
                      "liked_artist_preview_available": report["liked_artist_preview_available"],
                      "complete_playlist_preview_count": verified_preview["track_count"],
                      "mcp_tools": report["mcp_child_process"]["tool_count"], "source_library_unchanged": True}, ensure_ascii=False))


if __name__ == "__main__":
    main()
