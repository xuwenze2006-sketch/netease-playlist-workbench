import contextlib
import io
import json
import unittest
from unittest.mock import patch

from run_organizer import main
from netease_organizer.service import OrganizerError


class OrganizerEntryTests(unittest.TestCase):
    def setUp(self):
        # Entry routing is independent of a developer's existing frontend build.
        # Real build validation uses disposable assets in test_web_build instead.
        self.build = object()
        self.load_build = self.enterContext(patch('netease_organizer.web_build.load_build', return_value=self.build))
        self.startup_error = self.enterContext(patch('run_organizer.show_gui_startup_error'))

    def test_reconcile_command_only_invokes_explicit_read_only_recovery(self):
        with patch('run_organizer.Organizer') as controller, contextlib.redirect_stdout(io.StringIO()) as output:
            controller.return_value.reconcile_renames.return_value = {
                'status': 'completed', 'outcome_known': True, 'record_saved': True,
                'write_attempted': False, 'run_id': 'PRIVATE-INTERNAL-BATCH',
                'intent_digest': 'PRIVATE-INTERNAL-DIGEST'}
            self.assertEqual(main(['reconcile-renames']), 0)
            controller.return_value.reconcile_renames.assert_called_once_with()
            controller.return_value.execute_renames.assert_not_called()
            controller.return_value.execute_artist_playlists.assert_not_called()
            self.assertFalse(json.loads(output.getvalue())['write_attempted'])
            self.assertNotIn('PRIVATE', output.getvalue())

    def test_online_names_plan_requests_only_directory_and_header_mode(self):
        with patch("run_organizer.Organizer") as controller, contextlib.redirect_stdout(io.StringIO()) as output:
            controller.return_value.online_preview.return_value = {
                "status": "online_plan_ready", "applied_to_account": False,
            }
            self.assertEqual(main(["online-plan", "--names-only"]), 0)
            controller.return_value.online_preview.assert_called_once_with(names_only=True)
            controller.return_value.execute_renames.assert_not_called()
            controller.return_value.execute_artist_playlists.assert_not_called()
            self.assertFalse(json.loads(output.getvalue())["applied_to_account"])

    def test_online_plan_default_keeps_complete_read_call_contract(self):
        with patch("run_organizer.Organizer") as controller, contextlib.redirect_stdout(io.StringIO()):
            controller.return_value.online_preview.return_value = {"status": "online_plan_ready"}
            self.assertEqual(main(["online-plan"]), 0)
            controller.return_value.online_preview.assert_called_once_with()

    def test_names_only_flag_is_rejected_for_every_other_command_before_controller(self):
        for command in (None, "gui", "doctor", "plan", "login", "login-status", "discover",
                        "check-execution", "execute-renames", "execute-artists"):
            with self.subTest(command=command), patch("run_organizer.Organizer") as controller, contextlib.redirect_stderr(io.StringIO()):
                args = ["--names-only"] if command is None else [command, "--names-only"]
                with self.assertRaises(SystemExit) as raised:
                    main(args)
                self.assertEqual(raised.exception.code, 2)
                controller.assert_not_called()

    def test_execute_artists_forwards_explicit_visibility_acceptance(self):
        accepted = []
        with patch("run_organizer.Organizer") as controller, contextlib.redirect_stdout(io.StringIO()) as output:
            controller.return_value.execute_artist_playlists.side_effect = lambda *, accept_default_visibility: (
                accepted.append(accept_default_visibility) or {"status": "applied", "applied_to_account": True}
            )
            self.assertEqual(main(["execute-artists", "--accept-default-visibility"]), 0)
            self.assertEqual(accepted, [True])
            self.assertTrue(json.loads(output.getvalue())["applied_to_account"])

    def test_execute_artists_without_flag_never_automatically_accepts_visibility(self):
        accepted = []
        with patch("run_organizer.Organizer") as controller, contextlib.redirect_stdout(io.StringIO()) as output:
            controller.return_value.execute_artist_playlists.side_effect = lambda *, accept_default_visibility: (
                accepted.append(accept_default_visibility) or {"status": "blocked", "applied_to_account": False}
            )
            self.assertEqual(main(["execute-artists"]), 3)
            self.assertEqual(accepted, [False])
            self.assertEqual(json.loads(output.getvalue())["status"], "blocked")

    def test_default_visibility_flag_is_rejected_for_other_commands_before_controller(self):
        for command in ("gui", "doctor", "execute-renames", "login"):
            with self.subTest(command=command), patch("run_organizer.Organizer") as controller, contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit) as raised:
                    main([command, "--accept-default-visibility"])
                self.assertEqual(raised.exception.code, 2)
                controller.assert_not_called()

    def test_artist_creation_exception_reports_unknown_account_effect_not_false(self):
        with patch("run_organizer.Organizer") as controller, contextlib.redirect_stdout(io.StringIO()) as output:
            controller.return_value.execute_artist_playlists.side_effect = RuntimeError("SECRET-WRITE-DETAIL")
            self.assertEqual(main(["execute-artists", "--accept-default-visibility"]), 1)
            printed = json.loads(output.getvalue())
            self.assertIsNone(printed["applied_to_account"])
            self.assertNotIn("SECRET", output.getvalue())

    def test_gui_authorize_flag_is_rejected_for_artist_creation(self):
        with patch("run_organizer.Organizer") as controller, contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit):
                main(["execute-artists", "--authorize"])
            controller.assert_not_called()

    def test_gui_authorization_flag_is_explicitly_forwarded_to_window(self):
        received = []
        with patch("run_organizer.Organizer") as controller, patch("netease_organizer.ui.launch_gui"), patch("netease_organizer.web_launcher.launch_web", side_effect=lambda instance, *, authorize_on_start, build: received.append((instance, authorize_on_start, build))):
            self.assertEqual(main(["gui", "--authorize"]), 0)
            self.assertEqual(received, [(controller.return_value, True, self.build)])
            self.load_build.assert_called_once()
            self.startup_error.assert_not_called()
            controller.return_value.login.assert_not_called()

    def test_default_gui_keeps_startup_authorization_disabled(self):
        received = []
        with patch("run_organizer.Organizer") as controller, patch("netease_organizer.ui.launch_gui"), patch("netease_organizer.web_launcher.launch_web", side_effect=lambda instance, *, authorize_on_start, build: received.append((instance, authorize_on_start, build))):
            self.assertEqual(main([]), 0)
            self.assertEqual(received, [(controller.return_value, False, self.build)])
            self.load_build.assert_called_once()
            self.startup_error.assert_not_called()
            controller.return_value.login.assert_not_called()

    def test_tk_compatibility_is_only_explicit_and_keeps_startup_offline(self):
        with patch("run_organizer.Organizer") as controller, patch("netease_organizer.ui.launch_gui") as old_gui:
            self.assertEqual(main(['tk-gui']), 0)
            old_gui.assert_called_once_with(controller.return_value, authorize_on_start=False)

    def test_hidden_gui_startup_failure_is_visible_and_never_exposes_exception(self):
        with patch('run_organizer.Organizer') as controller, \
                patch('netease_organizer.web_launcher.launch_web', side_effect=RuntimeError('SECRET-DETAIL')), \
                patch('run_organizer.show_gui_startup_error', create=True) as visible, \
                contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(main(['gui']), 1)
            visible.assert_called_once_with()
            self.assertNotIn('SECRET', output.getvalue())
            controller.return_value.login.assert_not_called()
            controller.return_value.execute_artist_playlists.assert_not_called()

    def test_gui_controller_setup_failure_is_also_reported_without_raw_detail(self):
        with patch('run_organizer.Organizer', side_effect=RuntimeError('PRIVATE-PATH')), \
                patch('run_organizer.show_gui_startup_error', create=True) as visible:
            self.assertEqual(main(['gui']), 1)
            visible.assert_called_once_with()

    def test_authorize_flag_is_rejected_for_non_gui_before_controller_creation(self):
        with patch("run_organizer.Organizer") as controller, contextlib.redirect_stderr(io.StringIO()) as output:
            with self.assertRaises(SystemExit) as raised:
                main(["doctor", "--authorize"])
            self.assertEqual(raised.exception.code, 2)
            self.assertIn("gui", output.getvalue())
            controller.assert_not_called()

    def test_doctor_does_not_start_gui_or_online_discovery(self):
        with patch("run_organizer.Organizer") as controller, contextlib.redirect_stdout(io.StringIO()) as output:
            controller.return_value.doctor.return_value = {"status": "first_setup_required", "installed": True}
            self.assertEqual(main(["doctor"]), 0)
            self.assertEqual(json.loads(output.getvalue())["status"], "first_setup_required")
            controller.return_value.discover.assert_not_called()

    def test_check_execution_returns_blocked_exit_code(self):
        with patch("run_organizer.Organizer") as controller, contextlib.redirect_stdout(io.StringIO()):
            controller.return_value.execute.return_value = {"status": "blocked", "applied_to_account": False}
            self.assertEqual(main(["check-execution"]), 3)

    def test_unexpected_exception_does_not_expose_internal_detail(self):
        with patch("run_organizer.Organizer") as controller, contextlib.redirect_stdout(io.StringIO()) as output:
            controller.return_value.prepare.side_effect = RuntimeError("SECRET")
            self.assertEqual(main(["plan"]), 1)
            self.assertNotIn("SECRET", output.getvalue())

    def test_cli_login_does_not_dump_qr_image_data(self):
        with patch("run_organizer.Organizer") as controller, contextlib.redirect_stdout(io.StringIO()) as output:
            controller.return_value.login.return_value = {"status": "authorization_pending",
                "url": "https://music.163.com/authorize?code=test", "qr_png_base64": "AUTHORIZATION-IMAGE"}
            self.assertEqual(main(["login"]), 0)
            self.assertNotIn("AUTHORIZATION-IMAGE", output.getvalue())
            self.assertTrue(json.loads(output.getvalue())["url"].startswith("https://music.163.com/"))


if __name__ == "__main__":
    unittest.main()
