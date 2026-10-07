"""Single owned-playlist reads are stable, pausable, and strictly read-only."""

import json
import unittest

from netease_organizer.online import OnlineError, OnlineReader
from netease_organizer.official_cli import CliError
from netease_organizer.runtime import OperationControl, OperationPaused
from tests.test_online import FakeCli, encrypted, playlist, track


class PlaylistCli(FakeCli):
    def __init__(self, count=2):
        super().__init__(track_count=count)
        self.header = {**playlist(1000, count=count), "creatorId": self.account["id"]}
        self.get_key = ("playlist", "get", "--playlistId", encrypted(1000).upper())
        self.overrides[self.get_key] = lambda occurrence: {"code": 200, "data": self.header}
        for offset in range(0, count, 500):
            self.overrides[self.page_key(offset)] = lambda occurrence, offset=offset: {
                "code": 200, "data": [row for row in self.tracks[offset:offset + 500] if row is not None]}

    @staticmethod
    def page_key(offset):
        return ("playlist", "tracks", "--playlistId", encrypted(1000).upper(),
                "--limit", "500", "--offset", str(offset))


class OnlinePlaylistTests(unittest.TestCase):
    def read(self, cli, *, control=None, progress=None, **arguments):
        values = {"playlist_id": encrypted(1000), "original_playlist_id": "1000",
                  "expected_account_id": encrypted(123), "progress": progress}
        values.update(arguments)
        return OnlineReader(cli, "123", control=control).read_playlist(**values)

    def test_owned_normal_playlist_uses_fresh_header_and_only_required_reads(self):
        cli = PlaylistCli()
        cli.header.update(name="新的名称 · Unicode", specialType=50, trackUpdateTime=200)
        result = self.read(cli)
        self.assertTrue(result["complete"])
        self.assertEqual(set(result), {"account", "playlist", "complete"})
        detail = result["playlist"]
        self.assertEqual((detail["name"], detail["special_type"], detail["track_update_time"]),
                         ("新的名称 · Unicode", 50, 200))
        self.assertEqual((detail["id"], detail["original_id"], detail["creator_id"]),
                         (encrypted(1000).upper(), "1000", encrypted(123).upper()))
        self.assertEqual([row["original_id"] for row in detail["tracks"]], ["20000", "20001"])
        self.assertTrue(detail["membership_complete"])
        self.assertTrue(detail["metadata_complete"])
        self.assertEqual(detail["missing_record_count"], 0)
        self.assertEqual(cli.calls, [("user", "info"), cli.get_key, cli.page_key(0),
                                     cli.get_key, ("user", "info")])
        self.assertNotIn("SECRET", json.dumps(result, ensure_ascii=False))
        self.assertEqual(set(detail["tracks"][0]),
                         {"id", "original_id", "name", "artists", "metadata_available"})

    def test_wrong_original_or_encrypted_account_stops_at_first_user_info(self):
        for changes in ({"originalId": 124}, {"id": encrypted(124)}):
            cli = PlaylistCli()
            cli.account.update(changes)
            with self.subTest(changes=changes), self.assertRaises(OnlineError) as raised:
                self.read(cli)
            self.assertEqual(raised.exception.code, "account_mismatch")
            self.assertEqual(cli.calls, [("user", "info")])

    def test_foreign_owner_or_wrong_playlist_double_id_never_reads_tracks(self):
        for changes in ({"creatorId": encrypted(124)}, {"id": encrypted(1001)},
                        {"originalId": 1001}, {"trackCount": 10001}):
            cli = PlaylistCli()
            cli.header.update(changes)
            with self.subTest(changes=changes), self.assertRaises(OnlineError):
                self.read(cli)
            self.assertEqual(cli.calls, [("user", "info"), cli.get_key])

    def test_filtered_pages_keep_fixed_slots_and_report_missing_members(self):
        cli = PlaylistCli(501)
        cli.tracks[200] = None
        events = []
        result = self.read(cli, progress=events.append)
        detail = result["playlist"]
        self.assertFalse(result["complete"])
        self.assertEqual((detail["track_count"], len(detail["tracks"]), detail["missing_record_count"]),
                         (501, 500, 1))
        self.assertFalse(detail["membership_complete"])
        self.assertTrue(detail["metadata_complete"])
        self.assertEqual([call[-1] for call in cli.calls if call[:2] == ("playlist", "tracks")],
                         ["0", "500"])
        self.assertEqual(events, [{"stage": "reading", "completed_count": 500, "total_count": 501},
                                  {"stage": "reading", "completed_count": 501, "total_count": 501}])
        self.assertEqual(detail["tracks"][-1]["original_id"], "20500")

    def test_empty_playlist_is_complete_without_a_tracks_request(self):
        cli = PlaylistCli(0)
        events = []
        result = self.read(cli, progress=events.append)
        self.assertTrue(result["complete"])
        self.assertEqual(result["playlist"]["tracks"], [])
        self.assertEqual(result["playlist"]["missing_record_count"], 0)
        self.assertEqual(events, [])
        self.assertEqual(cli.calls, [("user", "info"), cli.get_key, cli.get_key, ("user", "info")])

    def test_duplicate_encrypted_or_original_track_ids_across_pages_fail(self):
        for field in ("id", "originalId"):
            cli = PlaylistCli(501)
            cli.tracks[-1][field] = cli.tracks[0][field]
            with self.subTest(field=field), self.assertRaises(OnlineError):
                self.read(cli)
            self.assertEqual(cli.calls, [("user", "info"), cli.get_key,
                                         cli.page_key(0), cli.page_key(500)])

    def test_partial_metadata_retains_known_artists_and_missing_song_ids(self):
        cli = PlaylistCli(3)
        cli.tracks[0]["artists"].append({"originalId": 0, "id": None, "name": "云盘占位歌手"})
        cli.tracks[1]["artists"] = []
        result = self.read(cli)
        detail = result["playlist"]
        self.assertFalse(result["complete"])
        self.assertTrue(detail["membership_complete"])
        self.assertFalse(detail["metadata_complete"])
        self.assertEqual(detail["missing_metadata_track_ids"], ["20000", "20001"])
        self.assertEqual([artist["name"] for artist in detail["tracks"][0]["artists"]], ["歌手甲"])
        self.assertFalse(detail["tracks"][0]["metadata_available"])
        self.assertEqual(detail["tracks"][1]["artists"], [])

    def test_each_post_read_header_identity_or_membership_change_fails(self):
        for field, value in (("name", "新名"), ("trackCount", 3), ("specialType", 50),
                             ("trackUpdateTime", 101), ("creatorId", encrypted(124)),
                             ("originalId", 1001), ("id", encrypted(1001))):
            cli = PlaylistCli()
            cli.overrides[cli.get_key] = lambda occurrence, field=field, value=value: {
                "code": 200, "data": {**cli.header, **({field: value} if occurrence == 2 else {})}}
            with self.subTest(field=field), self.assertRaises(OnlineError):
                self.read(cli)
            self.assertEqual(cli.calls, [("user", "info"), cli.get_key, cli.page_key(0), cli.get_key])

    def test_final_original_or_encrypted_account_switch_fails(self):
        for changes in ({"originalId": 124}, {"id": encrypted(124)}):
            cli = PlaylistCli()
            cli.overrides[("user", "info")] = lambda occurrence, changes=changes: {
                "code": 200, "data": {**cli.account, **(changes if occurrence == 2 else {})}}
            with self.subTest(changes=changes), self.assertRaises(OnlineError) as raised:
                self.read(cli)
            self.assertEqual(raised.exception.code, "account_mismatch")
            self.assertEqual(cli.calls[-1], ("user", "info"))
            self.assertEqual(len(cli.calls), 5)

    def test_latest_account_display_name_is_returned_without_identity_change(self):
        cli = PlaylistCli()
        cli.overrides[("user", "info")] = lambda occurrence: {
            "code": 200, "data": {**cli.account, "nickname": "最新昵称" if occurrence == 2 else "旧昵称"}}
        result = self.read(cli)
        self.assertTrue(result["complete"])
        self.assertEqual(result["account"]["nickname"], "最新昵称")

    def test_progress_pause_stops_before_next_page_or_after_get(self):
        for count in (2, 501):
            cli = PlaylistCli(count)
            control = OperationControl()
            events = []
            def pause(event):
                events.append(event)
                control.request_pause()
            with self.subTest(count=count), self.assertRaises(OperationPaused):
                self.read(cli, control=control, progress=pause)
            self.assertEqual(cli.calls, [("user", "info"), cli.get_key, cli.page_key(0)])
            self.assertEqual(len(events), 1)

    def test_pause_before_start_makes_no_cli_call(self):
        cli = PlaylistCli()
        control = OperationControl()
        control.request_pause()
        with self.assertRaises(OperationPaused):
            self.read(cli, control=control)
        self.assertEqual(cli.calls, [])

    def test_pause_during_final_account_request_prevents_publishing_result(self):
        cli = PlaylistCli()
        control = OperationControl()
        def account(occurrence):
            if occurrence == 2:
                control.request_pause()
            return {"code": 200, "data": cli.account}
        cli.overrides[("user", "info")] = account
        with self.assertRaises(OperationPaused):
            self.read(cli, control=control)
        self.assertEqual(cli.calls, [("user", "info"), cli.get_key, cli.page_key(0),
                                     cli.get_key, ("user", "info")])

    def test_invalid_parameters_fail_before_any_cli_request(self):
        invalid = []
        for field in ("playlist_id", "expected_account_id"):
            invalid.extend({field: value} for value in (None, True, 1000, "a"*31, "g"*32, "a"*32+"\n"))
        invalid.extend({"original_playlist_id": value}
                       for value in (None, True, 1000, "0", "01", "-1", "1.0", "1"*21, "1000\n"))
        invalid.append({"progress": False})
        for values in invalid:
            cli = PlaylistCli()
            with self.subTest(values=values), self.assertRaises(OnlineError):
                self.read(cli, **values)
            self.assertEqual(cli.calls, [])

    def test_progress_failure_propagates_without_another_request_or_retry(self):
        cli = PlaylistCli(501)
        failure = RuntimeError("callback failure")
        def fail(event):
            raise failure
        with self.assertRaises(RuntimeError) as raised:
            self.read(cli, progress=fail)
        self.assertIs(raised.exception, failure)
        self.assertEqual(cli.calls, [("user", "info"), cli.get_key, cli.page_key(0)])

    def test_oversized_or_invalid_page_is_not_published_or_retried(self):
        for data in ([track(30000+i) for i in range(501)], {"records": []}):
            cli = PlaylistCli(501)
            cli.overrides[cli.page_key(0)] = {"code": 200, "data": data}
            with self.subTest(kind=type(data).__name__), self.assertRaises(OnlineError):
                self.read(cli)
            self.assertEqual(cli.calls, [("user", "info"), cli.get_key, cli.page_key(0)])

    def test_typed_cli_timeout_stays_safe_and_never_retries_page(self):
        cli = PlaylistCli()
        def fail(occurrence):
            raise CliError("SECRET-RAW-TIMEOUT", code="cli_timeout")
        cli.overrides[cli.page_key(0)] = fail
        with self.assertRaises(OnlineError) as raised:
            self.read(cli)
        self.assertEqual(raised.exception.code, "cli_timeout")
        self.assertNotIn("SECRET", str(raised.exception))
        self.assertEqual(cli.calls, [("user", "info"), cli.get_key, cli.page_key(0)])


if __name__ == "__main__":
    unittest.main()
