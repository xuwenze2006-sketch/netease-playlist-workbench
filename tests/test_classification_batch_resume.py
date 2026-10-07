import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from netease_organizer import classification_execution as execution
from netease_organizer.create import ArtistExecutor, ArtistError
from tests.test_create import OWNER, Journal, job
from tests import test_classification_execution as fixtures


class ClassificationBatchResumeTests(unittest.TestCase):
    new_controller = fixtures.ClassificationExecutionTests.new_controller
    artifact = fixtures.ClassificationExecutionTests.artifact

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.project = Path(self.temp.name)
        self.cli = fixtures.ClassificationCli(329)
        self.plan = fixtures.make_plan(self.cli)
        main = self.plan["jobs"][0]
        self.plan["jobs"] = [
            {**main, "name": "完整旧前缀", "candidate_track_ids": self.cli.source_ids[:1]},
            {**main, "name": "绑定当前项"},
            {**main, "name": "未触及后缀", "candidate_track_ids": self.cli.source_ids[:2]},
        ]
        self.controller = self.new_controller()

    def seed(self):
        original = self.cli.run_json
        failed = False

        def fault(arguments):
            nonlocal failed
            if arguments[:2] == ["playlist", "tracks"]:
                ident = arguments[3]
                if (not failed and self.cli.created[ident]["name"] == "绑定当前项"
                        and len(self.cli.members[ident]) == 300):
                    failed = True
                    raise RuntimeError(self.cli.secret)
            return original(arguments)

        self.cli.run_json = fault
        result = execution.execute_classification(self.controller, self.plan)
        self.cli.run_json = original
        self.assertEqual(result["status"], "uncertain")
        self.assertEqual(result["completed_count"], 1)
        self.assertFalse(result["outcome_known"])
        self.ident = result["items"][1]["playlist_id"]
        self.assertEqual(len(self.cli.members[self.ident]), 300)
        self.old_writes = list(self.cli.writes)
        self.old_intent = self.artifact(execution.INTENT_FILE).read_bytes()
        self.old_receipt = self.artifact(execution.RECEIPT_FILE).read_bytes()
        self.old_run = json.loads(self.old_intent)["run_id"]

    def resume(self, controller=None):
        return execution.resume_verified_classification(controller or self.new_controller())

    def test_only_unattempted_29_then_new_suffix_are_written_with_full_coordinates(self):
        self.seed()
        observed = []
        self.cli.provider_reverse = True

        def observe(arguments):
            record = json.loads(self.artifact(execution.INTENT_FILE).read_text(encoding="utf-8"))
            observed.append(record)
            self.assertEqual(len(record["jobs"]), 3)
            self.assertEqual(record["plan_digest"], execution.plan_digest(self.plan))
            self.assertEqual(self.new_controller().write_recovery()["status"], "review_required")
            if arguments[:2] == ["playlist", "add"] and arguments[3] == self.ident:
                self.assertEqual((record["job_index"], record["add_offset"], record["add_count"]), (1, 300, 29))
                self.assertEqual(record["items"][1]["expected_count"], 329)
                self.assertEqual(record["items"][1]["added_count"], 300)
                self.assertEqual(json.loads(arguments[5]), self.cli.source_ids[300:])

        self.cli.on_write = observe
        result = self.resume()
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["completed_count"], 3)
        self.assertEqual(self.cli.writes[len(self.old_writes):], ["add", "create", "add"])
        self.assertEqual([len(x) for x in self.cli.add_batches], [1, 300, 29, 2])
        self.assertTrue(result["record_saved"])
        self.assertNotIn("reorder", self.cli.writes)
        self.assertEqual(self.new_controller().write_recovery()["status"], "clear")
        self.assertEqual(self.artifact(f"分类整理恢复备份-{self.old_run}-执行进度.json").read_bytes(), self.old_intent)
        self.assertEqual(self.artifact(f"分类整理恢复备份-{self.old_run}-执行结果.json").read_bytes(), self.old_receipt)
        self.assertTrue(observed)

    def test_short_long_or_same_count_wrong_members_cannot_resume(self):
        self.seed()
        expected = list(self.cli.members[self.ident])
        variants = [expected[:297], expected[:299], expected + [self.cli.source_ids[300]],
                    expected[:-1] + [self.cli.source_ids[300]]]
        for members in variants:
            with self.subTest(count=len(members)):
                self.cli.members[self.ident] = members
                self.cli.created[self.ident]["trackCount"] = len(members)
                result = self.resume()
                self.assertFalse(result["outcome_known"])
                self.assertEqual(self.cli.writes, self.old_writes)
                self.assertEqual(self.artifact(execution.INTENT_FILE).read_bytes(), self.old_intent)

    def test_rehashed_offset_count_and_preimage_tamper_is_rejected(self):
        self.seed()
        for field, value in (("add_offset", 1), ("add_count", 299), ("preimage", 1)):
            with self.subTest(field=field):
                intent, receipt = json.loads(self.old_intent), json.loads(self.old_receipt)
                if field == "preimage":
                    intent["items"][1]["added_count"] = value
                else:
                    intent[field] = value
                receipt["intent_digest"] = hashlib.sha256(execution._encoded(intent)).hexdigest()
                self.artifact(execution.INTENT_FILE).write_text(json.dumps(intent), encoding="utf-8")
                self.artifact(execution.RECEIPT_FILE).write_text(json.dumps(receipt), encoding="utf-8")
                result = self.resume()
                self.assertFalse(result["outcome_known"])
                self.assertEqual(self.cli.writes, self.old_writes)
        self.artifact(execution.INTENT_FILE).write_bytes(self.old_intent)
        self.artifact(execution.RECEIPT_FILE).write_bytes(self.old_receipt)

    def pause_during_save(self, phase):
        self.seed()
        controller = self.new_controller()
        save = controller._write_artifact
        requested = False

        def pause(name, text):
            nonlocal requested
            path = save(name, text)
            if name == execution.INTENT_FILE:
                record = json.loads(text)
                if not requested and record["phase"] == phase:
                    requested = True
                    controller.control.request_pause()
            return path

        controller._write_artifact = pause
        result = self.resume(controller)
        self.assertEqual(result["status"], "paused")
        self.assertTrue(result["outcome_known"])
        self.assertTrue(result["applied_to_account"])
        self.assertTrue(result["record_saved"])
        self.assertEqual(self.cli.writes, self.old_writes)
        record = json.loads(self.artifact(execution.INTENT_FILE).read_text(encoding="utf-8"))
        self.assertEqual(record["phase"], "paused")
        self.assertEqual((record["add_offset"], record["add_count"]), (0, 300))
        self.assertEqual(record["items"][1]["count"], 300)
        self.assertEqual(record["items"][1]["added_count"], 300)
        resumed = self.resume()
        self.assertEqual(resumed["status"], "completed")
        self.assertEqual(self.cli.writes[len(self.old_writes):], ["add", "create", "add"])

    def test_pause_after_verified_new_checkpoint_can_continue_29(self):
        self.pause_during_save("add_verified")

    def test_pause_after_attempt_journal_before_sdk_preserves_verified_300(self):
        self.pause_during_save("add_attempted")

    def test_new_checkpoint_save_failure_preserves_original_and_retries_only_unsent_tail(self):
        self.seed()
        controller = self.new_controller()
        save = controller._write_artifact

        def fail(name, text):
            if name == execution.INTENT_FILE and json.loads(text)["run_id"] != self.old_run:
                raise PermissionError(self.cli.secret)
            return save(name, text)

        controller._write_artifact = fail
        result = self.resume(controller)
        self.assertFalse(result["outcome_known"])
        self.assertTrue(result["applied_to_account"])
        self.assertEqual(self.cli.writes, self.old_writes)
        self.assertEqual(self.artifact(execution.INTENT_FILE).read_bytes(), self.old_intent)
        self.assertEqual(self.resume()["status"], "completed")
        self.assertEqual(self.cli.writes[len(self.old_writes):], ["add", "create", "add"])

    def test_after_tail_success_pause_keeps_completed_current_and_untouched_suffix(self):
        self.seed()
        controller = self.new_controller()
        self.cli.after_add = controller.control.request_pause
        result = self.resume(controller)
        self.assertEqual(result["status"], "paused")
        self.assertEqual(result["completed_count"], 2)
        self.assertEqual(self.cli.writes[len(self.old_writes):], ["add"])
        self.cli.after_add = None
        self.assertEqual(self.resume()["status"], "completed")
        self.assertEqual(self.cli.writes[len(self.old_writes):], ["add", "create", "add"])

    def test_attempted_tail_with_no_effect_or_incomplete_effect_is_never_resent(self):
        self.seed()
        original = self.cli.run_json

        def timeout_before(arguments):
            if arguments[:2] == ["playlist", "add"] and arguments[3] == self.ident:
                self.cli.calls.append(list(arguments))
                self.cli.writes.append("add")
                raise TimeoutError(self.cli.secret)
            return original(arguments)

        self.cli.run_json = timeout_before
        result = self.resume()
        self.cli.run_json = original
        self.assertEqual(result["status"], "uncertain")
        self.assertEqual(len(self.cli.members[self.ident]), 300)
        writes = list(self.cli.writes)
        again = self.resume()
        self.assertFalse(again["outcome_known"])
        self.assertEqual(self.cli.writes, writes)
        self.assertEqual(self.new_controller().write_recovery()["status"], "review_required")

    def test_tail_readback_failure_and_pause_stays_unknown_then_only_skips_full_current(self):
        self.seed()
        controller = self.new_controller()
        original = self.cli.run_json
        failed = False
        self.cli.after_add = controller.control.request_pause

        def fault(arguments):
            nonlocal failed
            if (not failed and arguments[:2] == ["playlist", "tracks"] and arguments[3] == self.ident
                    and len(self.cli.members[self.ident]) == 329):
                failed = True
                raise RuntimeError(self.cli.secret)
            return original(arguments)

        self.cli.run_json = fault
        result = self.resume(controller)
        self.cli.run_json, self.cli.after_add = original, None
        self.assertEqual(result["status"], "uncertain")
        self.assertFalse(result["outcome_known"])
        record = json.loads(self.artifact(execution.INTENT_FILE).read_text(encoding="utf-8"))
        self.assertEqual((record["phase"], record["add_offset"], record["add_count"]), ("add_attempted", 300, 29))
        self.assertEqual(self.resume()["status"], "completed")
        self.assertEqual(self.cli.writes[len(self.old_writes):], ["add", "create", "add"])

    def test_629_two_unknown_complete_batches_never_repeat_either_300(self):
        self.cli = fixtures.ClassificationCli(629)
        self.plan = fixtures.make_plan(self.cli)
        main = self.plan["jobs"][0]
        self.plan["jobs"] = [
            {**main, "name": "完整旧前缀", "candidate_track_ids": self.cli.source_ids[:1]},
            {**main, "name": "绑定当前项"},
            {**main, "name": "未触及后缀", "candidate_track_ids": self.cli.source_ids[:2]},
        ]
        self.controller = self.new_controller()
        self.seed()
        original = self.cli.run_json
        failed = False

        def fault(arguments):
            nonlocal failed
            if (not failed and arguments[:2] == ["playlist", "tracks"] and arguments[3] == self.ident
                    and len(self.cli.members[self.ident]) == 600):
                failed = True
                raise RuntimeError(self.cli.secret)
            return original(arguments)

        self.cli.run_json = fault
        result = self.resume()
        self.cli.run_json = original
        self.assertEqual(result["status"], "uncertain")
        self.assertFalse(result["outcome_known"])
        self.assertEqual(self.cli.writes[len(self.old_writes):], ["add"])
        record = json.loads(self.artifact(execution.INTENT_FILE).read_text(encoding="utf-8"))
        self.assertEqual((record["job_index"], record["add_offset"], record["add_count"]), (1, 300, 300))
        self.assertEqual(record["items"][1]["expected_count"], 629)
        self.assertEqual(len(self.cli.members[self.ident]), 600)
        self.assertEqual(self.resume()["status"], "completed")
        self.assertEqual(self.cli.writes[len(self.old_writes):], ["add", "add", "create", "add"])
        self.assertEqual(self.cli.add_batches, [self.cli.source_ids[:1], self.cli.source_ids[:300],
                                              self.cli.source_ids[300:600], self.cli.source_ids[600:],
                                              self.cli.source_ids[:2]])
        self.assertNotIn("reorder", self.cli.writes)

    def test_partial_effect_of_attempted_tail_cannot_be_supplemented(self):
        self.seed()
        self.cli.add_failure_at = len(self.cli.add_batches) + 1
        result = self.resume()
        self.assertEqual(result["status"], "uncertain")
        self.assertEqual(len(self.cli.members[self.ident]), 301)
        writes = list(self.cli.writes)
        again = self.resume()
        self.assertFalse(again["outcome_known"])
        self.assertEqual(self.cli.writes, writes)

    def test_batch_recovery_is_not_an_artist_resume_entry(self):
        actor = ArtistExecutor(self.cli, OWNER, Journal())
        with self.assertRaises(ArtistError):
            actor.resume_verified_batches([job()], {})
        self.assertEqual(self.cli.calls, [])

    def test_missing_batch_binding_never_degrades_to_new_creation(self):
        actor = ArtistExecutor(self.cli, OWNER, Journal(), job_kind="create_classification_playlist",
                               batch_add_size=300, preserve_order=False)
        jobs = [{**job("恢复目标", self.cli.source_ids), "kind": "create_classification_playlist"}]
        for recovery in (None, {}, [], True, "invalid"):
            with self.subTest(recovery_type=type(recovery).__name__), self.assertRaises(ArtistError):
                actor.resume_verified_batches(jobs, recovery)
        self.assertEqual(self.cli.calls, [])
        self.assertEqual(self.cli.writes, [])

    def test_final_receipt_failure_preserves_all_confirmed_effects_and_protection(self):
        self.seed()
        controller = self.new_controller()
        save = controller._write_artifact

        def fail(name, text):
            if name == execution.RECEIPT_FILE:
                raise PermissionError(self.cli.secret)
            return save(name, text)

        controller._write_artifact = fail
        result = self.resume(controller)
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["completed_count"], 3)
        self.assertTrue(result["applied_to_account"])
        self.assertTrue(result["outcome_known"])
        self.assertFalse(result["record_saved"])
        self.assertNotIn(self.cli.secret, json.dumps(result))
        self.assertEqual(self.new_controller().write_recovery()["status"], "review_required")


