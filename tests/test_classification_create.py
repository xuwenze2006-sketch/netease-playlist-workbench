import copy
import json
import unittest

from netease_organizer.create import ArtistExecutor
from netease_organizer.runtime import OperationControl
from tests.test_create import FakeCli, Journal, OWNER, job


def classification_job(count=601, name="离线分类"):
    return {**job(name, [f"{i:032X}" for i in range(1, count + 1)]),
            "kind": "create_classification_playlist"}


class AccumulatingCli(FakeCli):
    """Append like the official operation; prohibit accidental full reorder."""

    def __init__(self):
        super().__init__()
        self.add_batches = []
        self.add_failure_at = None
        self.provider_reverse = False
        self.after_add = None

    def run_json(self, arguments):
        if arguments[:2] == ["playlist", "reorder"]:
            raise AssertionError("Classification must not reorder")
        if arguments[:2] != ["playlist", "add"]:
            return super().run_json(arguments)
        self.calls.append(list(arguments))
        if (len(arguments) != 8 or arguments[2] != "--playlistId"
                or arguments[4] != "--songIdList" or arguments[6] != "--userInput"):
            raise AssertionError("Unexpected add contract")
        ident, tracks = arguments[3], json.loads(arguments[5])
        self.writes.append("add")
        self.add_batches.append(tracks)
        if self.add_failure_at == len(self.add_batches):
            # An observed partial effect must never trigger a resend or later batch.
            self.members[ident].extend(tracks[:1])
            self.created[ident]["trackCount"] = len(self.members[ident])
            self.created[ident]["trackUpdateTime"] += 1
            raise TimeoutError(self.secret)
        self.members[ident].extend(tracks)
        if self.provider_reverse:
            self.members[ident].reverse()
        self.created[ident]["trackCount"] = len(self.members[ident])
        self.created[ident]["trackUpdateTime"] += 1
        if self.after_add:
            self.after_add()
        return {"code": 200, "data": True}


def executor(cli, journal, **options):
    return ArtistExecutor(cli, OWNER, journal,
                          job_kind="create_classification_playlist",
                          batch_add_size=300, preserve_order=False, **options)


class ClassificationCreateTests(unittest.TestCase):
    def test_chunks_are_journaled_before_each_write_and_verify_cumulative_members(self):
        cli, journal = AccumulatingCli(), Journal()
        original_run = cli.run_json

        def checked_run(arguments):
            if arguments[:2] == ["playlist", "add"]:
                intent = journal.records[-1]
                self.assertEqual(intent["phase"], "add_attempted")
                self.assertEqual(intent["add_offset"], sum(map(len, cli.add_batches)))
                self.assertEqual(intent["add_count"], len(json.loads(arguments[5])))
            return original_run(arguments)

        cli.run_json = checked_run
        result = executor(cli, journal).execute([classification_job()])
        self.assertEqual(result["status"], "completed")
        self.assertEqual(list(map(len, cli.add_batches)), [300, 300, 1])
        self.assertEqual(cli.writes, ["create", "add", "add", "add"])
        self.assertEqual(result["items"][0]["count"], 601)

    def test_provider_order_is_accepted_without_reorder_or_reorder_manifest(self):
        cli, journal = AccumulatingCli(), Journal()
        cli.provider_reverse = True
        cli.commands = [c for c in cli.commands if c["command"] != ["playlist", "reorder"]]
        result = executor(cli, journal).execute([classification_job(301)])
        self.assertEqual(result["status"], "completed")
        self.assertTrue(result["outcome_known"])
        self.assertEqual(cli.writes, ["create", "add", "add"])

    def test_partial_timeout_stops_without_resend_remaining_batch_or_next_create(self):
        cli, journal = AccumulatingCli(), Journal()
        cli.add_failure_at = 2
        result = executor(cli, journal).execute([classification_job(), classification_job(1, "后项")])
        self.assertEqual(result["status"], "uncertain")
        self.assertEqual(cli.writes, ["create", "add", "add"])
        self.assertTrue(result["applied_to_account"])
        self.assertEqual(result["items"][0]["count"], 301)
        self.assertNotIn(cli.secret, json.dumps(result))

    def test_non_success_response_with_verified_prefix_still_stops_and_does_not_resend(self):
        for response in ({"code": 500, "message": "PRIVATE-TOKEN-DO-NOT-ECHO"}, {}):
            with self.subTest(response_type="rejected" if response else "unknown"):
                cli, journal = AccumulatingCli(), Journal()
                original_run = cli.run_json

                def altered(arguments):
                    actual = original_run(arguments)
                    return response if arguments[:2] == ["playlist", "add"] else actual

                cli.run_json = altered
                result = executor(cli, journal).execute([classification_job()])
                self.assertEqual(result["status"], "uncertain")
                self.assertEqual(cli.writes, ["create", "add"])
                self.assertTrue(result["outcome_known"])
                self.assertEqual(result["items"][0]["count"], 300)

    def test_accepted_incomplete_batch_stops_before_later_add(self):
        cli, journal = AccumulatingCli(), Journal()

        def lose_record():
            ident = next(reversed(cli.created))
            cli.members[ident].pop()
            cli.created[ident]["trackCount"] -= 1

        cli.after_add = lose_record
        result = executor(cli, journal).execute([classification_job()])
        self.assertEqual(result["status"], "partial")
        self.assertEqual(cli.writes, ["create", "add"])
        self.assertTrue(result["outcome_known"])

    def test_pause_during_batch_finishes_readback_and_prevents_next_batch(self):
        cli, journal, control = AccumulatingCli(), Journal(), OperationControl()
        cli.after_add = control.request_pause
        result = executor(cli, journal, control=control).execute([classification_job()])
        self.assertEqual(result["status"], "paused")
        self.assertEqual(cli.writes, ["create", "add"])
        self.assertEqual(result["items"][0]["count"], 300)
        self.assertTrue(result["outcome_known"])
        self.assertEqual(journal.records[-1]["phase"], "paused")

    def test_duplicate_track_and_invalid_chunk_size_send_no_mutation(self):
        cli, journal = AccumulatingCli(), Journal()
        invalid = classification_job(1)
        invalid["candidate_track_ids"] *= 2
        result = executor(cli, journal).execute([invalid])
        self.assertEqual(result["status"], "blocked")
        self.assertEqual(cli.calls, [])
        with self.assertRaises(RuntimeError):
            ArtistExecutor(cli, OWNER, journal, job_kind="create_classification_playlist",
                           batch_add_size=301, preserve_order=False)

    def test_journal_failure_prevents_add_but_preserves_confirmed_create(self):
        cli, journal = AccumulatingCli(), Journal(fail_phase="add_attempted")
        result = executor(cli, journal).execute([classification_job(1)])
        self.assertEqual(result["status"], "partial")
        self.assertEqual(cli.writes, ["create"])
        self.assertTrue(result["applied_to_account"])

    def test_default_artist_executor_keeps_one_add_and_full_order_contract(self):
        cli, journal = FakeCli(), Journal()
        cli.add_mode = "reverse"
        result = ArtistExecutor(cli, OWNER, journal).execute([job()])
        self.assertEqual(result["status"], "completed")
        self.assertEqual(cli.writes, ["create", "add", "reorder"])
        self.assertNotIn("add_offset", journal.records[0])

