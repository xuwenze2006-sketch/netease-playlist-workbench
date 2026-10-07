import copy
import hashlib
import json
import unittest

from netease_organizer.rename import RenameExecutor
from netease_organizer.runtime import OperationControl
from tests.test_rename import FakeCli, FIRST, OWNER, OWNER_ENC, SECOND, job


class RenameJournalTests(unittest.TestCase):
    def setUp(self):
        self.cli = FakeCli()

    def test_intent_is_saved_before_each_write_and_contains_only_bound_fields(self):
        self.cli.tracks[FIRST] = ["ab" * 16, "cd" * 16]
        records = []

        def journal(state):
            self.assertEqual(len(self.cli.writes), len(records))
            records.append(copy.deepcopy(state))

        result = RenameExecutor(self.cli, OWNER, journal_writer=journal).execute(
            [job(), job(SECOND, "102", "旧名称", "新名称")])
        self.assertEqual(result["status"], "completed")
        self.assertEqual(len(records), 2)
        self.assertEqual(records[0], {
            "phase": "rename_attempted", "job_index": 0,
            "jobs": [{**job(), "playlist_id": FIRST.upper()},
                     {**job(SECOND, "102", "旧名称", "新名称"), "playlist_id": SECOND.upper()}],
            "items": [{**job(), "playlist_id": FIRST.upper(), "kind": "rename_playlist",
                       "status": "blocked", "message": "本项尚未执行。"},
                      {**job(SECOND, "102", "旧名称", "新名称"), "playlist_id": SECOND.upper(),
                       "kind": "rename_playlist", "status": "blocked", "message": "本项尚未执行。"}],
            "expected_owner_id": OWNER, "owner_id": OWNER_ENC.upper(),
            "before": {"name": "funk", "trackCount": 2, "specialType": 0, "trackUpdateTime": 10},
            "tracks_sha256": hashlib.sha256("\n".join(self.cli.tracks[FIRST]).upper().encode()).hexdigest(),
        })
        self.assertEqual(records[1]["job_index"], 1)
        self.assertEqual(records[1]["items"][0]["status"], "completed")
        self.assertNotIn(self.cli.secret, json.dumps(records))
        self.assertNotIn("creatorId", json.dumps(records))
        self.assertNotIn("tracks", records[0])

    def test_failed_initial_intent_sends_no_write_and_exposes_no_exception(self):
        def journal(state):
            raise OSError(self.cli.secret)

        result = RenameExecutor(self.cli, OWNER, journal_writer=journal).execute([job()])
        self.assertEqual(result["status"], "blocked")
        self.assertEqual(self.cli.writes, [])
        self.assertFalse(result["write_attempted"])
        self.assertFalse(result["applied_to_account"])
        self.assertTrue(result["outcome_known"])
        self.assertNotIn(self.cli.secret, json.dumps(result))

    def test_later_intent_failure_preserves_confirmed_effect_and_stops_remaining(self):
        records = []

        def journal(state):
            records.append(copy.deepcopy(state))
            if len(records) == 2:
                raise PermissionError(self.cli.secret)

        result = RenameExecutor(self.cli, OWNER, journal_writer=journal).execute(
            [job(), job(SECOND, "102", "旧名称", "新名称")])
        self.assertEqual(result["status"], "partial")
        self.assertEqual(result["completed_count"], 1)
        self.assertTrue(result["applied_to_account"])
        self.assertTrue(result["write_attempted"])
        self.assertTrue(result["outcome_known"])
        self.assertEqual(self.cli.writes, [(FIRST, "Funk")])
        self.assertNotIn(self.cli.secret, json.dumps(result))

    def test_pause_is_checked_before_saving_an_intent(self):
        control, records = OperationControl(), []

        def listener(event):
            if event["stage"] == "write":
                control.request_pause()

        result = RenameExecutor(self.cli, OWNER, journal_writer=records.append, control=control,
                                progress_listener=listener).execute([job()])
        self.assertEqual(result["status"], "paused")
        self.assertEqual(records, [])
        self.assertEqual(self.cli.writes, [])

    def test_pause_after_intent_cannot_skip_its_request_or_readback(self):
        control, events = OperationControl(), []

        def journal(state):
            events.append("intent")
            control.request_pause()

        original = self.cli.run_json

        def call(arguments):
            if arguments[:2] == ["playlist", "updateName"]:
                events.append("write")
            return original(arguments)

        self.cli.run_json = call
        result = RenameExecutor(self.cli, OWNER, journal_writer=journal, control=control).execute(
            [job(), job(SECOND, "102", "旧名称", "新名称")])
        self.assertEqual(events, ["intent", "write"])
        self.assertEqual(result["status"], "paused")
        self.assertEqual(result["completed_count"], 1)
        self.assertTrue(result["outcome_known"])
        self.assertEqual(self.cli.calls[-1][:2], ["playlist", "get"])

    def test_writer_cannot_change_the_live_job_or_items_by_mutating_payload(self):
        def journal(state):
            state["jobs"][0]["name"] = "forged"
            state["items"][0]["name"] = "forged"
            state["before"]["trackCount"] = 999

        result = RenameExecutor(self.cli, OWNER, journal_writer=journal).execute([job()])
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["items"][0]["name"], "Funk")
        self.assertEqual(self.cli.writes, [(FIRST, "Funk")])

    def test_large_names_and_batches_are_rejected_before_reading_account(self):
        for jobs in ([job(name="歌" * 161)], [job()] * 1001):
            with self.subTest(size=len(jobs)):
                cli = FakeCli()
                result = RenameExecutor(cli, OWNER).execute(jobs)
                self.assertEqual(result["status"], "blocked")
                self.assertEqual(cli.calls, [])

    def test_pretty_printed_intent_size_is_bounded_before_the_first_write(self):
        jobs = [job(name="歌" * 120)] + [
            job(f"{index:032X}", str(index + 1000), "旧" * 120, "新" * 120)
            for index in range(1, 575)]
        records = []
        result = RenameExecutor(self.cli, OWNER, journal_writer=records.append).execute(jobs)
        self.assertEqual(result["status"], "blocked")
        self.assertEqual(self.cli.writes, [])
        self.assertEqual(records, [])


if __name__ == "__main__":
    unittest.main()
