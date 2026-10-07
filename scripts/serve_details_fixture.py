"""Serve saved, deliberately partial songs from a verifier-owned fake workspace.

No OfficialCli, user cache, credentials, account URL or account write is used.
The caller owns the temporary directory; stdin newline or EOF stops the helper.
"""

import argparse
import copy
import json
import os
from pathlib import Path
import re
import sys
import threading

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT))

from scripts.serve_web_fixture import create_fixture
from scripts.verify_restart import frontend_snapshot
from scripts.verify_web import VerificationError, _json, write_report
from netease_organizer.online_planning import build_online_plan
from netease_organizer.web_server import _WorkbenchHandler, shutdown_server
from netease_organizer.web_state import build_local_state


MARKER = 'details-fixture-owner.json'
COUNTS = {'expected': 67, 'observed': 62, 'missing': 5, 'metadata_missing': 2}


def validate_workspace(workspace, owner_token, *, require_parent=False):
    """Accept only a marked disposable directory, never a product data tree."""
    try:
        supplied = Path(workspace)
        if not supplied.is_absolute() or supplied.is_symlink():
            raise ValueError()
        resolved = supplied.resolve(strict=True)
        if (not resolved.is_dir() or not resolved.name.startswith('organizer-details-')
                or resolved.is_relative_to(PROJECT) or PROJECT.is_relative_to(resolved)
                or any(parent.is_symlink() for parent in supplied.parents)
                or not isinstance(owner_token, str) or not re.fullmatch(r'[a-f0-9]{64}', owner_token)):
            raise ValueError()
        path = resolved / MARKER
        if path.is_symlink():
            raise ValueError()
        with path.open('rb') as stream:
            marker = _json(stream.read(4097).decode('utf-8'), 'workspace_ownership', 4096)
        if (set(marker) != {'kind', 'version', 'owner_token', 'owner_pid', 'run_id'}
                or marker['kind'] != 'organizer_details_fixture_owner'
                or type(marker['version']) is not int or marker['version'] != 1
                or marker['owner_token'] != owner_token or type(marker['owner_pid']) is not int
                or marker['owner_pid'] <= 0 or not isinstance(marker['run_id'], str)
                or not re.fullmatch(r'[a-f0-9]{32}', marker['run_id'])
                or require_parent and marker['owner_pid'] != os.getppid()):
            raise ValueError()
        return resolved, marker['run_id']
    except (OSError, UnicodeError, ValueError, TypeError):
        raise VerificationError('workspace_ownership') from None


class DetailsHandler(_WorkbenchHandler):
    def do_GET(self):
        if self.path == '/fixture/details':
            if self._guard():
                cli = self.server.fake_cli
                self._json(200, {'kind': 'fake_details_account_state', 'real_account_calls': 0,
                    'fake_writes': list(cli.writes), 'fake_write_count': len(cli.writes),
                    'fake_call_count': len(cli.calls),
                    'frontend_assets_sha256': self.server.details_assets_sha256})
            return
        super().do_GET()


def saved_server(workspace, assets):
    """Seed a bounded historical record, then expose only local display reads."""
    frozen, index_hash, assets_hash = frontend_snapshot(assets)
    server = create_fixture(workspace, assets=assets)
    try:
        controller, cli = server.application.controller, server.fake_cli
        path = Path(workspace) / 'artifacts/在线整理快照.json'
        with path.open('rb') as stream:
            live = _json(stream.read(1024*1024+1).decode('utf-8'), 'fixture_seed')
        if len(live['liked']['tracks']) != 67 or live['liked']['track_count'] != 67:
            raise VerificationError('fixture_seed')
        tracks = live['liked']['tracks'][:62]
        tracks[0].update(artists=[], metadata_available=False)
        tracks[54]['metadata_available'] = False  # A second-page gap retains its known artist.
        tracks[6]['name'] = 'ＳＵＭＭＥＲ · 离线验收'
        live['liked'].update(tracks=tracks, membership_complete=False, metadata_complete=False,
                             missing_record_count=5,
                             missing_metadata_track_ids=[tracks[0]['original_id'], tracks[54]['original_id']])
        live['complete'] = False
        controller._write_artifact('在线整理快照.json', json.dumps(live, ensure_ascii=False))
        controller._write_artifact('在线整理清单.json', json.dumps(build_online_plan(live), ensure_ascii=False))
        # A separate all-complete record exercises an empty metadata filter.
        # These five songs and identities come only from this fake fixture's JSON.
        normal = next(row for row in live['playlists'] if row['name'] == '夜晚的纯音乐')
        normal_tracks = copy.deepcopy(tracks[21:26])
        if normal['track_count'] != 5 or not all(track['metadata_available'] for track in normal_tracks):
            raise VerificationError('fixture_seed')
        detail = {'kind': 'playlist_details', 'version': 1, 'read_at': 1700000000000,
                  'account': {key: live['account'][key] for key in ('id', 'original_id')},
                  'complete': True,
                  'playlist': {**normal, 'creator_id': live['account']['id'], 'tracks': normal_tracks,
                               'membership_complete': True, 'metadata_complete': True,
                               'missing_record_count': 0, 'missing_metadata_track_ids': []}}
        controller._write_artifact(Path('歌单明细')/f"{normal['original_id']}.json",
                                   json.dumps(detail, ensure_ascii=False))
        # A reproducible historical timestamp, never the time of a live account read.
        for name in ('在线整理快照.json', '在线整理清单.json'):
            os.utime(Path(workspace)/'artifacts'/name, (1700000000, 1700000000))
        cli.calls.clear()
        if cli.writes:
            raise VerificationError('fixture_seed')
        server.application.data = build_local_state(Path(workspace), organizer=controller)
        server.RequestHandlerClass = DetailsHandler
        server.asset_snapshot = frozen
        server.details_index_sha256, server.details_assets_sha256 = index_hash, assets_hash
        return server
    except BaseException:
        server.server_close()
        raise


def serve(workspace, run_id, assets):
    server = saved_server(workspace, assets)
    cli = server.fake_cli
    initial = server.application.state()
    startup = {'job_null': initial['job'] is None, 'fake_call_count': len(cli.calls)}
    stopped = threading.Event()
    def stop_on_input():
        sys.stdin.readline()  # EOF also closes a verifier-owned helper.
        stopped.set()
    threading.Thread(target=stop_on_input, daemon=True).start()
    serving = threading.Thread(target=server.serve_forever, daemon=False)
    serving.start()
    print(json.dumps({'kind': 'fake_account_only', 'pid': os.getpid(), 'real_account_calls': 0,
                      'url': f'http://127.0.0.1:{server.server_address[1]}/'}), flush=True)
    try:
        stopped.wait(150)
    finally:
        final = server.application.state()
        if not shutdown_server(server, timeout=10):
            raise VerificationError('helper_shutdown')
        serving.join(timeout=3)
        if serving.is_alive():
            raise VerificationError('helper_shutdown')
        write_report(Path(workspace)/'details-report.json', {
            'kind': 'modern_details_fake_account_report', 'version': 1, 'run_id': run_id,
            'process_id': os.getpid(), 'index_sha256': server.details_index_sha256,
            'frontend_assets_sha256': server.details_assets_sha256,
            'real_account_calls': 0, 'fake_writes': list(cli.writes), 'fake_write_count': len(cli.writes),
            'startup': startup, 'end': {'job_null': final['job'] is None, 'fake_call_count': len(cli.calls)},
            'saved_counts': dict(COUNTS)})


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--workspace', required=True)
    parser.add_argument('--owner-token', required=True)
    parser.add_argument('--assets', default=str(PROJECT/'frontend/dist'))
    options = parser.parse_args()
    try:
        workspace, run_id = validate_workspace(options.workspace, options.owner_token, require_parent=True)
        serve(workspace, run_id, Path(options.assets))
        return 0
    except (Exception, KeyboardInterrupt):
        print(json.dumps({'kind': 'fake_details_failure', 'verified': False, 'real_account_calls': 0}), flush=True)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
