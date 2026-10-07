"""Explicit fake reauthorization with a real durable rename and real service.

Only a caller-owned temporary fixture is allowed. The scan endpoint changes an
in-memory fake signature; no credential, token, or external URL is ever opened.
"""

import argparse
import copy
import json
import os
from pathlib import Path
import struct
import sys
import threading
import zlib

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT))

from scripts.serve_restart_fixture import (RestartCli, _remaining_unchanged,
                                          restored_server, seed, validate_workspace)
from scripts.verify_web import VerificationError, write_report
from netease_organizer.web_server import _WorkbenchHandler, shutdown_server


CASES = ('renew-and-reconcile', 'wrong-account', 'save-failure')


def _png():
    def chunk(kind, data):
        return struct.pack('>I', len(data)) + kind + data + struct.pack('>I', zlib.crc32(kind+data))
    return (b'\x89PNG\r\n\x1a\n' + chunk(b'IHDR', struct.pack('>IIBBBBB', 1, 1, 8, 6, 0, 0, 0))
            + chunk(b'IDAT', zlib.compress(b'\x00\x00\x00\x00\xff')) + chunk(b'IEND', b''))


class AuthorizationCli(RestartCli):
    def __init__(self, workspace, original, case):
        super().__init__(workspace)
        self.created, self.members = copy.deepcopy(original.created), copy.deepcopy(original.members)
        self.sequence, self.writes = original.sequence, list(original.writes)
        self.calls = list(original.calls)
        self.case, self.authorized, self.signature = case, False, 0
        self.background_count = self.scan_count = self.authorized_check_count = 0
        self.expired_check_observed = False
        self.node = 'fake-node-never-executed'

    def token_signature(self):
        return ('fake-metadata-only', self.signature)

    def run_json(self, arguments):
        if arguments == ['login', '--check']:
            self.calls.append(list(arguments))
            if self.authorized:
                self.authorized_check_count += 1
            elif self.background_count == 0:
                self.expired_check_observed = True
            return {'success': self.authorized}
        if arguments == ['login', '--background']:
            self.calls.append(list(arguments))
            if not self.expired_check_observed or self.background_count:
                raise ValueError('Fake authorization must be explicit and single use')
            self.background_count += 1
            return {'success': True, 'clickableUrl': 'https://music.163.com/authorization-fixture'}
        if arguments == ['user', 'info'] and not self.authorized:
            self.calls.append(list(arguments))
            return {'code': 401, 'data': None}
        if arguments == ['user', 'info'] and self.case == 'wrong-account':
            self.calls.append(list(arguments))
            return {'code': 200, 'data': {'originalId': 999, 'id': 'B'*32, 'nickname': '模拟其他账号'}}
        return super().run_json(arguments)

    def scan(self):
        if self.background_count != 1 or self.scan_count:
            return False
        self.scan_count += 1
        self.signature += 1
        self.authorized = True
        return True


class AuthorizationHandler(_WorkbenchHandler):
    def do_GET(self):
        if self.path == '/fixture/authorization':
            if self._guard():
                cli = self.server.fake_cli
                self._json(200, {'kind': 'fake_authorization_state', 'real_account_calls': 0,
                    'fake_writes': list(cli.writes), 'remaining_jobs_not_executed': _remaining_unchanged(cli, self.server.fake_jobs),
                    'background_count': cli.background_count, 'scan_count': cli.scan_count,
                    'fake_call_count': len(cli.calls),
                    'authorized_check_count': cli.authorized_check_count,
                    'frontend_assets_sha256': self.server.restart_assets_sha256})
            return
        super().do_GET()

    def do_POST(self):
        if self.path != '/fixture/scan':
            return super().do_POST()
        if not self._guard():
            return
        sessions = self.headers.get_all('X-Organizer-Session', [])
        if len(sessions) != 1 or not self.server.application.valid_session(sessions[0]):
            self._json(403, {'accepted': False})
            return
        if (self.headers.get_all('Content-Length', []) != ['2']
                or self.headers.get_all('Content-Type', []) != ['application/json']
                or self.headers.get('Transfer-Encoding') is not None):
            self.close_connection = True
            self._json(400, {'accepted': False})
            return
        self.connection.settimeout(2)
        if self.rfile.read(2) != b'{}':
            self._json(400, {'accepted': False})
            return
        accepted = self.server.fake_cli.scan()
        self._json(200 if accepted else 409, {'kind': 'fake_scan_only', 'accepted': accepted, 'real_account_calls': 0})


def authorization_server(workspace, run_id, assets, case):
    if case not in CASES:
        raise VerificationError('fixture_case')
    seeded = seed(workspace, run_id, assets)
    server, _ = restored_server(workspace, run_id, assets)
    cli = AuthorizationCli(workspace, server.fake_cli, case)
    controller = server.application.controller
    controller.cli, cli.control = cli, controller.control
    controller.qr_renderer = lambda project, node, url: _png()
    if case == 'save-failure':
        save = controller._write_artifact
        def fail_receipt(filename, text):
            if filename == '名称整理执行结果.json':
                raise PermissionError('模拟回执保存失败。')
            return save(filename, text)
        controller._write_artifact = fail_receipt
    server.fake_cli = cli
    server.RequestHandlerClass = AuthorizationHandler
    server.seed_intent_confirmed = seeded['intent_before_write']
    return server


def serve(workspace, run_id, assets, case):
    server = authorization_server(workspace, run_id, assets, case)
    cli = server.fake_cli
    initial = server.application.state()
    initial_call_count = len(cli.calls)
    stopped = threading.Event()
    def stop_on_input():
        sys.stdin.readline()
        stopped.set()
    threading.Thread(target=stop_on_input, daemon=True).start()
    serving = threading.Thread(target=server.serve_forever, daemon=False)
    serving.start()
    print(json.dumps({'kind': 'fake_account_only', 'pid': os.getpid(), 'real_account_calls': 0,
                      'url': f'http://127.0.0.1:{server.server_address[1]}/'}), flush=True)
    try:
        stopped.wait(120)
    finally:
        final = server.application.state()
        if not shutdown_server(server, timeout=10):
            raise VerificationError('helper_shutdown')
        serving.join(timeout=3)
        if serving.is_alive():
            raise VerificationError('helper_shutdown')
        raw = (final['job'] or {}).get('result') or {}
        result = {field: raw.get(field) for field in ('status', 'completed_count', 'write_attempted', 'outcome_known', 'record_saved')}
        report = {'kind': 'modern_authorization_fake_account_report', 'version': 1, 'case': case,
            'run_id': run_id, 'process_id': os.getpid(), 'real_account_calls': 0,
            'index_sha256': server.restart_index_sha256, 'frontend_assets_sha256': server.restart_assets_sha256,
            'fake_writes': list(cli.writes), 'remaining_jobs_not_executed': _remaining_unchanged(cli, server.fake_jobs),
            'actual_rename_intent_used': server.seed_intent_confirmed,
            'startup': {'job_null': initial['job'] is None, 'authorized_unknown': initial['connection']['authorized'] is None,
                        'fake_call_count': initial_call_count, 'recovery': initial['recovery']},
            'authorization': {'expired_check_observed': cli.expired_check_observed,
                              'background_count': cli.background_count, 'scan_count': cli.scan_count,
                              'authorized_check_count': cli.authorized_check_count, 'signature_changed': cli.signature == 1},
            'end': {'recovery': final['recovery'], 'reconcile': result}}
        write_report(workspace / 'authorization-report.json', report)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--workspace', required=True)
    parser.add_argument('--owner-token', required=True)
    parser.add_argument('--case', choices=CASES, required=True)
    parser.add_argument('--assets', default=str(PROJECT / 'frontend/dist'))
    options = parser.parse_args()
    try:
        workspace, run_id = validate_workspace(options.workspace, options.owner_token, require_parent=True)
        serve(workspace, run_id, Path(options.assets), options.case)
        return 0
    except (Exception, KeyboardInterrupt):
        print(json.dumps({'kind': 'fake_authorization_failure', 'verified': False, 'real_account_calls': 0}), flush=True)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
