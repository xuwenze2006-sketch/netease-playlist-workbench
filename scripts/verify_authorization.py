"""Repeatable fake browser acceptance for explicit reauthorization and recovery."""

import argparse
import json
import os
from pathlib import Path
import platform
import secrets
import shutil
import subprocess
import sys
import tempfile
import time

PROJECT = Path(__file__).resolve().parents[1]
if str(PROJECT) not in sys.path:
    sys.path.insert(0, str(PROJECT))

from scripts.verify_web import (Cli, VerificationError, _NO_WINDOW, _announcement, _json,
                               file_signature, load_fresh_report, locate_cli, stop_helper, write_report)
from scripts.verify_restart import (_cleanup_browser, frontend_snapshot, mark_workspace,
                                   require_frontend_fresh)


_PENDING = {'status': 'review_required', 'operation': 'renames', 'can_reconcile': True}
_CLEAR = {'status': 'clear', 'operation': 'unknown', 'can_reconcile': False}
_COMMON = ('startup_has_no_action', 'narrow_no_horizontal_overflow', 'expired_status_explicit',
    'recovery_renewal_explicit', 'qr_decoded_locally', 'signature_probe_confirmed',
    'authorized_still_freezes_writes', 'explicit_reconcile_only', 'reload_no_replay',
    'single_original_write', 'remaining_jobs_not_executed', 'served_assets_match', 'no_external_navigation')
ASSERTIONS = {'renew-and-reconcile': _COMMON + ('known_saved_reconcile_unfreezes',),
              'wrong-account': _COMMON + ('wrong_account_remains_frozen',),
              'save-failure': _COMMON + ('unsaved_receipt_remains_frozen',)}
_STAGES = frozenset(('startup', 'layout', 'expired', 'renew', 'qr', 'scan', 'authorized',
                     'reconcile', 'result', 'refresh', 'proof'))


def check_evidence(stdout, fixture, case, *, run_id, expected_pid, index_sha256, frontend_assets_sha256):
    outer = _json(stdout, 'browser_callback')
    value = outer.get('result')
    observed = _json(value, 'browser_callback') if isinstance(value, str) else value
    if (not isinstance(case, str) or case not in ASSERTIONS or outer.get('isError') is True
            or not isinstance(observed, dict) or observed.get('kind') != 'modern_authorization_browser_observation'
            or observed.get('case') != case or observed.get('verified') is not True):
        stage = observed.get('failed_stage') if isinstance(observed, dict) else None
        raise VerificationError(stage if isinstance(stage, str) and stage in _STAGES else 'browser_callback')
    assertions = observed.get('assertions')
    if (not isinstance(assertions, dict) or any(assertions.get(name) is not True for name in ASSERTIONS[case])
            or observed.get('viewport') != {'width': 390, 'height': 844}
            or observed.get('frontend_assets_sha256') != frontend_assets_sha256
            or not isinstance(fixture, dict) or fixture.get('kind') != 'modern_authorization_fake_account_report'
            or type(fixture.get('version')) is not int or fixture['version'] != 1
            or fixture.get('case') != case or fixture.get('run_id') != run_id
            or type(fixture.get('process_id')) is not int or fixture['process_id'] != expected_pid
            or fixture.get('index_sha256') != index_sha256 or fixture.get('frontend_assets_sha256') != frontend_assets_sha256
            or type(fixture.get('real_account_calls')) is not int or fixture['real_account_calls'] != 0
            or fixture.get('fake_writes') != ['updateName'] or fixture.get('remaining_jobs_not_executed') is not True
            or fixture.get('actual_rename_intent_used') is not True):
        raise VerificationError('fixture_report')
    startup, authorization, end = fixture.get('startup'), fixture.get('authorization'), fixture.get('end')
    if (not isinstance(startup, dict) or startup.get('job_null') is not True
            or startup.get('authorized_unknown') is not True or type(startup.get('fake_call_count')) is not int
            or startup['fake_call_count'] != 0 or startup.get('recovery') != _PENDING
            or not isinstance(authorization, dict) or authorization.get('expired_check_observed') is not True
            or authorization.get('signature_changed') is not True
            or any(type(authorization.get(key)) is not int or authorization[key] != 1
                   for key in ('background_count', 'scan_count', 'authorized_check_count'))
            or not isinstance(end, dict) or end.get('recovery') != (_CLEAR if case == 'renew-and-reconcile' else _PENDING)):
        raise VerificationError('fixture_report')
    result, wrong = end.get('reconcile'), case == 'wrong-account'
    if (not isinstance(result, dict) or result.get('status') != ('uncertain' if wrong else 'completed')
            or type(result.get('completed_count')) is not int or result['completed_count'] != (0 if wrong else 1)
            or result.get('write_attempted') is not False or result.get('outcome_known') is not (not wrong)
            or result.get('record_saved') is not (None if wrong else case == 'renew-and-reconcile')):
        raise VerificationError('fixture_report')
    return {'case': case, 'verified': True, 'process_id': expected_pid, 'fake_write_count': 1,
            'viewport': {'width': 390, 'height': 844}, 'frontend_assets_sha256': frontend_assets_sha256,
            'assertions': {name: True for name in ASSERTIONS[case]}}


def browser_script(case, url, *, assets_sha256):
    if case not in ASSERTIONS:
        raise VerificationError('browser_script')
    return r'''async page => {
  const scenario = __CASE__, base = __URL__, hash = __HASH__, assertions = {};
  let stage = 'startup', nonce, externalRequests = 0;
  const deadline = Date.now() + 42000;
  const require = value => {if (!value) throw new Error('acceptance');};
  const request = page.context().request;
  await page.context().route('**/*', async route => {
    if (route.request().url().startsWith(base)) await route.continue();
    else {externalRequests += 1; await route.abort();}
  });
  const get = async path => {
    const response = await request.get(base+path, {headers: {'X-Organizer-Session': nonce}, timeout: 2000});
    require(response.status() === 200); return await response.json();
  };
  const readNonce = async () => {
    nonce = await page.locator('meta[name="organizer-session"]').getAttribute('content');
    require(typeof nonce === 'string' && /^[a-f0-9]{64}$/.test(nonce));
  };
  const waitJob = async (action, condition = () => true) => {
    while (Date.now() < deadline) {
      const state = await get('api/state');
      if (state.job?.action === action && !['queued','running','pause_requested'].includes(state.job.status) && condition(state)) return state;
      await new Promise(resolve => setTimeout(resolve, 100));
    } throw new Error('timeout');
  };
  const pending = state => state.recovery.status === 'review_required' && state.recovery.operation === 'renames';
  const nav = page.getByRole('navigation', {name:'主导航'});
  const card = page.getByRole('region', {name:'上次操作待核对', exact:true});
  const rename = page.getByRole('button', {name:'整理现有歌单名称', exact:true});
  const artists = page.getByRole('button', {name:'创建歌手精选', exact:true});
  const consent = page.getByRole('checkbox', {name:'接受歌手精选可能公开', exact:true});
  const writes = async frozen => {
    require(await rename.isDisabled() === frozen && await artists.isDisabled() === frozen);
    const resume = page.getByRole('button', {name:'继续上次任务', exact:true});
    require(await resume.count() > 0);
    for (const button of await resume.all()) require(await button.isDisabled() === frozen);
  };
  const proof = async () => {
    const evidence = await get('fixture/authorization');
    require(evidence.kind === 'fake_authorization_state' && evidence.real_account_calls === 0 &&
      JSON.stringify(evidence.fake_writes) === '["updateName"]' && evidence.remaining_jobs_not_executed === true &&
      evidence.frontend_assets_sha256 === hash);
    return evidence;
  };
  const visibleResult = async state => {
    await page.locator('section[aria-label="当前任务"] .task-stage').filter({hasText:state.job.result.message}).waitFor();
  };
  try {
    page.setDefaultTimeout(6000);
    await page.setViewportSize({width:390,height:844});
    await readNonce();
    await page.getByRole('heading', {name:/让每一份喜欢/}).waitFor();
    const initial = await get('api/state');
    require(initial.job === null && initial.connection.authorized === null && pending(initial));
    require((await proof()).fake_call_count === 0);
    assertions.startup_has_no_action = true;
    await consent.check();
    await writes(true);
    stage = 'layout';
    require(await page.evaluate(() => innerWidth === 390 && document.documentElement.scrollWidth <= innerWidth+1));
    assertions.narrow_no_horizontal_overflow = true;
    stage = 'expired';
    await nav.getByRole('link', {name:'接入设置', exact:true}).click();
    await page.getByRole('button', {name:'检查本地接入', exact:true}).click();
    await waitJob('check');
    await page.getByRole('button', {name:'验证账号授权', exact:true}).click();
    const expired = await waitJob('login_status');
    require(expired.connection.authorized === false && expired.job.result.status === 'authorization_required' && pending(expired));
    await page.getByRole('region', {name:'接入步骤', exact:true}).filter({hasText:'需要重新授权'}).waitFor();
    assertions.expired_status_explicit = true;
    await nav.getByRole('link', {name:'概览', exact:true}).click();
    stage = 'renew';
    await card.getByRole('button', {name:'重新扫码后只读核对', exact:true}).click();
    const login = await waitJob('login');
    require(login.job.result.status === 'authorization_pending' && pending(login));
    assertions.recovery_renewal_explicit = true;
    stage = 'qr';
    const image = page.getByRole('img', {name:'网易云官方账号授权二维码', exact:true});
    await image.waitFor();
    require(await image.evaluate(async img => {await img.decode(); return img.complete && img.naturalWidth > 0 && img.src.startsWith('data:image/png;base64,');}));
    assertions.qr_decoded_locally = true;
    stage = 'scan';
    const scanned = await request.post(base+'fixture/scan', {headers: {'X-Organizer-Session':nonce,'Content-Type':'application/json'}, data:'{}', timeout:2000});
    require(scanned.status() === 200 && (await scanned.json()).accepted === true);
    const authorized = await waitJob('authorization_probe', state =>
      state.connection.authorized === true && state.job.result?.authorized === true);
    require(authorized.connection.authorized === true && authorized.job.result.authorized === true && pending(authorized));
    await image.waitFor({state:'detached'});
    const authProof = await proof();
    require(authProof.background_count === 1 && authProof.scan_count === 1 && authProof.authorized_check_count === 1);
    assertions.signature_probe_confirmed = true;
    stage = 'authorized';
    await nav.getByRole('link', {name:'概览',exact:true}).click();
    await card.waitFor();
    await writes(true);
    assertions.authorized_still_freezes_writes = true;
    stage = 'reconcile';
    await card.getByRole('button', {name:'只读核对上次改名', exact:true}).click();
    const final = await waitJob('reconcile_renames');
    require(final.job.result.write_attempted === false);
    assertions.explicit_reconcile_only = true;
    stage = 'result';
    await visibleResult(final);
    if (scenario === 'renew-and-reconcile') {
      require(final.job.status === 'completed' && final.job.result.outcome_known === true && final.job.result.record_saved === true && final.recovery.status === 'clear');
      await card.waitFor({state:'detached'}); await writes(false);
      assertions.known_saved_reconcile_unfreezes = true;
    } else {
      require(pending(final)); await card.waitFor(); await writes(true);
      require(scenario === 'wrong-account' ? final.job.status === 'uncertain' && final.job.result.outcome_known === false : final.job.status === 'completed' && final.job.result.outcome_known === true && final.job.result.record_saved === false);
      assertions[scenario === 'wrong-account' ? 'wrong_account_remains_frozen' : 'unsaved_receipt_remains_frozen'] = true;
    }
    stage = 'refresh';
    await page.reload(); await readNonce();
    await page.getByRole('heading', {name:/让每一份喜欢/}).waitFor();
    await consent.check();
    const refreshed = await get('api/state');
    require(refreshed.job.id === final.job.id && refreshed.connection.authorized === true);
    if (scenario === 'renew-and-reconcile') {require(refreshed.recovery.status === 'clear'); await writes(false);}
    else {require(pending(refreshed)); await card.waitFor(); await writes(true);}
    assertions.reload_no_replay = true;
    stage = 'proof';
    const last = await proof();
    require(last.background_count === 1 && last.scan_count === 1 && last.authorized_check_count === 1);
    require(externalRequests === 0 && page.url().startsWith(base));
    Object.assign(assertions, {single_original_write:true,remaining_jobs_not_executed:true,served_assets_match:true,no_external_navigation:true});
    return {kind:'modern_authorization_browser_observation',case:scenario,verified:true,
            viewport:{width:390,height:844},frontend_assets_sha256:hash,assertions};
  } catch {return {kind:'modern_authorization_browser_observation',case:scenario,verified:false,failed_stage:stage};}
}'''.replace('__CASE__', json.dumps(case)).replace('__URL__', json.dumps(url)).replace('__HASH__', json.dumps(assets_sha256))


def run_case(case, node, entry, workspace, token, run_id, index_hash, assets_hash, deadline):
    path = Path(workspace) / 'authorization-report.json'
    previous, started_ns = file_signature(path), time.time_ns()
    cli = Cli(node, entry, 'organizer-authorization-'+secrets.token_hex(6), workspace, deadline)
    process = None
    observed = error = None
    cleaned = stopped = False
    stage = 'helper_start'
    try:
        process = subprocess.Popen([sys.executable, '-X', 'utf8', str(PROJECT / 'scripts/serve_authorization_fixture.py'),
            '--workspace', str(workspace), '--owner-token', token, '--case', case], shell=False, cwd=workspace,
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            text=True, encoding='utf-8', creationflags=_NO_WINDOW)
        url = _announcement(process, deadline)
        stage = cli.stage = 'browser_open'
        cli.command('open', url, '--browser=msedge', '--idle-timeout=60000', timeout=25)
        script = Path(workspace) / 'authorization.js'
        script.write_text(browser_script(case, url, assets_sha256=assets_hash), encoding='utf-8')
        stage = cli.stage = 'browser_callback'
        observed = cli.command('run-code', '--filename='+str(script), timeout=45)
    except VerificationError as failure:
        error = failure
    except KeyboardInterrupt:
        error = VerificationError('cancelled')
    except Exception:
        error = VerificationError(stage)
    finally:
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
    if not cleaned or not stopped:
        raise VerificationError('cleanup')
    if error:
        raise error
    result = check_evidence(observed, load_fresh_report(path, previous, started_ns), case,
                            run_id=run_id, expected_pid=process.pid, index_sha256=index_hash,
                            frontend_assets_sha256=assets_hash)
    require_frontend_fresh(PROJECT / 'frontend/dist', index_hash, assets_hash)
    result['assertions']['own_browser_and_helper_cleaned'] = True
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--cli-path')
    parser.add_argument('--timeout', type=int, default=180)
    options = parser.parse_args()
    started = time.monotonic()
    report = {'kind':'modern_authorization_fake_browser_verification','version':1,'verified':False,
              'real_account_calls':0,'scenarios':[],'failed_stage':'started',
              'tested_versions':{'python':platform.python_version()},'assertions':{}}
    destination = PROJECT / 'artifacts/modern-authorization-verification.json'
    try:
        write_report(destination, report)
        if not 90 <= options.timeout <= 300:
            raise VerificationError('prerequisites')
        node = shutil.which('node')
        if not node:
            raise VerificationError('node_location')
        entry, versions = locate_cli(options.cli_path)
        _, index_hash, assets_hash = frontend_snapshot(PROJECT / 'frontend/dist')
        report['tested_versions'].update(versions, browser='msedge', frontend_index_sha256=index_hash,
                                         frontend_assets_sha256=assets_hash)
        pids = set()
        for case in ASSERTIONS:
            with tempfile.TemporaryDirectory(prefix='organizer-restart-authorization-') as folder:
                workspace, token, run_id = Path(folder), secrets.token_hex(32), secrets.token_hex(16)
                mark_workspace(workspace, token, run_id)
                try:
                    result = run_case(case,node,entry,workspace,token,run_id,index_hash,assets_hash,started+options.timeout)
                except VerificationError as error:
                    raise VerificationError(case+':'+error.stage) from None
                if result['process_id'] in pids:
                    raise VerificationError('process_identity')
                pids.add(result['process_id'])
                report['scenarios'].append(result)
        report.update(verified=True, failed_stage=None)
        report['assertions'].update(all_owned_helpers_and_browsers_cleaned=True,temporary_workspaces_cleaned=True,
                                    frontend_assets_fresh=True)
    except VerificationError as error:
        report['failed_stage'] = error.stage
    except KeyboardInterrupt:
        report['failed_stage'] = 'cancelled'
    except Exception:
        report['failed_stage'] = 'verification'
    report['elapsed_seconds'] = round(time.monotonic()-started,3)
    try:
        write_report(destination,report)
    except (OSError,ValueError):
        report.update(verified=False,failed_stage='report_save')
    print(json.dumps({'verified':report['verified'],'scenario_count':len(report['scenarios']),
                      'failed_stage':report['failed_stage'],'real_account_calls':0}))
    return 0 if report['verified'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
