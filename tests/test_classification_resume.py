import json
import unittest

from netease_organizer import classification_execution as execution
from netease_organizer.write_journal import write_lease
from tests import test_classification_execution as fixtures


class ClassificationResumeTests(unittest.TestCase):
    setUp = fixtures.ClassificationExecutionTests.setUp
    new_controller = fixtures.ClassificationExecutionTests.new_controller
    artifact = fixtures.ClassificationExecutionTests.artifact

    def seed_interrupted(self):
        first = self.plan["jobs"][0]
        self.plan["jobs"] = [first, *[
            {**first, "name": name, "candidate_track_ids": self.cli.source_ids[:count]}
            for name, count in (("已完成二", 2), ("已完成三", 3), ("待核对四", 301), ("未执行五", 2))
        ]]
        original_run = self.cli.run_json
        failed = False

        def snapshot_fault(arguments):
            nonlocal failed
            if arguments[:2] == ["playlist", "tracks"]:
                ident = arguments[3]
                if (not failed and self.cli.created[ident]["name"] == "待核对四"
                        and len(self.cli.members[ident]) == 301):
                    failed = True
                    raise TimeoutError(self.cli.secret)
            return original_run(arguments)

        self.cli.run_json = snapshot_fault
        result = execution.execute_classification(self.controller, self.plan)
        self.cli.run_json = original_run
        self.assertEqual(result["status"], "uncertain")
        self.assertFalse(result["outcome_known"])
        self.assertEqual(result["completed_count"], 3)
        self.assertEqual(self.cli.writes.count("create"), 4)
        self.assertEqual(len(self.cli.members[result["items"][3]["playlist_id"]]), 301)
        self.before_writes = list(self.cli.writes)
        self.old_intent_bytes = self.artifact(execution.INTENT_FILE).read_bytes()
        self.old_receipt_bytes = self.artifact(execution.RECEIPT_FILE).read_bytes()
        self.old_intent = json.loads(self.old_intent_bytes)
        self.old_receipt = json.loads(self.old_receipt_bytes)
        return result

    def resume(self):
        callback = getattr(execution, "resume_verified_classification", None)
        self.assertIsNotNone(callback, "verified classification continuation is missing")
        return callback(self.new_controller(), accept_default_visibility=True)

    def test_full_unknown_job_is_read_verified_and_only_suffix_is_created_once(self):
        old = self.seed_interrupted()
        prefix = [i["playlist_id"] for i in old["items"][:4]]
        self.cli.provider_reverse = True
        observed = []

        def before_write(arguments):
            record = json.loads(self.artifact(execution.INTENT_FILE).read_text(encoding="utf-8"))
            observed.append(record)
            self.assertEqual(record["job_index"], 4)
            self.assertEqual(len(record["jobs"]), 5)
            self.assertEqual([i["playlist_id"] for i in record["items"][:4]], prefix)
            self.assertEqual(record["completed_count"], 4)
            self.assertEqual(self.new_controller().write_recovery()["status"], "review_required")

        self.cli.on_write = before_write
        result = self.resume()
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["completed_count"], 5)
        self.assertTrue(result["record_saved"])
        self.assertEqual(self.cli.writes[len(self.before_writes):], ["create", "add"])
        self.assertEqual([i["playlist_id"] for i in result["items"][:4]], prefix)
        self.assertNotIn("reorder", self.cli.writes)
        self.assertTrue(observed)
        run_id = self.old_intent["run_id"]
        self.assertEqual(self.artifact(f"分类整理恢复备份-{run_id}-执行进度.json").read_bytes(), self.old_intent_bytes)
        self.assertEqual(self.artifact(f"分类整理恢复备份-{run_id}-执行结果.json").read_bytes(), self.old_receipt_bytes)
        latest = json.loads(self.artifact(execution.INTENT_FILE).read_text(encoding="utf-8"))
        self.assertNotEqual(latest["run_id"], run_id)
        self.assertEqual(latest["plan_digest"], execution.plan_digest(self.plan))
        self.assertEqual(self.new_controller().write_recovery()["status"], "clear")
        calls = len(self.cli.calls)
        self.assertNotEqual(self.resume()["status"], "completed")
        self.assertEqual(len(self.cli.calls), calls)

    def test_partial_unknown_or_changed_completed_prefix_never_gets_missing_songs(self):
        old = self.seed_interrupted()
        for index in (3, 0):
            with self.subTest(index=index):
                ident = old["items"][index]["playlist_id"]
                original = list(self.cli.members[ident])
                self.cli.members[ident].pop()
                self.cli.created[ident]["trackCount"] -= 1
                result = self.resume()
                self.assertEqual(result["status"], "uncertain")
                self.assertFalse(result["outcome_known"])
                self.assertTrue(result["applied_to_account"])
                self.assertEqual(self.cli.writes, self.before_writes)
                self.assertEqual(self.artifact(execution.INTENT_FILE).read_bytes(), self.old_intent_bytes)
                self.cli.members[ident] = original
                self.cli.created[ident]["trackCount"] += 1

    def test_plan_receipt_and_attempt_identity_tampering_fail_before_account_read(self):
        self.seed_interrupted()
        paths = [(execution.PLAN_FILE, "account_id"), (execution.RECEIPT_FILE, "run_id"),
                 (execution.RECEIPT_FILE, "intent_digest"), (execution.RECEIPT_FILE, "items")]
        for name, field in paths:
            with self.subTest(name=name, field=field):
                path = self.artifact(name)
                before = path.read_bytes()
                raw = json.loads(before)
                if field == "items":
                    raw[field][3]["original_playlist_id"] = "987654"
                else:
                    raw[field] = "B" * len(raw[field])
                path.write_text(json.dumps(raw), encoding="utf-8")
                calls = len(self.cli.calls)
                result = self.resume()
                self.assertFalse(result["outcome_known"])
                self.assertEqual(len(self.cli.calls), calls)
                self.assertEqual(self.cli.writes, self.before_writes)
                path.write_bytes(before)

    def test_suffix_same_name_and_fresh_source_changes_block_without_new_writes(self):
        self.seed_interrupted()
        ident = self.cli.make_playlist("未执行五")
        result = self.resume()
        self.assertFalse(result["outcome_known"])
        self.assertEqual(self.cli.writes, self.before_writes)
        del self.cli.created[ident], self.cli.members[ident]
        self.cli.members[self.cli.favorite_id].reverse()
        result = self.resume()
        self.assertFalse(result["outcome_known"])
        self.assertEqual(self.cli.writes, self.before_writes)

    def test_other_unresolved_operation_and_shared_lease_block_before_read(self):
        self.seed_interrupted()
        other = self.artifact("名称整理执行进度.json")
        other.write_text("null", encoding="utf-8")
        calls = len(self.cli.calls)
        result = self.resume()
        self.assertFalse(result["outcome_known"])
        self.assertTrue(result["applied_to_account"])
        self.assertTrue(result["write_attempted"])
        self.assertEqual(result["completed_count"], 3)
        self.assertEqual(len(self.cli.calls), calls)
        other.unlink()
        with write_lease(self.project / ".organizer/account-write.lock"):
            result = self.resume()
        self.assertNotEqual(result["status"], "completed")
        self.assertEqual(len(self.cli.calls), calls)
        self.assertEqual(self.cli.writes, self.before_writes)

    def test_backup_or_new_intent_failure_preserves_original_unknown_effects(self):
        self.seed_interrupted()
        for stage in ("备份", "执行进度"):
            with self.subTest(stage=stage):
                controller = self.new_controller()
                original = controller._write_artifact

                def fail(name, text):
                    if (stage == "备份" and "恢复备份" in name
                            or stage == "执行进度" and name == execution.INTENT_FILE):
                        raise PermissionError(self.cli.secret)
                    return original(name, text)

                controller._write_artifact = fail
                callback = getattr(execution, "resume_verified_classification", None)
                self.assertIsNotNone(callback)
                result = callback(controller)
                self.assertFalse(result["outcome_known"])
                self.assertTrue(result["applied_to_account"])
                self.assertEqual(self.cli.writes, self.before_writes)
                self.assertEqual(self.artifact(execution.INTENT_FILE).read_bytes(), self.old_intent_bytes)
                self.assertNotIn(self.cli.secret, json.dumps(result))
                for path in self.artifact(execution.INTENT_FILE).parent.glob("分类整理恢复备份-*"):
                    path.unlink()

    def test_suffix_receipt_save_failure_keeps_five_confirmed_effects_and_freeze(self):
        self.seed_interrupted()
        controller = self.new_controller()
        original = controller._write_artifact

        def fail(name, text):
            if name == execution.RECEIPT_FILE:
                raise OSError(self.cli.secret)
            return original(name, text)

        controller._write_artifact = fail
        callback = getattr(execution, "resume_verified_classification", None)
        self.assertIsNotNone(callback)
        result = callback(controller)
        self.assertEqual(result["completed_count"], 5)
        self.assertTrue(result["applied_to_account"])
        self.assertTrue(result["outcome_known"])
        self.assertFalse(result["record_saved"])
        self.assertEqual(self.new_controller().write_recovery()["status"], "review_required")
        self.assertEqual(self.cli.writes[len(self.before_writes):], ["create", "add"])
        self.assertNotIn(self.cli.secret, json.dumps(result))

    def test_incomplete_new_suffix_remains_bound_and_cannot_be_partially_resumed(self):
        self.seed_interrupted()
        # A one-song effect in the two-song suffix must never be supplemented.
        self.cli.add_failure_at = len(self.cli.add_batches) + 1
        result = self.resume()
        self.assertEqual(result["status"], "uncertain")
        self.assertEqual(result["completed_count"], 4)
        self.assertTrue(result["applied_to_account"])
        self.assertFalse(result["outcome_known"])
        self.assertIsNotNone(result["items"][4]["playlist_id"])
        writes = list(self.cli.writes)
        repeat = self.resume()
        self.assertFalse(repeat["outcome_known"])
        self.assertEqual(self.cli.writes, writes)
        self.assertEqual(self.new_controller().write_recovery()["status"], "review_required")

    def test_wrong_created_owner_or_dual_identity_has_no_suffix_mutation(self):
        old = self.seed_interrupted()
        ident = old["items"][3]["playlist_id"]
        header = self.cli.created[ident]
        for field, value in (("creatorId", "B" * 32), ("originalId", 9999), ("specialType", 5)):
            with self.subTest(field=field):
                before = header[field]
                header[field] = value
                result = self.resume()
                self.assertFalse(result["outcome_known"])
                self.assertTrue(result["applied_to_account"])
                self.assertEqual(self.cli.writes, self.before_writes)
                header[field] = before

    def backup_retry(self, stage):
        self.seed_interrupted()
        controller = self.new_controller()
        original = controller._write_artifact

        def fail(name, text):
            if (stage == "执行结果备份" and name.endswith("-执行结果.json")
                    or stage == "新checkpoint" and name == execution.INTENT_FILE):
                raise OSError(self.cli.secret)
            return original(name, text)

        controller._write_artifact = fail
        result = execution.resume_verified_classification(controller)
        self.assertFalse(result["outcome_known"])
        self.assertEqual(self.cli.writes, self.before_writes)
        self.assertEqual(self.artifact(execution.INTENT_FILE).read_bytes(), self.old_intent_bytes)
        # Retry only local preparation and fresh reads; no prior mutation
        # is replayed, and an already durable exact backup is reusable.
        result = self.resume()
        self.assertEqual(result["status"], "completed")
        self.assertEqual(self.cli.writes[len(self.before_writes):], ["create", "add"])

    def test_exact_intent_backup_reused_after_receipt_backup_failure(self):
        self.backup_retry("执行结果备份")

    def test_exact_two_backups_reused_after_new_checkpoint_failure(self):
        self.backup_retry("新checkpoint")

    def test_existing_different_backup_is_never_overwritten(self):
        self.seed_interrupted()
        backup = self.artifact(f"分类整理恢复备份-{self.old_intent['run_id']}-执行进度.json")
        backup.write_text("{}\n", encoding="utf-8")
        before = backup.read_bytes()
        result = self.resume()
        self.assertFalse(result["outcome_known"])
        self.assertEqual(self.cli.writes, self.before_writes)
        self.assertEqual(backup.read_bytes(), before)
        self.assertEqual(self.artifact(execution.INTENT_FILE).read_bytes(), self.old_intent_bytes)
