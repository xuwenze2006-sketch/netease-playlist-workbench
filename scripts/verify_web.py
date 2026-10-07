"""Repeatable Playwright CLI acceptance against two temporary fake accounts only.

No package/browser installation, account URL, credentials, or product launcher is
accepted. Run this script explicitly; importing it never starts a browser.
"""

import argparse
import hashlib
import json
import os
import platform
import queue
import re
import secrets
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path


PROJECT = Path(__file__).resolve().parents[1]
_CLI_VERSION = '0.1.22'
_CACHE_PACKAGE = Path('npm-cache/_npx/31e32ef8478fbf80/node_modules/@playwright/cli/package.json')
_NAMES = ('林俊杰 · 红心精选', '蔡健雅 · 红心精选', '王力宏 · 红心精选',
          '蔡依林 · 红心精选', 'Taylor Swift · 红心精选')
_COUNTS = (21, 13, 12, 11, 10)
_COMMON_ASSERTIONS = ('narrow_no_horizontal_overflow', 'create_gate_confirmed')
_ASSERTIONS = {
    'pause-resume': _COMMON_ASSERTIONS + ('narrow_pause_reachable', 'pause_preserves_progress',
        'checkpoint_blocks_new_creation', 'resume_enabled_when_paused', 'resume_completed',
        'completed_artist_disabled', 'completed_resume_disabled', 'reload_has_no_replay'),
    'close': _COMMON_ASSERTIONS + ('page_closed_during_create', 'closed_job_paused_before_shutdown'),
}
_BROWSER_STAGES = frozenset(('layout', 'consent', 'create', 'create_gate', 'pause', 'checkpoint',
                            'resume', 'completed', 'reload', 'close', 'closed_readback'))
_NO_WINDOW = subprocess.CREATE_NO_WINDOW if sys.platform == 'win32' else 0


class VerificationError(RuntimeError):
    """Keep failure stages, never raw subprocess output or browser state."""

    def __init__(self, stage):
        self.stage = stage if isinstance(stage, str) and re.fullmatch(r'[a-z_-]+(?::[a-z_-]+)?', stage) else 'verification'
        super().__init__('本地模拟浏览器验收未完成，请查看失败阶段。')


def _json(raw, stage, maximum=1024 * 1024):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError()
            result[key] = value
        return result

    def constant(value):
        raise ValueError()

    try:
        if not isinstance(raw, str) or len(raw) > maximum:
            raise ValueError()
        value = json.loads(raw, object_pairs_hook=pairs, parse_constant=constant)
        if not isinstance(value, dict):
            raise ValueError()
        return value
    except (ValueError, RecursionError, TypeError):
        raise VerificationError(stage) from None


def locate_cli(cli_path=None, *, local_app_data=None):
    """Resolve an already installed package's declared bin. Never run npm/npx."""
    try:
        if cli_path is None:
            local_app_data = local_app_data or os.environ.get('LOCALAPPDATA')
            if not local_app_data:
                raise ValueError()
            package = Path(local_app_data) / _CACHE_PACKAGE
        else:
            supplied = Path(cli_path)
            package = supplied / 'package.json' if supplied.is_dir() else supplied.parent / 'package.json'
        package = package.resolve()
        with package.open('rb') as stream:
            raw = stream.read(65537)
        metadata = _json(raw.decode('utf-8'), 'cli_location', 65536)
        declared = metadata.get('bin')
        entry = declared.get('playwright-cli') if isinstance(declared, dict) else None
        if (metadata.get('name') != '@playwright/cli' or metadata.get('version') != _CLI_VERSION
                or not isinstance(entry, str)):
            raise ValueError()
        target = (package.parent / entry).resolve()
        if not target.is_relative_to(package.parent) or not target.is_file() or target.suffix != '.js':
            raise ValueError()
        if cli_path is not None and Path(cli_path).is_file() and Path(cli_path).name != 'package.json':
            if Path(cli_path).resolve() != target:
                raise ValueError()
        dependencies = metadata.get('dependencies')
        engine = dependencies.get('playwright-core') if isinstance(dependencies, dict) else None
        versions = {'playwright_cli': _CLI_VERSION}
        if isinstance(engine, str) and re.fullmatch(r'[0-9A-Za-z.+-]{1,100}', engine):
            versions['playwright'] = engine
        return target, versions
    except (OSError, UnicodeError, ValueError, TypeError):
        raise VerificationError('cli_location') from None


def validate_helper_announcement(raw, expected_pid):
    value = _json(raw, 'helper_announcement', 4096)
    url = value.get('url')
    match = re.fullmatch(r'http://127\.0\.0\.1:([1-9][0-9]{0,4})/', url) if isinstance(url, str) else None
    if (value.get('kind') != 'fake_account_only' or type(value.get('pid')) is not int
            or value['pid'] != expected_pid or type(value.get('real_account_calls')) is not int
            or value['real_account_calls'] != 0 or match is None or int(match[1]) > 65535):
        raise VerificationError('helper_announcement')
    return url


def parse_browser_observation(stdout, scenario):
    outer = _json(stdout, 'browser_callback')
    if outer.get('isError') is True:
        raise VerificationError('browser_callback')
    value = outer.get('result')
    observed = _json(value, 'browser_callback') if isinstance(value, str) else value
    if (scenario not in _ASSERTIONS or not isinstance(observed, dict)
            or observed.get('kind') != 'modern_web_browser_observation' or observed.get('scenario') != scenario
            or observed.get('verified') is not True):
        stage = observed.get('failed_stage') if isinstance(observed, dict) else None
        raise VerificationError(stage if isinstance(stage, str) and stage in _BROWSER_STAGES else 'browser_callback')
    assertions = observed.get('assertions')
    metrics = observed.get('metrics')
    if (not isinstance(assertions, dict) or any(assertions.get(name) is not True for name in _ASSERTIONS[scenario])
            or observed.get('viewport') != {'width': 390, 'height': 844}
            or not isinstance(metrics, dict) or type(metrics.get('paused_completed_count')) is not int
            or metrics['paused_completed_count'] != 0 or type(metrics.get('completed_count')) is not int
            or metrics['completed_count'] != (5 if scenario == 'pause-resume' else 0)):
        raise VerificationError('browser_callback')
    return {'kind': 'modern_web_browser_observation', 'scenario': scenario, 'verified': True,
            'viewport': {'width': 390, 'height': 844},
            'assertions': {name: True for name in _ASSERTIONS[scenario]},
            'metrics': {'paused_completed_count': 0, 'completed_count': metrics['completed_count']}}


def check_scenario(browser, fixture, scenario):
    browser = parse_browser_observation(json.dumps({'result': browser}), scenario)
    complete = scenario == 'pause-resume'
    expected_writes = ['create', 'add'] * 5 if complete else ['create']
    if (not isinstance(fixture, dict) or fixture.get('kind') != 'modern_frontend_fake_account_report'
            or fixture.get('scenario') != scenario or type(fixture.get('real_account_calls')) is not int
            or fixture['real_account_calls'] != 0 or fixture.get('fake_writes') != expected_writes
            or type(fixture.get('fake_playlist_count')) is not int
            or fixture['fake_playlist_count'] != (8 if complete else 4)
            or type(fixture.get('fake_call_count')) is not int or not 0 <= fixture['fake_call_count'] <= 10000):
        raise VerificationError('fixture_postconditions')
    result = fixture.get('last_result')
    if (not isinstance(result, dict) or result.get('operation') != 'artists'
            or result.get('status') != ('completed' if complete else 'paused')
            or type(result.get('completed_count')) is not int or result['completed_count'] != (5 if complete else 0)
            or result.get('resumable') is not (not complete)
            or not isinstance(result.get('items'), list) or len(result['items']) != 5):
        raise VerificationError('fixture_postconditions')
    for index, item in enumerate(result['items']):
        if (not isinstance(item, dict) or item.get('name') != _NAMES[index]
                or type(item.get('count')) is not int or item['count'] != (_COUNTS[index] if complete else 0)
                or complete and item.get('status') != 'completed'):
            raise VerificationError('fixture_postconditions')
    assertions = dict(browser['assertions'])
    assertions['no_duplicate_mutations'] = True
    if not complete:
        before = fixture.get('before_shutdown')
        if (not isinstance(before, dict) or before.get('page_closing') is not True
                or before.get('pause_requested') is not True or before.get('job_status') != 'paused'):
            raise VerificationError('close_before_shutdown')
        assertions.update(page_closing_before_shutdown=True, pause_requested_before_shutdown=True,
                          single_create_no_add=True)
    return {'scenario': scenario, 'verified': True, 'viewport': browser['viewport'], 'assertions': assertions,
            'metrics': {**browser['metrics'], 'fake_create_count': expected_writes.count('create'),
                        'fake_add_count': expected_writes.count('add'),
                        'fake_playlist_count': fixture['fake_playlist_count'], 'fake_call_count': fixture['fake_call_count']}}


def file_signature(path):
    try:
        stat = Path(path).stat()
        return stat.st_mtime_ns, stat.st_size
    except OSError:
        return None


def load_fresh_report(path, previous, started_ns):
    try:
        path = Path(path)
        signature = file_signature(path)
        if path.is_symlink() or signature is None or signature == previous or signature[0] < started_ns:
            raise ValueError()
        with path.open('rb') as stream:
            raw = stream.read(1024 * 1024 + 1)
        if signature != file_signature(path):
            raise ValueError()
        return _json(raw.decode('utf-8'), 'fixture_report')
    except (OSError, UnicodeError, ValueError):
        raise VerificationError('fixture_report') from None


class Cli:
    def __init__(self, node, cli_path, session, workspace, deadline):
        self.node, self.cli_path, self.session = node, Path(cli_path), session
        self.workspace, self.deadline = Path(workspace), deadline
        self.stage = 'cli_command'

    def command(self, command, *arguments, timeout=55, cleanup=False):
        if command not in ('open', 'run-code', 'close', 'delete-data'):
            raise VerificationError(self.stage)
        remaining = timeout if cleanup else min(timeout, self.deadline - time.monotonic())
        if remaining <= 0:
            raise VerificationError(self.stage)
        environment = {key: value for key, value in os.environ.items()
                       if not key.startswith(('PLAYWRIGHT_CLI_', 'PLAYWRIGHT_MCP_'))}
        environment.update(NO_UPDATE_NOTIFIER='1', CI='1')
        try:
            result = subprocess.run([self.node, str(self.cli_path), '--session=' + self.session, '--json',
                                     command, *arguments], shell=False, cwd=self.workspace, env=environment,
                                    stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                    encoding='utf-8', errors='replace', timeout=remaining, creationflags=_NO_WINDOW)
            if result.returncode != 0 or _json(result.stdout, self.stage).get('isError') is True:
                raise VerificationError(self.stage)
            return result.stdout
        except (OSError, subprocess.SubprocessError):
            raise VerificationError(self.stage) from None

    def cleanup(self):
        successful = True
        for command in ('close', 'delete-data'):
            try:
                self.command(command, timeout=8, cleanup=True)
            except VerificationError:
                successful = False
        return successful


def stop_helper(process, *, timeout=12):
    """Gracefully stop our own fixture. Forced cleanup must never count as PASS."""
    try:
        process.stdin.write('\n')
        process.stdin.flush()
        return process.wait(timeout=timeout) == 0
    except (OSError, ValueError, subprocess.SubprocessError):
        try:
            process.terminate()
            process.wait(timeout=3)
        except (OSError, subprocess.SubprocessError):
            try:
                process.kill()
                process.wait(timeout=3)
            except (OSError, subprocess.SubprocessError):
                pass
        return False


def browser_script(scenario, url):
    if scenario not in _ASSERTIONS:
        raise VerificationError('browser_script')
    # The nonce is read only in this fake page, stays in memory, and is never returned.
    setup = r'''async (page) => {
  const scenario = __SCENARIO__;
  const base = __URL__;
  const assertions = {};
  const metrics = { paused_completed_count: 0, completed_count: 0 };
  const deadline = Date.now() + 45000;
  let stage = 'layout';
  let nonce;
  const request = page.context().request;
  const require = (condition) => { if (!condition) throw new Error('acceptance failed'); };
  const sleep = () => new Promise(resolve => setTimeout(resolve, 100));
  const readState = async () => {
    const response = await request.get(base + 'api/state', {
      headers: { 'X-Organizer-Session': nonce }, timeout: 2000
    });
    require(response.status() === 200);
    return await response.json();
  };
  const waitState = async predicate => {
    while (Date.now() < deadline) {
      const state = await readState();
      if (predicate(state)) return state;
      await sleep();
    }
    throw new Error('acceptance timeout');
  };
  const waitGate = async () => {
    while (Date.now() < deadline) {
      const response = await request.get(base + 'fixture/gate', { timeout: 2000 });
      require(response.status() === 200);
      const signal = await response.json();
      require(signal.kind === 'fake_create_gate' && typeof signal.entered === 'boolean');
      if (signal.entered) return;
      await sleep();
    }
    throw new Error('gate timeout');
  };
  try {
    await page.setViewportSize({ width: 390, height: 844 });
    page.setDefaultTimeout(6000);
    nonce = await page.locator('meta[name="organizer-session"]').getAttribute('content');
    require(typeof nonce === 'string' && /^[a-f0-9]{64}$/.test(nonce));
    await page.getByRole('checkbox', { name: '接受歌手精选可能公开', exact: true }).waitFor();
    const layout = await page.evaluate(() => ({ width: innerWidth, scroll: document.documentElement.scrollWidth }));
    require(layout.width === 390 && layout.scroll <= 391);
    assertions.narrow_no_horizontal_overflow = true;
    stage = 'consent';
    await page.getByRole('checkbox', { name: '接受歌手精选可能公开', exact: true }).check();
    stage = 'create';
    await page.getByRole('button', { name: '创建歌手精选', exact: true }).click();
    stage = 'create_gate';
    await waitGate();
    assertions.create_gate_confirmed = true;
__FLOW__
    return { kind: 'modern_web_browser_observation', scenario, verified: true,
      viewport: { width: 390, height: 844 }, assertions, metrics };
  } catch {
    return { kind: 'modern_web_browser_observation', scenario, verified: false, failed_stage: stage };
  }
}'''
    pause = r'''
    stage = 'pause';
    await page.getByRole('navigation', { name: '主导航' }).getByRole('link', { name: /我的歌单/ }).click();
    const pause = page.locator('.task-strip').getByRole('button', { name: '暂停任务', exact: true });
    await pause.waitFor();
    const box = await pause.boundingBox();
    require(box && box.x >= 0 && box.y >= 0 && box.x + box.width <= 391 && box.y + box.height <= 845);
    assertions.narrow_pause_reachable = true;
    await pause.click();
    const paused = await waitState(state => state.job?.status === 'paused');
    require(paused.job.result?.completed_count === 0 && paused.data.history?.resumable === true);
    metrics.paused_completed_count = paused.job.result.completed_count;
    assertions.pause_preserves_progress = true;
    stage = 'checkpoint';
    await page.getByRole('navigation', { name: '主导航' }).getByRole('link', { name: '概览', exact: true }).click();
    const existing = page.getByRole('button', { name: '已有歌手精选任务', exact: true });
    await existing.waitFor();
    require(await existing.isDisabled());
    assertions.checkpoint_blocks_new_creation = true;
    const resume = page.locator('section[aria-label="当前任务"]').getByRole('button', { name: '继续上次任务', exact: true });
    await resume.waitFor();
    require(await resume.isEnabled());
    assertions.resume_enabled_when_paused = true;
    stage = 'resume';
    await resume.click();
    const finished = await waitState(state => state.job?.status === 'completed');
    require(finished.job.result?.completed_count === 5 && finished.data.artists_completed === true);
    metrics.completed_count = finished.job.result.completed_count;
    assertions.resume_completed = true;
    stage = 'completed';
    const done = page.getByRole('button', { name: '歌手精选已完成', exact: true });
    await done.waitFor();
    require(await done.isDisabled());
    assertions.completed_artist_disabled = true;
    require(await resume.isDisabled());
    assertions.completed_resume_disabled = true;
    stage = 'reload';
    await page.reload();
    nonce = await page.locator('meta[name="organizer-session"]').getAttribute('content');
    await done.waitFor();
    require(await done.isDisabled() && await resume.isDisabled());
    const reloaded = await readState();
    require(reloaded.data.artists_completed === true && reloaded.data.history?.resumable === false);
    assertions.reload_has_no_replay = true;
'''
    close = r'''
    stage = 'close';
    const closed = page.waitForEvent('close', { timeout: 6000 });
    await page.close({ runBeforeUnload: true });
    await closed;
    assertions.page_closed_during_create = true;
    stage = 'closed_readback';
    const stopped = await waitState(state => state.job?.status === 'paused');
    require(stopped.job.result?.completed_count === 0 && stopped.data.history?.resumable === true);
    assertions.closed_job_paused_before_shutdown = true;
'''
    return setup.replace('__SCENARIO__', json.dumps(scenario)).replace('__URL__', json.dumps(url)).replace(
        '__FLOW__', pause if scenario == 'pause-resume' else close)


def _announcement(process, deadline):
    results = queue.Queue(maxsize=1)

    def read():
        try:
            results.put(process.stdout.readline(4097))
        except (OSError, ValueError):
            results.put('')

    threading.Thread(target=read, daemon=True).start()
    try:
        raw = results.get(timeout=max(0.01, min(12, deadline - time.monotonic())))
        return validate_helper_announcement(raw, process.pid)
    except queue.Empty:
        raise VerificationError('helper_announcement') from None


def _run_scenario(node, cli_path, scenario, workspace, deadline, browser, headed):
    report_path = PROJECT / 'artifacts' / f'现代前端-{scenario}-验收.json'
    if not report_path.resolve().is_relative_to(PROJECT):
        raise VerificationError('fixture_report')
    previous = file_signature(report_path)
    started_ns = time.time_ns()
    process, observed = None, None
    cli = Cli(node, cli_path, 'organizer-verification-' + secrets.token_hex(6) + '-' + scenario,
              workspace, deadline)
    failure, cleaned, stopped = None, False, False
    try:
        process = subprocess.Popen([sys.executable, str(PROJECT / 'scripts/serve_web_fixture.py'),
                                    '--gate', '--stdio-control', '--scenario', scenario], shell=False,
                                   cwd=PROJECT, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                   stderr=subprocess.DEVNULL, encoding='utf-8', errors='replace',
                                   env={**os.environ, 'PYTHONIOENCODING': 'utf-8'}, creationflags=_NO_WINDOW)
        url = _announcement(process, deadline)
        cli.stage = 'browser_open'
        cli.command('open', url, '--browser=' + browser, '--idle-timeout=60000',
                    *(['--headed'] if headed else []), timeout=25)
        script = Path(workspace) / (scenario + '.js')
        script.write_text(browser_script(scenario, url), encoding='utf-8')
        cli.stage = 'browser_callback'
        observed = parse_browser_observation(cli.command('run-code', '--filename=' + str(script)), scenario)
    except VerificationError as error:
        failure = error
    except Exception:
        failure = VerificationError('helper_start')
    finally:
        # run-code must have returned before stdin shutdown; readback is part of the callback.
        try:
            cleaned = cli.cleanup()
        except Exception:
            cleaned = False
        finally:
            if process is not None:
                try:
                    stopped = stop_helper(process)
                except Exception:
                    stopped = False
                finally:
                    for stream in (process.stdin, process.stdout):
                        if stream is not None:
                            stream.close()
    if failure is not None:
        raise failure
    if not cleaned or not stopped:
        raise VerificationError('cleanup')
    fixture = load_fresh_report(report_path, previous, started_ns)
    result = check_scenario(observed, fixture, scenario)
    result['assertions']['own_session_and_helper_cleaned'] = True
    return result


def write_report(path, report):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile('w', encoding='utf-8', dir=path.parent,
                                         prefix='.modern-web-verification-', suffix='.json', delete=False) as stream:
            temporary = Path(stream.name)
            json.dump(report, stream, ensure_ascii=False, indent=2)
            stream.write('\n')
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if temporary is not None and temporary.exists():
            temporary.unlink()


def run_verification(*, cli_path=None, timeout=180, browser='msedge', headed=False):
    started = time.monotonic()
    report = {'kind': 'modern_web_fake_browser_verification', 'schema_version': 1,
              'verified': False, 'status': 'failed', 'real_account_calls': 0,
              'tested_versions': {'python': platform.python_version()}, 'scenarios': [], 'failed_stage': None}
    stage = 'prerequisites'
    try:
        if type(timeout) is not int or not 60 <= timeout <= 300 or browser not in ('chrome', 'msedge'):
            raise VerificationError(stage)
        node = shutil.which('node')
        if not node:
            raise VerificationError('node_location')
        entry, versions = locate_cli(cli_path)
        report['tested_versions'].update(versions)
        version = subprocess.run([node, '--version'], shell=False, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                 encoding='utf-8', timeout=5, creationflags=_NO_WINDOW)
        if version.returncode != 0 or not re.fullmatch(r'v[0-9]+\.[0-9]+\.[0-9]+\s*', version.stdout):
            raise VerificationError('node_version')
        report['tested_versions']['node'] = version.stdout.strip()
        index = PROJECT / 'frontend/dist/index.html'
        if not index.is_file() or index.stat().st_size > 2 * 1024 * 1024:
            raise VerificationError('frontend_build')
        report['tested_versions']['frontend_index_sha256'] = hashlib.sha256(index.read_bytes()).hexdigest()
        deadline = started + timeout
        for scenario in ('pause-resume', 'close'):
            stage = scenario
            with tempfile.TemporaryDirectory(prefix='organizer-browser-acceptance-') as workspace:
                try:
                    result = _run_scenario(node, entry, scenario, workspace, deadline, browser, headed)
                except VerificationError as error:
                    raise VerificationError(scenario + ':' + error.stage) from None
                report['scenarios'].append(result)
        report.update(verified=True, status='passed')
    except VerificationError as error:
        report['failed_stage'] = error.stage
    except KeyboardInterrupt:
        report['failed_stage'] = 'cancelled'
    except Exception:
        report['failed_stage'] = stage
    report['metrics'] = {'elapsed_seconds': round(time.monotonic() - started, 3),
                         'verified_scenario_count': len(report['scenarios'])}
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--cli-path', type=Path, help='Already installed @playwright/cli 0.1.22 bin/package directory')
    parser.add_argument('--timeout', type=int, default=180, help='Verification deadline in seconds (60..300); bounded cleanup follows')
    parser.add_argument('--browser', choices=('chrome', 'msedge'), default='msedge')
    parser.add_argument('--headed', action='store_true')
    options = parser.parse_args()
    report = run_verification(cli_path=options.cli_path, timeout=options.timeout,
                              browser=options.browser, headed=options.headed)
    destination = PROJECT / 'artifacts/modern-web-verification.json'
    try:
        write_report(destination, report)
    except OSError:
        print(json.dumps({'verified': False, 'failed_stage': 'report_save', 'real_account_calls': 0}))
        return 1
    print(json.dumps({'report': str(destination), 'verified': report['verified'],
                      'failed_stage': report['failed_stage'], 'real_account_calls': 0}, ensure_ascii=False))
    return 0 if report['verified'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
