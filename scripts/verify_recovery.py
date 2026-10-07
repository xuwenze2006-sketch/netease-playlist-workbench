"""Browser recovery acceptance against temporary fake accounts, never live accounts."""

import argparse
import hashlib
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

try:
    from .verify_web import (PROJECT, Cli, VerificationError, _NO_WINDOW, _announcement,
                             _json, file_signature, load_fresh_report, locate_cli,
                             stop_helper, write_report)
except ImportError:
    from verify_web import (PROJECT, Cli, VerificationError, _NO_WINDOW, _announcement,
                            _json, file_signature, load_fresh_report, locate_cli,
                            stop_helper, write_report)


CASES = {
    'missing-cli': ('missing-cli', 'none', ()),
    'missing-credentials': ('missing-credentials', 'none', ()),
    'cache': ('ready', 'cache', ()),
    'account': ('ready', 'account', ()),
    'unknown-write': ('ready', 'unknown-write', ('updateName',)),
    'record-save': ('ready', 'record-save', ('updateName',)),
}
COMMON = ('startup_has_no_action', 'narrow_no_horizontal_overflow', 'fake_write_count_matches')
ASSERTIONS = {case: COMMON + (('setup_guidance_correct', 'online_actions_disabled') if case.startswith('missing-')
                              else ('diagnostic_visible', 'result_semantics_preserved') if case in ('cache', 'account')
                              else ('diagnostic_visible', 'result_semantics_preserved', 'write_buttons_frozen',
                                    'read_only_check_keeps_freeze', 'reload_does_not_replay'))
              for case in CASES}
STAGES = frozenset(('layout', 'setup', 'read', 'write', 'result', 'read_check', 'check_result',
                    'check_state', 'check_freeze', 'reload', 'proof'))


def check_evidence(stdout, fixture, case):
    outer = _json(stdout, 'browser_callback')
    value = outer.get('result')
    observed = _json(value, 'browser_callback') if isinstance(value, str) else value
    if (case not in CASES or outer.get('isError') is True or not isinstance(observed, dict)
            or observed.get('kind') != 'modern_recovery_browser_observation'
            or observed.get('case') != case or observed.get('verified') is not True):
        stage = observed.get('failed_stage') if isinstance(observed, dict) else None
        raise VerificationError(stage if isinstance(stage, str) and stage in STAGES else 'browser_callback')
    connection, failure, writes = CASES[case]
    assertions = observed.get('assertions')
    if (not isinstance(assertions, dict) or any(assertions.get(name) is not True for name in ASSERTIONS[case])
            or observed.get('viewport') != {'width': 390, 'height': 844}
            or not isinstance(fixture, dict) or fixture.get('kind') != 'modern_frontend_fake_account_report'
            or type(fixture.get('real_account_calls')) is not int or fixture['real_account_calls'] != 0
            or fixture.get('scenario') != 'recovery' or fixture.get('connection') != connection
            or fixture.get('failure') != failure or fixture.get('fake_writes') != list(writes)):
        raise VerificationError('fixture_report')
    return {'case': case, 'verified': True, 'fake_write_count': len(writes),
            'assertions': {name: True for name in ASSERTIONS[case]}}


def browser_script(case, url):
    if case not in CASES:
        raise VerificationError('browser_callback')
    return r'''async page => {
  const scenario = __CASE__, base = __URL__, assertions = {};
  const require = value => { if (!value) throw new Error('acceptance'); };
  let stage = 'layout';
  const deadline = Date.now() + 45000;
  let nonce;
  const request = page.context().request;
  const get = async path => {
    const response = await request.get(base + path, { headers: { 'X-Organizer-Session': nonce }, timeout: 2000 });
    require(response.status() === 200);
    return await response.json();
  };
  const waitJob = async (previousId = undefined) => {
    while (Date.now() < deadline) {
      const state = await get('api/state');
      if (state.job && state.job.id !== previousId && !['queued', 'running', 'pause_requested'].includes(state.job.status)) return state.job;
      await new Promise(resolve => setTimeout(resolve, 100));
    }
    throw new Error('timeout');
  };
  const requireWritesFrozen = async () => {
    require(await page.getByRole('button', { name: '整理现有歌单名称', exact: true }).isDisabled());
    require(await page.getByRole('button', { name: '创建歌手精选', exact: true }).isDisabled());
    const resume = page.getByRole('button', { name: '继续上次任务', exact: true });
    require(await resume.count() > 0);
    for (const button of await resume.all()) require(await button.isDisabled());
  };
  try {
    page.setDefaultTimeout(6000);
    await page.setViewportSize({ width: 390, height: 844 });
    nonce = await page.locator('meta[name="organizer-session"]').getAttribute('content');
    require(typeof nonce === 'string' && /^[a-f0-9]{64}$/.test(nonce));
    await page.getByRole('heading', { name: /让每一份喜欢/ }).waitFor();
    require((await get('api/state')).job === null);
    assertions.startup_has_no_action = true;
    const nav = page.getByRole('navigation', { name: '主导航' });
    if (scenario.startsWith('missing-')) {
      stage = 'setup';
      await page.getByRole('link', { name: '前往接入设置', exact: true }).click();
      await page.getByRole('heading', { name: '接入设置', exact: true }).waitFor();
      const steps = page.getByRole('region', { name: '接入步骤' });
      const text = await steps.innerText();
      require(text.includes(scenario === 'missing-cli' ? '下一步：安装本机 CLI' : '下一步：保存开放平台凭证'));
      if (scenario === 'missing-cli') require(text.includes('install-official-cli.ps1'));
      assertions.setup_guidance_correct = true;
      require(await page.getByRole('button', { name: '生成扫码授权', exact: true }).isDisabled());
      require(await page.getByRole('button', { name: '验证账号授权', exact: true }).isDisabled());
      require((await get('api/state')).job === null);
      assertions.online_actions_disabled = true;
    } else {
      stage = scenario === 'cache' || scenario === 'account' ? 'read' : 'write';
      if (stage === 'write') {
        await page.getByRole('checkbox', { name: '接受歌手精选可能公开', exact: true }).check();
        require(await page.getByRole('button', { name: '创建歌手精选', exact: true }).isEnabled());
      }
      await page.getByRole('button', { name: stage === 'read' ? '更新歌单清单' : '整理现有歌单名称', exact: true }).click();
      const job = await waitJob();
      stage = 'result';
      await nav.getByRole('link', { name: /任务记录/ }).click();
      const panel = page.locator('section[aria-label="当前任务"]');
      const phrase = scenario === 'cache' ? '桌面' : scenario === 'account' ? '账号不一致' : '请勿重复提交';
      await panel.locator('.task-stage').filter({ hasText: phrase }).waitFor();
      assertions.diagnostic_visible = true;
      if (scenario === 'cache' || scenario === 'account') {
        require(job.status === 'blocked');
        require(job.result.error_code === (scenario === 'cache' ? 'local_snapshot_unavailable' : 'account_mismatch'));
      } else if (scenario === 'unknown-write') {
        require(job.status === 'uncertain' && job.result.outcome_known === false && job.result.next_step === 'inspect_records');
        require(job.result.write_attempted !== false);
      } else {
        require(job.status === 'completed' && job.result.completed_count === 1);
        require(job.result.outcome_known === true && job.result.applied_to_account === true && job.result.record_saved === false);
      }
      assertions.result_semantics_preserved = true;
      if (scenario === 'unknown-write' || scenario === 'record-save') {
        await nav.getByRole('link', { name: '概览', exact: true }).click();
        const rename = page.getByRole('button', { name: '整理现有歌单名称', exact: true });
        await rename.waitFor();
        await requireWritesFrozen();
        assertions.write_buttons_frozen = true;
        stage = 'read_check';
        await nav.getByRole('link', { name: '接入设置', exact: true }).click();
        await page.getByRole('button', { name: '检查本地接入', exact: true }).click();
        const checked = await waitJob(job.id);
        stage = 'check_result';
        require(checked.action === 'check' && checked.status === 'completed');
        stage = 'check_state';
        require((await get('api/state')).update.status === 'review_required');
        stage = 'check_freeze';
        await nav.getByRole('link', { name: '概览', exact: true }).click();
        // A completed API job can precede React's next render. Require its
        // visible result before inspecting idle/resume controls.
        await panel.locator('.task-stage').filter({ hasText: checked.result.message }).waitFor();
        await requireWritesFrozen();
        assertions.read_only_check_keeps_freeze = true;
        stage = 'reload';
        await page.reload();
        nonce = await page.locator('meta[name="organizer-session"]').getAttribute('content');
        await page.getByRole('region', { name: '程序更新状态', exact: true }).filter({ hasText: '上次操作结果待核对' }).waitFor();
        await page.getByRole('checkbox', { name: '接受歌手精选可能公开', exact: true }).check();
        await requireWritesFrozen();
        require((await get('api/state')).job.id === checked.id);
        assertions.reload_does_not_replay = true;
      }
    }
    stage = 'layout';
    require(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1));
    assertions.narrow_no_horizontal_overflow = true;
    stage = 'proof';
    const proof = await get('fixture/state');
    require(proof.kind === 'fake_account_state' && proof.count === (scenario === 'unknown-write' || scenario === 'record-save' ? 1 : 0));
    assertions.fake_write_count_matches = true;
    return { kind: 'modern_recovery_browser_observation', case: scenario, verified: true, assertions,
             viewport: { width: 390, height: 844 } };
  } catch { return { kind: 'modern_recovery_browser_observation', case: scenario, verified: false, failed_stage: stage }; }
}'''.replace('__CASE__', json.dumps(case)).replace('__URL__', json.dumps(url))


def run_case(case, node, entry, workspace, deadline):
    connection, failure, _ = CASES[case]
    report_path = PROJECT / 'artifacts/现代前端-recovery-验收.json'
    previous, started_ns = file_signature(report_path), time.time_ns()
    cli = Cli(node, entry, 'organizer-recovery-' + secrets.token_hex(6), workspace, deadline)
    process = None
    observed = None
    error = None
    stage = 'helper_start'
    cleaned = stopped = False
    try:
        process = subprocess.Popen([sys.executable, '-X', 'utf8', str(PROJECT / 'scripts/serve_web_fixture.py'),
            '--stdio-control', '--scenario', 'recovery', '--connection', connection, '--failure', failure],
            cwd=PROJECT, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            encoding='utf-8', shell=False, creationflags=_NO_WINDOW)
        url = _announcement(process, deadline)
        stage = 'browser_open'
        cli.stage = 'browser_open'
        cli.command('open', url, '--browser=msedge', '--idle-timeout=60000', timeout=25)
        script = Path(workspace) / 'recovery.js'
        script.write_text(browser_script(case, url), encoding='utf-8')
        cli.stage = 'browser_callback'
        stage = 'browser_callback'
        observed = cli.command('run-code', '--filename=' + str(script))
    except VerificationError as failure_error:
        error = failure_error
    except KeyboardInterrupt:
        error = VerificationError('cancelled')
    except Exception:
        error = VerificationError(stage)
    finally:
        try:
            try:
                cleaned = cli.cleanup()
            except (Exception, KeyboardInterrupt):
                cleaned = False
        finally:
            if process is not None:
                try:
                    stopped = stop_helper(process)
                except (Exception, KeyboardInterrupt):
                    stopped = False
                finally:
                    for stream in (process.stdin, process.stdout):
                        if stream is not None:
                            try:
                                stream.close()
                            except (Exception, KeyboardInterrupt):
                                cleaned = False
    if error:
        raise error
    if not cleaned or not stopped:
        raise VerificationError('cleanup')
    result = check_evidence(observed, load_fresh_report(report_path, previous, started_ns), case)
    result['assertions']['own_session_and_helper_cleaned'] = True
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--cli-path')
    parser.add_argument('--timeout', type=int, default=180)
    args = parser.parse_args()
    started = time.monotonic()
    report = {'kind': 'modern_recovery_fake_browser_verification', 'verified': False,
              'real_account_calls': 0, 'scenarios': [], 'failed_stage': None,
              'tested_versions': {'python': platform.python_version()}}
    try:
        if not 60 <= args.timeout <= 300:
            raise VerificationError('prerequisites')
        node = shutil.which('node')
        if not node:
            raise VerificationError('node_location')
        entry, versions = locate_cli(args.cli_path)
        report['tested_versions'].update(versions)
        report['tested_versions']['frontend_index_sha256'] = hashlib.sha256((PROJECT / 'frontend/dist/index.html').read_bytes()).hexdigest()
        for case in CASES:
            with tempfile.TemporaryDirectory(prefix='organizer-recovery-verification-') as workspace:
                try:
                    report['scenarios'].append(run_case(case, node, entry, workspace, started + args.timeout))
                except VerificationError as error:
                    raise VerificationError(case + ':' + error.stage) from None
        report['verified'] = True
    except VerificationError as error:
        report['failed_stage'] = error.stage
    except KeyboardInterrupt:
        report['failed_stage'] = 'cancelled'
    except Exception:
        report['failed_stage'] = 'verification'
    report['elapsed_seconds'] = round(time.monotonic() - started, 3)
    write_report(PROJECT / 'artifacts/modern-recovery-verification.json', report)
    print(json.dumps({'verified': report['verified'], 'scenario_count': len(report['scenarios']),
                      'failed_stage': report['failed_stage'], 'real_account_calls': 0}))
    return 0 if report['verified'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
