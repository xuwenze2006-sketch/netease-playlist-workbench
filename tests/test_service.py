import json
import tempfile
import unittest
from pathlib import Path

from tests.fixtures import make_cache
from netease_bridge.service import BridgeService


class ServiceTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        self.db = make_cache(self.root)
        self.service = BridgeService(self.root)

    def tearDown(self):
        self.db.close()
        self.directory.cleanup()

    def call(self, name, args=None):
        return self.service.call_tool(name, args or {})

    def test_status_reports_cache_and_explicitly_unavailable_writeback(self):
        data = self.call("desktop_status")["structuredContent"]
        self.assertEqual(data["playlist_counts"], {"all": 4, "owned": 3, "collected": 1})
        self.assertFalse(data["online_account_verified"])
        self.assertFalse(data["capabilities"]["account_writeback"])

    def test_playlist_pagination_and_owned_filter(self):
        data = self.call("list_playlists", {"kind": "owned", "offset": 1, "limit": 1})["structuredContent"]
        self.assertEqual(data["total"], 3)
        self.assertEqual([p["id"] for p in data["items"]], ["101"])
        self.assertEqual(data["next_offset"], 2)

    def test_song_pages_preserve_playlist_order(self):
        data = self.call("get_playlist_tracks", {"playlist_id": "100", "offset": 1, "limit": 2})["structuredContent"]
        self.assertEqual([t["id"] for t in data["items"]], ["2", "3"])
        self.assertIsNone(data["next_offset"])

    def test_missing_membership_returns_tool_error(self):
        result = self.call("get_playlist_tracks", {"playlist_id": "102"})
        self.assertTrue(result["isError"])
        self.assertEqual(result["structuredContent"]["error"]["code"], "membership_unavailable")

    def test_preview_uses_exact_artist_and_includes_collaboration(self):
        data = self.call("preview_artist_playlist", {"artist_name": "歌手甲"})["structuredContent"]
        self.assertEqual(data["track_ids"], ["1", "3"])
        self.assertEqual(data["track_count"], 2)
        self.assertFalse(data["applied_to_account"])
        self.assertFalse(data["online_account_verified"])

    def test_incomplete_source_cannot_be_used_for_a_complete_artist_preview(self):
        result = self.call("preview_artist_playlist", {"playlist_id": "101", "artist_id": "10"})
        self.assertTrue(result["isError"])
        self.assertEqual(result["structuredContent"]["error"]["code"], "incomplete_source")

    def test_ambiguous_artist_name_requires_id(self):
        raw = self.db.execute("SELECT jsonStr FROM dbTrack WHERE id='2'").fetchone()[0]
        song = json.loads(raw)
        song["ar"][0]["name"] = "歌手甲"
        self.db.execute("UPDATE dbTrack SET jsonStr=? WHERE id='2'", (json.dumps(song),))
        self.db.commit()
        result = self.call("preview_artist_playlist", {"artist_name": "歌手甲"})
        self.assertTrue(result["isError"])
        self.assertEqual(result["structuredContent"]["error"]["code"], "ambiguous_artist")
        data = self.call("preview_artist_playlist", {"artist_id": "10"})["structuredContent"]
        self.assertEqual(data["track_ids"], ["1", "3"])

    def test_search_matches_artist_but_stays_in_source_playlist(self):
        data = self.call("search_tracks", {"query": "歌手甲"})["structuredContent"]
        self.assertEqual([t["id"] for t in data["items"]], ["1", "3"])

    def test_unknown_metadata_remains_visible_in_song_page(self):
        self.db.execute("DELETE FROM dbTrack WHERE id='2'")
        self.db.commit()
        data = self.call("get_playlist_tracks", {"playlist_id": "100"})["structuredContent"]
        self.assertEqual([t["id"] for t in data["items"]], ["1", "2", "3"])
        self.assertFalse(data["items"][1]["metadata_available"])

    def test_rejects_bad_pagination_and_unknown_arguments(self):
        for args in [{"offset": -1}, {"limit": 0}, {"limit": 201}, {"limit": True}, {"command": "write"}]:
            with self.subTest(args=args), self.assertRaises(ValueError):
                self.call("list_playlists", args)

    def test_does_not_expose_an_account_mutation_tool(self):
        tools = self.service.list_tools()
        self.assertEqual({t["name"] for t in tools}, {"desktop_status", "list_playlists", "get_playlist_tracks", "search_tracks", "preview_artist_playlist"})
        self.assertTrue(all(t["annotations"]["readOnlyHint"] for t in tools))


if __name__ == "__main__":
    unittest.main()
