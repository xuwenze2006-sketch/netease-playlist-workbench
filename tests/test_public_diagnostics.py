import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from netease_bridge.cache import CacheError
from netease_organizer import official_cli
from netease_organizer.official_cli import CliError, OfficialCli
from netease_organizer.online import OnlineError
from netease_organizer.service import Organizer, OrganizerError


class PublicDiagnosticTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        script = self.root / '.tools/ncm-cli/node_modules/@music163/ncm-cli/dist/index.js'
        script.parent.mkdir(parents=True)
        script.write_text('// fake only', encoding='utf-8')
        self.node = self.root / 'node.exe'
        self.node.write_bytes(b'fake node')
        self.run_process = Mock(return_value=subprocess.CompletedProcess([], 0, '0.1.7\n', ''))
        self.runner = OfficialCli(self.root, node=self.node, run_process=self.run_process)
        self.cli = Mock()
        self.cli.installed.return_value = True
        self.cli.version.return_value = '0.1.7'
        self.cli.configured.return_value = True
        self.reader = Mock()
        self.reader.load.return_value = {'owner_id': '42'}
        self.controller = Organizer(self.root, cli=self.cli, reader=self.reader,
                                    data_dir=self.root / 'fake-cache', qr_renderer=lambda *args: b'')

    def test_public_error_enum_is_fixed_and_immutable(self):
        self.assertEqual(official_cli.PUBLIC_ERROR_CODES, frozenset({
            'node_missing', 'cli_missing', 'credentials_required', 'cli_timeout',
            'invalid_app_id', 'invalid_private_key', 'local_permission_denied',
            'local_snapshot_unavailable', 'account_mismatch', 'operation_failed',
        }))
        self.assertIsInstance(official_cli.PUBLIC_ERROR_CODES, frozenset)

    def test_exception_message_arguments_and_runtime_error_compatibility(self):
        for error_type in (CliError, OrganizerError, OnlineError):
            with self.subTest(error_type=error_type):
                error = error_type('原安全文案', '另一个原参数', code='cli_timeout')
                self.assertIsInstance(error, RuntimeError)
                self.assertEqual(error.args, ('原安全文案', '另一个原参数'))
                self.assertEqual(str(error), str(RuntimeError(*error.args)))
                self.assertEqual(error.code, 'cli_timeout')
                self.assertEqual(error_type('原安全文案').code, 'operation_failed')

    def test_unknown_or_non_string_codes_fall_back_without_echo(self):
        for value in ('SECRET-FOREIGN-CODE', None, [], {'token': 'SECRET'}, True, 1):
            for error_type in (CliError, OrganizerError, OnlineError):
                with self.subTest(value=value, error_type=error_type):
                    error = error_type('固定安全文案', code=value)
                    self.assertEqual(error.code, 'operation_failed')
                    self.assertNotIn('SECRET', json.dumps({'code': error.code, 'message': str(error)}))

    def test_missing_node_or_cli_is_local_and_keeps_original_message(self):
        self.runner.node = ''
        for operation in (lambda: self.runner.run_text(['playlist', 'create']), self.runner.version):
            with self.assertRaises(CliError) as raised:
                operation()
            self.assertEqual(raised.exception.code, 'node_missing')
            self.assertEqual(str(raised.exception), '官方 CLI 或 Node.js 未安装，请运行项目的安装脚本。')
        self.runner.node = str(self.node)
        self.runner.script.unlink()
        with self.assertRaises(CliError) as raised:
            self.runner.run_text(['playlist', 'create'])
        self.assertEqual(raised.exception.code, 'cli_missing')
        self.run_process.assert_not_called()

    def test_timeout_is_typed_without_repeating_the_process_or_exposing_output(self):
        self.run_process.side_effect = subprocess.TimeoutExpired(['SECRET-KEY'], 1, output='SECRET-TOKEN')
        with self.assertRaises(CliError) as raised:
            self.runner.run_text(['playlist', 'create', '--playlistName', 'SECRET-NAME'])
        self.assertEqual(raised.exception.code, 'cli_timeout')
        self.assertNotIn('SECRET', str(raised.exception))
        self.assertEqual(self.run_process.call_count, 1)
        self.assertEqual(self.runner.performance_summary()['failure_count'], 1)

    def test_nonzero_cli_error_text_does_not_classify_or_escape(self):
        self.run_process.return_value = subprocess.CompletedProcess(
            [], 1, 'invalid_private_key node_missing SECRET-TOKEN', 'PermissionError SECRET-KEY')
        with self.assertRaises(CliError) as raised:
            self.runner.run_text(['config', 'set', 'appId', 'app'])
        self.assertEqual(raised.exception.code, 'operation_failed')
        self.assertNotIn('SECRET', str(raised.exception))
        self.assertEqual(self.run_process.call_count, 1)

    def test_credential_input_validation_stops_before_process_or_config_write(self):
        for app_id, key, expected in (('', 'SECRET-KEY', 'invalid_app_id'),
                                      (['SECRET'], 'SECRET-KEY', 'invalid_app_id'),
                                      ('app', '', 'invalid_private_key'),
                                      ('app', ['SECRET'], 'invalid_private_key'),
                                      ('app', 'x' * 65537, 'invalid_private_key')):
            with self.subTest(expected=expected), self.assertRaises(CliError) as raised:
                self.runner.save_credentials(app_id, key)
            self.assertEqual(raised.exception.code, expected)
            self.assertNotIn('SECRET', str(raised.exception))
        self.run_process.assert_not_called()
        self.assertFalse(self.runner.home.exists())

    def test_cli_configuration_permission_error_is_typed_and_sanitized(self):
        with patch.object(Path, 'mkdir', side_effect=PermissionError('SECRET-PRIVATE-KEY')):
            with self.assertRaises(CliError) as raised:
                self.runner.save_credentials('app', 'SECRET-KEY')
        self.assertEqual(raised.exception.code, 'local_permission_denied')
        self.assertNotIn('SECRET', str(raised.exception))
        self.run_process.assert_not_called()

    def test_cli_marker_permission_failure_is_safe_and_not_retried(self):
        with patch.object(Path, 'write_text', side_effect=PermissionError('SECRET-PRIVATE-KEY')):
            with self.assertRaises(CliError) as raised:
                self.runner.save_credentials('app', 'SECRET-KEY')
        self.assertEqual(raised.exception.code, 'local_permission_denied')
        self.assertNotIn('SECRET', str(raised.exception))
        self.run_process.assert_not_called()

    def test_unconfigured_service_has_explicit_code_and_no_online_call(self):
        self.cli.configured.return_value = False
        for operation in (self.controller.login, self.controller.discover, self.controller.online_preview):
            with self.subTest(operation=operation.__name__), self.assertRaises(OrganizerError) as raised:
                operation()
            self.assertEqual(raised.exception.code, 'credentials_required')
            self.assertEqual(str(raised.exception), '请先申请开放平台 App ID 和私钥，并在程序中保存凭证。')
        self.cli.run_json.assert_not_called()
        self.reader.load.assert_not_called()

    def test_service_preserves_typed_cli_failure_and_defaults_for_mutated_code(self):
        error = CliError('原安全超时文案', code='cli_timeout')
        self.cli.run_json.side_effect = error
        with self.assertRaises(OrganizerError) as raised:
            self.controller.login_status()
        self.assertEqual(raised.exception.code, 'cli_timeout')
        self.assertEqual(raised.exception.args, error.args)
        self.assertFalse(hasattr(raised.exception, 'write_attempted'))
        self.assertFalse(hasattr(raised.exception, 'outcome_known'))
        error.code = {'token': 'SECRET'}
        with self.assertRaises(OrganizerError) as raised:
            self.controller.login_status()
        self.assertEqual(raised.exception.code, 'operation_failed')
        self.assertNotIn('SECRET', str(raised.exception))

    def test_cache_failure_has_fixed_diagnostic_before_preview_calls(self):
        self.reader.load.side_effect = CacheError('SECRET-RAW-CACHE')
        with self.assertRaises(OrganizerError) as raised:
            self.controller.online_preview()
        self.assertEqual(raised.exception.code, 'local_snapshot_unavailable')
        self.assertEqual(str(raised.exception), '无法取得可靠的本地歌单快照，请启动网易云后重试。')
        self.cli.run_json.assert_not_called()
        self.assertFalse((self.root / 'artifacts').exists())

    def test_explicit_preview_owner_mismatch_stops_before_plan_or_mutation(self):
        online = Mock()
        online.read_snapshot.return_value = {'account': {'original_id': '43'}}
        self.controller.online_reader = online
        with self.assertRaises(OrganizerError) as raised:
            self.controller.online_preview()
        self.assertEqual(raised.exception.code, 'account_mismatch')
        self.assertEqual(str(raised.exception), '在线账号与本地歌单账号不一致，未生成可执行清单。')
        self.cli.run_json.assert_not_called()
        self.assertFalse((self.root / 'artifacts').exists())

    def test_real_online_reader_owner_mismatch_is_typed_after_one_identity_read(self):
        self.cli.run_json.return_value = {'code': 200, 'data': {
            'originalId': 999, 'id': 'A' * 32, 'nickname': 'fake-account', 'token': 'SECRET-TOKEN'}}
        with self.assertRaises(OrganizerError) as raised:
            self.controller.online_preview()
        self.assertEqual(raised.exception.code, 'account_mismatch')
        self.assertEqual(str(raised.exception), '在线账号与指定账号不一致，已停止读取。')
        self.assertNotIn('SECRET', str(raised.exception))
        self.cli.run_json.assert_called_once_with(['user', 'info'])
        self.cli.run_text.assert_not_called()
        self.assertIsNone(self.controller._online_plan)
        self.assertFalse((self.root / 'artifacts').exists())

    def test_real_reader_cli_timeout_keeps_code_but_discards_cli_message(self):
        self.cli.run_json.side_effect = CliError('SECRET-PRIVATE-KEY', code='cli_timeout')
        with self.assertRaises(OrganizerError) as raised:
            self.controller.online_preview()
        self.assertEqual(raised.exception.code, 'cli_timeout')
        self.assertEqual(str(raised.exception), '官方在线读取未完成，请检查账号授权和接口状态后重试。')
        self.assertNotIn('SECRET', str(raised.exception))
        self.cli.run_json.assert_called_once_with(['user', 'info'])
        self.assertFalse((self.root / 'artifacts').exists())

    def test_real_reader_ignores_forged_runtime_error_code_and_raw_message(self):
        error = RuntimeError('account_mismatch SECRET-TOKEN')
        error.code = 'account_mismatch'
        self.cli.run_json.side_effect = error
        with self.assertRaises(OrganizerError) as raised:
            self.controller.online_preview()
        self.assertEqual(raised.exception.code, 'operation_failed')
        self.assertNotIn('SECRET', str(raised.exception))
        self.cli.run_json.assert_called_once_with(['user', 'info'])

    def test_service_whitelists_mutated_online_error_code(self):
        error = OnlineError('固定安全文案', code='account_mismatch')
        error.code = {'token': 'SECRET'}
        self.controller.online_reader = Mock()
        self.controller.online_reader.read_snapshot.side_effect = error
        with self.assertRaises(OrganizerError) as raised:
            self.controller.online_preview()
        self.assertEqual(raised.exception.code, 'operation_failed')
        self.assertEqual(str(raised.exception), '固定安全文案')

    def test_unmarked_value_error_cannot_classify_from_secret_error_text(self):
        self.reader.load.side_effect = ValueError('invalid_private_key SECRET-KEY')
        with self.assertRaises(OrganizerError) as raised:
            self.controller.online_preview()
        self.assertEqual(raised.exception.code, 'operation_failed')
        self.assertNotIn('SECRET', str(raised.exception))

    def test_private_key_file_invalid_encoding_or_oversize_is_input_diagnostic(self):
        key = self.root / 'fake-key.pem'
        for content in (b'\xffSECRET', b'x' * 65537):
            key.write_bytes(content)
            with self.subTest(length=len(content)), self.assertRaises(OrganizerError) as raised:
                self.controller.save_credentials_file('app', key)
            self.assertEqual(raised.exception.code, 'invalid_private_key')
            self.assertNotIn('SECRET', str(raised.exception))
        self.cli.save_credentials.assert_not_called()

    def test_key_file_permission_error_preserves_safe_message(self):
        with patch.object(Path, 'stat', side_effect=PermissionError('SECRET-PATH')):
            with self.assertRaises(OrganizerError) as raised:
                self.controller.save_credentials_file('app', self.root / 'fake-key.pem')
        self.assertEqual(raised.exception.code, 'local_permission_denied')
        self.assertEqual(str(raised.exception), '本地文件或返回数据不可用；本次操作未确认完成。')
        self.cli.save_credentials.assert_not_called()

    def test_doctor_reports_only_local_installation_or_credentials_problem(self):
        controller = Organizer(self.root, cli=self.runner, reader=self.reader, data_dir=self.root / 'fake-cache')
        result = controller.doctor()
        self.assertEqual(result['error_code'], 'credentials_required')
        self.assertEqual(result['status'], 'first_setup_required')
        self.assertEqual(self.run_process.call_count, 1)
        self.run_process.reset_mock()
        self.runner.node = ''
        self.assertEqual(controller.doctor()['error_code'], 'node_missing')
        self.runner.node = str(self.node)
        self.runner.script.unlink()
        self.assertEqual(controller.doctor()['error_code'], 'cli_missing')
        self.run_process.assert_not_called()


if __name__ == '__main__':
    unittest.main()
