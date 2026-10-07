"""Browser acceptance of durable rename recovery across three fake processes.

Only a new owned temporary workspace is accepted. No real organizer launcher,
account arguments, credential files, package installation, or npm is used.
"""

import argparse
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import secrets
import shutil
import subprocess
import sys
import tempfile
import time
from types import MappingProxyType

try:
    from .verify_web import (PROJECT, Cli, VerificationError, _NO_WINDOW, _announcement,
                             _json, file_signature, load_fresh_report, locate_cli,
                             stop_helper, write_report)
except ImportError:
    from verify_web import (PROJECT, Cli, VerificationError, _NO_WINDOW, _announcement,
                            _json, file_signature, load_fresh_report, locate_cli,
                            stop_helper, write_report)

# Direct execution starts with scripts/ on sys.path, rather than the project.
# Asset validation imports the local package and must work without PYTHONPATH.
if str(PROJECT) not in sys.path:
    sys.path.insert(0, str(PROJECT))


_MARKER = 'restart-fixture-owner.json'
_HELPER = PROJECT / 'scripts/serve_restart_fixture.py'
_CLEAR = {'status': 'clear', 'operation': 'unknown', 'can_reconcile': False}
_PENDING = {'status': 'review_required', 'operation': 'renames', 'can_reconcile': True}
_COMMON = ('startup_job_null', 'recovery_no_automatic_action', 'narrow_no_horizontal_overflow',
           'fake_update_name_count_one', 'remaining_jobs_not_executed', 'resume_available_without_replay',
           'served_assets_match')
ASSERTIONS = {
    'recovery': _COMMON + ('pending_card_visible', 'three_write_buttons_frozen',
        'readonly_check_keeps_freeze', 'refresh_keeps_freeze', 'reconcile_completed_known_saved',
        'reconcile_does_not_write', 'pending_card_closed', 'available_writes_unfrozen'),
    'clear-restart': _COMMON + ('startup_recovery_clear', 'pending_card_absent', 'available_writes_unfrozen'),
}
_STAGES = frozenset(('layout', 'startup', 'freeze', 'read_check', 'refresh', 'reconcile',
                     'reconcile_result', 'unfreeze', 'proof'))


def mark_workspace(workspace, owner_token, run_id):
    path = Path(workspace)
    if (not path.is_absolute() or path.is_symlink() or not path.name.startswith('organizer-restart-')
            or path.resolve().is_relative_to(PROJECT) or PROJECT.is_relative_to(path.resolve())
            or not isinstance(owner_token, str) or not re.fullmatch('[a-f0-9]{64}', owner_token)
            or not isinstance(run_id, str) or not re.fullmatch('[a-f0-9]{32}', run_id)
            or (path / _MARKER).exists()):
        raise VerificationError('workspace_ownership')
    # Exclusive creation ensures a stale marker cannot be silently adopted.
    with (path / _MARKER).open('x', encoding='utf-8') as stream:
        json.dump({'kind': 'organizer_restart_fixture_owner', 'version': 1,
                   'owner_token': owner_token, 'owner_pid': os.getpid(), 'run_id': run_id}, stream)
        stream.flush()
        os.fsync(stream.fileno())


def check_evidence(stdout, fixture, case, *, run_id, index_sha256, frontend_assets_sha256, expected_pid, seed_pid):
    outer = _json(stdout, 'browser_callback')
    value = outer.get('result')
    observed = _json(value, 'browser_callback') if isinstance(value, str) else value
    if (not isinstance(case, str) or case not in ASSERTIONS or outer.get('isError') is True or not isinstance(observed, dict)
            or observed.get('kind') != 'modern_restart_browser_observation'
            or observed.get('case') != case or observed.get('verified') is not True):
        stage = observed.get('failed_stage') if isinstance(observed, dict) else None
        raise VerificationError(stage if isinstance(stage, str) and stage in _STAGES else 'browser_callback')
    assertions = observed.get('assertions')
    if (not isinstance(assertions, dict) or any(assertions.get(name) is not True for name in ASSERTIONS[case])
            or observed.get('viewport') != {'width': 390, 'height': 844}
            or observed.get('frontend_assets_sha256') != frontend_assets_sha256
            or not isinstance(fixture, dict) or fixture.get('kind') != 'modern_restart_fake_account_report'
            or type(fixture.get('version')) is not int or fixture['version'] != 1
            or fixture.get('case') != case or fixture.get('run_id') != run_id
            or fixture.get('index_sha256') != index_sha256
            or fixture.get('frontend_assets_sha256') != frontend_assets_sha256
            or type(fixture.get('process_id')) is not int or fixture['process_id'] != expected_pid
            or type(fixture.get('seed_pid')) is not int or fixture['seed_pid'] != seed_pid
            or type(expected_pid) is not int or type(seed_pid) is not int
            or expected_pid <= 0 or seed_pid <= 0 or expected_pid == seed_pid
            or type(fixture.get('real_account_calls')) is not int or fixture['real_account_calls'] != 0
            or type(fixture.get('fake_update_name_count')) is not int or fixture['fake_update_name_count'] != 1
            or fixture.get('fake_writes') != ['updateName'] or fixture.get('remaining_jobs_not_executed') is not True):
        raise VerificationError('fixture_report')
    startup, end = fixture.get('startup'), fixture.get('end')
    if (not isinstance(startup, dict) or startup.get('job_null') is not True
            or type(startup.get('fake_read_count')) is not int or startup['fake_read_count'] != 0
            or startup.get('recovery') != (_PENDING if case == 'recovery' else _CLEAR)
            or not isinstance(end, dict) or end.get('recovery') != _CLEAR
            or end.get('reconcile_known_saved') is not (case == 'recovery')
            or end.get('remaining_resumable') is not True):
        raise VerificationError('fixture_report')
    return {'case': case, 'verified': True, 'process_id': expected_pid,
            'fake_update_name_count': 1, 'viewport': {'width': 390, 'height': 844},
            'assertions': {name: True for name in ASSERTIONS[case]}}


def browser_script(case, url, *, assets_sha256):
    if case not in ASSERTIONS:
        raise VerificationError('browser_script')
    # Session nonce remains solely in this fake page's memory.
    return r'''async page => {
  const scenario = __CASE__, base = __URL__, assetsHash = __ASSETS_HASH__, assertions = {};
  const deadline = Date.now() + 42000;
  let stage = 'layout', nonce;
  const require = value => { if (!value) throw new Error('acceptance'); };
  const request = page.context().request;
  const get = async path => {
    const response = await request.get(base + path, {headers: {'X-Organizer-Session': nonce}, timeout: 2000});
    require(response.status() === 200);
    return await response.json();
  };
  const waitState = async predicate => {
    while (Date.now() < deadline) {
      const state = await get('api/state');
      if (predicate(state)) return state;
      await new Promise(resolve => setTimeout(resolve, 100));
    }
    throw new Error('acceptance timeout');
  };
  const readNonce = async () => {
    nonce = await page.locator('meta[name="organizer-session"]').getAttribute('content');
    require(typeof nonce === 'string' && /^[a-f0-9]{64}$/.test(nonce));
  };
  const pending = state => state.recovery.status === 'review_required' &&
    state.recovery.operation === 'renames' && state.recovery.can_reconcile === true;
  const clear = state => state.recovery.status === 'clear' && state.recovery.can_reconcile === false;
  const card = page.getByRole('region', {name: '上次操作待核对', exact: true});
  const rename = page.getByRole('button', {name: '整理现有歌单名称', exact: true});
  const artists = page.getByRole('button', {name: '创建歌手精选', exact: true});
  const consent = page.getByRole('checkbox', {name: '接受歌手精选可能公开', exact: true});
  const frozen = async () => {
    require(await rename.isDisabled() && await artists.isDisabled());
    const resumes = page.getByRole('button', {name: '继续上次任务', exact: true});
    require(await resumes.count() > 0);
    for (const button of await resumes.all()) require(await button.isDisabled());
  };
  const proof = async () => {
    const evidence = await get('fixture/restart');
    require(evidence.kind === 'fake_restart_account_state' && evidence.real_account_calls === 0 &&
      evidence.fake_update_name_count === 1 && JSON.stringify(evidence.fake_writes) === '["updateName"]' &&
      evidence.remaining_jobs_not_executed === true && evidence.frontend_assets_sha256 === assetsHash);
    assertions.served_assets_match = true;
  };
  try {
    page.setDefaultTimeout(6000);
    await page.setViewportSize({width: 390, height: 844});
    await readNonce();
    await page.getByRole('heading', {name: /让每一份喜欢/}).waitFor();
    await rename.waitFor();
    require(await page.evaluate(() => innerWidth === 390 && document.documentElement.scrollWidth <= innerWidth + 1));
    assertions.narrow_no_horizontal_overflow = true;
    stage = 'startup';
    const initial = await get('api/state');
    require(initial.job === null);
    assertions.startup_job_null = true;
    await proof();
    assertions.recovery_no_automatic_action = true;
    await consent.check();
    if (scenario === 'recovery') {
      require(pending(initial));
      await card.waitFor();
      assertions.pending_card_visible = true;
      stage = 'freeze';
      await frozen();
      assertions.three_write_buttons_frozen = true;
      stage = 'read_check';
      const nav = page.getByRole('navigation', {name: '主导航'});
      await nav.getByRole('link', {name: '接入设置', exact: true}).click();
      await page.getByRole('button', {name: '检查本地接入', exact: true}).click();
      const checked = await waitState(state => state.job?.action === 'check' && state.job.status === 'completed');
      require(pending(checked));
      await nav.getByRole('link', {name: '概览', exact: true}).click();
      await page.locator('section[aria-label="当前任务"] .task-stage').filter({hasText: checked.job.result.message}).waitFor();
      await frozen();
      await card.waitFor();
      assertions.readonly_check_keeps_freeze = true;
      stage = 'refresh';
      await page.reload();
      await readNonce();
      await card.waitFor();
      await consent.check();
      await frozen();
      const refreshed = await get('api/state');
      require(pending(refreshed) && refreshed.job.id === checked.job.id);
      await proof();
      assertions.refresh_keeps_freeze = true;
      stage = 'reconcile';
      await page.getByRole('button', {name: '只读核对上次改名', exact: true}).click();
      const resolved = await waitState(state => state.job?.action === 'reconcile_renames' &&
        !['queued', 'running', 'pause_requested'].includes(state.job.status));
      stage = 'reconcile_result';
      require(resolved.job.status === 'completed' && resolved.job.result.status === 'completed' &&
        resolved.job.result.outcome_known === true && resolved.job.result.record_saved === true &&
        resolved.job.result.write_attempted === false && resolved.job.result.completed_count === 1 && clear(resolved));
      assertions.reconcile_completed_known_saved = true;
      await proof();
      assertions.reconcile_does_not_write = true;
      await page.locator('section[aria-label="当前任务"] .task-stage').filter({hasText: resolved.job.result.message}).waitFor();
      stage = 'unfreeze';
      await card.waitFor({state: 'detached'});
      assertions.pending_card_closed = true;
    } else {
      require(clear(initial));
      assertions.startup_recovery_clear = true;
      require(await card.count() === 0);
      assertions.pending_card_absent = true;
    }
    stage = 'unfreeze';
    require(await rename.isEnabled() && await artists.isEnabled());
    assertions.available_writes_unfrozen = true;
    const resumable = await get('api/state');
    require(resumable.data.history?.status === 'paused' && resumable.data.history.resumable === true);
    const resumes = page.getByRole('button', {name: '继续上次任务', exact: true});
    require(await resumes.count() > 0);
    for (const button of await resumes.all()) require(await button.isEnabled());
    assertions.resume_available_without_replay = true;
    stage = 'proof';
    await proof();
    assertions.fake_update_name_count_one = true;
    assertions.remaining_jobs_not_executed = true;
    return {kind: 'modern_restart_browser_observation', case: scenario, verified: true,
            viewport: {width: 390, height: 844}, frontend_assets_sha256: assetsHash, assertions};
  } catch { return {kind: 'modern_restart_browser_observation', case: scenario, verified: false, failed_stage: stage}; }
}'''.replace('__CASE__', json.dumps(case)).replace('__URL__', json.dumps(url)).replace('__ASSETS_HASH__', json.dumps(assets_sha256))


def _arguments(mode, workspace, token, case='recovery'):
    return [sys.executable, '-X', 'utf8', str(_HELPER), '--mode', mode, '--workspace', str(workspace),
            '--owner-token', token, '--case', case]


def frontend_snapshot(assets):
    """Bound and freeze every served public asset, including its relative name."""
    from netease_organizer.web_build import (LaunchError, MAX_ASSET_BYTES, MAX_FILES, MAX_TOTAL_BYTES,
                                            _ASSET_SUFFIXES, _signature, _validate_entry)
    try:
        assets = Path(assets)
        if assets.is_symlink():
            raise ValueError()
        assets = assets.resolve(strict=True)
        def files():
            result = []
            for scanned, path in enumerate(assets.rglob('*'), 1):
                if scanned > MAX_FILES * 4 or path.is_symlink():
                    raise ValueError()
                relative = path.relative_to(assets)
                if (path.is_file() and path.suffix.lower() in _ASSET_SUFFIXES
                        and not any(part.startswith('.') for part in relative.parts)):
                    if not path.resolve().is_relative_to(assets):
                        raise ValueError()
                    result.append(path)
                    if len(result) > MAX_FILES:
                        raise ValueError()
            return sorted(result, key=lambda path: path.relative_to(assets).as_posix())
        paths = files()
        signatures = {path: _signature(path) for path in paths}
        snapshot, digest, total = {}, hashlib.sha256(), 0
        for path in paths:
            if signatures[path][2] > MAX_ASSET_BYTES:
                raise ValueError()
            with path.open('rb') as stream:
                raw = stream.read(MAX_ASSET_BYTES + 1)
            total += len(raw)
            if len(raw) > MAX_ASSET_BYTES or total > MAX_TOTAL_BYTES or _signature(path) != signatures[path]:
                raise ValueError()
            relative = path.relative_to(assets).as_posix()
            name = relative.encode('utf-8')
            digest.update(len(name).to_bytes(4, 'big'))
            digest.update(name)
            digest.update(len(raw).to_bytes(8, 'big'))
            digest.update(raw)
            snapshot[relative] = raw
        if files() != paths or any(_signature(path) != signatures[path] for path in paths):
            raise ValueError()
        index = snapshot.get('index.html', b'')
        if len(index) > 1024 * 1024 or b'__ORGANIZER_SESSION__' not in index:
            raise ValueError()
        _validate_entry(snapshot)
        return MappingProxyType(snapshot), hashlib.sha256(index).hexdigest(), digest.hexdigest()
    except (OSError, ValueError, UnicodeError, LaunchError):
        raise VerificationError('assets') from None


def require_frontend_fresh(assets, index_hash, assets_hash):
    _, current_index, current_assets = frontend_snapshot(assets)
    if current_index != index_hash or current_assets != assets_hash:
        raise VerificationError('assets_changed')


def run_seed(workspace, token, run_id, index_hash, assets_hash, deadline):
    path = Path(workspace) / 'seed-report.json'
    previous, started_ns = file_signature(path), time.time_ns()
    process = None
    try:
        remaining = min(15, deadline - time.monotonic())
        if remaining <= 0:
            raise VerificationError('seed_deadline')
        process = subprocess.Popen(_arguments('seed', workspace, token), shell=False, cwd=PROJECT,
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            text=True, encoding='utf-8', creationflags=_NO_WINDOW)
        stdout, _ = process.communicate(timeout=remaining)
        if process.returncode != 0:
            raise VerificationError('seed_process')
        emitted = _json(stdout, 'seed_report')
        saved = load_fresh_report(path, previous, started_ns)
        if (emitted != saved or saved.get('kind') != 'modern_restart_seed_report'
                or saved.get('run_id') != run_id or saved.get('index_sha256') != index_hash
                or saved.get('frontend_assets_sha256') != assets_hash
                or type(saved.get('process_id')) is not int or saved['process_id'] != process.pid
                or type(saved.get('real_account_calls')) is not int or saved['real_account_calls'] != 0
                or type(saved.get('fake_update_name_count')) is not int or saved['fake_update_name_count'] != 1
                or any(saved.get(name) is not True for name in ('intent_before_write', 'result_uncertain',
                                                              'delayed_effect_not_replay', 'remaining_jobs_pending'))):
            raise VerificationError('seed_report')
        return {'process_id': process.pid, 'assertions': {'actual_rename_intent_before_write': True,
                'uncertain_original_request_delayed_effect': True, 'seed_process_exited_naturally': True}}
    except (OSError, subprocess.SubprocessError):
        raise VerificationError('seed_process') from None
    finally:
        if process is not None:
            if process.poll() is None:
                try:
                    process.terminate()
                    process.wait(timeout=3)
                except (OSError, subprocess.SubprocessError):
                    process.kill()
                    process.wait(timeout=3)
            if process.stdout:
                process.stdout.close()


def _cleanup_browser(cli):
    """Every owned cleanup step runs; an interrupted close has one extra try.

    Any exception still rejects the verification, even if the bounded second
    close confirms exit. These commands only address this driver's session.
    """
    successful = True
    try:
        for _ in range(2):
            try:
                cli.command('close', timeout=8, cleanup=True)
                break
            except (Exception, KeyboardInterrupt):
                successful = False
    finally:
        try:
            cli.command('delete-data', timeout=8, cleanup=True)
        except (Exception, KeyboardInterrupt):
            successful = False
    return successful


def run_case(case, node, entry, workspace, token, run_id, index_hash, seed_pid, deadline, *, assets_sha256, browser='msedge'):
    report_path = Path(workspace) / f'{case}-report.json'
    previous, started_ns = file_signature(report_path), time.time_ns()
    cli = Cli(node, entry, 'organizer-restart-' + secrets.token_hex(6), workspace, deadline)
    process = None
    observed = error = None
    stage = 'helper_start'
    cleaned = stopped = False
    try:
        process = subprocess.Popen(_arguments('serve', workspace, token, case), shell=False, cwd=PROJECT,
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            text=True, encoding='utf-8', creationflags=_NO_WINDOW)
        url = _announcement(process, deadline)
        stage = cli.stage = 'browser_open'
        cli.command('open', url, '--browser=' + browser, '--idle-timeout=60000', timeout=25)
        script = Path(workspace) / f'{case}.js'
        script.write_text(browser_script(case, url, assets_sha256=assets_sha256), encoding='utf-8')
        stage = cli.stage = 'browser_callback'
        observed = cli.command('run-code', '--filename=' + str(script), timeout=45)
    except VerificationError as failure:
        error = failure
    except KeyboardInterrupt:
        error = VerificationError('cancelled')
    except Exception:
        error = VerificationError(stage)
    finally:
        try:
            try:
                cleaned = _cleanup_browser(cli)
            except (Exception, KeyboardInterrupt):
                cleaned = False
        finally:
            if process is not None:
                try:
                    stopped = stop_helper(process, timeout=12)
                except (Exception, KeyboardInterrupt):
                    stopped = False
                finally:
                    for stream in (process.stdin, process.stdout):
                        if stream:
                            try:
                                stream.close()
                            except (Exception, KeyboardInterrupt):
                                cleaned = False
    # Cleanup failure also rejects a callback which had already reported PASS.
    if not cleaned or not stopped:
        raise VerificationError('cleanup')
    if error:
        raise error
    result = check_evidence(observed, load_fresh_report(report_path, previous, started_ns), case,
                            run_id=run_id, index_sha256=index_hash, frontend_assets_sha256=assets_sha256,
                            expected_pid=process.pid, seed_pid=seed_pid)
    require_frontend_fresh(PROJECT / 'frontend/dist', index_hash, assets_sha256)
    result['assertions']['own_browser_and_helper_cleaned'] = True
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--cli-path')
    parser.add_argument('--browser', choices=('msedge', 'chrome'), default='msedge')
    parser.add_argument('--timeout', type=int, default=180)
    options = parser.parse_args()
    started = time.monotonic()
    run_id = secrets.token_hex(16)
    report = {'kind': 'modern_restart_fake_browser_verification', 'version': 1, 'run_id': run_id,
              'verified': False, 'real_account_calls': 0, 'scenarios': [], 'failed_stage': 'started',
              'tested_versions': {'python': platform.python_version()}, 'assertions': {}}
    destination = PROJECT / 'artifacts/modern-restart-verification.json'
    try:
        # Invalidate an old PASS before prerequisites or external processes.
        write_report(destination, report)
        if not 90 <= options.timeout <= 300:
            raise VerificationError('prerequisites')
        node = shutil.which('node')
        if not node:
            raise VerificationError('node_location')
        entry, versions = locate_cli(options.cli_path)
        report['tested_versions'].update(versions)
        _, index_hash, assets_hash = frontend_snapshot(PROJECT / 'frontend/dist')
        report['tested_versions']['frontend_index_sha256'] = index_hash
        report['tested_versions']['frontend_assets_sha256'] = assets_hash
        report['tested_versions']['browser'] = options.browser
        deadline = started + options.timeout
        with tempfile.TemporaryDirectory(prefix='organizer-restart-acceptance-') as folder:
            workspace, token = Path(folder), secrets.token_hex(32)
            mark_workspace(workspace, token, run_id)
            seeded = run_seed(workspace, token, run_id, index_hash, assets_hash, deadline)
            report['assertions'].update(seeded['assertions'])
            pids = {seeded['process_id']}
            for case in ASSERTIONS:
                try:
                    result = run_case(case, node, entry, workspace, token, run_id, index_hash,
                                      seeded['process_id'], deadline, assets_sha256=assets_hash, browser=options.browser)
                    if result['process_id'] in pids:
                        raise VerificationError('process_identity')
                    pids.add(result['process_id'])
                    report['scenarios'].append(result)
                except VerificationError as error:
                    raise VerificationError(case + ':' + error.stage) from None
        report['assertions'].update(three_distinct_python_processes=True, temporary_workspace_cleaned=True,
                                    all_owned_helpers_and_browsers_cleaned=True, frontend_index_fresh=True,
                                    frontend_assets_fresh=True)
        report.update(verified=True, failed_stage=None)
    except VerificationError as error:
        report['failed_stage'] = error.stage
    except KeyboardInterrupt:
        report['failed_stage'] = 'cancelled'
    except Exception:
        report['failed_stage'] = 'verification'
    report['elapsed_seconds'] = round(time.monotonic() - started, 3)
    try:
        write_report(destination, report)
    except (OSError, ValueError):
        report.update(verified=False, failed_stage='report_save')
    print(json.dumps({'verified': report['verified'], 'scenario_count': len(report['scenarios']),
                      'failed_stage': report['failed_stage'], 'real_account_calls': 0}))
    return 0 if report['verified'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
