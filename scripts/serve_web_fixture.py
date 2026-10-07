"""Run the modern frontend against a temporary fake account for browser acceptance."""

import argparse
import copy
import json
import os
import sys
import tempfile
import threading
from pathlib import Path
from unittest.mock import Mock

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT))
sys.path.insert(0, str(PROJECT / 'tests'))

from test_create import FakeCli as ArtistCli
from netease_organizer.online import OnlineReader
from netease_organizer.online_planning import build_online_plan
from netease_organizer.service import Organizer, OrganizerError, _APPROVED_ARTIST_COUNTS
from netease_organizer.web_server import _WorkbenchHandler, create_server, shutdown_server
from netease_organizer.web_state import build_local_state


_CONNECTIONS = ('ready', 'missing-cli', 'missing-credentials')
_FAILURES = ('none', 'cache', 'account', 'unknown-write', 'record-save')


class FixtureCli(ArtistCli):
    def __init__(self, *, gate=False, connection='ready'):
        super().__init__()
        self.connection = connection
        self.control = None
        self.gate = gate
        self.gate_used = False
        self.gate_entered = threading.Event()
        self.full_tracks, self.jobs = [], []
        index = 11
        for artist_index, (name, count) in enumerate(_APPROVED_ARTIST_COUNTS.items(), 1):
            ids = []
            for number in range(count):
                ident = f'{index:032X}'
                ids.append(ident)
                self.full_tracks.append({'id': ident, 'originalId': index,
                    'name': f'{name.split(" · ")[0]} · 验收曲目 {number + 1}',
                    'artists': [{'id': f'{700+artist_index:032X}', 'originalId': 700+artist_index,
                                 'name': name.split(' · ')[0]}]})
                index += 1
            self.jobs.append({'kind': 'create_artist_playlist', 'name': name,
                              'candidate_track_ids': ids, 'intended_visibility': 'provider_default'})
        self.favorite = {'id': 'F'*32, 'originalId': 9000, 'name': '我喜欢的音乐',
                         'trackCount': len(self.full_tracks), 'specialType': 5,
                         'trackUpdateTime': 100, 'creatorId': 'A'*32}
        self.make_playlist('funk', self.jobs[0]['candidate_track_ids'][:10])
        self.make_playlist('夜晚的纯音乐', self.jobs[1]['candidate_track_ids'][:5])
        self.make_playlist('英语', self.jobs[4]['candidate_track_ids'])
        self.commands.append({'command': ['playlist', 'updateName'], 'parameters': [
            {'name': name, 'in': 'query', 'type': 'string', 'required': True}
            for name in ('playlistId', 'name')]})

    def installed(self):
        return self.connection != 'missing-cli'

    def configured(self):
        return self.connection == 'ready'

    def version(self):
        return '0.1.7（模拟账号）'

    def run_json(self, arguments):
        if arguments == ['user', 'favorite']:
            self.calls.append(list(arguments))
            return {'code': 200, 'data': copy.deepcopy(self.favorite)}
        if arguments[:2] == ['playlist', 'get'] and arguments[3] == self.favorite['id']:
            self.calls.append(list(arguments))
            return {'code': 200, 'data': copy.deepcopy(self.favorite)}
        if arguments[:2] == ['playlist', 'tracks'] and arguments[3] == self.favorite['id']:
            self.calls.append(list(arguments))
            offset = int(arguments[-1])
            return {'code': 200, 'data': copy.deepcopy(self.full_tracks[offset:offset + 500])}
        if arguments[:2] == ['playlist', 'updateName']:
            self.calls.append(list(arguments))
            self.writes.append('updateName')
            self.created[arguments[3]]['name'] = arguments[5]
            return {'code': 200, 'data': True}
        result = super().run_json(arguments)
        if arguments == ['user', 'info']:
            result['data']['nickname'] = '前端验收账号'
        if arguments[:2] == ['playlist', 'create'] and self.gate and not self.gate_used:
            self.gate_used = True
            self.gate_entered.set()
            # Fake latency lets the browser exercise pause while a request is in flight.
            self.control._paused.wait(30)
        return result


class FixtureOrganizer(Organizer):
    """Inject explicit fake failures without changing the product controller."""

    def __init__(self, *args, failure='none', **kwargs):
        super().__init__(*args, **kwargs)
        self.failure = failure
        self._unknown_write_sent = False

    def online_preview(self, *, names_only=False):
        if self.failure == 'cache':
            raise OrganizerError('模拟本地歌单资料暂不可用。', code='local_snapshot_unavailable')
        if self.failure == 'account':
            raise OrganizerError('模拟授权账号与本地账号不一致。', code='account_mismatch')
        return super().online_preview(names_only=names_only)

    def execute_renames(self):
        if self.failure == 'unknown-write':
            if not self._unknown_write_sent:
                ident = next(ident for ident, row in self.cli.created.items() if row['name'] == 'funk')
                self._unknown_write_sent = True
                self.cli.run_json(['playlist', 'updateName', '--playlistId', ident, '--name', 'Funk'])
            # The fake account changed, but the controller's result never
            # reached the API. Repeated clicks cannot send another fake write.
            raise TimeoutError('模拟写入结果交付中断。')
        return super().execute_renames()

    def _write_artifact(self, filename, text):
        if self.failure == 'record-save' and filename == '名称整理执行结果.json':
            # Real execute_renames has already completed its exact readback.
            # Its own save-failure branch must retain all confirmed effects.
            raise PermissionError('模拟名称核对回执保存被拒绝。')
        return super()._write_artifact(filename, text)


class FixtureHandler(_WorkbenchHandler):
    """Deterministic fake-only signal; never installed on the product server."""

    def do_GET(self):
        if self.path == '/fixture/state':
            if self._guard():
                writes = list(self.server.fake_cli.writes)
                self._json(200, {'kind': 'fake_account_state', 'fake_writes': writes, 'count': len(writes)})
            return
        if self.path == '/fixture/gate':
            if self._guard():
                self._json(200, {'kind': 'fake_create_gate',
                                 'entered': self.server.fake_cli.gate_entered.is_set()})
            return
        super().do_GET()


def create_fixture(workspace, *, gate=False, connection='ready', failure='none', assets=None, port=0):
    """Build a caller-owned temporary fake account; never construct a real CLI."""
    if connection not in _CONNECTIONS or failure not in _FAILURES:
        raise ValueError('Unknown fake fixture scenario')
    workspace = Path(workspace)
    cli = FixtureCli(gate=gate, connection=connection)
    reader = Mock()
    reader.load.return_value = {'owner_id': '42'}
    controller = FixtureOrganizer(workspace, cli=cli, reader=reader,
                                  data_dir=workspace / 'fixture-cache', failure=failure)
    cli.control = controller.control
    # Seed historical display/approval from the fake account before exposing
    # the selected failure. This never reads an installed CLI or user cache.
    snapshot = OnlineReader(cli, '42').read_snapshot()
    controller._write_artifact('在线整理快照.json', json.dumps(snapshot, ensure_ascii=False))
    controller._write_artifact('在线整理清单.json', json.dumps(build_online_plan(snapshot), ensure_ascii=False))
    controller._write_artifact('已批准歌手精选.json', json.dumps({
        'kind': 'approved_artist_selection', 'account_original_id': '42',
        'intended_visibility': 'provider_default', 'jobs': cli.jobs}, ensure_ascii=False))
    server = create_server(controller, assets=assets if assets is not None else PROJECT / 'frontend/dist',
                           port=port, state_provider=lambda: build_local_state(workspace, organizer=controller))
    server.RequestHandlerClass = FixtureHandler
    server.fake_cli = cli
    return server


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--gate', action='store_true', help='First fake create waits for pause, at most30s')
    parser.add_argument('--port', type=int, default=0)
    parser.add_argument('--stdio-control', action='store_true', help='Enter on stdin gracefully stops fake server')
    parser.add_argument('--scenario', choices=('close', 'pause-resume', 'preview', 'idle', 'recovery'), default='pause-resume')
    parser.add_argument('--connection', choices=_CONNECTIONS, default='ready', help='Simulated local CLI setup state')
    parser.add_argument('--failure', choices=_FAILURES, default='none', help='Explicit fake recovery failure')
    options = parser.parse_args()
    scenario = 'recovery' if options.connection != 'ready' or options.failure != 'none' else options.scenario
    with tempfile.TemporaryDirectory(prefix='organizer-web-fixture-') as folder:
        workspace = Path(folder)
        server = create_fixture(workspace, gate=options.gate, connection=options.connection,
                                failure=options.failure, port=options.port)
        cli, controller = server.fake_cli, server.application.controller
        record = {'url': f'http://127.0.0.1:{server.server_address[1]}/',
                  'pid': os.getpid(), 'kind': 'fake_account_only', 'real_account_calls': 0}
        (PROJECT / 'artifacts/web-fixture-session.json').write_text(json.dumps(record), encoding='utf-8')
        print(json.dumps(record), flush=True)
        stopped = threading.Event()
        if options.stdio_control:
            def stop_on_input():
                if sys.stdin.readline():
                    stopped.set()
            threading.Thread(target=stop_on_input, daemon=True).start()
        serving = threading.Thread(target=server.serve_forever, daemon=True)
        serving.start()
        try:
            while not stopped.wait(0.25) and serving.is_alive():
                if server.application.stop_requested:
                    break
        except KeyboardInterrupt:
            pass
        finally:
            before_shutdown = {
                'page_closing': server.application.page_closing,
                'pause_requested': server.application.pause_requested,
                'job_status': (server.application.state()['job'] or {}).get('status'),
            }
            shutdown_server(server)
            serving.join(timeout=2)
            report = {'kind': 'modern_frontend_fake_account_report', 'real_account_calls': 0,
                      'scenario': scenario,
                      'before_shutdown': before_shutdown,
                      'fake_writes': cli.writes, 'fake_playlist_count': len(cli.created),
                      'fake_call_count': len(cli.calls),
                      'last_result': controller._last_result()}
            if scenario == 'recovery':
                report.update(connection=options.connection, failure=options.failure,
                              job_result=(server.application.state()['job'] or {}).get('result'))
            (PROJECT / f'artifacts/现代前端-{scenario}-验收.json').write_text(
                json.dumps(report, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')


if __name__ == '__main__':
    main()
