"""Own temporary records and a strict fake CLI for explicit playlist reads only."""

import argparse
import copy
import json
import os
from pathlib import Path
import sys
import threading
from unittest.mock import Mock

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT))

from scripts.serve_restart_fixture import validate_workspace
from scripts.verify_restart import frontend_snapshot
from scripts.verify_web import VerificationError, write_report
from netease_organizer.service import Organizer
from netease_organizer.web_server import _WorkbenchHandler, create_server, shutdown_server
from netease_organizer.web_state import build_local_state


CASES = ('normal-complete', 'filtered-partial', 'pause-retains-old', 'wrong-account', 'save-failure')
KEY = '9001'
NAME = '英语 🎵'


def _identity(number):
    return f'{number:032X}'


def _track(number, name, *, old=False):
    return {'id': _identity(number), 'original_id': str(number), 'name': name,
            'artists': [{'id': 'E'*32, 'original_id': '701',
                         'name': '旧资料歌手' if old else '验收歌手'}], 'metadata_available': True}


def seed_records(workspace, count):
    """Seed pure JSON; not even fake SDK reads or writes are used for setup."""
    artifacts = workspace/'artifacts'
    if artifacts.exists():
        raise VerificationError('seed_already_exists')
    directory = {'account': {'id': 'A'*32, 'original_id': '42', 'nickname': '明细验收账号'},
        'playlists': [{'id': 'B'*32, 'original_id': KEY, 'name': NAME, 'track_count': count,
                       'special_type': 0, 'track_update_time': 100}],
        'liked': None, 'overview_complete': True, 'tracks_loaded': False, 'complete': False}
    details = {'kind': 'playlist_details', 'version': 1, 'read_at': 1700000000000,
        'account': {'id': 'A'*32, 'original_id': '42'}, 'complete': True,
        'playlist': {'id': 'B'*32, 'original_id': KEY, 'name': NAME, 'track_count': 2,
            'special_type': 0, 'track_update_time': 50, 'creator_id': 'A'*32,
            'tracks': [_track(501, '旧记录验收曲目甲', old=True), _track(502, '旧记录验收曲目乙', old=True)],
            'membership_complete': True, 'metadata_complete': True,
            'missing_record_count': 0, 'missing_metadata_track_ids': []}}
    write_report(artifacts/'在线名称整理快照.json', directory)
    write_report(artifacts/'歌单明细'/f'{KEY}.json', details)


def _file_evidence(path):
    return path.read_bytes(), path.stat().st_mtime_ns


class PlaylistReadCli:
    """Allow only the exact three official readonly command shapes in this test."""
    def __init__(self, case):
        self.case = case
        self.count = 501 if case in ('filtered-partial', 'pause-retains-old') else 2
        self.account = {'originalId': 42, 'id': 'A'*32, 'nickname': '明细验收账号'}
        self.header = {'originalId': int(KEY), 'id': 'B'*32, 'name': NAME, 'trackCount': self.count,
                       'specialType': 0, 'trackUpdateTime': 200, 'creatorId': 'A'*32}
        self.tracks = []
        for index in range(self.count):
            row = _track(10000+index, f'新读取验收曲目 {index+1:03}')
            row['originalId'] = int(row.pop('original_id'))
            row['artists'][0]['originalId'] = int(row['artists'][0].pop('original_id'))
            self.tracks.append(row)
        if case == 'filtered-partial':
            self.tracks[200] = None
            self.tracks[0]['artists'].append({'originalId': 0, 'id': None, 'name': '云盘占位歌手'})
            self.tracks[1]['artists'] = []
        self.calls, self.forbidden_command_count = [], 0
        self.control, self.observer = None, None
        self.gate_entered = threading.Event()
        self.metrics_start = 0

    def installed(self): return True
    def configured(self): return True
    def version(self): return '0.1.7（模拟只读账号）'
    def set_observer(self, callback): self.observer = callback
    def reset_metrics(self): self.metrics_start = len(self.calls)
    def performance_summary(self):
        return {'call_count': len(self.calls)-self.metrics_start, 'elapsed_seconds': 0, 'failure_count': 0}

    def run_json(self, arguments):
        self.calls.append(list(arguments))
        if arguments == ['user', 'info']:
            data = {**self.account, **({'originalId': 999, 'id': 'F'*32} if self.case == 'wrong-account' else {})}
        elif arguments == ['playlist', 'get', '--playlistId', 'B'*32]:
            data = self.header
        elif (arguments in [['playlist', 'tracks', '--playlistId', 'B'*32, '--limit', '500', '--offset', str(offset)]
                            for offset in range(0, self.count, 500)]):
            offset = int(arguments[-1])
            data = [row for row in self.tracks[offset:offset+500] if row is not None]
            if self.case == 'pause-retains-old' and offset == 0:
                # This signal proves the first page is in flight. Only the
                # explicit product pause releases it before the next checkpoint.
                self.gate_entered.set()
                self.control._paused.wait(15)
        else:
            self.forbidden_command_count += 1
            raise AssertionError('Fixture rejected a command outside the readonly contract')
        if callable(self.observer):
            self.observer({'kind': 'cli', 'command': ' '.join(arguments[:2]), 'stage': 'finished'})
        return {'code': 200, 'data': copy.deepcopy(data)}


class PlaylistReadHandler(_WorkbenchHandler):
    def do_GET(self):
        if self.path == '/fixture/playlist-read':
            if self._guard():
                self._json(200, evidence(self.server))
            return
        super().do_GET()


def evidence(server):
    cli = server.fake_cli
    with server.application.lock:
        paused = server.application.pause_requested
    return {'kind': 'fake_playlist_read_state', 'real_account_calls': 0,
        'fake_read_count': len(cli.calls), 'fake_write_count': 0,
        'forbidden_command_count': cli.forbidden_command_count,
        'command_labels': [' '.join(arguments[:2]) for arguments in cli.calls],
        'track_offsets': [int(arguments[-1]) for arguments in cli.calls if arguments[:2] == ['playlist', 'tracks']],
        'gate_entered': cli.gate_entered.is_set(),
        'pause_requested': paused,
        'old_record_unchanged': _file_evidence(server.old_record_path) == server.old_record_evidence,
        'directory_unchanged': _file_evidence(server.directory_path) == server.directory_evidence,
        'frontend_assets_sha256': server.assets_sha256}


def fixture_server(workspace, assets, case):
    if case not in CASES:
        raise VerificationError('fixture_case')
    snapshot, index_hash, assets_hash = frontend_snapshot(assets)
    cli = PlaylistReadCli(case)
    seed_records(workspace, cli.count)
    reader = Mock()
    reader.load.side_effect = AssertionError('Desktop cache is outside this fixture')
    controller = Organizer(workspace, cli=cli, reader=reader, data_dir=workspace/'unused-fake-cache')
    cli.control = controller.control
    if case == 'save-failure':
        original = controller._write_artifact
        def save(filename, text):
            if str(filename).replace('\\', '/') == f'歌单明细/{KEY}.json':
                raise PermissionError('模拟独立明细保存失败。')
            return original(filename, text)
        controller._write_artifact = save
    server = create_server(controller, assets=assets, asset_snapshot=snapshot,
        state_provider=lambda: build_local_state(workspace, organizer=controller))
    server.RequestHandlerClass = PlaylistReadHandler
    server.fake_cli, server.assets_sha256, server.index_sha256 = cli, assets_hash, index_hash
    server.old_record_path = workspace/'artifacts/歌单明细'/f'{KEY}.json'
    server.directory_path = workspace/'artifacts/在线名称整理快照.json'
    server.old_record_evidence, server.directory_evidence = _file_evidence(server.old_record_path), _file_evidence(server.directory_path)
    return server


def serve(workspace, run_id, assets, case):
    server = fixture_server(workspace, assets, case)
    initial = server.application.state()
    initial_calls = len(server.fake_cli.calls)
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
        # Capture this before shutdown requests its own pause: cleanup cannot
        # provide the evidence for the browser's explicit pause assertion.
        final, observed = server.application.state(), evidence(server)
        if not shutdown_server(server, timeout=10):
            raise VerificationError('helper_shutdown')
        serving.join(timeout=3)
        if serving.is_alive():
            raise VerificationError('helper_shutdown')
        raw = (final['job'] or {}).get('result') or {}
        result = {field: raw.get(field) for field in ('status', 'playlist_key', 'record_saved',
            'expected_count', 'count', 'missing_count', 'missing_metadata_count')}
        report = {'kind': 'modern_playlist_read_fake_account_report', 'version': 1, 'case': case,
            'run_id': run_id, 'process_id': os.getpid(), 'real_account_calls': 0,
            'index_sha256': server.index_sha256, 'frontend_assets_sha256': server.assets_sha256,
            'fake_write_count': 0, 'forbidden_command_count': observed['forbidden_command_count'],
            'startup': {'job_null': initial['job'] is None, 'fake_read_count': initial_calls},
            'end': {key: observed[key] for key in ('fake_read_count', 'command_labels', 'track_offsets',
                                                   'old_record_unchanged', 'directory_unchanged')}}
        report['end'].update(job_status=(final['job'] or {}).get('status'),
            pause_requested=observed['pause_requested'], result=result)
        write_report(workspace/'playlist-read-report.json', report)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--workspace', required=True)
    parser.add_argument('--owner-token', required=True)
    parser.add_argument('--case', choices=CASES, required=True)
    parser.add_argument('--assets', default=str(PROJECT/'frontend/dist'))
    options = parser.parse_args()
    try:
        workspace, run_id = validate_workspace(options.workspace, options.owner_token, require_parent=True)
        serve(workspace, run_id, Path(options.assets), options.case)
        return 0
    except (Exception, KeyboardInterrupt):
        print(json.dumps({'kind': 'fake_playlist_read_failure', 'verified': False, 'real_account_calls': 0}), flush=True)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
