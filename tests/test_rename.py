import copy
import json
import threading
import unittest

from netease_organizer.runtime import OperationControl

try:
    from netease_organizer.rename import RenameExecutor
except ModuleNotFoundError as error:
    if error.name != "netease_organizer.rename":
        raise
    RenameExecutor = None


OWNER = "42"
OWNER_ENC = "a" * 32
FIRST = "b" * 32
SECOND = "c" * 32


def job(playlist_id=FIRST, original_id="101", old="funk", name="Funk"):
    return {"playlist_id": playlist_id, "original_playlist_id": original_id,
            "old_name": old, "name": name}


class FakeCli:
    """A strict in-memory account at the official JSON subprocess boundary."""

    def __init__(self):
        self.calls = []
        self.writes = []
        self.mode = "normal"
        self.info_calls = 0
        self.owner_changes = False
        self.secret = "PRIVATE-TOKEN-DO-NOT-ECHO"
        self.commands = [{"command": ["playlist", "updateName"], "parameters": [
            {"name": "playlistId", "in": "query", "type": "string", "required": True},
            {"name": "name", "in": "query", "type": "string", "required": True}]}]
        self.playlists = {
            FIRST: {"id": FIRST, "originalId": 101, "name": "funk", "creatorId": OWNER_ENC,
                    "trackCount": 2, "specialType": 0, "trackUpdateTime": 10},
            SECOND: {"id": SECOND, "originalId": "102", "name": "旧名称",
                     "creatorId": OWNER_ENC, "trackCount": 1, "specialType": 0,
                     "trackUpdateTime": 10}}
        self.tracks = {FIRST: [f"{1:032x}", f"{2:032x}"], SECOND: [f"{3:032x}"]}

    def manifest(self):
        return {}, copy.deepcopy(self.commands)

    def run_json(self, arguments):
        self.calls.append(list(arguments))
        command = arguments[:2]
        if command == ["user", "info"]:
            if arguments != ["user", "info"]:
                raise AssertionError("Unexpected identity flags")
            self.info_calls += 1
            original = "43" if self.owner_changes and self.info_calls > 1 else OWNER
            return {"code": 200, "data": {"originalId": original, "id": OWNER_ENC,
                                           "nickname": "测试用户"}}
        if command == ["playlist", "get"]:
            if len(arguments) != 4 or arguments[2] != "--playlistId":
                raise AssertionError("Unexpected detail flags")
            return {"code": 200, "data": copy.deepcopy(self.playlists[arguments[3].lower()])}
        if command == ["playlist", "tracks"]:
            if (len(arguments) != 8 or arguments[2] != "--playlistId"
                    or arguments[4:7] != ["--limit", "500", "--offset"]):
                raise AssertionError("Unexpected pagination flags")
            tracks = self.tracks[arguments[3].lower()]
            offset = int(arguments[7])
            return {"code": 200, "data": [{"id": ident} for ident in tracks[offset:offset + 500]]}
        if command != ["playlist", "updateName"]:
            raise AssertionError("An unsupported write was attempted")
        if (len(arguments) != 8 or arguments[2] != "--playlistId"
                or arguments[4] != "--name" or arguments[6] != "--userInput"
                or not arguments[7].startswith("按用户要求规范歌单名称")):
            raise AssertionError("Unexpected rename flags")
        ident, target = arguments[3], arguments[5]
        self.writes.append((ident, target))
        ident = ident.lower()
        if self.mode == "rejected":
            return {"code": 403, "message": self.secret, "data": False}
        if self.mode == "timeout_before":
            raise TimeoutError(self.secret)
        self.playlists[ident]["name"] = target
        if self.mode == "timeout_after":
            raise TimeoutError(self.secret)
        if self.mode == "members_changed":
            self.tracks[ident] = list(reversed(self.tracks[ident]))
        if self.mode == "count_changed":
            self.playlists[ident]["trackCount"] += 1
        if self.mode == "owner_changed":
            self.playlists[ident]["creatorId"] = "d" * 32
        if self.mode == "id_changed":
            self.playlists[ident]["originalId"] = 999
        if self.mode == "rejected_after":
            return {"code": 500, "data": False, "message": self.secret}
        if self.mode == "malformed":
            return {"code": "200", "message": self.secret}
        return {"code": 200, "data": True}


class RenameTests(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(RenameExecutor, "RenameExecutor implementation is missing")
        self.cli = FakeCli()
        self.executor = RenameExecutor(self.cli, OWNER)

    def test_pause_before_first_rename_makes_no_account_call(self):
        control = OperationControl()
        control.request_pause()
        result = RenameExecutor(self.cli, OWNER, control=control).execute([job()])
        self.assertEqual(result["status"], "paused")
        self.assertFalse(result["write_attempted"])
        self.assertFalse(result["applied_to_account"])
        self.assertTrue(result["outcome_known"])
        self.assertEqual(self.cli.calls, [])
        self.assertEqual(result["items"][0]["status"], "pending")

    def test_pause_during_rename_completes_readback_then_stops_next_job(self):
        control = OperationControl()
        original = self.cli.run_json
        def wrapped(arguments):
            response = original(arguments)
            if arguments[:2] == ["playlist", "updateName"]:
                control.request_pause()
            return response
        self.cli.run_json = wrapped
        result = RenameExecutor(self.cli, OWNER, control=control).execute(
            [job(), job(SECOND, "102", "旧名称", "新名称")])
        self.assertEqual(result["status"], "paused")
        self.assertEqual(result["completed_count"], 1)
        self.assertTrue(result["applied_to_account"])
        self.assertTrue(result["outcome_known"])
        self.assertEqual(self.cli.writes, [(FIRST, "Funk")])
        self.assertEqual([item["status"] for item in result["items"]], ["completed", "pending"])
        write_index = next(i for i, call in enumerate(self.cli.calls) if call[:2] == ["playlist", "updateName"])
        self.assertEqual([call[:2] for call in self.cli.calls[write_index + 1:]],
                         [["playlist", "get"], ["playlist", "tracks"], ["playlist", "get"]])

    def test_pause_before_write_after_preflight_does_not_misreport_attempt(self):
        control = OperationControl()
        original = self.cli.run_json
        gets = []
        def wrapped(arguments):
            response = original(arguments)
            if arguments[:2] == ["playlist", "get"]:
                gets.append(arguments)
                if len(gets) == 2:
                    control.request_pause()
            return response
        self.cli.run_json = wrapped
        result = RenameExecutor(self.cli, OWNER, control=control).execute([job()])
        self.assertEqual(result["status"], "paused")
        self.assertFalse(result["write_attempted"])
        self.assertEqual(self.cli.writes, [])

    def test_last_completed_rename_can_finish_despite_pause_and_safe_progress_listener_failure(self):
        control = OperationControl()
        original = self.cli.run_json
        events = []
        def wrapped(arguments):
            response = original(arguments)
            if arguments[:2] == ["playlist", "updateName"]:
                control.request_pause()
            return response
        def listener(event):
            events.append(event)
            raise RuntimeError(self.cli.secret)
        self.cli.run_json = wrapped
        result = RenameExecutor(self.cli, OWNER, control=control, progress_listener=listener).execute([job()])
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["completed_count"], 1)
        self.assertTrue(any(event["stage"] == "completed" for event in events))
        self.assertNotIn(self.cli.secret, json.dumps(events))
        self.assertNotIn("playlist_id", json.dumps(events))

    def test_pause_requested_in_post_write_tracks_keeps_final_detail_read(self):
        control = OperationControl()
        original = self.cli.run_json
        def wrapped(arguments):
            response = original(arguments)
            if arguments[:2] == ["playlist", "tracks"] and self.cli.writes:
                control.request_pause()
            return response
        self.cli.run_json = wrapped
        result = RenameExecutor(self.cli, OWNER, control=control).execute(
            [job(), job(SECOND, "102", "旧名称", "新名称")])
        self.assertEqual(result["status"], "paused")
        self.assertEqual(result["completed_count"], 1)
        self.assertTrue(result["outcome_known"])
        self.assertEqual(self.cli.calls[-1][:2], ["playlist", "get"])
        self.assertEqual(self.cli.writes, [(FIRST, "Funk")])

    def test_pause_and_failed_readback_keeps_unknown_write_frozen(self):
        control = OperationControl()
        original = self.cli.run_json
        def wrapped(arguments):
            response = original(arguments)
            if arguments[:2] == ["playlist", "updateName"]:
                control.request_pause()
            if arguments[:2] == ["playlist", "tracks"] and self.cli.writes:
                raise RuntimeError(self.cli.secret)
            return response
        self.cli.run_json = wrapped
        executor = RenameExecutor(self.cli, OWNER, control=control)
        first = executor.execute([job(), job(SECOND, "102", "旧名称", "新名称")])
        self.assertEqual(first["status"], "uncertain")
        self.assertFalse(first["outcome_known"])
        control.reset()
        second = executor.execute([job()])
        self.assertEqual(second["status"], "blocked")
        self.assertEqual(self.cli.writes, [(FIRST, "Funk")])

    def test_only_update_name_is_written_once_then_verified(self):
        result = self.executor.execute([job()])
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["completed_count"], 1)
        self.assertTrue(result["applied_to_account"])
        self.assertEqual(result["items"][0]["status"], "completed")
        self.assertEqual(self.cli.playlists[FIRST]["name"], "Funk")
        self.assertEqual(self.cli.tracks[FIRST], [f"{1:032x}", f"{2:032x}"])
        self.assertEqual(self.cli.writes, [(FIRST, "Funk")])
        write_index = next(i for i, call in enumerate(self.cli.calls)
                           if call[:2] == ["playlist", "updateName"])
        self.assertIn(["playlist", "get", "--playlistId", FIRST], self.cli.calls[write_index + 1:])
        self.assertIn(["playlist", "tracks", "--playlistId", FIRST, "--limit", "500", "--offset", "0"],
                      self.cli.calls[write_index + 1:])

    def test_unicode_name_and_special_type_50_are_supported(self):
        self.cli.playlists[FIRST].update(name="我喜欢的音乐-陶喆", specialType=50)
        result = self.executor.execute([job(old="我喜欢的音乐-陶喆", name="陶喆 · 喜欢的音乐")])
        self.assertEqual(result["status"], "completed")
        self.assertEqual(self.cli.playlists[FIRST]["name"], "陶喆 · 喜欢的音乐")

    def test_canonical_uppercase_job_matches_lowercase_sdk_ids(self):
        self.cli.playlists[FIRST]["creatorId"] = OWNER_ENC.upper()
        result = self.executor.execute([job(playlist_id=FIRST.upper())])
        self.assertEqual(result["status"], "completed")
        self.assertEqual(self.cli.playlists[FIRST]["name"], "Funk")
        self.assertEqual(self.cli.writes, [(FIRST.upper(), "Funk")])

    def test_case_variants_of_one_hex_track_are_duplicate_members(self):
        self.cli.tracks[FIRST] = ["ab" * 16, "AB" * 16]
        result = self.executor.execute([job()])
        self.assertEqual(result["status"], "blocked")
        self.assertEqual(self.cli.writes, [])

    def test_wrong_expected_owner_blocks_without_writes(self):
        result = RenameExecutor(self.cli, "99").execute([job()])
        self.assertEqual(result["status"], "blocked")
        self.assertFalse(result["applied_to_account"])
        self.assertEqual(self.cli.writes, [])

    def test_wrong_playlist_owner_or_id_blocks(self):
        for change in ({"creatorId": "d" * 32}, {"originalId": 999}, {"id": "d" * 32}):
            with self.subTest(change=change):
                cli = FakeCli()
                cli.playlists[FIRST].update(change)
                result = RenameExecutor(cli, OWNER).execute([job()])
                self.assertEqual(result["status"], "blocked")
                self.assertEqual(cli.writes, [])

    def test_red_system_playlist_is_never_renamed(self):
        self.cli.playlists[FIRST]["specialType"] = 5
        result = self.executor.execute([job()])
        self.assertEqual(result["status"], "blocked")
        self.assertEqual(self.cli.writes, [])

    def test_stale_old_name_blocks_and_freezes_remaining(self):
        result = self.executor.execute([job(old="旧快照"), job(SECOND, "102", "旧名称", "新名称")])
        self.assertEqual(result["status"], "blocked")
        self.assertEqual([item["status"] for item in result["items"]], ["blocked", "blocked"])
        self.assertEqual(self.cli.writes, [])

    def test_target_name_already_present_is_skipped_without_write(self):
        self.cli.playlists[FIRST]["name"] = "Funk"
        result = self.executor.execute([job()])
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["items"][0]["status"], "skipped")
        self.assertEqual(result["completed_count"], 0)
        self.assertFalse(result["applied_to_account"])
        self.assertEqual(self.cli.writes, [])

    def test_tracks_are_read_in_500_item_pages_before_and_after(self):
        self.cli.tracks[FIRST] = [f"{i:032x}" for i in range(1, 502)]
        self.cli.playlists[FIRST]["trackCount"] = 501
        result = self.executor.execute([job()])
        self.assertEqual(result["status"], "completed")
        offsets = [call[7] for call in self.cli.calls if call[:2] == ["playlist", "tracks"]]
        self.assertEqual(offsets, ["0", "500", "0", "500"])

    def test_duplicate_tracks_or_count_mismatch_block_before_write(self):
        for tracks, count in (([f"{1:032x}", f"{1:032x}"], 2), ([f"{1:032x}"], 2)):
            with self.subTest(count=count):
                cli = FakeCli()
                cli.tracks[FIRST] = tracks
                cli.playlists[FIRST]["trackCount"] = count
                result = RenameExecutor(cli, OWNER).execute([job()])
                self.assertEqual(result["status"], "blocked")
                self.assertEqual(cli.writes, [])

    def test_more_than_10000_tracks_blocks_without_write(self):
        self.cli.playlists[FIRST]["trackCount"] = 10001
        result = self.executor.execute([job()])
        self.assertEqual(result["status"], "blocked")
        self.assertEqual(self.cli.writes, [])

    def test_business_rejection_stops_without_retry_or_secret_echo(self):
        self.cli.mode = "rejected"
        result = self.executor.execute([job(), job(SECOND, "102", "旧名称", "新名称")])
        self.assertEqual(result["status"], "blocked")
        self.assertEqual(self.cli.writes, [(FIRST, "Funk")])
        self.assertEqual(self.cli.playlists[SECOND]["name"], "旧名称")
        self.assertIs(result.get("write_attempted"), True)
        self.assertIs(result.get("outcome_known"), True)
        self.assertFalse(result["applied_to_account"])
        write_index = next(i for i, call in enumerate(self.cli.calls)
                           if call[:2] == ["playlist", "updateName"])
        self.assertIn(["playlist", "get", "--playlistId", FIRST], self.cli.calls[write_index + 1:])
        self.assertNotIn(self.cli.secret, json.dumps(result, ensure_ascii=False))

    def test_business_failure_after_apply_reports_actual_effect_as_uncertain(self):
        self.cli.mode = "rejected_after"
        result = self.executor.execute([job(), job(SECOND, "102", "旧名称", "新名称")])
        self.assertEqual(result["status"], "uncertain")
        self.assertTrue(result["applied_to_account"])
        self.assertIs(result.get("outcome_known"), True)
        self.assertIs(result.get("write_attempted"), True)
        self.assertEqual(result["completed_count"], 0)
        self.assertEqual(self.cli.playlists[FIRST]["name"], "Funk")
        self.assertEqual(self.cli.playlists[SECOND]["name"], "旧名称")
        self.assertEqual(self.cli.writes, [(FIRST, "Funk")])

    def test_timeout_after_apply_is_completed_only_after_readback(self):
        self.cli.mode = "timeout_after"
        result = self.executor.execute([job()])
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["completed_count"], 1)
        self.assertTrue(result["applied_to_account"])
        self.assertEqual(self.cli.writes, [(FIRST, "Funk")])
        self.assertNotIn(self.cli.secret, json.dumps(result))

    def test_timeout_before_apply_is_uncertain_and_remaining_frozen(self):
        self.cli.mode = "timeout_before"
        result = self.executor.execute([job(), job(SECOND, "102", "旧名称", "新名称")])
        self.assertEqual(result["status"], "uncertain")
        self.assertEqual(result["completed_count"], 0)
        self.assertFalse(result["applied_to_account"])
        self.assertIs(result.get("write_attempted"), True)
        self.assertIs(result.get("outcome_known"), False)
        self.assertEqual([item["status"] for item in result["items"]], ["uncertain", "blocked"])
        self.assertEqual(self.cli.writes, [(FIRST, "Funk")])

    def test_membership_or_metadata_change_after_write_is_uncertain(self):
        for mode in ("members_changed", "count_changed", "owner_changed", "id_changed"):
            with self.subTest(mode=mode):
                cli = FakeCli()
                cli.mode = mode
                result = RenameExecutor(cli, OWNER).execute([job(), job(SECOND, "102", "旧名称", "新名称")])
                self.assertEqual(result["status"], "uncertain")
                self.assertEqual(result["completed_count"], 0)
                self.assertEqual(cli.writes, [(FIRST, "Funk")])
                self.assertEqual(cli.playlists[SECOND]["name"], "旧名称")

    def test_identity_is_checked_again_for_each_job_and_completed_results_survive(self):
        self.cli.owner_changes = True
        result = self.executor.execute([job(), job(SECOND, "102", "旧名称", "新名称")])
        self.assertEqual(result["status"], "partial")
        self.assertEqual(result["completed_count"], 1)
        self.assertTrue(result["applied_to_account"])
        self.assertEqual([item["status"] for item in result["items"]], ["completed", "blocked"])
        self.assertEqual(self.cli.writes, [(FIRST, "Funk")])

    def test_manifest_mismatch_or_unknown_required_parameter_blocks(self):
        changes = [lambda p: p[0].update(type="number"),
                   lambda p: p[0].update(in_="body"),
                   lambda p: p.append({"name": "mystery", "in": "query", "type": "string", "required": True}),
                   lambda p: p.pop()]
        for change in changes:
            with self.subTest(change=change):
                cli = FakeCli()
                params = cli.commands[0]["parameters"]
                if change == changes[1]:
                    params[0]["in"] = "body"
                else:
                    change(params)
                result = RenameExecutor(cli, OWNER).execute([job()])
                self.assertEqual(result["status"], "blocked")
                self.assertEqual(cli.calls, [])

    def test_invalid_batch_does_not_start_account_operations(self):
        for invalid in (job(playlist_id="123"), job(original_id="001"), job(name=""),
                        job(name="bad\x00name"), job(old="")):
            with self.subTest(invalid=invalid):
                cli = FakeCli()
                result = RenameExecutor(cli, OWNER).execute([job(), invalid])
                self.assertEqual(result["status"], "blocked")
                self.assertEqual(cli.calls, [])

    def test_duplicate_playlist_jobs_are_rejected_before_account_operations(self):
        result = self.executor.execute([job(), job(playlist_id=FIRST.upper())])
        self.assertEqual(result["status"], "blocked")
        self.assertEqual(self.cli.calls, [])

    def test_malformed_business_success_is_not_accepted(self):
        self.cli.mode = "malformed"
        result = self.executor.execute([job()])
        self.assertNotEqual(result["status"], "completed")
        self.assertEqual(result["completed_count"], 0)
        self.assertEqual(self.cli.writes, [(FIRST, "Funk")])
        self.assertNotIn(self.cli.secret, json.dumps(result))

    def test_uncertain_executor_cannot_resend_in_a_later_execute_call(self):
        self.cli.mode = "timeout_before"
        first = self.executor.execute([job()])
        self.assertEqual(first["status"], "uncertain")
        result = self.executor.execute([job()])
        self.assertEqual(result["status"], "blocked")
        self.assertEqual(self.cli.writes, [(FIRST, "Funk")])

    def test_concurrent_executor_is_blocked_before_account_commands(self):
        entered, release = threading.Event(), threading.Event()
        results = []

        class BlockingCli(FakeCli):
            def run_json(self, arguments):
                if arguments == ["user", "info"]:
                    entered.set()
                    if not release.wait(3):
                        raise AssertionError("Test did not release identity reading")
                return super().run_json(arguments)

        cli = BlockingCli()
        worker = threading.Thread(target=lambda: results.append(RenameExecutor(cli, OWNER).execute([job()])))
        worker.start()
        try:
            self.assertTrue(entered.wait(3))
            other = FakeCli()
            result = RenameExecutor(other, OWNER).execute([job(SECOND, "102", "旧名称", "新名称")])
            self.assertEqual(result["status"], "blocked")
            self.assertEqual(other.calls, [])
            self.assertEqual(other.playlists[SECOND]["name"], "旧名称")
        finally:
            release.set()
            worker.join(3)
        self.assertFalse(worker.is_alive())
        self.assertEqual(results[0]["status"], "completed")


if __name__ == "__main__":
    unittest.main()
