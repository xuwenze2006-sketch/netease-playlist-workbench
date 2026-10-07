import copy
import json
import hashlib
import tempfile
import unittest
import os
import subprocess
import sys
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

from netease_organizer.service import Organizer
from netease_organizer.write_journal import write_lease
from tests.test_classification_create import AccumulatingCli
from tests.test_create import OWNER, OWNER_ENC

try:
    from netease_organizer.classification_execution import execute_classification, plan_digest
except ModuleNotFoundError as error:
    if error.name != "netease_organizer.classification_execution":
        raise
    execute_classification = plan_digest = None


class LocalReader:
    def load(self):
        return {"owner_id": OWNER}


class ClassificationCli(AccumulatingCli):
    def __init__(self, count=301):
        super().__init__()
        self.source_ids = [f"{i:032X}" for i in range(1, count + 1)]
        self.favorite_id = self.make_playlist("离线红心", self.source_ids)
        self.created[self.favorite_id]["specialType"] = 5
        self.on_write = None

    def configured(self):
        return True

    def run_json(self, arguments):
        command = arguments[:2]
        if command in (["playlist", "create"], ["playlist", "add"], ["playlist", "reorder"]):
            if self.on_write:
                self.on_write(arguments)
        if arguments == ["user", "favorite"]:
            self.calls.append(list(arguments))
            return {"code": 200, "data": copy.deepcopy(self.created[self.favorite_id])}
        response = super().run_json(arguments)
        if command == ["playlist", "tracks"]:
            response["data"] = [{**row, "originalId": str(int(row["id"], 16)),
                                  "name": "离线歌曲", "artists": []} for row in response["data"]]
        return response


def make_plan(cli):
    return {"kind": "approved_classification_plan", "version": 1,
            "account_original_id": OWNER, "account_id": OWNER_ENC,
            "source_playlist_id": cli.favorite_id,
            "original_source_playlist_id": str(cli.created[cli.favorite_id]["originalId"]),
            "source_track_count": cli.created[cli.favorite_id]["trackCount"],
            "source_track_ids": list(cli.source_ids),
            "jobs": [{"kind": "create_classification_playlist", "name": "离线风格",
                      "dimension": "style", "candidate_track_ids": list(cli.source_ids),
                      "intended_visibility": "provider_default"}]}


class ClassificationExecutionTests(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(execute_classification, "classification executor is missing")
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.project = Path(self.temp.name)
        self.cli = ClassificationCli()
        self.plan = make_plan(self.cli)
        self.controller = self.new_controller()

    def new_controller(self):
        return Organizer(self.project, cli=self.cli, reader=LocalReader(), data_dir=self.project / "desktop")

    def artifact(self, name):
        return self.project / "artifacts" / name

    def test_complete_metadata_partial_baseline_chunks_once_and_keeps_legacy_records(self):
        old = self.artifact("歌手精选执行结果.json")
        old.parent.mkdir()
        legacy = {"status": "completed", "completed_count": 0, "outcome_known": True,
                  "applied_to_account": False, "write_attempted": False, "items": []}
        old.write_text(json.dumps(legacy), encoding="utf-8")
        old_bytes = old.read_bytes()
        intent_phases = []

        def observe(arguments):
            record = json.loads(self.artifact("分类整理执行进度.json").read_text(encoding="utf-8"))
            intent_phases.append(record["phase"])
            self.assertEqual(record["plan_digest"], plan_digest(self.plan))
            self.assertEqual(record["phase"], arguments[1] + "_attempted")

        self.cli.on_write = observe
        result = execute_classification(self.controller, self.plan)
        self.assertEqual(result["status"], "completed")
        self.assertTrue(result["record_saved"])
        self.assertTrue(result["outcome_known"])
        self.assertEqual(self.cli.writes, ["create", "add", "add"])
        self.assertEqual(intent_phases, ["create_attempted", "add_attempted", "add_attempted"])
        self.assertEqual(old.read_bytes(), old_bytes)
        self.assertEqual(self.new_controller().write_recovery()["status"], "clear")
        calls = len(self.cli.calls)
        repeat = execute_classification(self.new_controller(), self.plan)
        self.assertEqual(repeat["status"], "blocked")
        self.assertEqual(len(self.cli.calls), calls)
        self.assertNotIn(self.cli.secret, json.dumps(result))

    def test_multiple_dimensions_allow_overlap_and_reset_each_job_batch_intent(self):
        self.plan["jobs"].extend([
            {**self.plan["jobs"][0], "name": "离线场景", "dimension": "scene",
             "candidate_track_ids": self.plan["source_track_ids"][:2]},
            {**self.plan["jobs"][0], "name": "离线语言", "dimension": "language",
             "candidate_track_ids": self.plan["source_track_ids"][:1]},
        ])
        self.cli.provider_reverse = True
        result = execute_classification(self.controller, self.plan)
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["completed_count"], 3)
        self.assertEqual(self.cli.writes.count("create"), 3)
        self.assertEqual(list(map(len, self.cli.add_batches)), [300, 1, 2, 1])
        self.assertNotIn("reorder", self.cli.writes)
        self.assertEqual(self.new_controller().write_recovery()["status"], "clear")

    def test_changed_fresh_membership_blocks_before_create(self):
        self.cli.members[self.cli.favorite_id][-1] = "B" * 32
        result = execute_classification(self.controller, self.plan)
        self.assertEqual(result["status"], "blocked")
        self.assertFalse(result["write_attempted"])
        self.assertEqual(self.cli.writes, [])
        self.assertFalse(self.artifact("分类整理执行进度.json").exists())

    def test_filtered_source_is_honest_and_uses_only_actual_formal_ids(self):
        self.cli.created[self.cli.favorite_id]["trackCount"] += 1
        self.plan["source_track_count"] += 1
        result = execute_classification(self.controller, self.plan)
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["source_missing_count"], 1)
        self.assertEqual(result["source_observed_count"], 301)

    def test_unknown_batch_freezes_new_controller_and_blocks_all_existing_write_guards(self):
        self.cli.add_failure_at = 2
        result = execute_classification(self.controller, self.plan)
        self.assertEqual(result["status"], "uncertain")
        other = self.new_controller()
        recovery = other.write_recovery()
        self.assertEqual(recovery["status"], "review_required")
        self.assertEqual(recovery["operation"], "classification")
        self.assertFalse(recovery["can_reconcile"])
        calls = len(self.cli.calls)
        self.assertEqual(other.execute_renames()["status"], "blocked")
        self.assertEqual(execute_classification(other, self.plan)["status"], "blocked")
        self.assertEqual(len(self.cli.calls), calls)
        self.assertEqual(self.cli.writes, ["create", "add", "add"])

    def test_new_python_process_restores_freeze_without_cli_or_credentials(self):
        self.cli.add_failure_at = 2
        execute_classification(self.controller, self.plan)
        script = """import json,sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
from netease_organizer.service import Organizer
class NoCli:
    def run_json(self, arguments):
        raise AssertionError('CLI is forbidden in startup recovery')
project=Path(sys.argv[2])
owner=Organizer(project,cli=NoCli(),reader=object(),data_dir=project/'desktop')
print(json.dumps(owner.write_recovery()))
"""
        settings = {"capture_output": True, "text": True, "encoding": "utf-8", "timeout": 5,
                    "shell": False}
        if os.name == "nt":
            settings["creationflags"] = subprocess.CREATE_NO_WINDOW
        process = subprocess.run([sys.executable, "-I", "-c", script,
                                  str(Path(__file__).resolve().parents[1]), str(self.project)], **settings)
        self.assertEqual(process.returncode, 0, "isolated recovery process failed")
        recovery = json.loads(process.stdout)
        self.assertEqual(recovery["status"], "review_required")
        self.assertEqual(recovery["operation"], "classification")

    def test_completed_receipt_save_failure_keeps_effect_and_new_process_freeze(self):
        original_write = self.controller._write_artifact

        def save(name, text):
            if name == "分类整理执行结果.json":
                raise PermissionError(self.cli.secret)
            return original_write(name, text)

        self.controller._write_artifact = save
        result = execute_classification(self.controller, self.plan)
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["completed_count"], 1)
        self.assertTrue(result["applied_to_account"])
        self.assertFalse(result["record_saved"])
        self.assertEqual(self.new_controller().write_recovery()["status"], "review_required")
        self.assertNotIn(self.cli.secret, json.dumps(result))

    def test_local_lease_teardown_error_never_erases_confirmed_account_effects(self):
        @contextmanager
        def failing_exit(path):
            with write_lease(path):
                yield
            raise OSError(self.cli.secret)

        with patch("netease_organizer.classification_execution.write_lease", failing_exit):
            result = execute_classification(self.controller, self.plan)
        self.assertTrue(result["applied_to_account"])
        self.assertTrue(result["write_attempted"])
        self.assertTrue(result["outcome_known"])
        self.assertEqual(result["completed_count"], 1)
        self.assertNotIn(self.cli.secret, json.dumps(result))

    def test_add_intent_save_failure_sends_no_add(self):
        original_write = self.controller._write_artifact

        def save(name, text):
            if name == "分类整理执行进度.json" and json.loads(text)["phase"] == "add_attempted":
                raise PermissionError(self.cli.secret)
            return original_write(name, text)

        self.controller._write_artifact = save
        result = execute_classification(self.controller, self.plan)
        self.assertEqual(result["status"], "partial")
        self.assertTrue(result["applied_to_account"])
        self.assertEqual(self.cli.writes, ["create"])

    def test_receipt_requires_current_intent_digest_run_plan_and_items(self):
        result = execute_classification(self.controller, self.plan)
        self.assertEqual(result["status"], "completed")
        path = self.artifact("分类整理执行结果.json")
        valid = json.loads(path.read_text(encoding="utf-8"))
        for field in ("run_id", "plan_digest", "intent_digest", "items"):
            with self.subTest(field=field):
                wrong = copy.deepcopy(valid)
                if field == "items":
                    wrong["items"][0]["name"] = "错配名称"
                else:
                    wrong[field] = "0" * len(wrong[field])
                path.write_text(json.dumps(wrong), encoding="utf-8")
                self.assertEqual(self.new_controller().write_recovery()["status"], "review_required")
        path.write_text(json.dumps(valid), encoding="utf-8")
        self.assertEqual(self.new_controller().write_recovery()["status"], "clear")

    def test_matching_hashes_cannot_make_incomplete_counts_a_completed_receipt(self):
        execute_classification(self.controller, self.plan)
        intent_path, receipt_path = self.artifact("分类整理执行进度.json"), self.artifact("分类整理执行结果.json")
        intent = json.loads(intent_path.read_text(encoding="utf-8"))
        receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
        intent["items"][0]["count"] -= 1
        receipt["items"] = copy.deepcopy(intent["items"])
        canonical = json.dumps(intent, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
        receipt["intent_digest"] = hashlib.sha256(canonical).hexdigest()
        intent_path.write_bytes(canonical)
        receipt_path.write_text(json.dumps(receipt), encoding="utf-8")
        self.assertEqual(self.new_controller().write_recovery()["status"], "review_required")

    def test_null_and_duplicate_key_journal_are_unresolved_not_missing(self):
        path = self.artifact("分类整理执行进度.json")
        path.parent.mkdir()
        for text in ("null", '{"phase":"completed","phase":"paused"}'):
            path.write_text(text, encoding="utf-8")
            self.assertEqual(self.new_controller().write_recovery()["status"], "review_required")
        self.assertEqual(self.cli.calls, [])

    def test_workspace_path_rejection_degrades_to_freeze_without_startup_exception(self):
        original = self.controller._workspace_target

        def target(relative):
            if "分类整理" in str(relative):
                raise RuntimeError("PRIVATE-TOKEN-DO-NOT-ECHO")
            return original(relative)

        self.controller._workspace_target = target
        self.assertEqual(self.controller.write_recovery()["status"], "review_required")
        self.assertEqual(self.cli.calls, [])

    def test_authoritative_pre_send_intent_freezes_even_before_boolean_update(self):
        captured = []
        original_write = self.controller._write_artifact

        def save(name, text):
            if name == "分类整理执行进度.json" and json.loads(text)["phase"] == "create_attempted":
                captured.append(text)
            return original_write(name, text)

        self.controller._write_artifact = save
        execute_classification(self.controller, self.plan)
        intent = json.loads(captured[0])
        self.assertFalse(intent["write_attempted"])
        self.assertTrue(intent["outcome_known"])
        self.artifact("分类整理执行进度.json").write_text(captured[0], encoding="utf-8")
        self.artifact("分类整理执行结果.json").unlink()
        self.assertEqual(self.new_controller().write_recovery()["status"], "review_required")

    def test_duplicate_or_uncovered_plan_and_wrong_account_have_zero_mutations(self):
        invalids = []
        duplicate = copy.deepcopy(self.plan)
        duplicate["jobs"][0]["candidate_track_ids"].append(duplicate["source_track_ids"][0])
        invalids.append(duplicate)
        uncovered = copy.deepcopy(self.plan)
        uncovered["jobs"][0]["candidate_track_ids"].pop()
        invalids.append(uncovered)
        foreign = copy.deepcopy(self.plan)
        foreign["account_id"] = "B" * 32
        invalids.append(foreign)
        for plan in invalids:
            with self.subTest(plan=invalids.index(plan)):
                result = execute_classification(self.controller, plan)
                self.assertEqual(result["status"], "blocked")
                self.assertFalse(result["write_attempted"])
        self.assertEqual(self.cli.writes, [])

    def test_shared_os_lease_rejects_before_any_sdk_read(self):
        with write_lease(self.project / ".organizer/account-write.lock"):
            result = execute_classification(self.controller, self.plan)
        self.assertEqual(result["status"], "blocked")
        self.assertEqual(self.cli.calls, [])

