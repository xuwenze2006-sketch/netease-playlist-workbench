"""Offline progress evidence for the recorded classification read preflight."""

import contextlib
import copy
import io
import json
import unittest

from netease_organizer import classification_execution as execution
from tests import test_classification_batch_resume as batch_fixtures
from tests import test_classification_resume as fixtures


class ClassificationPreflightProgressTests(unittest.TestCase):
    def fixture(self):
        case = fixtures.ClassificationResumeTests(methodName="runTest")
        case.setUp()
        self.addCleanup(case.doCleanups)
        case.seed_interrupted()
        return case

    def resume(self, case, callback):
        controller = case.new_controller()
        controller._progress = callback
        calls = len(case.cli.calls)
        writes = len(case.cli.writes)
        result = execution.resume_verified_classification(controller)
        return result, case.cli.calls[calls:], case.cli.writes[writes:]

    def test_bound_prefix_progress_does_not_change_reads_or_suffix_writes(self):
        baseline = self.fixture()
        original, reads, writes = self.resume(baseline, lambda event: None)
        observed = self.fixture()
        events = []
        result, actual_reads, actual_writes = self.resume(observed, lambda event: events.append(copy.deepcopy(event)))
        preflight = [event for event in events if event.get("stage") == "classification_preflight"]
        self.assertEqual([event["step"] for event in preflight], [
            "source_start", "playlist_start", "playlist_verified", "playlist_start", "playlist_verified",
            "playlist_start", "playlist_verified", "playlist_start", "playlist_verified", "finished",
        ])
        self.assertEqual([event["completed_count"] for event in preflight], [0, 0, 1, 1, 2, 2, 3, 3, 4, 4])
        self.assertTrue(all(event["total_count"] == 4 for event in preflight))
        self.assertEqual([event["job_index"] for event in preflight if "job_index" in event], [0, 0, 1, 1, 2, 2, 3, 3])
        self.assertEqual([event["name"] for event in preflight if "name" in event], [
            "离线风格", "离线风格", "已完成二", "已完成二", "已完成三", "已完成三", "待核对四", "待核对四",
        ])
        self.assertEqual(actual_reads, reads)
        self.assertEqual(actual_writes, ["create", "add"])
        self.assertEqual(actual_writes, writes)
        self.assertEqual(result["status"], original["status"])
        self.assertEqual(result["completed_count"], 5)

    def test_progress_has_no_private_identifiers_or_account_completion_claim(self):
        case = self.fixture()
        events, output = [], io.StringIO()
        with contextlib.redirect_stdout(output):
            result, _, _ = self.resume(case, lambda event: events.append(copy.deepcopy(event)))
        preflight = [event for event in events if event.get("stage") == "classification_preflight"]
        self.assertEqual(len(preflight), 10)
        allowed = {"stage", "phase", "step", "label", "name", "job_index", "completed_count", "total_count"}
        self.assertTrue(all(set(event) <= allowed for event in preflight))
        self.assertTrue(all(event["phase"] in {"checking", "preflight", "reading"} for event in preflight))
        serialized = json.dumps(preflight, ensure_ascii=False)
        for ident in [case.plan["account_id"], case.plan["source_playlist_id"],
                      *case.plan["source_track_ids"], *[item["playlist_id"] for item in case.old_receipt["items"][:4]]]:
            self.assertNotIn(ident, serialized)
        self.assertNotIn(case.cli.secret, serialized)
        self.assertEqual(output.getvalue(), "")
        self.assertEqual(result["status"], "completed")

    def test_receipt_tampering_does_not_announce_or_run_online_preflight(self):
        case = self.fixture()
        path = case.artifact(execution.RECEIPT_FILE)
        record = json.loads(path.read_text(encoding="utf-8"))
        record["run_id"] = "f" * 32
        path.write_text(json.dumps(record), encoding="utf-8")
        events = []
        result, reads, writes = self.resume(case, lambda event: events.append(copy.deepcopy(event)))
        self.assertFalse(result["outcome_known"])
        self.assertEqual(reads, [])
        self.assertEqual(writes, [])
        self.assertEqual(events, [])

    def test_changed_source_does_not_announce_finished_preflight(self):
        case = self.fixture()
        case.cli.members[case.cli.favorite_id].reverse()
        events = []
        result, _, writes = self.resume(case, lambda event: events.append(copy.deepcopy(event)))
        preflight = [event for event in events if event.get("stage") == "classification_preflight"]
        self.assertEqual([event["step"] for event in preflight], ["source_start"])
        self.assertFalse(result["outcome_known"])
        self.assertEqual(writes, [])
        self.assertEqual(case.artifact(execution.INTENT_FILE).read_bytes(), case.old_intent_bytes)

    def test_pause_requested_from_preflight_keeps_original_evidence_and_no_new_write(self):
        for step in ("source_start", "playlist_start", "finished"):
            with self.subTest(step=step):
                case = self.fixture()
                controller = case.new_controller()
                events = []

                def observe(event):
                    events.append(copy.deepcopy(event))
                    if event.get("stage") == "classification_preflight" and event.get("step") == step:
                        controller.request_pause()

                controller._progress = observe
                calls = len(case.cli.calls)
                result = execution.resume_verified_classification(controller)
                self.assertTrue(any(event.get("step") == step for event in events))
                self.assertFalse(result["outcome_known"])
                self.assertTrue(result["applied_to_account"])
                self.assertEqual(case.cli.writes, case.before_writes)
                self.assertEqual(case.artifact(execution.INTENT_FILE).read_bytes(), case.old_intent_bytes)
                self.assertEqual(case.artifact(execution.RECEIPT_FILE).read_bytes(), case.old_receipt_bytes)
                if step == "source_start":
                    self.assertEqual(len(case.cli.calls), calls)

    def test_progress_callback_error_does_not_interrupt_verified_suffix(self):
        case = self.fixture()
        seen = []

        def fail(event):
            if event.get("stage") == "classification_preflight":
                seen.append(event["step"])
                raise RuntimeError(case.cli.secret)

        result, _, writes = self.resume(case, fail)
        self.assertEqual(len(seen), 10)
        self.assertEqual(result["status"], "completed")
        self.assertEqual(writes, ["create", "add"])
        self.assertNotIn(case.cli.secret, json.dumps(result))

    def test_partial_batch_preflight_count_is_verified_playlists_not_completed_writes(self):
        case = batch_fixtures.ClassificationBatchResumeTests(methodName="runTest")
        case.setUp()
        self.addCleanup(case.doCleanups)
        case.seed()
        controller = case.new_controller()
        events = []

        def observe(event):
            events.append(copy.deepcopy(event))
            if event.get("stage") == "classification_preflight" and event.get("step") == "finished":
                controller.request_pause()

        controller._progress = observe
        result = execution.resume_verified_classification(controller)
        preflight = [event for event in events if event.get("stage") == "classification_preflight"]
        self.assertEqual([event["completed_count"] for event in preflight], [0, 0, 1, 1, 2, 2])
        self.assertTrue(all(event["total_count"] == 2 for event in preflight))
        self.assertEqual(result["completed_count"], 1)
        self.assertTrue(result["applied_to_account"])
        self.assertEqual(len(case.cli.members[case.ident]), 300)
        self.assertFalse(result["outcome_known"])
        self.assertEqual(case.cli.writes, case.old_writes)
        self.assertEqual(case.artifact(execution.INTENT_FILE).read_bytes(), case.old_intent)

    def test_service_listener_preserves_safe_read_preflight_stage(self):
        case = self.fixture()
        controller = case.new_controller()
        events = []
        controller.set_progress_listener(lambda event: events.append(copy.deepcopy(event)))
        result = execution.resume_verified_classification(controller)
        preflight = [event for event in events if event.get("stage") == "classification_preflight"]
        self.assertEqual(len(preflight), 10)
        self.assertEqual(preflight[0]["step"], "source_start")
        self.assertEqual(preflight[-1]["step"], "finished")
        self.assertEqual(preflight[-1]["completed_count"], 4)
        self.assertEqual(preflight[-1]["total_count"], 4)
        self.assertTrue(all(event["phase"] != "completed" for event in preflight))
        self.assertNotIn(case.cli.secret, json.dumps(events))
        self.assertNotIn(case.plan["account_id"], json.dumps(events))
        self.assertEqual(result["status"], "completed")


if __name__ == "__main__":
    unittest.main()
