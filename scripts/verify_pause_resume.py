"""Real Tk + Organizer + fake account; never open account credentials or APIs."""

import copy
import json
import sys
import tempfile
import threading
import time
import tkinter as tk
from pathlib import Path
from unittest.mock import Mock

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT))
sys.path.insert(0, str(PROJECT / 'tests'))

from netease_organizer.service import Organizer, OrganizerError
from netease_organizer.ui import OrganizerWindow
from test_create import FakeCli
from test_artist_service import APPROVED


def main():
    with tempfile.TemporaryDirectory(prefix='organizer-pause-resume-') as folder:
        workspace = Path(folder)
        cli = FakeCli()
        cli.configured = lambda: True
        cli.installed = lambda: True
        cli.version = lambda: '0.1.7（测试替身）'
        reader = Mock()
        reader.load.return_value = {'owner_id': '42'}
        controller = Organizer(workspace, cli=cli, reader=reader, data_dir=workspace / 'fixture-cache')
        jobs, start = [], 11
        for name, count in APPROVED.items():
            jobs.append({'kind': 'create_artist_playlist', 'name': name,
                         'candidate_track_ids': [f'{i:032X}' for i in range(start, start + count)],
                         'intended_visibility': 'provider_default', 'status': 'ready'})
            start += count
        controller._write_artifact('已批准歌手精选.json', json.dumps(
            {'kind': 'approved_artist_selection', 'account_original_id': '42',
             'intended_visibility': 'provider_default', 'jobs': jobs}, ensure_ascii=False))
        def preview(**kwargs):
            controller.control.checkpoint()
            controller._online_plan = {'jobs': copy.deepcopy(jobs)}
            controller._online_snapshot = {'account': {'original_id': '42'}}
            return {'status': 'online_plan_ready'}
        controller.online_preview = preview
        entered, release = threading.Event(), threading.Event()
        run = cli.run_json
        gate_used = False
        def gated(arguments):
            nonlocal gate_used
            result = run(arguments)
            if arguments[:2] == ['playlist', 'create'] and not gate_used:
                gate_used = True
                entered.set()
                if not release.wait(10):
                    raise TimeoutError('Fixture gate timed out')
            return result
        cli.run_json = gated
        root = tk.Tk()
        root.withdraw()
        window = None
        def spin_until(predicate):
            deadline = time.monotonic() + 12
            while not predicate():
                root.update()
                assert time.monotonic() < deadline, 'GUI fake flow did not finish'
                time.sleep(0.01)
        try:
            controller.request_pause()
            window = OrganizerWindow(root, controller, OrganizerError)
            spin_until(lambda: window._startup_after_id is None and not window._busy)
            assert controller.control.pause_requested and cli.calls == []
            window._start_task('创建歌手精选', lambda: controller.execute_artist_playlists(
                accept_default_visibility=True))
            spin_until(entered.is_set)
            assert window._busy and not window._pause_button.instate(['disabled'])
            window._request_pause()
            assert controller.control.pause_requested
            release.set()
            spin_until(lambda: not window._busy)
            assert '已暂停' in window._status.get()
            assert cli.writes == ['create']
            assert len(cli.created) == 1
            assert not window._resume_button.instate(['disabled'])
            assert '上次执行记录' in window._last_result.get()
            checkpoint = json.loads((workspace / 'artifacts/歌手精选执行进度.json').read_text(encoding='utf-8'))
            assert checkpoint['items'][0]['phase'] == 'created'
            assert checkpoint['phase'] == 'paused' and checkpoint['outcome_known'] is True
            window._resume_last_operation()
            spin_until(lambda: not window._busy)
            assert '已完成' in window._status.get()
            assert window._resume_button.instate(['disabled'])
            assert cli.writes.count('create') == cli.writes.count('add') == 5
            result = json.loads((workspace / 'artifacts/歌手精选执行结果.json').read_text(encoding='utf-8'))
            assert result['completed_count'] == 5 and result['status'] == 'completed'
            for item, job in zip(result['items'], jobs):
                assert cli.members[item['playlist_id']] == job['candidate_track_ids']
            assert cli.secret not in window._log.get('1.0', 'end')
            report = {'kind': 'organizer_pause_resume_local_verification', 'real_tk': True,
                      'startup_account_calls': 0, 'startup_keeps_pause': True,
                      'pause_while_request_in_flight': True, 'post_write_readback_finished': True,
                      'checkpoint_preserved': True, 'manual_resume_completed': True,
                      'fake_playlist_count': 5, 'fake_create_count': 5, 'fake_add_count': 5,
                      'no_duplicate_mutations': True, 'full_members_and_order_verified': True,
                      'completed_resume_disabled': True, 'real_account_calls': 0}
        finally:
            release.set()
            if window is not None:
                window._closed = True
                window._stop_ui_callbacks()
            root.destroy()
    destination = PROJECT / 'artifacts/程序暂停续做验收.json'
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print(json.dumps({'report': str(destination), 'verified': True, 'real_account_calls': 0}, ensure_ascii=False))


if __name__ == '__main__':
    main()
