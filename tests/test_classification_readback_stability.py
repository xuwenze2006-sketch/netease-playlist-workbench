import json
import unittest

from netease_organizer.create import ArtistExecutor
from netease_organizer.official_cli import CliError
from netease_organizer.runtime import OperationControl
from tests.test_classification_create import AccumulatingCli, classification_job, executor
from tests.test_create import Journal, OWNER, job


class ReadbackCli(AccumulatingCli):
    def __init__(self, mode):
        super().__init__()
        self.mode = mode
        self.post_add = False
        self.post_gets = 0
        self.post_tracks = 0

    def run_json(self, arguments):
        command = arguments[:2]
        if self.post_add and command == ["playlist", "tracks"]:
            self.post_tracks += 1
            if self.mode == "cli_error":
                raise CliError(self.secret, code="cli_timeout")
        response = super().run_json(arguments)
        if command == ["playlist", "add"]:
            self.post_add = True
            return response
        if not self.post_add:
            return response
        if command == ["user", "info"] and self.mode == "account_change":
            response["data"].update(originalId=43, id="B" * 32)
        if command == ["playlist", "get"]:
            index = self.post_gets
            self.post_gets += 1
            raw = response["data"]
            if self.mode == "persistent":
                raw["trackUpdateTime"] += index
            elif self.mode in {"once", "account_change", "wrong_drifting", "special_between"}:
                raw["trackUpdateTime"] += int(index > 0)
            if self.mode == "special_between" and index >= 2:
                raw["specialType"] = 50
            if index == 1:
                if self.mode == "count_change":
                    raw["trackCount"] += 1
                elif self.mode == "special_change":
                    raw["specialType"] = 50
                elif self.mode == "identity_change":
                    raw["originalId"] += 1
                elif self.mode == "owner_change":
                    raw["creatorId"] = "B" * 32
        if command == ["playlist", "tracks"]:
            if self.mode in {"wrong", "wrong_drifting"}:
                response["data"][-1]["id"] = "F" * 32
            elif self.mode == "duplicate":
                response["data"][-1]["id"] = response["data"][0]["id"]
            elif self.mode == "bad_page":
                response["data"] = {"token": self.secret}
        return response


class ClassificationReadbackStabilityTests(unittest.TestCase):
    def run_mode(self, mode):
        cli = ReadbackCli(mode)
        result = executor(cli, Journal()).execute([classification_job(3)])
        self.assertEqual(cli.writes, ["create", "add"])
        self.assertNotIn(cli.secret, json.dumps(result))
        self.assertNotIn("reorder", cli.writes)
        return cli, result

    def test_one_timestamp_drift_then_exact_stable_readback_completes_without_new_add(self):
        cli, result = self.run_mode("once")
        self.assertEqual(result["status"], "completed")
        self.assertTrue(result["outcome_known"])
        self.assertEqual(cli.post_tracks, 2)
        # Ordinary artist reads keep their original one-shot stability contract.
        cli = ReadbackCli("once")
        result = ArtistExecutor(cli, OWNER, Journal()).execute([job()])
        self.assertEqual(result["status"], "uncertain")
        self.assertEqual(cli.post_tracks, 1)
        self.assertEqual(cli.writes, ["create", "add"])
        # A pause arriving after the add cannot interrupt the bounded readback.
        cli, control = ReadbackCli("once"), OperationControl()
        cli.after_add = control.request_pause
        result = executor(cli, Journal(), control=control).execute(
            [classification_job(3), classification_job(1, "后项")])
        self.assertEqual(result["status"], "paused")
        self.assertEqual(result["completed_count"], 1)
        self.assertTrue(result["outcome_known"])
        self.assertEqual(cli.post_tracks, 2)
        self.assertEqual(cli.writes, ["create", "add"])

    def test_continuous_timestamp_drift_stops_after_three_complete_readbacks(self):
        cli, result = self.run_mode("persistent")
        self.assertEqual(result["status"], "uncertain")
        self.assertFalse(result["outcome_known"])
        self.assertTrue(result["applied_to_account"])
        self.assertEqual(cli.post_tracks, 3)

    def test_count_or_special_type_change_is_not_retried(self):
        for mode in ("count_change", "special_change", "special_between"):
            with self.subTest(mode=mode):
                cli, result = self.run_mode(mode)
                self.assertEqual(result["status"], "uncertain")
                self.assertEqual(cli.post_tracks, 2 if mode == "special_between" else 1)

    def test_wrong_same_count_members_are_never_retried_even_when_stamp_drifts(self):
        for mode in ("wrong", "wrong_drifting"):
            with self.subTest(mode=mode):
                cli, result = self.run_mode(mode)
                self.assertIn(result["status"], {"partial", "uncertain"})
                self.assertEqual(cli.post_tracks, 1)

    def test_playlist_identity_owner_or_current_account_change_is_not_retried(self):
        for mode in ("identity_change", "owner_change", "account_change"):
            with self.subTest(mode=mode):
                cli, result = self.run_mode(mode)
                self.assertEqual(result["status"], "uncertain")
                self.assertEqual(cli.post_tracks, 1)

    def test_cli_error_bad_page_and_duplicate_members_stop_immediately(self):
        for mode in ("cli_error", "bad_page", "duplicate"):
            with self.subTest(mode=mode):
                cli, result = self.run_mode(mode)
                self.assertEqual(result["status"], "uncertain")
                self.assertFalse(result["outcome_known"])
                self.assertEqual(cli.post_tracks, 1)

