"""Two isolated fake modes: persist an uncertain rename, or restore its records.

The caller owns the temporary workspace and marker. This helper never creates an
OfficialCli, reads user caches, or accepts a product workspace as fixture data.
"""

import argparse
import copy
import json
import os
from pathlib import Path
import re
import sys
import threading
from unittest.mock import Mock

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT))
sys.path.insert(0, str(PROJECT / 'tests'))

from scripts.serve_web_fixture import FixtureCli
from scripts.verify_web import VerificationError, _json, write_report
from scripts.verify_restart import frontend_snapshot
from netease_organizer.online import OnlineReader
from netease_organizer.online_planning import build_online_plan
from netease_organizer.service import Organizer
from netease_organizer.web_server import _WorkbenchHandler, create_server, shutdown_server
from netease_organizer.web_state import build_local_state
from netease_organizer.write_journal import read_rename_intent


MARKER = 'restart-fixture-owner.json'
STATE = 'fake-account.json'
CASES = ('recovery', 'clear-restart')
_DETAIL_FIELDS = ('id', 'originalId', 'name', 'creatorId', 'trackCount', 'specialType', 'trackUpdateTime')


def _read(path, stage, maximum=1024 * 1024):
    try:
        if path.is_symlink():
            raise ValueError()
        with path.open('rb') as stream:
            raw = stream.read(maximum + 1)
        if len(raw) > maximum:
            raise ValueError()
        return _json(raw.decode('utf-8'), stage, maximum)
    except (OSError, UnicodeError, ValueError):
        raise VerificationError(stage) from None


def validate_workspace(workspace, owner_token, *, require_parent=False):
    """Refuse missing ownership and every path touching the real project tree."""
    try:
        supplied = Path(workspace)
        if not supplied.is_absolute() or supplied.is_symlink():
            raise ValueError()
        resolved = supplied.resolve(strict=True)
        if (not resolved.is_dir() or not resolved.name.startswith('organizer-restart-')
                or resolved.is_relative_to(PROJECT) or PROJECT.is_relative_to(resolved)
                or any(parent.is_symlink() for parent in supplied.parents)
                or not isinstance(owner_token, str) or not re.fullmatch('[a-f0-9]{64}', owner_token)):
            raise ValueError()
        marker = _read(resolved / MARKER, 'workspace_ownership', 4096)
        if (set(marker) != {'kind', 'version', 'owner_token', 'owner_pid', 'run_id'}
                or marker['kind'] != 'organizer_restart_fixture_owner' or marker['version'] != 1
                or marker['owner_token'] != owner_token or type(marker['owner_pid']) is not int
                or marker['owner_pid'] <= 0 or not re.fullmatch('[a-f0-9]{32}', marker['run_id'])
                or require_parent and marker['owner_pid'] != os.getppid()):
            raise ValueError()
        return resolved, marker['run_id']
    except (OSError, ValueError, TypeError):
        raise VerificationError('workspace_ownership') from None


class RestartCli(FixtureCli):
    def __init__(self, workspace, *, seeding=False):
        super().__init__()
        self.workspace, self.seeding = Path(workspace), seeding
        self.intent_before_write = False

    def run_json(self, arguments):
        if arguments[:2] == ['playlist', 'updateName'] and self.seeding:
            intent = read_rename_intent(self.workspace / 'artifacts/名称整理执行进度.json')
            if intent is None or intent['job_index'] != 0 or intent['jobs'][0]['playlist_id'] != arguments[3]:
                raise AssertionError('Fake write must have a durable intent')
            self.intent_before_write = True
            self.calls.append(list(arguments))
            self.writes.append('updateName')
            raise TimeoutError('模拟原请求响应丢失。')
        return super().run_json(arguments)


def _controller(workspace, cli):
    reader = Mock()
    reader.load.return_value = {'owner_id': '42', 'playlists': []}
    controller = Organizer(workspace, cli=cli, reader=reader, data_dir=workspace / 'fixture-cache')
    cli.control = controller.control
    return controller


def _state(cli, jobs, run_id, seed_pid):
    return {'kind': 'organizer_restart_fake_account', 'version': 1, 'run_id': run_id,
            'seed_pid': seed_pid, 'account': {'original_id': '42', 'id': 'A'*32},
            'fake_writes': list(cli.writes), 'jobs': copy.deepcopy(jobs),
            'created': [{field: row[field] for field in _DETAIL_FIELDS} for row in cli.created.values()],
            'members': copy.deepcopy(cli.members), 'sequence': cli.sequence}


def _restore(workspace, run_id):
    data = _read(workspace / STATE, 'fake_state')
    try:
        if (set(data) != {'kind', 'version', 'run_id', 'seed_pid', 'account', 'fake_writes',
                         'jobs', 'created', 'members', 'sequence'}
                or data['kind'] != 'organizer_restart_fake_account' or data['version'] != 1
                or data['run_id'] != run_id or data['account'] != {'original_id': '42', 'id': 'A'*32}
                or type(data['seed_pid']) is not int or data['seed_pid'] <= 0
                or data['fake_writes'] != ['updateName'] or type(data['sequence']) is not int
                or not 1000 <= data['sequence'] <= 2000
                or not isinstance(data['created'], list) or len(data['created']) != 3
                or not isinstance(data['members'], dict) or not isinstance(data['jobs'], list)
                or len(data['jobs']) != 2):
            raise ValueError()
        cli = RestartCli(workspace)
        expected_ids = set(cli.created)
        created = {}
        for row in data['created']:
            if (not isinstance(row, dict) or set(row) != set(_DETAIL_FIELDS) or row['id'] not in expected_ids
                    or row['id'] in created or type(row['originalId']) is not int
                    or row['originalId'] != cli.created[row['id']]['originalId']
                    or row['creatorId'] != 'A'*32 or not isinstance(row['name'], str)
                    or not 1 <= len(row['name']) <= 160 or row['specialType'] != 0
                    or type(row['trackCount']) is not int or not 0 <= row['trackCount'] <= 500
                    or type(row['trackUpdateTime']) is not int or row['trackUpdateTime'] < 0):
                raise ValueError()
            created[row['id']] = row
        if set(data['members']) != expected_ids:
            raise ValueError()
        for ident, members in data['members'].items():
            if (not isinstance(members, list) or len(members) != created[ident]['trackCount']
                    or len(members) > 500 or any(not isinstance(item, str) or not re.fullmatch('[A-F0-9]{32}', item)
                                                for item in members) or len(set(members)) != len(members)):
                raise ValueError()
        for index, job in enumerate(data['jobs']):
            if (not isinstance(job, dict) or set(job) != {'playlist_id', 'original_playlist_id', 'old_name', 'name'}
                    or job['playlist_id'] not in created
                    or job['original_playlist_id'] != str(created[job['playlist_id']]['originalId'])
                    or not all(isinstance(job[key], str) and 1 <= len(job[key]) <= 160 for key in ('name', 'old_name'))
                    or created[job['playlist_id']]['name'] != (job['name'] if index == 0 else job['old_name'])):
                raise ValueError()
        if data['jobs'][0]['playlist_id'] == data['jobs'][1]['playlist_id']:
            raise ValueError()
        cli.created, cli.members, cli.sequence = created, data['members'], data['sequence']
        cli.writes = data['fake_writes']
        return cli, data['jobs'], data['seed_pid']
    except (ValueError, TypeError, KeyError):
        raise VerificationError('fake_state') from None


def seed(workspace, run_id, assets):
    if (workspace / STATE).exists() or (workspace / 'artifacts').exists():
        raise VerificationError('seed_already_exists')
    _, index_hash, assets_hash = frontend_snapshot(assets)
    cli = RestartCli(workspace, seeding=True)
    controller = _controller(workspace, cli)
    snapshot = OnlineReader(cli, '42').read_snapshot()
    plan = build_online_plan(snapshot)
    first, second = list(cli.created)[:2]
    jobs = [{'playlist_id': first, 'original_playlist_id': str(cli.created[first]['originalId']),
             'old_name': 'funk', 'name': 'Funk'},
            {'playlist_id': second, 'original_playlist_id': str(cli.created[second]['originalId']),
             'old_name': cli.created[second]['name'], 'name': '夜晚 · 纯音乐'}]
    controller._write_artifact('在线整理快照.json', json.dumps(snapshot, ensure_ascii=False))
    controller._write_artifact('在线整理清单.json', json.dumps(plan, ensure_ascii=False))
    # This second fake-only pending job proves read-only recovery cannot resume
    # a batch. No frontend or product planning rules are altered.
    controller._online_snapshot = snapshot
    controller._online_plan = {'jobs': [{**job, 'kind': 'rename_playlist', 'status': 'ready'} for job in jobs]}
    result = controller.execute_renames()
    if result['status'] != 'uncertain' or cli.writes != ['updateName'] or not cli.intent_before_write:
        raise VerificationError('seed_intent')
    # Delayed application of that same original request: no second CLI call.
    cli.created[first]['name'] = 'Funk'
    cli.created[first]['trackUpdateTime'] += 1
    write_report(workspace / STATE, _state(cli, jobs, run_id, os.getpid()))
    report = {'kind': 'modern_restart_seed_report', 'version': 1, 'run_id': run_id,
              'process_id': os.getpid(), 'index_sha256': index_hash, 'frontend_assets_sha256': assets_hash,
              'real_account_calls': 0, 'fake_update_name_count': 1,
              'intent_before_write': True, 'result_uncertain': True,
              'delayed_effect_not_replay': True, 'remaining_jobs_pending': True}
    write_report(workspace / 'seed-report.json', report)
    return report


def _remaining_unchanged(cli, jobs):
    return cli.created[jobs[1]['playlist_id']]['name'] == jobs[1]['old_name']


class RestartHandler(_WorkbenchHandler):
    def do_GET(self):
        if self.path == '/fixture/restart':
            if self._guard():
                self._json(200, {'kind': 'fake_restart_account_state', 'real_account_calls': 0,
                                'fake_update_name_count': self.server.fake_cli.writes.count('updateName'),
                                'fake_writes': list(self.server.fake_cli.writes),
                                'frontend_assets_sha256': self.server.restart_assets_sha256,
                                'remaining_jobs_not_executed': _remaining_unchanged(self.server.fake_cli, self.server.fake_jobs)})
            return
        super().do_GET()


def restored_server(workspace, run_id, assets):
    snapshot, index_hash, assets_hash = frontend_snapshot(assets)
    cli, jobs, seed_pid = _restore(workspace, run_id)
    # No create_fixture/OnlineReader here: the new process only reads its own
    # local records, with a fake cache reader and an explicitly supplied CLI.
    controller = _controller(workspace, cli)
    server = create_server(controller, assets=assets,
                           asset_snapshot=snapshot,
                           state_provider=lambda: build_local_state(workspace, organizer=controller))
    server.RequestHandlerClass = RestartHandler
    server.fake_cli, server.fake_jobs = cli, jobs
    server.restart_index_sha256, server.restart_assets_sha256 = index_hash, assets_hash
    return server, seed_pid


def serve(workspace, run_id, assets, case):
    server, seed_pid = restored_server(workspace, run_id, assets)
    cli = server.fake_cli
    initial = server.application.state()
    startup = {'job_null': initial['job'] is None, 'fake_read_count': len(cli.calls),
               'recovery': initial['recovery']}
    index_hash, assets_hash = server.restart_index_sha256, server.restart_assets_sha256
    stopped = threading.Event()
    def stop_on_input():
        # EOF also stops a verifier-owned helper, avoiding orphan servers.
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
        write_report(workspace / STATE, _state(cli, server.fake_jobs, run_id, seed_pid))
        result = (final['job'] or {}).get('result') or {}
        report = {'kind': 'modern_restart_fake_account_report', 'version': 1,
                  'case': case, 'run_id': run_id, 'process_id': os.getpid(), 'seed_pid': seed_pid,
                  'index_sha256': index_hash, 'frontend_assets_sha256': assets_hash, 'real_account_calls': 0,
                  'fake_writes': list(cli.writes), 'fake_update_name_count': cli.writes.count('updateName'),
                  'remaining_jobs_not_executed': _remaining_unchanged(cli, server.fake_jobs),
                  'startup': startup, 'end': {'recovery': final['recovery'],
                    'remaining_resumable': (final['data'].get('history') or {}).get('resumable') is True,
                    'reconcile_known_saved': ((final['job'] or {}).get('action') == 'reconcile_renames'
                       and (final['job'] or {}).get('status') == 'completed'
                       and result.get('outcome_known') is True and result.get('record_saved') is True)}}
        write_report(workspace / f'{case}-report.json', report)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--mode', required=True, choices=('seed', 'serve'))
    parser.add_argument('--workspace', required=True)
    parser.add_argument('--owner-token', required=True)
    parser.add_argument('--case', choices=CASES, default='recovery')
    parser.add_argument('--assets', default=str(PROJECT / 'frontend/dist'))
    options = parser.parse_args()
    try:
        workspace, run_id = validate_workspace(options.workspace, options.owner_token, require_parent=True)
        if options.mode == 'seed':
            print(json.dumps(seed(workspace, run_id, Path(options.assets))), flush=True)
        else:
            serve(workspace, run_id, Path(options.assets), options.case)
        return 0
    except (Exception, KeyboardInterrupt):
        print(json.dumps({'kind': 'fake_restart_failure', 'verified': False, 'real_account_calls': 0}), flush=True)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
