import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tests.fixtures import make_cache, put_response
from netease_bridge.cache import CacheReader, CacheError


class CacheTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        self.db = make_cache(self.root)

    def tearDown(self):
        self.db.close()
        self.directory.cleanup()

    def test_reads_owned_and_collected_with_order_and_normalized_tracks(self):
        snapshot = CacheReader(self.root).load()
        self.assertEqual(snapshot["owner_id"], "7")
        self.assertEqual([p["id"] for p in snapshot["playlists"]], ["100", "101", "102", "103"])
        self.assertEqual([p["owned"] for p in snapshot["playlists"]], [True, True, True, False])
        self.assertEqual(snapshot["playlists"][0]["members"], ["1", "2", "3"])
        self.assertTrue(snapshot["playlists"][0]["snapshot_complete"])
        self.assertEqual(snapshot["tracks"]["2"]["artists"][0]["name"], "Artist B")
        self.assertEqual(snapshot["tracks"]["2"]["duration_ms"], 210000)

    def test_equal_count_with_older_timestamp_is_stale(self):
        playlist = CacheReader(self.root).load()["playlists"][1]
        self.assertFalse(playlist["snapshot_complete"])
        self.assertIn("membership_timestamp_mismatch", playlist["issues"])

    def test_missing_membership_is_not_a_verified_empty_playlist(self):
        playlist = CacheReader(self.root).load()["playlists"][2]
        self.assertFalse(playlist["membership_available"])
        self.assertFalse(playlist["snapshot_complete"])
        self.assertIn("missing_membership", playlist["issues"])

    def test_does_not_output_extra_fields_or_cached_request_bodies(self):
        result = json.dumps(CacheReader(self.root).load())
        self.assertNotIn("SECRET-", result)
        self.assertNotIn("private_extra", result)

    def test_reader_preserves_database_bytes(self):
        path = self.root / "Library" / "webdb.dat"
        before = hashlib.sha256(path.read_bytes()).digest()
        CacheReader(self.root).load()
        self.assertEqual(hashlib.sha256(path.read_bytes()).digest(), before)

    def test_bad_json_is_skipped_without_hiding_valid_snapshots(self):
        self.db.execute("INSERT INTO requestCache VALUES(?,?)", ('{"url":"/eapi/user/playlist"}', "{bad json"))
        self.db.commit()
        self.assertEqual(len(CacheReader(self.root).load()["playlists"]), 4)

    def test_newest_detail_uses_track_timestamp_instead_of_insert_order(self):
        put_response(self.db, "/eapi/v6/playlist/detail", {"code": 200, "playlist": {"id": 100, "trackIds": [{"id": 4}], "trackCount": 1, "trackUpdateTime": 100}})
        self.db.commit()
        self.assertEqual(CacheReader(self.root).load()["playlists"][0]["members"], ["1", "2", "3"])

    def test_unknown_track_does_not_disappear_from_membership_count(self):
        self.db.execute("DELETE FROM dbTrack WHERE id='2'")
        self.db.commit()
        playlist = CacheReader(self.root).load()["playlists"][0]
        self.assertEqual(playlist["cached_track_count"], 3)
        self.assertEqual(playlist["resolved_track_count"], 2)
        self.assertFalse(playlist["snapshot_complete"])
        self.assertIn("missing_song_metadata", playlist["issues"])

    def test_missing_source_reports_safe_error(self):
        with self.assertRaises(CacheError):
            CacheReader(self.root / "absent").load()

    def test_wal_data_is_visible_before_checkpoint(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            db = make_cache(root, wal=True)
            try:
                self.assertGreater((root / "Library" / "webdb.dat-wal").stat().st_size, 0)
                self.assertEqual(CacheReader(root).load()["playlists"][0]["members"], ["1", "2", "3"])
            finally:
                db.close()

    def test_reading_closed_wal_cache_creates_no_client_files(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            db = make_cache(root, wal=True)
            db.close()
            folder = root / "Library"
            before = {p.name: p.read_bytes() for p in folder.iterdir()}
            CacheReader(root).load()
            self.assertEqual({p.name: p.read_bytes() for p in folder.iterdir()}, before)

    def test_malformed_detail_track_field_does_not_abort_other_records(self):
        for field in (7, True, {"id": 1}, "bad"):
            with self.subTest(field=field):
                put_response(self.db, "/eapi/v6/playlist/detail", {"code": 200, "playlist": {
                    "id": 100, "trackIds": [{"id": 1}, {"id": 2}, {"id": 3}],
                    "trackUpdateTime": 200, "tracks": field,
                }})
                self.db.commit()
                snapshot = CacheReader(self.root).load()
                self.assertEqual(snapshot["playlists"][0]["members"], ["1", "2", "3"])
                self.assertTrue(snapshot["playlists"][0]["snapshot_complete"])

    def test_incomplete_song_metadata_blocks_complete_artist_draft(self):
        from netease_bridge.service import BridgeService
        self.db.execute("UPDATE dbTrack SET jsonStr=? WHERE id='1'", (
            json.dumps({"id": 1, "name": "一首歌", "artists": {"id": 10, "name": "歌手甲"}}),
        ))
        self.db.commit()
        snapshot = CacheReader(self.root).load()
        self.assertFalse(snapshot["playlists"][0]["snapshot_complete"])
        self.assertEqual(snapshot["playlists"][0]["resolved_track_count"], 2)
        result = BridgeService(self.root).call_tool("preview_artist_playlist", {"artist_id": "10"})
        self.assertTrue(result["isError"])

    def test_bad_overview_record_is_reported_as_incomplete(self):
        snapshot = CacheReader(self.root).load()
        # Preserve the owner record but include a fifth, unusable record.
        overview = [{"id": int(p["id"]), "name": p["name"], "userId": 7,
                     "specialType": p["special_type"], "subscribed": False,
                     "trackCount": p["overview_track_count"], "trackUpdateTime": p["overview_update_ms"]}
                    for p in snapshot["playlists"]]
        put_response(self.db, "/eapi/user/playlist", {"code": 200, "more": False, "playlist": overview + [{"id": "bad"}]})
        self.db.commit()
        source = CacheReader(self.root).load()["source"]
        self.assertFalse(source["overview_complete"])
        self.assertEqual(source["invalid_overview_records"], 1)

    def test_continuously_changing_source_returns_safe_error(self):
        signatures = [((i, 0, 1, 1, 1), None, None) for i in range(20)]
        with patch("netease_bridge.cache.source_signature", side_effect=signatures):
            with self.assertRaises(CacheError):
                CacheReader(self.root).load()

    def test_nonempty_rollback_journal_is_not_ignored(self):
        (self.root / "Library" / "webdb.dat-journal").write_bytes(b"busy")
        with self.assertRaises(CacheError):
            CacheReader(self.root).load()


if __name__ == "__main__":
    unittest.main()
