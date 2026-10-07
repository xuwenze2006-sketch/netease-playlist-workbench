import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from netease_organizer.service import Organizer, OrganizerError
from test_planning import playlist, snapshot
from test_online_planning import live_snapshot


class OrganizerServiceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.cli = Mock()
        self.cli.installed.return_value = True
        self.cli.configured.return_value = False
        self.cli.version.return_value = "0.1.7"
        self.reader = Mock()
        self.reader.load.return_value = snapshot([playlist(1, "funk")])
        self.controller = Organizer(self.root, cli=self.cli, reader=self.reader,
                                    data_dir=self.root / "cloud-cache", qr_renderer=lambda *args: b"")

    def tearDown(self):
        self.temp.cleanup()

    def test_doctor_is_offline_and_does_not_claim_write_ready(self):
        result = self.controller.doctor()
        self.assertTrue(result["installed"])
        self.assertFalse(result["configured"])
        self.assertFalse(result["account_writeback_available"])
        self.cli.run_json.assert_not_called()
        self.cli.run_text.assert_not_called()

    def test_unconfigured_login_and_discovery_make_no_online_call(self):
        for method in (self.controller.login, self.controller.discover):
            with self.assertRaises(OrganizerError):
                method()
        self.cli.run_json.assert_not_called()
        self.cli.run_text.assert_not_called()

    def test_login_returns_official_link_without_other_cli_fields(self):
        self.cli.configured.return_value = True
        self.cli.run_json.return_value = {
            "success": True, "clickableUrl": "https://music.163.com/authorize?code=test",
            "token": "SECRET", "message": "SECRET",
        }
        result = self.controller.login()
        self.cli.run_json.assert_called_once_with(["login", "--background"])
        self.assertEqual(result["status"], "authorization_pending")
        self.assertEqual(result["url"], "https://music.163.com/authorize?code=test")
        self.assertNotIn("SECRET", json.dumps(result))

    def test_actual_cli_short_link_format_starts_authorization(self):
        self.cli.configured.return_value = True
        self.cli.run_json.return_value = {"success": True, "clickableUrl": "https://163cn.tv/local-example",
                                          "qrCodeUrl": "https://163cn.tv/local-example"}
        result = self.controller.login()
        self.assertEqual(result["status"], "authorization_pending")
        self.assertFalse(result["authorized"])
        self.assertEqual(result["url"], "https://163cn.tv/local-example")

    def test_false_login_success_or_foreign_link_is_rejected(self):
        self.cli.configured.return_value = True
        for response in ({"success": False}, {"success": True, "clickableUrl": "https://music.163.com.evil.test/"},
                         {"success": True, "clickableUrl": "https://evil.test\\.music.163.com/"}):
            self.cli.run_json.return_value = response
            with self.subTest(response=response), self.assertRaises(OrganizerError):
                self.controller.login()

    def test_discovery_checks_login_before_commands_and_exports_no_raw_secrets(self):
        self.cli.configured.return_value = True
        self.cli.run_json.return_value = {"success": True, "token": "SECRET"}
        self.cli.manifest.return_value = ({"token": "SECRET"}, [
            {"command": ["playlist", "create"], "description": "创建歌单", "parameters": [
                {"name": "playlistName", "in": "body", "required": True, "type": "string", "default": "SECRET"}
            ]}
        ])
        result = self.controller.discover()
        self.cli.run_json.assert_called_once_with(["login", "--check"])
        self.cli.run_text.assert_called_once_with(["commands"])
        self.assertEqual(result["command_count"], 1)
        self.assertFalse(result["account_writeback_available"])
        exported = (self.root / "artifacts/official-commands.json").read_text(encoding="utf-8")
        self.assertNotIn("SECRET", exported)
        self.assertIn("playlistName", exported)

    def test_pending_login_does_not_fetch_manifest(self):
        self.cli.configured.return_value = True
        self.cli.run_json.return_value = {"success": False}
        result = self.controller.discover()
        self.assertEqual(result["status"], "authorization_required")
        self.cli.run_text.assert_not_called()
        self.cli.manifest.assert_not_called()

    def test_prepare_is_offline_and_leaves_ids_pending(self):
        result = self.controller.prepare()
        plan = json.loads((self.root / "artifacts/自动整理清单.json").read_text(encoding="utf-8"))
        self.assertEqual(result["job_count"], 1)
        self.assertEqual(plan["jobs"][0]["playlist_id"], "1")
        self.assertFalse(plan["applied_to_account"])
        self.assertFalse(plan["online_account_verified"])
        self.assertTrue(Path(result["path"]).is_file())
        self.cli.run_json.assert_not_called()
        self.cli.run_text.assert_not_called()

    def test_execute_blocks_without_mutation_even_after_credentials(self):
        self.cli.configured.return_value = True
        self.controller.prepare()
        result = self.controller.execute()
        self.assertEqual(result["status"], "blocked")
        self.assertEqual(result["completed_count"], 0)
        self.assertFalse(result["applied_to_account"])
        self.cli.run_json.assert_not_called()
        self.cli.run_text.assert_not_called()

    def test_key_file_reads_utf8_and_does_not_expose_content(self):
        key = self.root / "key.pem"
        key.write_text("SECRET-PRIVATE-KEY\n", encoding="utf-8-sig")
        result = self.controller.save_credentials_file("app", key)
        self.cli.save_credentials.assert_called_once_with("app", "SECRET-PRIVATE-KEY\n")
        self.assertNotIn("SECRET", json.dumps(result))

    def test_export_refuses_client_data_directory(self):
        controller = Organizer(self.root, cli=self.cli, reader=self.reader, data_dir=self.root)
        with self.assertRaises(OrganizerError):
            controller.prepare()
        self.assertFalse((self.root / "artifacts/自动整理清单.json").exists())

    def _begin_authorization(self):
        self.cli.configured.return_value = True
        self.cli.token_signature.return_value = (100, 128)
        self.cli.run_json.return_value = {"success": True, "clickableUrl": "https://music.163.com/authorize?code=test"}
        self.controller.login()
        self.cli.run_json.reset_mock()

    def test_unchanged_encrypted_token_does_not_cause_online_polling(self):
        self._begin_authorization()
        result = self.controller.authorization_probe()
        self.assertEqual(result["status"], "authorization_pending")
        self.assertFalse(result["authorized"])
        self.cli.run_json.assert_not_called()

    def test_token_change_triggers_authoritative_login_check(self):
        self._begin_authorization()
        self.cli.token_signature.return_value = (101, 160)
        self.cli.run_json.return_value = {"success": True, "token": "SECRET"}
        result = self.controller.authorization_probe()
        self.cli.run_json.assert_called_once_with(["login", "--check"])
        self.assertTrue(result["authorized"])
        self.assertEqual(result["status"], "authorized")
        self.assertFalse(result["account_writeback_available"])
        self.assertNotIn("SECRET", json.dumps(result))

    def test_anonymous_token_change_never_counts_as_authorization(self):
        self._begin_authorization()
        self.cli.token_signature.return_value = (101, 160)
        self.cli.run_json.return_value = {"success": False}
        result = self.controller.authorization_probe()
        self.assertEqual(result["status"], "authorization_pending")
        self.assertFalse(result["authorized"])
        self.cli.run_json.reset_mock()
        self.controller.authorization_probe()
        self.cli.run_json.assert_not_called()

    def test_authorization_written_during_failed_check_is_seen_next_probe(self):
        self._begin_authorization()
        signature = [(101, 160)]
        self.cli.token_signature.side_effect = lambda: signature[0]
        def first_check(arguments):
            signature[0] = (102, 192)
            return {"success": False}
        self.cli.run_json.side_effect = first_check
        self.assertFalse(self.controller.authorization_probe()["authorized"])
        self.cli.run_json.side_effect = None
        self.cli.run_json.return_value = {"success": True}
        self.assertTrue(self.controller.authorization_probe()["authorized"])
        self.assertEqual(self.cli.run_json.call_count, 2)

    def test_unknown_check_result_does_not_repeat_network_for_same_change(self):
        self._begin_authorization()
        self.cli.token_signature.return_value = (101, 160)
        self.cli.run_json.return_value = {"message": "unknown"}
        with self.assertRaises(OrganizerError):
            self.controller.authorization_probe()
        self.cli.run_json.reset_mock()
        self.controller.authorization_probe()
        self.cli.run_json.assert_not_called()

    def test_probe_deadline_is_bounded_without_an_online_call(self):
        now = [100.0]
        self.controller.time_source = lambda: now[0]
        self._begin_authorization()
        now[0] = 401.0
        result = self.controller.authorization_probe()
        self.assertEqual(result["status"], "authorization_expired")
        self.cli.run_json.assert_not_called()

    def test_missing_active_authorization_never_checks_network(self):
        result = self.controller.authorization_probe()
        self.assertEqual(result["status"], "authorization_required")
        self.cli.run_json.assert_not_called()

    def test_save_new_credentials_invalidates_observed_authorization(self):
        self._begin_authorization()
        self.cli.run_json.return_value = {"success": True}
        self.controller.login_status()
        self.controller.save_credentials("new-app", "key")
        self.cli.run_json.reset_mock()
        self.assertFalse(self.controller.authorization_probe()["authorized"])
        self.cli.run_json.assert_not_called()

    def test_recent_auth_check_is_not_repeated_during_discovery(self):
        self.cli.configured.return_value = True
        self.cli.run_json.return_value = {"success": True}
        self.controller.login_status()
        self.cli.manifest.return_value = ({}, [])
        self.controller.discover()
        self.cli.run_json.assert_called_once_with(["login", "--check"])

    def test_invalid_login_check_format_is_not_called_unauthorized(self):
        self.cli.configured.return_value = True
        self.cli.run_json.return_value = {"message": "SECRET"}
        with self.assertRaises(OrganizerError) as error:
            self.controller.login_status()
        self.assertNotIn("SECRET", str(error.exception))

    def test_qr_failure_preserves_official_link_without_leaking_error(self):
        self.controller.qr_renderer = Mock(side_effect=RuntimeError("SECRET"))
        self._begin_authorization()
        self.cli.run_json.return_value = {"success": True, "clickableUrl": "https://music.163.com/authorize?code=test"}
        result = self.controller.login()
        self.assertTrue(result["url"].startswith("https://music.163.com/"))
        self.assertNotIn("SECRET", json.dumps(result))

    def test_online_preview_exports_verified_ids_and_keeps_unsupported_jobs_blocked(self):
        self.cli.configured.return_value = True
        self.reader.load.return_value['owner_id'] = '7'
        self.controller.online_reader = Mock()
        self.controller.online_reader.read_snapshot.return_value = live_snapshot()
        result = self.controller.online_preview()
        plan = json.loads((self.root / 'artifacts/在线整理清单.json').read_text(encoding='utf-8'))
        self.assertEqual(result['blocked_count'], 1)
        self.assertEqual(plan['summary']['ready_count'], 1)
        self.assertTrue(plan['online_account_verified'])
        self.assertFalse(result['applied_to_account'])
        self.cli.run_json.assert_not_called()

    def test_wrong_online_account_cannot_create_executable_plan(self):
        self.cli.configured.return_value = True
        self.reader.load.return_value['owner_id'] = '8'
        self.controller.online_reader = Mock()
        self.controller.online_reader.read_snapshot.return_value = live_snapshot()
        with self.assertRaises(OrganizerError):
            self.controller.online_preview()
        self.assertIsNone(self.controller._online_plan)
        self.assertFalse((self.root / 'artifacts/在线整理清单.json').exists())

    def test_explicit_name_execution_never_sends_blocked_artist_jobs_and_clears_old_plan(self):
        self.cli.configured.return_value = True
        self.reader.load.return_value['owner_id'] = '7'
        self.controller.online_reader = Mock()
        self.controller.online_reader.read_snapshot.return_value = live_snapshot()
        self.controller.rename_executor = Mock()
        self.controller.rename_executor.execute.return_value = {'status': 'completed', 'completed_count': 1,
                                                                'applied_to_account': True, 'items': []}
        result = self.controller.execute_renames()
        jobs = self.controller.rename_executor.execute.call_args.args[0]
        self.assertEqual(len(jobs), 1)
        self.assertEqual(jobs[0]['kind'], 'rename_playlist')
        self.assertEqual(jobs[0]['playlist_id'], 'B' * 32)
        self.assertTrue(result['applied_to_account'])
        self.assertIsNone(self.controller._online_plan)
        self.assertTrue((self.root / 'artifacts/名称整理执行结果.json').is_file())

    def test_result_save_failure_preserves_confirmed_account_changes(self):
        self.cli.configured.return_value = True
        self.controller._online_snapshot = live_snapshot()
        self.controller._online_plan = {'jobs': []}
        self.controller.rename_executor = Mock()
        self.controller.rename_executor.execute.return_value = {'status': 'completed', 'completed_count': 2,
                                                                'applied_to_account': True, 'items': []}
        self.controller._write_artifact = Mock(side_effect=OSError('PRIVATE-INTERNAL-DETAIL'))
        result = self.controller.execute_renames()
        self.assertTrue(result['applied_to_account'])
        self.assertEqual(result['completed_count'], 2)
        self.assertFalse(result['record_saved'])
        self.assertNotIn('PRIVATE-INTERNAL-DETAIL', json.dumps(result))

    def test_next_gui_execution_keeps_uncertain_executor_frozen(self):
        self.cli.configured.return_value = True
        self.controller.online_reader = Mock()
        self.controller.online_reader.read_snapshot.return_value = live_snapshot()
        class GuardedExecutor:
            def __init__(self):
                self.attempted = False
            def execute(self, jobs):
                if self.attempted:
                    return {'status': 'blocked', 'completed_count': 0, 'applied_to_account': False, 'items': []}
                self.attempted = True
                return {'status': 'uncertain', 'completed_count': 0, 'applied_to_account': False, 'items': []}
        with patch('netease_organizer.rename.RenameExecutor', side_effect=lambda *args, **kwargs: GuardedExecutor()) as factory:
            self.assertEqual(self.controller.execute_renames()['status'], 'uncertain')
            self.assertEqual(self.controller.execute_renames()['status'], 'blocked')
            self.assertEqual(factory.call_count, 1)


if __name__ == "__main__":
    unittest.main()
