"""Durable rename recovery at a fake official CLI boundary."""

import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from netease_organizer.service import Organizer, _APPROVED_ARTIST_COUNTS
from test_rename import FakeCli, FIRST, OWNER, SECOND, job


class RecoveryCli(FakeCli):
    def configured(self):
        return True

    def installed(self):
        return True

    def version(self):
        return "fake-1"


class WriteRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.cli = RecoveryCli()

    def tearDown(self):
        self.temp.cleanup()

    def organizer(self):
        reader = Mock()
        reader.load.return_value = {"owner_id": int(OWNER), "playlists": []}
        organizer = Organizer(self.root, cli=self.cli, reader=reader,
                              data_dir=self.root / "cache")
        organizer._online_snapshot = {"account": {"original_id": OWNER}}
        organizer._online_plan = {"jobs": [{**job(), "kind": "rename_playlist", "status": "ready"}]}
        return organizer

    def receipt(self):
        return self.root / "artifacts/名称整理执行结果.json"

    def test_unknown_restart_blocks_all_writes_without_overwriting_original_receipt(self):
        self.cli.mode = "timeout_before"
        first = self.organizer().execute_renames()
        self.assertEqual(first["status"], "uncertain")
        original = self.receipt().read_bytes()
        calls = len(self.cli.calls)
        restarted = self.organizer()
        self.assertEqual(restarted.write_recovery(), {
            "status": "review_required", "operation": "renames", "can_reconcile": True})
        for execute in (restarted.execute_renames,
                        lambda: restarted.execute_artist_playlists(accept_default_visibility=True),
                        restarted.resume_last_operation):
            self.assertEqual(execute()["status"], "blocked")
            self.assertEqual(self.receipt().read_bytes(), original)
        self.assertEqual(len(self.cli.writes), 1)
        self.assertEqual(len(self.cli.calls), calls)

    def test_newer_other_completed_receipt_does_not_hide_unknown_rename(self):
        self.cli.mode = "timeout_before"
        self.organizer().execute_renames()
        (self.root / "artifacts/歌手精选执行结果.json").write_text(json.dumps({
            "status": "completed", "completed_count": 5, "items": [],
            "outcome_known": True, "write_attempted": True, "applied_to_account": True}), encoding="utf-8")
        restarted = self.organizer()
        self.assertEqual(restarted.write_recovery()["status"], "review_required")
        self.assertEqual(restarted.execute_renames()["status"], "blocked")
        self.assertEqual(len(self.cli.writes), 1)

    def test_receipt_save_failure_keeps_intent_across_restart(self):
        organizer = self.organizer()
        original_write = organizer._write_artifact

        def write(filename, text):
            if filename == "名称整理执行结果.json":
                raise PermissionError("PRIVATE-PATH")
            return original_write(filename, text)

        organizer._write_artifact = write
        result = organizer.execute_renames()
        self.assertEqual(result["status"], "completed")
        self.assertFalse(result["record_saved"])
        restarted = self.organizer()
        self.assertEqual(restarted.write_recovery()["status"], "review_required")
        self.assertEqual(restarted.execute_renames()["status"], "blocked")
        self.assertEqual(len(self.cli.writes), 1)

    def test_explicit_read_only_reconcile_confirms_target_and_reliably_unfreezes(self):
        self.cli.mode = "timeout_before"
        self.organizer().execute_renames()
        # Model a delayed application of the original request, never a replay.
        self.cli.playlists[FIRST]["name"] = "Funk"
        self.cli.playlists[FIRST]["trackUpdateTime"] = 11
        restarted = self.organizer()
        result = restarted.reconcile_renames()
        self.assertEqual(result["status"], "completed")
        self.assertTrue(result["record_saved"])
        self.assertTrue(result["outcome_known"])
        self.assertFalse(result["write_attempted"])
        self.assertEqual(result['items'][0]['status'], 'completed')
        self.assertEqual(restarted.write_recovery()["status"], "clear")
        self.assertEqual(self.organizer().write_recovery()["status"], "clear")
        self.assertEqual(len(self.cli.writes), 1)
        self.assertNotIn(self.cli.secret, json.dumps(result))

    def test_old_name_or_changed_order_stays_pending_without_writing(self):
        self.cli.mode = "timeout_before"
        self.organizer().execute_renames()
        original = self.receipt().read_bytes()
        self.assertEqual(self.organizer().reconcile_renames()["status"], "uncertain")
        self.assertEqual(self.receipt().read_bytes(), original)
        self.cli.playlists[FIRST]["name"] = "Funk"
        self.cli.tracks[FIRST].reverse()
        self.assertEqual(self.organizer().reconcile_renames()["status"], "uncertain")
        self.assertEqual(self.receipt().read_bytes(), original)
        self.assertEqual(len(self.cli.writes), 1)

    def test_reconcile_save_failure_does_not_clear_pending(self):
        self.cli.mode = "timeout_before"
        self.organizer().execute_renames()
        self.cli.playlists[FIRST]["name"] = "Funk"
        restarted = self.organizer()
        restarted._write_artifact = Mock(side_effect=PermissionError("PRIVATE-PATH"))
        result = restarted.reconcile_renames()
        self.assertTrue(result["applied_to_account"])
        self.assertFalse(result["record_saved"])
        self.assertEqual(result['items'][0]['status'], 'completed')
        self.assertEqual(self.organizer().write_recovery()["status"], "review_required")
        self.assertEqual(len(self.cli.writes), 1)

    def test_legacy_unknown_receipt_requires_review_without_auto_account_reads(self):
        self.receipt().parent.mkdir(parents=True)
        self.receipt().write_text(json.dumps({"status": "uncertain", "completed_count": 0,
            "items": [{**job(), "kind": "rename_playlist", "status": "uncertain"}],
            "write_attempted": True, "outcome_known": False, "applied_to_account": False}), encoding="utf-8")
        restarted = self.organizer()
        self.assertEqual(restarted.write_recovery(), {
            "status": "review_required", "operation": "renames", "can_reconcile": False})
        self.assertEqual(restarted.execute_renames()["status"], "blocked")
        self.assertEqual(self.cli.calls, [])

    def test_write_intent_failure_blocks_before_account_mutation(self):
        organizer = self.organizer()
        original = organizer._write_artifact

        def fail_intent(filename, text):
            if filename == "名称整理执行进度.json":
                raise PermissionError("PRIVATE-PATH")
            return original(filename, text)

        organizer._write_artifact = fail_intent
        result = organizer.execute_renames()
        self.assertEqual(result["status"], "blocked")
        self.assertFalse(result["write_attempted"])
        self.assertEqual(self.cli.writes, [])
        self.assertEqual(self.organizer().write_recovery()["status"], "clear")

    def test_crash_after_durable_intent_retains_gate_without_receipt(self):
        run_json = self.cli.run_json

        def interrupted(arguments):
            if arguments[:2] == ["playlist", "updateName"]:
                raise SystemExit("simulated process exit")
            return run_json(arguments)

        self.cli.run_json = interrupted
        with self.assertRaises(SystemExit):
            self.organizer().execute_renames()
        self.assertFalse(self.receipt().exists())
        restarted = self.organizer()
        self.assertEqual(restarted.write_recovery()["status"], "review_required")
        calls = len(self.cli.calls)
        self.assertEqual(restarted.execute_renames()["status"], "blocked")
        self.assertEqual(len(self.cli.calls), calls)

    def test_reconcile_identity_members_and_stability_failures_never_write(self):
        mutations = {
            "encrypted_identity": lambda: self.cli.playlists[FIRST].update(id="d" * 32),
            "original_identity": lambda: self.cli.playlists[FIRST].update(originalId="999"),
            "owner": lambda: self.cli.playlists[FIRST].update(creatorId="d" * 32),
            "count": lambda: self.cli.playlists[FIRST].update(trackCount=3),
            "type": lambda: self.cli.playlists[FIRST].update(specialType=1),
            "short_page": lambda: self.cli.tracks[FIRST].pop(),
            "duplicate_page": lambda: self.cli.tracks[FIRST].__setitem__(1, self.cli.tracks[FIRST][0]),
        }
        for name, mutate in mutations.items():
            with self.subTest(name=name):
                self.cli = RecoveryCli()
                self.cli.mode = "timeout_before"
                organizer = self.organizer()
                # Each subcase uses a fresh local batch, not manually cleared protection.
                with tempfile.TemporaryDirectory() as case_dir:
                    organizer.project = Path(case_dir)
                    organizer.data_dir = Path(case_dir) / "cache"
                    organizer.execute_renames()
                    self.cli.playlists[FIRST]["name"] = "Funk"
                    mutate()
                    result = organizer.reconcile_renames()
                    self.assertEqual(result["status"], "uncertain")
                    self.assertEqual(organizer.write_recovery()["status"], "review_required")
                    self.assertEqual(len(self.cli.writes), 1)

    def test_final_account_check_prevents_unfreezing_after_identity_change(self):
        self.cli.mode = "timeout_before"
        self.organizer().execute_renames()
        self.cli.playlists[FIRST]["name"] = "Funk"
        run_json = self.cli.run_json
        account_reads = 0

        def changed_after_read(arguments):
            nonlocal account_reads
            result = run_json(arguments)
            if arguments == ["user", "info"]:
                account_reads += 1
                if account_reads == 2:
                    result["data"]["id"] = "d" * 32
            return result

        self.cli.run_json = changed_after_read
        result = self.organizer().reconcile_renames()
        self.assertEqual(result["status"], "uncertain")
        self.assertEqual(account_reads, 2)
        self.assertEqual(len(self.cli.writes), 1)

    def test_reconcile_pending_batch_does_not_start_remaining_jobs(self):
        self.cli.mode = "timeout_before"
        organizer = self.organizer()
        organizer._online_plan["jobs"].append({
            **job(SECOND, "102", "旧名称", "新名称"), "kind": "rename_playlist", "status": "ready"})
        organizer.execute_renames()
        self.cli.playlists[FIRST]["name"] = "Funk"
        restarted = self.organizer()
        self.assertEqual(restarted.reconcile_renames()["status"], "completed")
        receipt = json.loads(self.receipt().read_text(encoding="utf-8"))
        self.assertEqual(receipt["status"], "paused")
        self.assertEqual(receipt["items"][1]["status"], "pending")
        self.assertEqual(self.cli.playlists[SECOND]["name"], "旧名称")
        self.assertEqual(len(self.cli.writes), 1)
        self.assertEqual(restarted.write_recovery()["status"], "clear")

    def test_later_unknown_item_preserves_confirmed_prefix_during_failed_reconcile(self):
        organizer = self.organizer()
        organizer._online_plan["jobs"].append({
            **job(SECOND, "102", "旧名称", "新名称"), "kind": "rename_playlist", "status": "ready"})
        run_json = self.cli.run_json

        def fail_second(arguments):
            if arguments[:2] == ["playlist", "updateName"] and arguments[3].lower() == SECOND:
                self.cli.mode = "timeout_before"
            return run_json(arguments)

        self.cli.run_json = fail_second
        initial = organizer.execute_renames()
        self.assertEqual(initial["completed_count"], 1)
        self.assertEqual(initial["status"], "uncertain")
        for changed_account in (False, True):
            with self.subTest(changed_account=changed_account):
                self.cli.owner_changes = changed_account
                restarted = self.organizer()
                result = restarted.reconcile_renames()
                self.assertEqual(result["status"], "uncertain")
                self.assertEqual(result["completed_count"], 1)
                self.assertTrue(result["applied_to_account"])
                self.assertEqual(result["items"][0]["status"], "completed")
                self.assertEqual(restarted.write_recovery()["status"], "review_required")
                self.assertEqual(len(self.cli.writes), 2)

    def test_service_lease_covers_readback_and_receipt_save(self):
        organizer = self.organizer()
        original_write = organizer._write_artifact
        entered, release = threading.Event(), threading.Event()
        results = []

        def held_receipt(filename, text):
            if filename == "名称整理执行结果.json":
                entered.set()
                if not release.wait(10):
                    raise RuntimeError("test release was not reached")
            return original_write(filename, text)

        organizer._write_artifact = held_receipt
        worker = threading.Thread(target=lambda: results.append(organizer.execute_renames()))
        worker.start()
        try:
            self.assertTrue(entered.wait(10))
            calls = len(self.cli.calls)
            other = self.organizer()
            self.assertEqual(other.execute_renames()["status"], "blocked")
            self.assertEqual(len(self.cli.calls), calls)
            self.assertEqual(len(self.cli.writes), 1)
        finally:
            release.set()
            worker.join(10)
        self.assertFalse(worker.is_alive())
        self.assertEqual(results[0]["status"], "completed")

    def test_unavailable_lease_is_a_known_zero_write_failure(self):
        from netease_organizer.write_journal import JournalError
        with patch('netease_organizer.write_journal.write_lease', side_effect=JournalError('PRIVATE-PATH')):
            result = self.organizer().execute_renames()
        self.assertEqual(result['status'], 'blocked')
        self.assertTrue(result['outcome_known'])
        self.assertFalse(result['write_attempted'])
        self.assertFalse(result['applied_to_account'])
        self.assertNotIn('PRIVATE', json.dumps(result))
        self.assertEqual(self.cli.calls, [])
        self.assertEqual(self.organizer().write_recovery()['status'], 'clear')

    def test_artist_attempt_phase_is_pending_even_before_boolean_change(self):
        path = self.root / "artifacts/歌手精选执行进度.json"
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps({"kind": "artist_execution_journal", "phase": "create_attempted",
            "run_id": "1" * 32, "status": "running", "completed_count": 0,
            "write_attempted": False, "outcome_known": True,
            "jobs": [{} for _ in range(5)], "items": [{} for _ in range(5)]}), encoding="utf-8")
        restarted = self.organizer()
        self.assertEqual(restarted.write_recovery(), {
            "status": "review_required", "operation": "artists", "can_reconcile": False})
        self.assertEqual(restarted.execute_renames()["status"], "blocked")
        self.assertEqual(self.cli.calls, [])

    def test_new_artist_receipt_cannot_use_legacy_fallback_for_another_batch(self):
        artifacts = self.root / 'artifacts'
        artifacts.mkdir()
        receipt = {'status': 'paused', 'completed_count': 0, 'outcome_known': True,
                   'write_attempted': False, 'applied_to_account': False,
                   'items': [{} for _ in range(5)], 'run_id': '1' * 32}
        progress = {**receipt, 'kind': 'artist_execution_journal', 'phase': 'paused',
                    'stage': 'finished', 'run_id': '2' * 32, 'jobs': [{} for _ in range(5)]}
        (artifacts / '歌手精选执行结果.json').write_text(json.dumps(receipt), encoding='utf-8')
        (artifacts / '歌手精选执行进度.json').write_text(json.dumps(progress), encoding='utf-8')
        self.assertEqual(self.organizer().write_recovery()['status'], 'review_required')
        receipt.pop('run_id')
        (artifacts / '歌手精选执行结果.json').write_text(json.dumps(receipt), encoding='utf-8')
        self.assertEqual(self.organizer().write_recovery()['status'], 'clear')

    def test_completed_legacy_recovery_merges_prefix_and_four_remaining_jobs(self):
        artifacts = self.root / 'artifacts'
        artifacts.mkdir()
        items, jobs = [], []
        for index, (name, count) in enumerate(_APPROVED_ARTIST_COUNTS.items()):
            items.append({'kind': 'create_artist_playlist', 'name': name, 'status': 'completed',
                          'playlist_id': f'{index + 101:032X}', 'original_playlist_id': str(index + 101),
                          'count': count, 'expected_count': count, 'message': '已核对'})
            jobs.append({'kind': 'create_artist_playlist', 'name': name,
                         'candidate_track_ids': [f'{index * 100 + track + 1:032X}' for track in range(count)],
                         'intended_visibility': 'provider_default'})
        receipt = {'status': 'completed', 'completed_count': 5, 'outcome_known': True,
                   'write_attempted': True, 'applied_to_account': True, 'items': items}
        progress = {**receipt, 'kind': 'artist_execution_journal', 'phase': 'completed',
                    'stage': 'finished', 'expected_owner_id': OWNER, 'account_original_id': OWNER,
                    'previous_completed_items': items[:1], 'jobs': jobs[1:]}
        (artifacts / '歌手精选执行结果.json').write_text(json.dumps(receipt), encoding='utf-8')
        path = artifacts / '歌手精选执行进度.json'
        path.write_text(json.dumps(progress), encoding='utf-8')
        restarted = self.organizer()
        self.assertEqual(restarted.write_recovery()['status'], 'clear')
        self.assertEqual(restarted.execute_artist_playlists(accept_default_visibility=True)['status'], 'blocked')
        self.assertEqual(self.cli.calls, [])
        for changed in ('missing_prefix', 'unconfirmed', 'new_run_id', 'wrong_count', 'wrong_job'):
            with self.subTest(changed=changed):
                altered = json.loads(json.dumps(progress))
                if changed == 'missing_prefix':
                    altered.pop('previous_completed_items')
                elif changed == 'unconfirmed':
                    altered['outcome_known'] = False
                elif changed == 'new_run_id':
                    altered['run_id'] = '1' * 32
                elif changed == 'wrong_count':
                    altered['items'][0]['count'] = 0
                else:
                    altered['jobs'][0]['candidate_track_ids'].pop()
                path.write_text(json.dumps(altered), encoding='utf-8')
                self.assertEqual(restarted.write_recovery()['status'], 'review_required')

    def test_damaged_or_oversized_local_evidence_fails_closed_without_raw_errors(self):
        for content in (b'{"status":"completed","status":"uncertain"}',
                        b'PRIVATE-PATH', b'x' * (1024 * 1024 + 1)):
            with self.subTest(size=len(content)), tempfile.TemporaryDirectory() as case_dir:
                organizer = self.organizer()
                organizer.project = Path(case_dir)
                organizer.data_dir = Path(case_dir) / "cache"
                path = organizer.project / "artifacts/名称整理执行进度.json"
                path.parent.mkdir(parents=True)
                path.write_bytes(content)
                recovery = organizer.write_recovery()
                self.assertEqual(recovery, {
                    "status": "review_required", "operation": "renames", "can_reconcile": False})
                self.assertNotIn("PRIVATE", json.dumps(organizer.execute_renames()))
        self.assertEqual(self.cli.calls, [])

    def test_deeply_nested_receipt_is_a_bounded_local_review_state(self):
        self.receipt().parent.mkdir()
        self.receipt().write_bytes(b'[' * 2000 + b'0' + b']' * 2000)
        result = self.organizer().write_recovery()
        self.assertEqual(result, {'status': 'review_required', 'operation': 'renames', 'can_reconcile': False})
        self.assertEqual(self.cli.calls, [])

    def test_wrong_type_status_or_phase_is_a_safe_local_review_state(self):
        artifacts = self.root / 'artifacts'
        artifacts.mkdir()
        for operation, filename, record in (
            ('renames', '名称整理执行结果.json', {'status': [], 'completed_count': 0, 'items': []}),
            ('artists', '歌手精选执行进度.json', {'kind': 'artist_execution_journal', 'phase': []})):
            with self.subTest(operation=operation):
                path = artifacts / filename
                path.write_text(json.dumps(record), encoding='utf-8')
                try:
                    result = self.organizer().write_recovery()
                    self.assertEqual(result, {'status': 'review_required', 'operation': operation, 'can_reconcile': False})
                finally:
                    path.unlink()
        self.assertEqual(self.cli.calls, [])


if __name__ == "__main__":
    unittest.main()
