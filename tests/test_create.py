import copy
import json
import unittest

from netease_organizer.runtime import OperationControl

try:
    from netease_organizer.create import ArtistExecutor
except ModuleNotFoundError as error:
    if error.name != "netease_organizer.create":
        raise
    ArtistExecutor = None


OWNER = "42"
OWNER_ENC = "A" * 32
TRACKS = [f"{i:032X}" for i in (11, 12, 13)]


def job(name="林俊杰 · 红心精选", tracks=None):
    return {"kind": "create_artist_playlist", "name": name,
            "candidate_track_ids": list(TRACKS if tracks is None else tracks),
            "intended_visibility": "provider_default"}


def recovery(ident, original="1001", name="林俊杰 · 红心精选"):
    return {"job_index": 0, "playlist_id": ident, "original_playlist_id": original,
            "name": name, "create_only_verified": True}


class Journal:
    def __init__(self, fail_phase=None):
        self.records = []
        self.fail_phase = fail_phase

    def __call__(self, record):
        if record["phase"] == self.fail_phase:
            raise OSError("PRIVATE-TOKEN-DO-NOT-ECHO")
        self.records.append(copy.deepcopy(record))


class FakeCli:
    def __init__(self):
        self.calls, self.writes = [], []
        self.create_mode = "normal"
        self.add_mode = "normal"
        self.reorder_mode = "normal"
        self.created = {}
        self.members = {}
        self.sequence = 1000
        self.secret = "PRIVATE-TOKEN-DO-NOT-ECHO"
        self.commands = []
        for command, fields in (
                ("create", [("playlistName", "query", "string")]),
                ("add", [("playlistId", "query", "string"), ("songIdList", "query", "array")]),
                ("reorder", [("playlistId", "body", "string"), ("trackIds", "body", "array")])):
            self.commands.append({"command": ["playlist", command], "parameters": [
                {"name": name, "in": location, "type": kind, "required": True}
                for name, location, kind in fields]})

    def manifest(self):
        return {}, copy.deepcopy(self.commands)

    def make_playlist(self, name, tracks=()):
        self.sequence += 1
        ident = f"{self.sequence:032X}"
        self.created[ident] = {"id": ident, "originalId": self.sequence, "name": name,
                               "creatorId": OWNER_ENC, "trackCount": len(tracks), "specialType": 0,
                               "trackUpdateTime": 10, "token": self.secret}
        self.members[ident] = list(tracks)
        return ident

    def run_json(self, arguments):
        self.calls.append(list(arguments))
        command = arguments[:2]
        if arguments == ["user", "info"]:
            return {"code": 200, "data": {"originalId": 42, "id": OWNER_ENC, "nickname": "用户",
                                           "token": self.secret}}
        if command == ["playlist", "created"]:
            if len(arguments) != 6 or arguments[2:5] != ["--limit", "500", "--offset"]:
                raise AssertionError("Wrong catalog flags")
            offset = int(arguments[5])
            rows = list(self.created.values())
            return {"code": 200, "data": {"recordCount": len(rows), "records": copy.deepcopy(rows[offset:offset + 500])}}
        if command == ["playlist", "get"]:
            if len(arguments) != 4 or arguments[2] != "--playlistId":
                raise AssertionError("Wrong detail flags")
            return {"code": 200, "data": copy.deepcopy(self.created[arguments[3].upper()])}
        if command == ["playlist", "tracks"]:
            if (len(arguments) != 8 or arguments[2] != "--playlistId"
                    or arguments[4:7] != ["--limit", "500", "--offset"]):
                raise AssertionError("Wrong member flags")
            offset = int(arguments[7])
            return {"code": 200, "data": [{"id": ident} for ident in self.members[arguments[3].upper()][offset:offset + 500]]}
        if command == ["playlist", "create"]:
            if len(arguments) != 6 or arguments[2] != "--playlistName" or arguments[4] != "--userInput":
                raise AssertionError("Wrong create flags")
            if "默认可见性" not in arguments[5] or "可能公开" not in arguments[5]:
                raise AssertionError("Missing user's visibility consent")
            self.writes.append("create")
            if self.create_mode == "timeout_before":
                raise TimeoutError(self.secret)
            if self.create_mode == "failure_before":
                return {"code": 500, "data": None, "message": self.secret}
            self.make_playlist(arguments[3])
            if self.create_mode == "ambiguous":
                self.make_playlist(arguments[3])
            if self.create_mode == "timeout_after":
                raise TimeoutError(self.secret)
            if self.create_mode == "failure_after":
                return {"code": 500, "data": None, "message": self.secret}
            if self.create_mode == "unknown_after":
                return {"unknownResponseShape": self.secret}
            return {"code": 200, "data": {"unknownResponseShape": self.secret, "id": "FAKE-DO-NOT-USE"}}
        if command not in (["playlist", "add"], ["playlist", "reorder"]):
            raise AssertionError("Forbidden mutation")
        expected_flag = "--songIdList" if command[1] == "add" else "--trackIds"
        if (len(arguments) != 8 or arguments[2] != "--playlistId" or arguments[4] != expected_flag
                or arguments[6] != "--userInput"):
            raise AssertionError("Wrong array mutation flags")
        ident, tracks = arguments[3].upper(), json.loads(arguments[5])
        if not isinstance(tracks, list) or any(not isinstance(value, str) for value in tracks):
            raise AssertionError("Array must be one JSON argv")
        self.writes.append(command[1])
        mode = self.add_mode if command[1] == "add" else self.reorder_mode
        if mode == "timeout_before":
            raise TimeoutError(self.secret)
        if mode == "reverse":
            tracks = list(reversed(tracks))
        elif mode == "partial":
            tracks = tracks[:-1]
        self.members[ident] = list(tracks)
        self.created[ident]["trackCount"] = len(tracks)
        self.created[ident]["trackUpdateTime"] += 1
        if mode == "timeout_after":
            raise TimeoutError(self.secret)
        if mode == "failure_after":
            return {"code": 500, "data": False, "message": self.secret}
        return {"code": 200, "data": True}


class ArtistTests(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(ArtistExecutor, "ArtistExecutor implementation is missing")
        self.cli = FakeCli()
        self.journal = Journal()
        self.executor = ArtistExecutor(self.cli, OWNER, self.journal)

    def test_pause_before_create_sends_no_account_command_and_saves_full_pending_batch(self):
        control = OperationControl()
        control.request_pause()
        jobs = [job(), job("下一精选")]
        result = ArtistExecutor(self.cli, OWNER, self.journal, control=control).execute(jobs)
        self.assertEqual(result["status"], "paused")
        self.assertFalse(result["write_attempted"])
        self.assertFalse(result["applied_to_account"])
        self.assertTrue(result["outcome_known"])
        self.assertEqual(self.cli.calls, [])
        self.assertEqual(self.cli.writes, [])
        self.assertEqual(self.journal.records[-1]["phase"], "paused")
        self.assertEqual(self.journal.records[-1]["jobs"], jobs)
        self.assertEqual([item["status"] for item in result["items"]], ["pending", "pending"])

    def test_pause_during_create_finishes_empty_readback_and_retains_new_id_without_add(self):
        control = OperationControl()
        original = self.cli.run_json
        def wrapped(arguments):
            response = original(arguments)
            if arguments[:2] == ["playlist", "create"]:
                control.request_pause()
            return response
        self.cli.run_json = wrapped
        result = ArtistExecutor(self.cli, OWNER, self.journal, control=control).execute([job()])
        self.assertEqual(result["status"], "paused")
        self.assertEqual(self.cli.writes, ["create"])
        self.assertTrue(result["applied_to_account"])
        self.assertTrue(result["outcome_known"])
        self.assertEqual(result["completed_count"], 0)
        item = result["items"][0]
        self.assertIn(item["playlist_id"], self.cli.created)
        self.assertEqual(item["count"], 0)
        self.assertEqual(item["phase"], "created")
        write_index = next(i for i, call in enumerate(self.cli.calls) if call[:2] == ["playlist", "create"])
        self.assertIn(["playlist", "tracks", "--playlistId", item["playlist_id"],
                       "--limit", "500", "--offset", "0"], self.cli.calls[write_index + 1:])
        self.assertEqual(self.journal.records[-1]["phase"], "paused")

    def test_pause_during_add_verifies_entire_first_job_and_stops_before_next_create(self):
        control = OperationControl()
        original = self.cli.run_json
        def wrapped(arguments):
            response = original(arguments)
            if arguments[:2] == ["playlist", "add"]:
                control.request_pause()
            return response
        self.cli.run_json = wrapped
        result = ArtistExecutor(self.cli, OWNER, self.journal, control=control).execute([job(), job("下一精选")])
        self.assertEqual(result["status"], "paused")
        self.assertEqual(result["completed_count"], 1)
        self.assertTrue(result["applied_to_account"])
        self.assertTrue(result["outcome_known"])
        self.assertEqual(self.cli.writes, ["create", "add"])
        self.assertEqual([item["status"] for item in result["items"]], ["completed", "pending"])
        self.assertEqual(self.cli.members[result["items"][0]["playlist_id"]], TRACKS)

    def test_pause_after_last_add_returns_completed_and_listener_errors_do_not_abort_readback(self):
        control = OperationControl()
        original = self.cli.run_json
        events = []
        def wrapped(arguments):
            response = original(arguments)
            if arguments[:2] == ["playlist", "add"]:
                control.request_pause()
            return response
        def listener(event):
            events.append(event)
            raise RuntimeError(self.cli.secret)
        self.cli.run_json = wrapped
        result = ArtistExecutor(self.cli, OWNER, self.journal, control=control,
                                progress_listener=listener).execute([job()])
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["completed_count"], 1)
        self.assertTrue(any(event["stage"] == "completed" for event in events))
        self.assertNotIn(self.cli.secret, json.dumps(events))
        self.assertNotIn("playlist_id", json.dumps(events))

    def test_pause_during_post_add_member_read_finishes_final_detail_and_identity(self):
        control = OperationControl()
        original = self.cli.run_json
        def wrapped(arguments):
            response = original(arguments)
            if arguments[:2] == ["playlist", "tracks"] and self.cli.writes == ["create", "add"]:
                control.request_pause()
            return response
        self.cli.run_json = wrapped
        result = ArtistExecutor(self.cli, OWNER, self.journal, control=control).execute([job(), job("下一精选")])
        self.assertEqual(result["status"], "paused")
        self.assertEqual(result["completed_count"], 1)
        self.assertTrue(result["outcome_known"])
        last_tracks = max(i for i, call in enumerate(self.cli.calls) if call[:2] == ["playlist", "tracks"])
        self.assertEqual([call[:2] for call in self.cli.calls[last_tracks + 1:]],
                         [["playlist", "get"], ["user", "info"]])

    def test_pause_and_failed_post_add_readback_remains_uncertain_instead_of_paused(self):
        control = OperationControl()
        original = self.cli.run_json
        def wrapped(arguments):
            response = original(arguments)
            if arguments[:2] == ["playlist", "add"]:
                control.request_pause()
            if arguments[:2] == ["playlist", "tracks"] and self.cli.writes == ["create", "add"]:
                raise RuntimeError(self.cli.secret)
            return response
        self.cli.run_json = wrapped
        result = ArtistExecutor(self.cli, OWNER, self.journal, control=control).execute([job(), job("下一精选")])
        self.assertEqual(result["status"], "uncertain")
        self.assertFalse(result["outcome_known"])
        self.assertEqual(self.cli.writes, ["create", "add"])
        self.assertNotIn("paused", [record["phase"] for record in self.journal.records])

    def test_pause_requested_during_intent_journal_saves_paused_without_sending_write(self):
        control = OperationControl()
        def writer(record):
            self.journal(record)
            if record["phase"] == "create_attempted":
                control.request_pause()
        result = ArtistExecutor(self.cli, OWNER, writer, control=control).execute([job()])
        self.assertEqual(result["status"], "paused")
        self.assertFalse(result["write_attempted"])
        self.assertEqual(self.cli.writes, [])
        self.assertEqual(self.journal.records[-1]["phase"], "paused")

    def test_new_empty_snapshot_is_not_repeated_before_add_but_latest_header_and_post_tracks_remain(self):
        result = self.executor.execute([job()])
        self.assertEqual(result["status"], "completed")
        add_index = next(i for i, call in enumerate(self.cli.calls) if call[:2] == ["playlist", "add"])
        before_add = self.cli.calls[:add_index]
        after_add = self.cli.calls[add_index + 1:]
        self.assertEqual(sum(call[:2] == ["playlist", "tracks"] for call in before_add), 1)
        self.assertEqual(sum(call[:2] == ["playlist", "tracks"] for call in after_add), 1)
        self.assertEqual(before_add[-1][:2], ["playlist", "get"])

    def test_latest_pre_add_header_change_blocks_even_with_reused_empty_snapshot(self):
        def writer(record):
            self.journal(record)
            if record["phase"] == "playlist_identified":
                ident = record["items"][0]["playlist_id"]
                self.cli.created[ident]["trackCount"] = 1
                self.cli.members[ident] = [TRACKS[0]]
        result = ArtistExecutor(self.cli, OWNER, writer).execute([job()])
        self.assertEqual(result["status"], "partial")
        self.assertTrue(result["applied_to_account"])
        self.assertEqual(self.cli.writes, ["create"])

    def test_resume_checkpoint_after_verified_create_adds_existing_id_and_creates_only_pending(self):
        control = OperationControl()
        def listener(event):
            if event["stage"] == "created":
                control.request_pause()
        executor = ArtistExecutor(self.cli, OWNER, self.journal, control=control, progress_listener=listener)
        jobs = [job(), job("下一精选")]
        paused = executor.execute(jobs)
        self.assertEqual(paused["status"], "paused")
        ident = paused["items"][0]["playlist_id"]
        control.reset()
        executor.progress_listener = None
        result = executor.resume_checkpoint(jobs, paused["items"])
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["completed_count"], 2)
        self.assertEqual(self.cli.writes, ["create", "add", "create", "add"])
        self.assertEqual(result["items"][0]["playlist_id"], ident)
        self.assertEqual(len(self.cli.created), 2)

    def test_resume_checkpoint_skips_completed_job_and_revalidates_full_members_before_next_create(self):
        control = OperationControl()
        def listener(event):
            if event["stage"] == "completed":
                control.request_pause()
        executor = ArtistExecutor(self.cli, OWNER, self.journal, control=control, progress_listener=listener)
        jobs = [job(), job("下一精选")]
        paused = executor.execute(jobs)
        self.assertEqual(paused["status"], "paused")
        prior_calls = len(self.cli.calls)
        control.reset()
        executor.progress_listener = None
        result = executor.resume_checkpoint(jobs, paused["items"])
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["completed_count"], 2)
        self.assertEqual(self.cli.writes, ["create", "add", "create", "add"])
        self.assertEqual(result["items"][0]["playlist_id"], paused["items"][0]["playlist_id"])
        new_calls = self.cli.calls[prior_calls:]
        before_next_create = new_calls[:next(i for i, call in enumerate(new_calls)
                                             if call[:2] == ["playlist", "create"])]
        self.assertIn(["playlist", "tracks", "--playlistId", paused["items"][0]["playlist_id"],
                       "--limit", "500", "--offset", "0"], before_next_create)

    def test_resume_checkpoint_added_complete_set_only_reorders_once_without_add_or_create(self):
        control = OperationControl()
        self.cli.add_mode = "reverse"
        def listener(event):
            if event["stage"] == "reorder":
                control.request_pause()
        executor = ArtistExecutor(self.cli, OWNER, self.journal, control=control, progress_listener=listener)
        jobs = [job()]
        paused = executor.execute(jobs)
        self.assertEqual(paused["status"], "paused")
        self.assertEqual(paused["items"][0]["phase"], "added")
        self.assertEqual(self.cli.writes, ["create", "add"])
        control.reset()
        executor.progress_listener = None
        result = executor.resume_checkpoint(jobs, paused["items"])
        self.assertEqual(result["status"], "completed")
        self.assertEqual(self.cli.writes, ["create", "add", "reorder"])
        self.assertEqual(self.cli.members[result["items"][0]["playlist_id"]], TRACKS)

    def test_resume_checkpoint_live_completed_members_mismatch_blocks_entire_batch(self):
        ident = self.cli.make_playlist(job()["name"], TRACKS)
        evidence = [{"kind": "create_artist_playlist", "name": job()["name"], "playlist_id": ident,
                     "original_playlist_id": "1001", "count": 3, "expected_count": 3,
                     "phase": "completed", "status": "completed"},
                    {"kind": "create_artist_playlist", "name": "下一精选", "playlist_id": None,
                     "original_playlist_id": None, "count": 0, "expected_count": 3,
                     "phase": "pending", "status": "pending"}]
        self.cli.members[ident] = list(reversed(TRACKS))
        result = self.executor.resume_checkpoint([job(), job("下一精选")], evidence)
        self.assertIn(result["status"], {"partial", "blocked"})
        self.assertEqual(self.cli.writes, [])

    def test_resume_checkpoint_validates_every_bound_job_before_first_new_mutation(self):
        first = self.cli.make_playlist(job()["name"])
        second = self.cli.make_playlist("下一精选", TRACKS[:1])
        evidence = [{"kind": "create_artist_playlist", "name": job()["name"], "playlist_id": first,
                     "original_playlist_id": "1001", "count": 0, "expected_count": 3,
                     "phase": "created", "status": "pending"},
                    {"kind": "create_artist_playlist", "name": "下一精选", "playlist_id": second,
                     "original_playlist_id": "1002", "count": 0, "expected_count": 3,
                     "phase": "created", "status": "pending"}]
        result = self.executor.resume_checkpoint([job(), job("下一精选")], evidence)
        self.assertIn(result["status"], {"partial", "blocked"})
        self.assertEqual(self.cli.writes, [])

    def test_resume_checkpoint_invalid_binding_never_falls_back_to_new_create(self):
        ident = self.cli.make_playlist(job()["name"])
        evidence = {"kind": "create_artist_playlist", "name": job()["name"], "playlist_id": ident,
                    "original_playlist_id": "1001", "count": 0, "expected_count": 3,
                    "phase": "created", "status": "pending"}
        for change in ({"playlist_id": None}, {"original_playlist_id": "1002"},
                       {"name": "其他名称"}, {"phase": "uncertain"}, {"expected_count": 2},
                       {"phase": "pending"}, {"status": "completed"}):
            with self.subTest(change=change):
                cli = FakeCli()
                cli.make_playlist(job()["name"])
                result = ArtistExecutor(cli, OWNER, Journal()).resume_checkpoint([job()], [{**evidence, **change}])
                self.assertIn(result["status"], {"blocked", "partial"})
                self.assertEqual(cli.writes, [])

    def test_resume_added_with_missing_extra_member_or_wrong_owner_cannot_add_or_reorder(self):
        for defect in ("missing", "extra", "owner", "id", "directory_duplicate"):
            with self.subTest(defect=defect):
                cli = FakeCli()
                ident = cli.make_playlist(job()["name"], list(reversed(TRACKS)))
                evidence = {"kind": "create_artist_playlist", "name": job()["name"], "playlist_id": ident,
                            "original_playlist_id": "1001", "count": 3, "expected_count": 3,
                            "phase": "added", "status": "pending"}
                if defect in {"missing", "extra"}:
                    cli.members[ident] = TRACKS[:2] if defect == "missing" else [*TRACKS, f"{99:032X}"]
                    cli.created[ident]["trackCount"] = len(cli.members[ident])
                elif defect == "owner":
                    cli.created[ident]["creatorId"] = "C" * 32
                elif defect == "id":
                    cli.created[ident]["originalId"] = 1002
                else:
                    cli.make_playlist(job()["name"])
                result = ArtistExecutor(cli, OWNER, Journal()).resume_checkpoint([job()], [evidence])
                self.assertIn(result["status"], {"blocked", "partial"})
                self.assertEqual(cli.writes, [])

    def test_paused_resume_keeps_verified_completed_history_and_can_resume_again(self):
        ident = self.cli.make_playlist(job()["name"], TRACKS)
        evidence = [{"kind": "create_artist_playlist", "name": job()["name"], "playlist_id": ident,
                     "original_playlist_id": "1001", "count": 3, "expected_count": 3,
                     "phase": "completed", "status": "completed"},
                    {"kind": "create_artist_playlist", "name": "下一精选", "playlist_id": None,
                     "original_playlist_id": None, "count": 0, "expected_count": 3,
                     "phase": "pending", "status": "pending"}]
        control = OperationControl()
        control.request_pause()
        executor = ArtistExecutor(self.cli, OWNER, self.journal, control=control)
        jobs = [job(), job("下一精选")]
        first = executor.resume_checkpoint(jobs, evidence)
        self.assertEqual(first["status"], "paused")
        self.assertEqual(first["completed_count"], 1)
        self.assertTrue(first["applied_to_account"])
        self.assertEqual(self.cli.calls, [])
        control.reset()
        second = executor.resume_checkpoint(jobs, first["items"])
        self.assertEqual(second["status"], "completed")
        self.assertEqual(second["completed_count"], 2)
        self.assertEqual(self.cli.writes, ["create", "add"])

    def test_pause_before_legacy_recovery_preflight_preserves_binding_for_checkpoint_resume(self):
        ident = self.cli.make_playlist(job()["name"])
        control = OperationControl()
        def listener(event):
            if event["stage"] == "preflight":
                control.request_pause()
        executor = ArtistExecutor(self.cli, OWNER, self.journal, control=control, progress_listener=listener)
        paused = executor.resume_created([job()], recovery(ident))
        self.assertEqual(paused["status"], "paused")
        self.assertEqual(self.cli.calls, [])
        self.assertTrue(paused["applied_to_account"])
        self.assertTrue(paused["write_attempted"])
        self.assertEqual(paused["items"][0]["playlist_id"], ident)
        self.assertEqual(paused["items"][0]["original_playlist_id"], "1001")
        self.assertEqual(paused["items"][0]["phase"], "created")
        self.assertEqual(paused["items"][0]["status"], "pending")
        self.assertEqual(paused["items"][0]["count"], 0)
        control.reset()
        executor.progress_listener = None
        result = executor.resume_checkpoint([job()], paused["items"])
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["items"][0]["playlist_id"], ident)
        self.assertEqual(self.cli.writes, ["add"])
        self.assertEqual(len(self.cli.created), 1)

    def test_success_creates_and_adds_once_with_exact_readback_and_safe_journal(self):
        result = self.executor.execute([job()])
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["completed_count"], 1)
        self.assertTrue(result["applied_to_account"])
        self.assertTrue(result["write_attempted"])
        self.assertTrue(result["outcome_known"])
        self.assertEqual(self.cli.writes, ["create", "add"])
        ident = result["items"][0]["playlist_id"]
        self.assertEqual(self.cli.members[ident], TRACKS)
        self.assertEqual(result["items"][0]["count"], 3)
        self.assertEqual([record["phase"] for record in self.journal.records],
                         ["create_attempted", "playlist_identified", "add_attempted", "completed"])
        self.assertEqual(self.journal.records[1]["items"][0]["playlist_id"], ident)
        self.assertNotIn(self.cli.secret, json.dumps(result))
        self.assertNotIn(self.cli.secret, json.dumps(self.journal.records))

    def test_wrong_owner_blocks_before_any_mutation_or_journal(self):
        result = ArtistExecutor(self.cli, "43", self.journal).execute([job()])
        self.assertEqual(result["status"], "blocked")
        self.assertFalse(result["applied_to_account"])
        self.assertEqual(self.cli.writes, [])
        self.assertEqual(self.journal.records, [])

    def test_existing_name_in_any_job_blocks_whole_batch_without_reuse(self):
        ident = self.cli.make_playlist("已存在精选", TRACKS)
        result = self.executor.execute([job(), job("已存在精选")])
        self.assertEqual(result["status"], "blocked")
        self.assertEqual(self.cli.writes, [])
        self.assertEqual(self.cli.members[ident], TRACKS)

    def test_invalid_or_duplicate_job_format_blocks_before_cli(self):
        batches = [[job(), job()], [job(tracks=[TRACKS[0], TRACKS[0].lower()])],
                   [job(tracks=["123"])], [job(tracks=[])],
                   [{**job(), "intended_visibility": "private"}], [{**job(), "name": "bad\x00name"}]]
        for jobs in batches:
            with self.subTest(jobs=jobs):
                cli, journal = FakeCli(), Journal()
                result = ArtistExecutor(cli, OWNER, journal).execute(jobs)
                self.assertEqual(result["status"], "blocked")
                self.assertEqual(cli.calls, [])
                self.assertEqual(journal.records, [])

    def test_manifest_unknown_required_or_wrong_array_location_blocks_before_cli(self):
        for mutate in (lambda c: c[1]["parameters"][1].update(type="string"),
                       lambda c: c[2]["parameters"][1].update(in_="query"),
                       lambda c: c[0]["parameters"].append({"name": "unknown", "required": True})):
            with self.subTest(mutate=mutate):
                cli = FakeCli()
                mutate(cli.commands)
                if "in_" in cli.commands[2]["parameters"][1]:
                    cli.commands[2]["parameters"][1]["in"] = "query"
                result = ArtistExecutor(cli, OWNER, Journal()).execute([job()])
                self.assertEqual(result["status"], "blocked")
                self.assertEqual(cli.calls, [])

    def test_reorder_only_when_expected_set_complete_and_changes_exact_sequence_once(self):
        self.cli.add_mode = "reverse"
        result = self.executor.execute([job()])
        self.assertEqual(result["status"], "completed")
        self.assertEqual(self.cli.writes, ["create", "add", "reorder"])
        self.assertEqual(self.cli.members[result["items"][0]["playlist_id"]], TRACKS)
        self.assertIn("reorder_attempted", [record["phase"] for record in self.journal.records])

    def test_partial_add_freezes_following_jobs_and_never_retries_or_reorders(self):
        self.cli.add_mode = "partial"
        result = self.executor.execute([job(), job("蔡健雅 · 红心精选")])
        self.assertEqual(result["status"], "partial")
        self.assertEqual(result["completed_count"], 0)
        self.assertTrue(result["applied_to_account"])
        self.assertTrue(result["outcome_known"])
        self.assertEqual(result["items"][0]["count"], 2)
        self.assertEqual(self.cli.writes, ["create", "add"])
        self.assertEqual(len(self.cli.created), 1)

    def test_add_timeout_before_effect_is_uncertain_and_not_repeated(self):
        self.cli.add_mode = "timeout_before"
        first = self.executor.execute([job()])
        self.assertEqual(first["status"], "uncertain")
        self.assertTrue(first["applied_to_account"])
        self.assertFalse(first["outcome_known"])
        second = self.executor.execute([job()])
        self.assertEqual(second["status"], "blocked")
        self.assertEqual(self.cli.writes, ["create", "add"])

    def test_add_timeout_after_exact_effect_is_completed_by_readback(self):
        self.cli.add_mode = "timeout_after"
        result = self.executor.execute([job()])
        self.assertEqual(result["status"], "completed")
        self.assertEqual(self.cli.writes, ["create", "add"])

    def test_failed_business_response_keeps_effect_and_stops(self):
        self.cli.add_mode = "failure_after"
        result = self.executor.execute([job(), job("下一精选")])
        self.assertEqual(result["status"], "uncertain")
        self.assertTrue(result["applied_to_account"])
        self.assertTrue(result["outcome_known"])
        self.assertEqual(result["completed_count"], 0)
        self.assertEqual(self.cli.writes, ["create", "add"])

    def test_create_timeout_is_not_repeated_and_unique_verified_new_list_can_continue(self):
        self.cli.create_mode = "timeout_after"
        result = self.executor.execute([job()])
        self.assertEqual(result["status"], "completed")
        self.assertEqual(self.cli.writes, ["create", "add"])

    def test_failed_or_unknown_create_response_can_continue_only_after_verified_empty_effect(self):
        for mode in ("failure_after", "unknown_after"):
            with self.subTest(mode=mode):
                cli, journal = FakeCli(), Journal()
                cli.create_mode = mode
                result = ArtistExecutor(cli, OWNER, journal).execute([job()])
                self.assertEqual(result["status"], "completed")
                self.assertTrue(result["outcome_known"])
                self.assertTrue(result["applied_to_account"])
                self.assertTrue(result["items"][0]["creation_response_warning"])
                self.assertEqual(cli.writes, ["create", "add"])
                self.assertEqual(len(cli.created), 1)
                self.assertEqual(cli.members[result["items"][0]["playlist_id"]], TRACKS)
                self.assertTrue(journal.records[1]["items"][0]["creation_response_warning"])
                self.assertNotIn(cli.secret, json.dumps(result))
                self.assertNotIn(cli.secret, json.dumps(journal.records))

    def test_failed_create_without_new_playlist_stops_without_add_or_retry(self):
        self.cli.create_mode = "failure_before"
        result = self.executor.execute([job(), job("下一精选")])
        self.assertEqual(result["status"], "blocked")
        self.assertFalse(result["applied_to_account"])
        self.assertEqual(result["completed_count"], 0)
        self.assertEqual(self.cli.writes, ["create"])
        self.assertEqual(self.cli.created, {})

    def test_failed_create_with_ambiguous_unowned_or_nonempty_effect_never_adds(self):
        class UnsafeCreateCli(FakeCli):
            def __init__(self, defect):
                super().__init__()
                self.create_mode, self.defect = "failure_after", defect

            def run_json(self, arguments):
                result = super().run_json(arguments)
                if arguments[:2] == ["playlist", "create"]:
                    ident = next(iter(self.created))
                    if self.defect == "ambiguous":
                        self.make_playlist(arguments[3])
                    elif self.defect == "wrong_owner":
                        self.created[ident]["creatorId"] = "B" * 32
                    else:
                        self.created[ident]["trackCount"] = 1
                        self.members[ident] = [TRACKS[0]]
                return result

        for defect in ("ambiguous", "wrong_owner", "nonempty"):
            with self.subTest(defect=defect):
                cli = UnsafeCreateCli(defect)
                result = ArtistExecutor(cli, OWNER, Journal()).execute([job()])
                self.assertIn(result["status"], {"blocked", "partial", "uncertain"})
                self.assertEqual(result["completed_count"], 0)
                self.assertEqual(cli.writes, ["create"])

    def test_unobserved_or_ambiguous_create_never_adds_or_retries(self):
        for mode in ("timeout_before", "ambiguous"):
            with self.subTest(mode=mode):
                cli = FakeCli()
                cli.create_mode = mode
                result = ArtistExecutor(cli, OWNER, Journal()).execute([job()])
                self.assertEqual(result["status"], "uncertain")
                self.assertFalse(result["outcome_known"])
                self.assertEqual(cli.writes, ["create"])

    def test_confirmed_new_owned_playlist_effect_survives_member_read_failure(self):
        class BrokenMembersCli(FakeCli):
            def run_json(self, arguments):
                if arguments[:2] == ["playlist", "tracks"] and self.created:
                    raise RuntimeError(self.secret)
                return super().run_json(arguments)

        cli = BrokenMembersCli()
        result = ArtistExecutor(cli, OWNER, Journal()).execute([job()])
        self.assertEqual(result["status"], "uncertain")
        self.assertTrue(result["applied_to_account"])
        self.assertFalse(result["outcome_known"])
        self.assertEqual(result["items"][0]["playlist_id"], next(iter(cli.created)))
        self.assertEqual(result["items"][0]["original_playlist_id"], "1001")
        self.assertEqual(cli.writes, ["create"])
        self.assertNotIn(cli.secret, json.dumps(result))

    def test_account_switch_after_last_add_or_reorder_is_uncertain(self):
        class SwitchingCli(FakeCli):
            def __init__(self, change_at):
                super().__init__()
                self.change_at, self.changed = change_at, False
                if change_at == "reorder":
                    self.add_mode = "reverse"

            def run_json(self, arguments):
                if arguments == ["user", "info"] and self.changed:
                    return {"code": 200, "data": {"originalId": 43, "id": "B" * 32}}
                result = super().run_json(arguments)
                if arguments[:2] == ["playlist", self.change_at]:
                    self.changed = True
                return result

        for change_at, expected_writes in (("add", ["create", "add"]),
                                           ("reorder", ["create", "add", "reorder"])):
            with self.subTest(change_at=change_at):
                cli = SwitchingCli(change_at)
                result = ArtistExecutor(cli, OWNER, Journal()).execute([job()])
                self.assertEqual(result["status"], "uncertain")
                self.assertTrue(result["applied_to_account"])
                self.assertFalse(result["outcome_known"])
                self.assertEqual(result["completed_count"], 0)
                self.assertIsNotNone(result["items"][0]["playlist_id"])
                self.assertEqual(cli.writes, expected_writes)
                self.assertEqual(len(cli.created), 1)

    def test_journal_failure_before_create_prevents_mutation(self):
        result = ArtistExecutor(self.cli, OWNER, Journal("create_attempted")).execute([job()])
        self.assertEqual(result["status"], "blocked")
        self.assertFalse(result["write_attempted"])
        self.assertFalse(result["applied_to_account"])
        self.assertEqual(self.cli.writes, [])
        self.assertNotIn(self.cli.secret, json.dumps(result))

    def test_journal_failure_after_creation_keeps_effect_and_prevents_add(self):
        for phase in ("playlist_identified", "add_attempted"):
            with self.subTest(phase=phase):
                cli = FakeCli()
                result = ArtistExecutor(cli, OWNER, Journal(phase)).execute([job(), job("下一精选")])
                self.assertEqual(result["status"], "partial")
                self.assertTrue(result["applied_to_account"])
                self.assertTrue(result["outcome_known"])
                self.assertEqual(cli.writes, ["create"])
                self.assertEqual(len(cli.created), 1)
                self.assertNotIn(cli.secret, json.dumps(result))

    def test_final_journal_failure_preserves_completed_account_result_and_stops(self):
        result = ArtistExecutor(self.cli, OWNER, Journal("completed")).execute([job(), job("下一精选")])
        self.assertEqual(result["status"], "partial")
        self.assertEqual(result["completed_count"], 1)
        self.assertTrue(result["applied_to_account"])
        self.assertTrue(result["outcome_known"])
        self.assertEqual(self.cli.writes, ["create", "add"])

    def test_empty_timestamp_initialization_retries_reads_and_writes_each_command_once(self):
        class TimestampCli(FakeCli):
            def __init__(self, continuous=False):
                super().__init__()
                self.continuous, self.empty_reads = continuous, 0

            def run_json(self, arguments):
                result = super().run_json(arguments)
                if arguments[:2] == ["playlist", "tracks"] and not result["data"]:
                    self.empty_reads += 1
                    if self.continuous or self.empty_reads == 1:
                        self.created[arguments[3]]["trackUpdateTime"] += 1
                return result

        for continuous in (False, True):
            with self.subTest(continuous=continuous):
                cli = TimestampCli(continuous)
                result = ArtistExecutor(cli, OWNER, Journal()).execute([job()])
                self.assertTrue(result["applied_to_account"])
                self.assertIsNotNone(result["items"][0]["playlist_id"])
                if continuous:
                    self.assertEqual(result["status"], "uncertain")
                    self.assertFalse(result["outcome_known"])
                    self.assertEqual(cli.empty_reads, 3)
                    self.assertEqual(cli.writes, ["create"])
                else:
                    self.assertEqual(result["status"], "completed")
                    self.assertEqual(cli.writes, ["create", "add"])
                    self.assertEqual(cli.empty_reads, 2)

    def test_empty_stabilization_never_retries_count_or_owner_changes(self):
        class ChangedCli(FakeCli):
            def __init__(self, field):
                super().__init__()
                self.field, self.empty_reads = field, 0

            def run_json(self, arguments):
                result = super().run_json(arguments)
                if arguments[:2] == ["playlist", "tracks"] and not result["data"]:
                    self.empty_reads += 1
                    self.created[arguments[3]][self.field] = 1 if self.field == "trackCount" else "B" * 32
                return result

        for field in ("trackCount", "creatorId"):
            with self.subTest(field=field):
                cli = ChangedCli(field)
                result = ArtistExecutor(cli, OWNER, Journal()).execute([job()])
                self.assertEqual(result["status"], "uncertain")
                self.assertEqual(cli.empty_reads, 1)
                self.assertEqual(cli.writes, ["create"])

    def test_resume_bound_empty_playlist_skips_create_and_continues_remaining_jobs(self):
        ident = self.cli.make_playlist(job()["name"])
        jobs = [job(), job("蔡健雅 · 红心精选")]
        result = self.executor.resume_created(jobs, recovery(ident))
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["completed_count"], 2)
        self.assertEqual(self.cli.writes, ["add", "create", "add"])
        self.assertEqual(result["items"][0]["playlist_id"], ident)
        self.assertEqual(self.cli.members[ident], TRACKS)
        self.assertEqual(len(self.cli.created), 2)
        self.assertEqual(self.journal.records[0]["phase"], "recovery_identified")
        self.assertEqual(self.journal.records[0]["recovery"], recovery(ident))
        self.assertEqual(self.journal.records[0]["jobs"], jobs)
        self.assertNotIn(self.cli.secret, json.dumps(self.journal.records))

    def test_resume_can_reorder_only_exact_complete_set_once(self):
        ident = self.cli.make_playlist(job()["name"])
        self.cli.add_mode = "reverse"
        result = self.executor.resume_created([job()], recovery(ident))
        self.assertEqual(result["status"], "completed")
        self.assertEqual(self.cli.writes, ["add", "reorder"])
        self.assertEqual(self.cli.members[ident], TRACKS)

    def test_resume_invalid_binding_or_unverified_create_never_falls_back_to_create(self):
        variants = [None, {}, {"create_only_verified": True},
                    {"create_only_verified": False}, {"create_only_verified": 1},
                    {"job_index": 1}, {"job_index": False}, {"playlist_id": "bad"},
                    {"original_playlist_id": "bad"}, {"name": "其他名字"}]
        for changes in variants:
            with self.subTest(changes=changes):
                cli = FakeCli()
                ident = cli.make_playlist(job()["name"])
                binding = None if changes is None else {**recovery(ident), **changes}
                if changes == {}:
                    binding.pop("create_only_verified")
                elif changes == {"create_only_verified": True}:
                    binding = changes
                result = ArtistExecutor(cli, OWNER, Journal()).resume_created([job()], binding)
                self.assertEqual(result["status"], "blocked")
                self.assertEqual(cli.calls, [])
                self.assertEqual(cli.writes, [])

    def test_resume_rechecks_owner_dual_id_empty_and_all_target_name_collisions(self):
        for change in ("wrong_owner", "wrong_original", "wrong_name", "nonempty", "duplicate", "later_name"):
            with self.subTest(change=change):
                cli = FakeCli()
                ident = cli.make_playlist(job()["name"])
                binding = recovery(ident)
                jobs = [job(), job("下一精选")]
                if change == "wrong_owner":
                    cli.created[ident]["creatorId"] = "B" * 32
                elif change == "wrong_original":
                    binding["original_playlist_id"] = "9999"
                elif change == "wrong_name":
                    cli.created[ident]["name"] = "后来改名"
                elif change == "nonempty":
                    cli.created[ident]["trackCount"] = 1
                    cli.members[ident] = [TRACKS[0]]
                elif change == "duplicate":
                    cli.make_playlist(job()["name"])
                else:
                    cli.make_playlist("下一精选")
                result = ArtistExecutor(cli, OWNER, Journal()).resume_created(jobs, binding)
                self.assertIn(result["status"], {"blocked", "partial"})
                self.assertEqual(cli.writes, [])
                if change in {"nonempty", "duplicate", "later_name"}:
                    self.assertTrue(result["applied_to_account"])
                    self.assertEqual(result["items"][0]["playlist_id"], ident)

    def test_resume_journal_failure_preserves_existing_effect_and_prevents_add(self):
        for phase in ("recovery_identified", "add_attempted"):
            with self.subTest(phase=phase):
                cli = FakeCli()
                ident = cli.make_playlist(job()["name"])
                result = ArtistExecutor(cli, OWNER, Journal(phase)).resume_created([job()], recovery(ident))
                self.assertEqual(result["status"], "partial")
                self.assertTrue(result["applied_to_account"])
                self.assertTrue(result["outcome_known"])
                self.assertEqual(result["items"][0]["playlist_id"], ident)
                self.assertEqual(cli.writes, [])

    def test_resume_add_timeout_freezes_without_retry_or_new_creation(self):
        ident = self.cli.make_playlist(job()["name"])
        self.cli.add_mode = "timeout_before"
        first = self.executor.resume_created([job()], recovery(ident))
        self.assertEqual(first["status"], "uncertain")
        self.assertTrue(first["applied_to_account"])
        self.assertFalse(first["outcome_known"])
        second = self.executor.resume_created([job()], recovery(ident))
        self.assertEqual(second["status"], "blocked")
        self.assertEqual(self.cli.writes, ["add"])


if __name__ == "__main__":
    unittest.main()
