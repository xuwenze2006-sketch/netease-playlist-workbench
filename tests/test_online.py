"""Official online snapshots must be complete, consistent, and read-only."""

import copy
import json
import unittest
from collections import Counter

from netease_organizer.online import OnlineError, OnlineReader
from netease_organizer.official_cli import CliError
from netease_organizer.runtime import OperationControl, OperationPaused


def encrypted(ident):
    return f"{ident:032x}"


def playlist(ident, count=2, special=0):
    return {"originalId": ident, "id": encrypted(ident), "name": "允许同名歌单",
            "trackCount": count, "specialType": special, "trackUpdateTime": 100,
            "unrequested": "SECRET-RESPONSE"}


def track(ident):
    return {"originalId": ident, "id": encrypted(ident), "name": "歌曲甲",
            "artists": [{"originalId": 77, "id": encrypted(77), "name": "歌手甲"}],
            "album": {"token": "SECRET-RESPONSE"}}


class FakeCli:
    def __init__(self, playlist_count=2, track_count=2):
        self.account = {"originalId": 123, "id": encrypted(123), "nickname": "用户甲", "token": "SECRET-RESPONSE"}
        self.playlists = [playlist(1000 + index) for index in range(playlist_count)]
        self.favorite = playlist(9999, track_count, special=5)
        self.tracks = [track(20000 + index) for index in range(track_count)]
        self.calls = []
        self.counts = Counter()
        self.overrides = {}

    def run_json(self, arguments):
        key = tuple(arguments)
        self.calls.append(key)
        self.counts[key] += 1
        if key in self.overrides:
            value = self.overrides[key]
            response = value(self.counts[key]) if callable(value) else value
        elif arguments == ["user", "info"]:
            response = {"code": 200, "data": self.account}
        elif arguments == ["user", "favorite"]:
            response = {"code": 200, "data": self.favorite}
        elif arguments[:2] == ["playlist", "created"]:
            if arguments[2:4] != ["--limit", "500"] or arguments[4] != "--offset":
                raise AssertionError("Unexpected created paging arguments")
            offset = int(arguments[5])
            response = {"code": 200, "data": {"recordCount": len(self.playlists), "records": self.playlists[offset:offset + 500]}}
        elif arguments[:2] == ["playlist", "get"]:
            if arguments != ["playlist", "get", "--playlistId", encrypted(9999).upper()]:
                raise AssertionError("Unexpected detail arguments")
            response = {"code": 200, "data": {**self.favorite, "creatorId": self.account["id"]}}
        elif arguments[:2] == ["playlist", "tracks"]:
            if arguments[2:6] != ["--playlistId", encrypted(9999).upper(), "--limit", "500"] or arguments[6] != "--offset":
                raise AssertionError("Unexpected tracks paging arguments")
            offset = int(arguments[7])
            response = {"code": 200, "subCode": "OK", "data": [item for item in self.tracks[offset:offset + 500] if item is not None]}
        else:
            raise AssertionError("Reader attempted a command outside the read allowlist")
        return copy.deepcopy(response)


class OnlineReaderTests(unittest.TestCase):
    def test_complete_paging_preserves_same_name_lists_and_outputs_only_whitelist(self):
        cli = FakeCli(playlist_count=501, track_count=501)
        snapshot = OnlineReader(cli, "123").read_snapshot()
        self.assertTrue(snapshot["complete"])
        self.assertEqual(snapshot["account"], {"original_id": "123", "id": "0000000000000000000000000000007B", "nickname": "用户甲"})
        self.assertEqual(len(snapshot["playlists"]), 501)
        self.assertEqual(len(snapshot["liked"]["tracks"]), 501)
        self.assertEqual(snapshot["liked"]["creator_id"], snapshot["account"]["id"])
        self.assertIn(("playlist", "created", "--limit", "500", "--offset", "500"), cli.calls)
        self.assertIn(("playlist", "tracks", "--playlistId", encrypted(9999).upper(), "--limit", "500", "--offset", "500"), cli.calls)
        self.assertEqual(cli.counts[("user", "info")], 2)
        self.assertEqual(cli.counts[("user", "favorite")], 2)
        self.assertNotIn("SECRET", json.dumps(snapshot))
        self.assertTrue(snapshot["overview_complete"])
        self.assertTrue(snapshot["tracks_loaded"])
        self.assertEqual(set(snapshot), {"account", "playlists", "liked", "complete", "overview_complete", "tracks_loaded"})
        self.assertEqual(set(snapshot["liked"]["tracks"][0]), {"id", "original_id", "name", "artists", "metadata_available"})

    def test_wrong_account_stops_before_any_playlist_read(self):
        cli = FakeCli()
        with self.assertRaises(OnlineError) as raised:
            OnlineReader(cli, "124").read_snapshot()
        self.assertEqual(raised.exception.code, 'account_mismatch')
        self.assertEqual(cli.calls, [("user", "info")])

    def test_only_typed_cli_diagnostics_propagate_without_original_message(self):
        typed = CliError('SECRET-RAW-RESPONSE', code='cli_timeout')
        malformed = CliError('SECRET-RAW-RESPONSE')
        malformed.code = ['SECRET']
        forged = RuntimeError('SECRET account_mismatch')
        forged.code = 'account_mismatch'
        for failure, expected in ((typed, 'cli_timeout'), (malformed, 'operation_failed'),
                                  (forged, 'operation_failed')):
            cli = FakeCli()
            def fail(occurrence):
                raise failure
            cli.overrides[('user', 'info')] = fail
            with self.subTest(expected=expected), self.assertRaises(OnlineError) as raised:
                OnlineReader(cli, 123).read_snapshot()
            self.assertEqual(raised.exception.code, expected)
            self.assertEqual(str(raised.exception), '官方在线读取未完成，请检查账号授权和接口状态后重试。')
            self.assertNotIn('SECRET', str(raised.exception))
            self.assertEqual(cli.calls, [('user', 'info')])

    def test_pause_checkpoint_still_escapes_before_any_cli_read(self):
        cli = FakeCli()
        control = OperationControl()
        control.request_pause()
        with self.assertRaises(OperationPaused):
            OnlineReader(cli, 123, control=control).read_snapshot()
        self.assertEqual(cli.calls, [])

    def test_invalid_expected_owner_makes_no_cli_call(self):
        for expected in (None, True, 0, -1, "0", "01", "not-an-id"):
            cli = FakeCli()
            with self.subTest(expected=expected), self.assertRaises(OnlineError):
                OnlineReader(cli, expected).read_snapshot()
            self.assertEqual(cli.calls, [])

    def test_code_and_identifier_types_are_strict(self):
        for code in ("200", 200.0, True, 500):
            cli = FakeCli()
            cli.overrides[("user", "info")] = {"code": code, "data": cli.account}
            with self.subTest(code=code), self.assertRaises(OnlineError):
                OnlineReader(cli, 123).read_snapshot()
        for field, bad in (("originalId", 0), ("originalId", True), ("originalId", "01"), ("originalId", 123.0),
                           ("id", "a" * 31), ("id", "g" * 32), ("nickname", "")):
            cli = FakeCli()
            cli.account[field] = bad
            with self.subTest(field=field, bad=bad), self.assertRaises(OnlineError):
                OnlineReader(cli, 123).read_snapshot()

    def test_created_empty_page_cannot_hide_truncation(self):
        cli = FakeCli(playlist_count=501)
        cli.overrides[("playlist", "created", "--limit", "500", "--offset", "500")] = {
            "code": 200, "data": {"recordCount": 501, "records": []},
        }
        with self.assertRaises(OnlineError):
            OnlineReader(cli, 123).read_snapshot()

    def test_empty_track_page_is_reported_as_missing_membership(self):
        cli = FakeCli(track_count=501)
        key = ("playlist", "tracks", "--playlistId", encrypted(9999).upper(), "--limit", "500", "--offset", "500")
        cli.overrides[key] = {"code": 200, "data": []}
        snapshot = OnlineReader(cli, 123).read_snapshot()
        self.assertFalse(snapshot["complete"])
        self.assertTrue(snapshot["overview_complete"])
        self.assertFalse(snapshot["liked"]["membership_complete"])
        self.assertEqual(snapshot["liked"]["missing_record_count"], 1)

    def test_duplicate_and_conflicting_playlist_ids_are_rejected(self):
        for field in ("id", "originalId"):
            cli = FakeCli()
            cli.playlists[1][field] = cli.playlists[0][field]
            with self.subTest(field=field), self.assertRaises(OnlineError):
                OnlineReader(cli, 123).read_snapshot()

    def test_duplicate_track_ids_across_pages_are_rejected(self):
        for field in ("id", "originalId"):
            cli = FakeCli(track_count=501)
            cli.tracks[500][field] = cli.tracks[0][field]
            with self.subTest(field=field), self.assertRaises(OnlineError):
                OnlineReader(cli, 123).read_snapshot()

    def test_count_changes_during_created_paging_fail(self):
        cli = FakeCli(playlist_count=501)
        cli.overrides[("playlist", "created", "--limit", "500", "--offset", "500")] = {
            "code": 200, "data": {"recordCount": 502, "records": cli.playlists[500:]},
        }
        with self.assertRaises(OnlineError):
            OnlineReader(cli, 123).read_snapshot()

    def test_favorite_detail_must_belong_to_account_and_match_header(self):
        for changes in ({"creatorId": encrypted(124)}, {"trackCount": 3}, {"id": encrypted(8888)}):
            cli = FakeCli()
            key = ("playlist", "get", "--playlistId", encrypted(9999).upper())
            cli.overrides[key] = {"code": 200, "data": {**cli.favorite, "creatorId": cli.account["id"], **changes}}
            with self.subTest(changes=changes), self.assertRaises(OnlineError):
                OnlineReader(cli, 123).read_snapshot()

    def test_endpoint_specific_timestamps_are_stable_without_cross_endpoint_equality(self):
        cli = FakeCli()
        cli.favorite["trackUpdateTime"] = 0
        catalog_favorite = {**cli.favorite, "trackUpdateTime": 1791044991387}
        cli.playlists.append(catalog_favorite)
        cli.overrides[("playlist", "get", "--playlistId", encrypted(9999).upper())] = {
            "code": 200, "data": {**cli.favorite, "creatorId": cli.account["id"], "trackUpdateTime": 1791044991387},
        }
        snapshot = OnlineReader(cli, 123).read_snapshot()
        self.assertTrue(snapshot["overview_complete"])
        self.assertTrue(snapshot["complete"])
        self.assertEqual(snapshot["liked"]["track_update_time"], 1791044991387)

    def test_detail_timestamp_change_is_rejected_even_with_favorite_zero_placeholder(self):
        cli = FakeCli()
        cli.favorite["trackUpdateTime"] = 0
        cli.overrides[("playlist", "get", "--playlistId", encrypted(9999).upper())] = lambda occurrence: {
            "code": 200, "data": {**cli.favorite, "creatorId": cli.account["id"], "trackUpdateTime": 100 + occurrence},
        }
        with self.assertRaises(OnlineError):
            OnlineReader(cli, 123).read_snapshot()

    def test_favorite_post_read_changes_are_rejected(self):
        for field, changed in (("trackCount", 3), ("trackUpdateTime", 101), ("id", encrypted(8888))):
            cli = FakeCli()
            cli.overrides[("user", "favorite")] = lambda occurrence: {
                "code": 200, "data": {**cli.favorite, **({field: changed} if occurrence == 2 else {})},
            }
            with self.subTest(field=field), self.assertRaises(OnlineError):
                OnlineReader(cli, 123).read_snapshot()

    def test_account_switch_after_read_is_rejected(self):
        cli = FakeCli()
        cli.overrides[("user", "info")] = lambda occurrence: {
            "code": 200, "data": {**cli.account, **({"originalId": 124} if occurrence == 2 else {})},
        }
        with self.assertRaises(OnlineError):
            OnlineReader(cli, 123).read_snapshot()

    def test_malformed_track_or_artist_metadata_is_rejected(self):
        for changes in ({"name": ""}, {"artists": None}, {"artists": [{"id": encrypted(77), "name": "歌手甲"}]}):
            cli = FakeCli()
            cli.tracks[0].update(changes)
            with self.subTest(changes=changes), self.assertRaises(OnlineError):
                OnlineReader(cli, 123).read_snapshot()

    def test_filtered_slots_use_fixed_offsets_and_missing_metadata_is_honest_partial(self):
        cli = FakeCli(track_count=2090)
        for index in (510, 1010, 1510, 1511, 2010):
            cli.tracks[index] = None
        for index in range(8):
            cli.tracks[index]["artists"] = []
        snapshot = OnlineReader(cli, 123).read_snapshot()
        liked = snapshot["liked"]
        self.assertTrue(snapshot["overview_complete"])
        self.assertFalse(snapshot["complete"])
        self.assertEqual(len(liked["tracks"]), 2085)
        self.assertEqual(liked["missing_record_count"], 5)
        self.assertFalse(liked["membership_complete"])
        self.assertFalse(liked["metadata_complete"])
        self.assertEqual(liked["missing_metadata_track_ids"], [str(20000 + index) for index in range(8)])
        self.assertEqual(liked["tracks"][0]["artists"], [])
        self.assertFalse(liked["tracks"][0]["metadata_available"])
        offsets = [call[-1] for call in cli.calls if call[:2] == ("playlist", "tracks")]
        self.assertEqual(offsets, ["0", "500", "1000", "1500", "2000"])

    def test_full_artists_can_restore_metadata_when_display_artists_are_empty(self):
        cli = FakeCli()
        cli.tracks[0]["fullArtists"] = cli.tracks[0]["artists"]
        cli.tracks[0]["artists"] = []
        snapshot = OnlineReader(cli, 123).read_snapshot()
        self.assertTrue(snapshot["complete"])
        self.assertTrue(snapshot["liked"]["tracks"][0]["metadata_available"])

    def test_full_artist_metadata_is_used_when_available(self):
        cli = FakeCli()
        cli.tracks[0]["fullArtists"] = [*cli.tracks[0]["artists"], {"id": encrypted(78), "originalId": 78, "name": "歌手乙"}]
        snapshot = OnlineReader(cli, 123).read_snapshot()
        self.assertEqual([artist["name"] for artist in snapshot["liked"]["tracks"][0]["artists"]], ["歌手甲", "歌手乙"])

    def test_resource_limits_fail_before_excessive_reading(self):
        cli = FakeCli()
        cli.favorite["trackCount"] = 10001
        with self.assertRaises(OnlineError):
            OnlineReader(cli, 123).read_snapshot()
        cli = FakeCli()
        cli.overrides[("playlist", "created", "--limit", "500", "--offset", "0")] = {
            "code": 200, "data": {"recordCount": 1001, "records": cli.playlists},
        }
        with self.assertRaises(OnlineError):
            OnlineReader(cli, 123).read_snapshot()

    def test_cli_error_does_not_expose_original_response(self):
        cli = FakeCli()
        def fail(occurrence):
            raise RuntimeError("SECRET-RAW-RESPONSE")
        cli.overrides[("user", "info")] = fail
        with self.assertRaises(OnlineError) as raised:
            OnlineReader(cli, 123).read_snapshot()
        self.assertNotIn("SECRET", str(raised.exception))

    def test_final_detail_and_catalog_changes_are_rejected(self):
        for kind in ("detail", "catalog"):
            cli = FakeCli()
            if kind == "detail":
                key = ("playlist", "get", "--playlistId", encrypted(9999).upper())
                cli.overrides[key] = lambda occurrence: {
                    "code": 200, "data": {**cli.favorite, "creatorId": cli.account["id"],
                    **({"trackUpdateTime": 101} if occurrence == 2 else {})},
                }
            else:
                key = ("playlist", "created", "--limit", "500", "--offset", "0")
                def changed_catalog(occurrence):
                    records = copy.deepcopy(cli.playlists)
                    if occurrence == 2:
                        records[0]["name"] = "读取过程中更改的名称"
                    return {"code": 200, "data": {"recordCount": 2, "records": records}}
                cli.overrides[key] = changed_catalog
            with self.subTest(kind=kind), self.assertRaises(OnlineError):
                OnlineReader(cli, 123).read_snapshot()

    def test_same_raw_account_with_changed_encrypted_identity_is_rejected(self):
        cli = FakeCli()
        cli.overrides[("user", "info")] = lambda occurrence: {
            "code": 200, "data": {**cli.account, **({"id": encrypted(124)} if occurrence == 2 else {})},
        }
        with self.assertRaises(OnlineError):
            OnlineReader(cli, 123).read_snapshot()

    def test_empty_membership_is_complete_when_metadata_confirms_zero_twice(self):
        cli = FakeCli(playlist_count=0, track_count=0)
        snapshot = OnlineReader(cli, 123).read_snapshot()
        self.assertTrue(snapshot["complete"])
        self.assertEqual(snapshot["liked"]["tracks"], [])
        self.assertEqual(snapshot["liked"]["missing_record_count"], 0)
        self.assertFalse(any(call[:2] == ("playlist", "tracks") for call in cli.calls))

    def test_mutation_command_is_rejected_before_cli_boundary(self):
        cli = FakeCli()
        reader = OnlineReader(cli, 123)
        for method in ("create", "delete", "update", "subscribe", "reorder"):
            with self.subTest(method=method), self.assertRaises(OnlineError):
                reader._read(["playlist", method], dict)
        with self.assertRaises(OnlineError):
            reader._read(["playlist", "get", "--playlistId", encrypted(9999).upper(), "--method", "delete"], dict)
        self.assertEqual(cli.calls, [])

    def test_directory_only_snapshot_never_fetches_or_claims_complete_tracks(self):
        cli = FakeCli()
        # Deliberately unusable song DTOs cannot block independent name reads.
        cli.tracks = [{"id": "INVALID-ID"}, {"id": "INVALID-ID"}]
        snapshot = OnlineReader(cli, 123).read_snapshot(include_tracks=False)
        self.assertTrue(snapshot["overview_complete"])
        self.assertFalse(snapshot["complete"])
        self.assertFalse(snapshot["tracks_loaded"])
        self.assertEqual(snapshot["liked"]["tracks"], [])
        self.assertFalse(snapshot["liked"]["membership_complete"])
        self.assertFalse(snapshot["liked"]["metadata_complete"])
        self.assertEqual(snapshot["liked"]["missing_record_count"], 2)
        self.assertFalse(any(call[:2] == ("playlist", "tracks") for call in cli.calls))
        self.assertEqual(cli.counts[("user", "info")], 2)
        self.assertEqual(cli.counts[("user", "favorite")], 2)

    def test_directory_only_snapshot_still_rejects_account_switch(self):
        cli = FakeCli()
        cli.overrides[("user", "info")] = lambda occurrence: {
            "code": 200, "data": {**cli.account, **({"originalId": 124} if occurrence == 2 else {})},
        }
        with self.assertRaises(OnlineError):
            OnlineReader(cli, 123).read_snapshot(include_tracks=False)
        self.assertFalse(any(call[:2] == ("playlist", "tracks") for call in cli.calls))

    def test_directory_only_empty_playlist_does_not_claim_tracks_were_loaded(self):
        snapshot = OnlineReader(FakeCli(track_count=0), 123).read_snapshot(include_tracks=False)
        self.assertFalse(snapshot["complete"])
        self.assertFalse(snapshot["liked"]["metadata_complete"])
        self.assertFalse(snapshot["liked"]["membership_complete"])

    def test_known_artist_placeholder_is_not_mapped_and_marks_song_metadata_missing(self):
        placeholder = {"originalId": 0, "id": None, "name": "热爱音乐的旅行家"}
        for source in ("artists", "fullArtists"):
            cli = FakeCli()
            cli.tracks[0][source] = [*cli.tracks[0]["artists"], placeholder]
            snapshot = OnlineReader(cli, 123).read_snapshot()
            song = snapshot["liked"]["tracks"][0]
            with self.subTest(source=source):
                self.assertTrue(snapshot["overview_complete"])
                self.assertTrue(snapshot["liked"]["membership_complete"])
                self.assertFalse(snapshot["complete"])
                self.assertFalse(song["metadata_available"])
                self.assertEqual([artist["original_id"] for artist in song["artists"]], ["77"])
                self.assertEqual(snapshot["liked"]["missing_metadata_track_ids"], ["20000"])

    def test_placeholder_in_display_artists_remains_missing_even_with_valid_full_artists(self):
        cli = FakeCli()
        valid_artist = cli.tracks[0]["artists"][0]
        cli.tracks[0]["artists"] = [{"originalId": 0, "id": None, "name": "热爱音乐的旅行家"}]
        cli.tracks[0]["fullArtists"] = [valid_artist]
        snapshot = OnlineReader(cli, 123).read_snapshot()
        self.assertEqual(snapshot["liked"]["tracks"][0]["artists"][0]["original_id"], "77")
        self.assertFalse(snapshot["liked"]["tracks"][0]["metadata_available"])

    def test_other_invalid_artist_shapes_still_fail_safely(self):
        for artist in (
            {"originalId": "0", "id": None, "name": "热爱音乐的旅行家"},
            {"originalId": False, "id": None, "name": "热爱音乐的旅行家"},
            {"originalId": 0, "name": "热爱音乐的旅行家"},
            {"originalId": 0, "id": None, "name": ""},
            {"originalId": 0, "id": encrypted(77), "name": "热爱音乐的旅行家"},
            {"originalId": 77, "id": None, "name": "SECRET-ARTIST"},
        ):
            cli = FakeCli()
            cli.tracks[0]["artists"] = [artist]
            with self.subTest(artist=artist), self.assertRaises(OnlineError) as raised:
                OnlineReader(cli, 123).read_snapshot()
            self.assertNotIn("SECRET", str(raised.exception))

    def test_valid_artist_identity_conflict_between_display_and_full_still_fails(self):
        cli = FakeCli()
        cli.tracks[0]["fullArtists"] = [{"originalId": 78, "id": encrypted(77), "name": "歌手甲"}]
        with self.assertRaises(OnlineError):
            OnlineReader(cli, 123).read_snapshot()


if __name__ == "__main__":
    unittest.main()
