import copy
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

try:
    from netease_organizer import write_journal
except ImportError:
    write_journal = None


def intent():
    job = {"playlist_id": "b" * 32, "original_playlist_id": "101", "old_name": "旧名称", "name": "新名称"}
    return {"kind": "rename_execution_journal", "version": 1, "run_id": "d" * 32,
            "phase": "rename_attempted", "job_index": 0, "jobs": [job],
            "items": [{**job, "kind": "rename_playlist", "status": "blocked", "message": "本项尚未执行。"}],
            "expected_owner_id": "42", "owner_id": "a" * 32,
            "before": {"name": "旧名称", "trackCount": 2, "specialType": 0, "trackUpdateTime": 10},
            "tracks_sha256": hashlib.sha256(b"00000000000000000000000000000001\n00000000000000000000000000000002").hexdigest()}


class WriteJournalTests(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(write_journal, "write journal implementation is missing")
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "intent.json"
        self.receipt = self.path.with_name("receipt.json")

    def save(self, data, path=None):
        (path or self.path).write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")

    def final_receipt(self, state, *, status="completed", item_status="completed"):
        return {"run_id": state["run_id"], "intent_digest": write_journal.intent_digest(state),
                "status": status, "outcome_known": True, "completed_count": int(item_status == "completed"),
                "items": [{**state["items"][0], "status": item_status}],
                "performance": {"elapsed_seconds": 2}, "message": "ignored raw message", "token": "ignored"}

    def test_missing_intent_is_none_and_valid_unicode_intent_is_canonical_copy(self):
        self.assertIsNone(write_journal.read_rename_intent(self.path))
        original = intent()
        self.save(original)
        result = write_journal.read_rename_intent(self.path)
        self.assertEqual(result["owner_id"], "A" * 32)
        self.assertEqual(result["jobs"][0]["playlist_id"], "B" * 32)
        self.assertEqual(result["jobs"][0]["name"], "新名称")
        self.assertEqual(original["owner_id"], "a" * 32)

    def test_bad_json_duplicate_keys_and_nonfinite_values_fail_safely(self):
        for raw in (b"{", b'{"run_id":"SECRET","run_id":"again"}', b'{"value":NaN}',
                    b"[]", b"null", b'"SECRET"', b"\xff"):
            with self.subTest(raw=raw[:1]):
                self.path.write_bytes(raw)
                with self.assertRaises(write_journal.JournalError) as caught:
                    write_journal.read_rename_intent(self.path)
                self.assertNotIn("SECRET", str(caught.exception))

    def test_bound_identity_types_and_preimage_cannot_be_omitted_or_conflict(self):
        changes = [lambda s: s.update(version=True), lambda s: s.update(job_index=True),
                   lambda s: s.update(job_index=1), lambda s: s.update(owner_id="bad"),
                   lambda s: s.update(expected_owner_id="042"), lambda s: s.update(run_id="bad"),
                   lambda s: s.update(tracks_sha256="bad"), lambda s: s.update(extra="SECRET"),
                   lambda s: s["jobs"][0].update(name=["bad"]),
                   lambda s: s["jobs"][0].update(name="名" * 161),
                   lambda s: s["items"][0].update(playlist_id="c" * 32),
                   lambda s: s["items"][0].update(old_name="other"),
                   lambda s: s["items"][0].update(status=["blocked"]),
                   lambda s: s["before"].update(name="other"),
                   lambda s: s["before"].update(trackCount=True),
                   lambda s: s["before"].update(specialType=5),
                   lambda s: s["before"].update(trackUpdateTime=-1)]
        for change in changes:
            state = intent()
            change(state)
            self.save(state)
            with self.subTest(change=changes.index(change)):
                with self.assertRaises(write_journal.JournalError):
                    write_journal.read_rename_intent(self.path)

    def test_duplicate_identity_and_too_large_batch_or_file_are_rejected(self):
        state = intent()
        state["jobs"] *= 2
        state["items"] *= 2
        self.save(state)
        with self.assertRaises(write_journal.JournalError):
            write_journal.read_rename_intent(self.path)
        state["jobs"] *= 501
        state["items"] *= 501
        self.save(state)
        with self.assertRaises(write_journal.JournalError):
            write_journal.read_rename_intent(self.path)
        self.path.write_bytes(b" " * (1024 * 1024 + 1))
        with self.assertRaises(write_journal.JournalError):
            write_journal.read_rename_intent(self.path)

    def test_directory_intent_is_not_a_missing_record(self):
        self.path.mkdir()
        with self.assertRaises(write_journal.JournalError):
            write_journal.read_rename_intent(self.path)

    def test_target_symlink_is_rejected(self):
        target = self.path.with_name("target.json")
        self.save(intent(), target)
        try:
            self.path.symlink_to(target)
        except OSError:
            self.skipTest("This Windows environment does not permit test symlinks")
        with self.assertRaises(write_journal.JournalError):
            write_journal.read_rename_intent(self.path)

    def test_digest_binds_all_validated_evidence_and_uses_canonical_utf8_json(self):
        state = intent()
        self.save(state)
        validated = write_journal.read_rename_intent(self.path)
        expected = hashlib.sha256(json.dumps(validated, ensure_ascii=False, sort_keys=True,
                                            separators=(",", ":"), allow_nan=False).encode("utf-8")).hexdigest()
        self.assertEqual(write_journal.intent_digest(state), expected)
        for update in ({"run_id": "e" * 32}, {"owner_id": "f" * 32}, {"expected_owner_id": "43"},
                       {"tracks_sha256": "0" * 64}):
            self.assertNotEqual(write_journal.intent_digest({**state, **update}), expected)
        changed = copy.deepcopy(state)
        changed["before"]["trackUpdateTime"] += 1
        self.assertNotEqual(write_journal.intent_digest(changed), expected)

    def test_only_same_digest_run_and_known_current_terminal_receipt_resolve(self):
        state = intent()
        for status, item_status in (("completed", "completed"), ("blocked", "blocked"),
                                    ("paused", "completed"), ("partial", "completed")):
            with self.subTest(status=status):
                self.save(self.final_receipt(state, status=status, item_status=item_status), self.receipt)
                self.assertTrue(write_journal.receipt_resolves_intent(state, self.receipt))
        invalid = [lambda r: r.update(run_id="e" * 32), lambda r: r.update(intent_digest="0" * 64),
                   lambda r: r.update(outcome_known=False), lambda r: r.update(outcome_known=1),
                   lambda r: r.update(status="uncertain"), lambda r: r.update(status=["completed"]),
                   lambda r: r.update(completed_count=True), lambda r: r.update(record_saved=False),
                   lambda r: r["items"][0].update(status="pending"),
                   lambda r: r["items"][0].update(original_playlist_id="102"),
                   lambda r: r["items"][0].update(playlist_id="c" * 32),
                   lambda r: r["items"][0].update(old_name="other"),
                   lambda r: r["items"][0].update(name="other"), lambda r: r.update(items=[])]
        for change in invalid:
            receipt = self.final_receipt(state)
            change(receipt)
            self.save(receipt, self.receipt)
            with self.subTest(change=invalid.index(change)):
                self.assertFalse(write_journal.receipt_resolves_intent(state, self.receipt))

    def test_missing_corrupt_duplicate_or_oversize_receipt_never_resolves(self):
        self.assertFalse(write_journal.receipt_resolves_intent(intent(), self.receipt))
        for raw in (b"bad", b'{"status":"completed","status":"blocked"}', b" " * (1024 * 1024 + 1)):
            self.receipt.write_bytes(raw)
            self.assertFalse(write_journal.receipt_resolves_intent(intent(), self.receipt))

    def test_partial_or_paused_receipt_preserves_prior_results_and_binds_current_index(self):
        state = intent()
        first = state["jobs"][0]
        current = {**first, "playlist_id": "c" * 32, "original_playlist_id": "102", "name": "第二项"}
        later = {**first, "playlist_id": "e" * 32, "original_playlist_id": "103", "name": "第三项"}
        state.update(job_index=1, jobs=[first, current, later], items=[
            {**first, "kind": "rename_playlist", "status": "completed", "message": "已核对。"},
            {**current, "kind": "rename_playlist", "status": "blocked", "message": "本项尚未执行。"},
            {**later, "kind": "rename_playlist", "status": "blocked", "message": "本项尚未执行。"}])
        for status, current_status, final_status in (("paused", "completed", "pending"),
                                                    ("partial", "blocked", "blocked")):
            receipt = {"run_id": state["run_id"], "intent_digest": write_journal.intent_digest(state),
                       "status": status, "outcome_known": True,
                       "completed_count": 1 + int(current_status == "completed"),
                       "items": [{**state["items"][0]}, {**state["items"][1], "status": current_status},
                                 {**state["items"][2], "status": final_status}]}
            self.save(receipt, self.receipt)
            self.assertTrue(write_journal.receipt_resolves_intent(state, self.receipt))
            changed = copy.deepcopy(receipt)
            changed["items"][0]["status"] = "blocked"
            changed["completed_count"] -= 1
            self.save(changed, self.receipt)
            self.assertFalse(write_journal.receipt_resolves_intent(state, self.receipt))
            changed = copy.deepcopy(receipt)
            changed["items"][1]["status"] = "pending"
            changed["completed_count"] = 1
            self.save(changed, self.receipt)
            self.assertFalse(write_journal.receipt_resolves_intent(state, self.receipt))

    def test_growing_file_is_read_with_a_limit_instead_of_trusting_stat(self):
        self.save(intent())
        opened = os.fdopen
        observed = []

        class GrowingReader:
            def __init__(self, stream):
                self.stream = stream

            def __enter__(self):
                return self

            def __exit__(self, *args):
                self.stream.close()

            def fileno(self):
                return self.stream.fileno()

            def read(self, size=-1):
                with self_path.open("ab") as stream:
                    stream.write(b" " * (1024 * 1024 + 100))
                result = self.stream.read(size)
                observed.append((size, len(result)))
                return result

        self_path = self.path
        with patch.object(write_journal.os, "fdopen", side_effect=lambda *a, **k: GrowingReader(opened(*a, **k))):
            with self.assertRaises(write_journal.JournalError):
                write_journal.read_rename_intent(self.path)
        self.assertEqual(observed, [(1024 * 1024 + 1, 1024 * 1024 + 1)])

    def test_same_process_lease_competes_and_releases_on_exception(self):
        lease = self.path.with_name("account-write.lock")
        with write_journal.write_lease(lease):
            with self.assertRaises(write_journal.WriteLeaseBusy):
                with write_journal.write_lease(lease):
                    self.fail("concurrent lease acquired")
        with self.assertRaisesRegex(RuntimeError, "fake crash"):
            with write_journal.write_lease(lease):
                raise RuntimeError("fake crash")
        with write_journal.write_lease(lease):
            pass
        self.assertTrue(lease.is_file())

    def test_child_process_competes_then_abrupt_exit_releases_its_os_lease(self):
        lease = self.path.with_name("account-write.lock")
        script = ("import os,sys\nfrom netease_organizer.write_journal import write_lease\n"
                  "with write_lease(sys.argv[1]):\n print('ready',flush=True)\n"
                  " sys.stdin.readline()\n os._exit(23)\n")
        process = subprocess.Popen([sys.executable, "-c", script, str(lease)],
                                   cwd=Path(__file__).resolve().parents[1], stdin=subprocess.PIPE,
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                                   encoding="utf-8", shell=False,
                                   creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
        ready, line = threading.Event(), []

        def read_ready():
            line.append(process.stdout.readline())
            ready.set()

        reader = threading.Thread(target=read_ready, daemon=True)
        reader.start()
        try:
            self.assertTrue(ready.wait(3), "own fake lease helper did not become ready")
            self.assertEqual(line, ["ready\n"])
            with self.assertRaises(write_journal.WriteLeaseBusy):
                with write_journal.write_lease(lease):
                    self.fail("child lease was not exclusive")
            process.communicate("crash\n", timeout=3)
            self.assertEqual(process.returncode, 23)
            with write_journal.write_lease(lease):
                pass
        finally:
            if process.poll() is None:
                process.kill()  # Only this disposable helper, never a product process.
                process.communicate(timeout=3)
            reader.join(1)
            for stream in (process.stdin, process.stdout, process.stderr):
                stream.close()


if __name__ == "__main__":
    unittest.main()
