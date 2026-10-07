"""Explicit readonly playlist acceptance against caller-owned fake accounts only."""

import argparse
import json
import math
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


_COMMON = ('startup_has_no_action', 'narrow_no_horizontal_overflow', 'opening_and_search_are_local',
           'one_explicit_read', 'matching_target_job', 'directory_unchanged', 'reload_no_replay',
           'zero_account_writes', 'served_assets_match', 'no_external_navigation')
ASSERTIONS = {
    'normal-complete': _COMMON + ('complete_counts_saved', 'new_songs_visible'),
    'filtered-partial': _COMMON + ('partial_counts_honest', 'known_artists_retained', 'new_songs_visible'),
    'pause-retains-old': _COMMON + ('first_page_gate_confirmed', 'pause_reachable',
                                 'short_viewport_pause_reachable',
                                 'pause_precedes_shutdown', 'old_record_bytes_and_time_unchanged'),
}
_STAGES = frozenset(('startup', 'layout', 'open', 'search', 'submit', 'gate', 'pause',
                     'result', 'details', 'refresh', 'proof'))


def _short_pause_evidence(value):
    if not isinstance(value, dict) or value.get('viewport') != {'width': 820, 'height': 360}:
        raise VerificationError('pause')
    rectangles = {}
    for name in ('button', 'scroller', 'task'):
        raw = value.get(name)
        if not isinstance(raw, dict):
            raise VerificationError('pause')
        rect = {key: raw.get(key) for key in ('x', 'y', 'width', 'height')}
        if (any(type(number) not in (int, float) or not math.isfinite(number)
                or not -10000 <= number <= 10000 for number in rect.values())
                or rect['width'] <= 0 or rect['height'] <= 0):
            raise VerificationError('pause')
        rectangles[name] = rect
    scroll = value['scroller']
    if (scroll.get('overflow_y') not in ('auto', 'scroll')
            or any(type(scroll.get(key)) is not int or not 0 <= scroll[key] <= 10000
                   for key in ('client_height', 'scroll_height'))
            or scroll['client_height'] != rectangles['scroller']['height']
            or scroll['scroll_height'] <= scroll['client_height']
            or type(scroll.get('scroll_top')) not in (int, float)
            or not math.isfinite(scroll['scroll_top'])
            or not 0 <= scroll['scroll_top'] <= scroll['scroll_height'] - scroll['client_height']):
        raise VerificationError('pause')
    button = rectangles['button']
    clips = [rectangles['scroller'], rectangles['task'],
             {'x': 0, 'y': 0, 'width': 820, 'height': 360}]
    left = max(rect['x'] for rect in clips)
    top = max(rect['y'] for rect in clips)
    right = min(rect['x'] + rect['width'] for rect in clips)
    bottom = min(rect['y'] + rect['height'] for rect in clips)
    if (button['x'] < left - 1 or button['y'] < top - 1
            or button['x'] + button['width'] > right + 1
            or button['y'] + button['height'] > bottom + 1):
        raise VerificationError('pause')
    rectangles['scroller'].update({key: scroll[key] for key in
        ('overflow_y', 'client_height', 'scroll_height', 'scroll_top')})
    return {'viewport': {'width': 820, 'height': 360}, **rectangles}


def check_evidence(stdout, fixture, case, *, run_id, expected_pid, index_sha256, frontend_assets_sha256):
    outer = _json(stdout, 'browser_callback')
    value = outer.get('result')
    observed = _json(value, 'browser_callback') if isinstance(value, str) else value
    if (not isinstance(case, str) or case not in ASSERTIONS or outer.get('isError') is True
            or not isinstance(observed, dict) or observed.get('kind') != 'modern_playlist_read_browser_observation'
            or observed.get('case') != case or observed.get('verified') is not True):
        stage = observed.get('failed_stage') if isinstance(observed, dict) else None
        raise VerificationError(stage if isinstance(stage, str) and stage in _STAGES else 'browser_callback')
    assertions = observed.get('assertions')
    if (not isinstance(assertions, dict) or any(assertions.get(name) is not True for name in ASSERTIONS[case])
            or observed.get('viewport') != {'width': 390, 'height': 844}
            or observed.get('frontend_assets_sha256') != frontend_assets_sha256
            or not isinstance(fixture, dict) or fixture.get('kind') != 'modern_playlist_read_fake_account_report'
            or type(fixture.get('version')) is not int or fixture['version'] != 1
            or fixture.get('case') != case or fixture.get('run_id') != run_id
            or type(fixture.get('process_id')) is not int or fixture['process_id'] != expected_pid
            or fixture.get('index_sha256') != index_sha256 or fixture.get('frontend_assets_sha256') != frontend_assets_sha256
            or any(type(fixture.get(key)) is not int or fixture[key] != 0
                   for key in ('real_account_calls', 'fake_write_count', 'forbidden_command_count'))):
        raise VerificationError('fixture_report')
    startup, end = fixture.get('startup'), fixture.get('end')
    paused, partial = case == 'pause-retains-old', case == 'filtered-partial'
    short_pause = _short_pause_evidence(observed.get('short_pause')) if paused else None
    if not paused and ('short_pause' in observed or 'short_viewport_pause_reachable' in assertions):
        raise VerificationError('pause')
    expected_reads = 3 if paused else 6 if partial else 5
    labels = ['user info', 'playlist get', 'playlist tracks']
    if partial:
        labels.append('playlist tracks')
    if not paused:
        labels += ['playlist get', 'user info']
    if (not isinstance(startup, dict) or startup.get('job_null') is not True
            or type(startup.get('fake_read_count')) is not int or startup['fake_read_count'] != 0
            or not isinstance(end, dict) or type(end.get('fake_read_count')) is not int
            or end['fake_read_count'] != expected_reads or end.get('command_labels') != labels
            or end.get('track_offsets') != ([0, 500] if partial else [0])
            or end.get('directory_unchanged') is not True
            or end.get('old_record_unchanged') is not paused
            or end.get('job_status') != ('paused' if paused else 'partial' if partial else 'completed')
            or type(end.get('pause_requested')) is not bool
            or (paused and end['pause_requested'] is not True)):
        raise VerificationError('fixture_report')
    result = end.get('result')
    if (not isinstance(result, dict) or result.get('status') != end['job_status']
            or result.get('playlist_key') != '9001' or result.get('record_saved') is not (not paused)):
        raise VerificationError('fixture_report')
    if not paused:
        counts = {'expected_count': 501 if partial else 2, 'count': 500 if partial else 2,
                  'missing_count': 1 if partial else 0, 'missing_metadata_count': 2 if partial else 0}
        if any(type(result.get(key)) is not int or result[key] != value for key, value in counts.items()):
            raise VerificationError('fixture_report')
    checked = {'case': case, 'verified': True, 'process_id': expected_pid, 'fake_write_count': 0,
            'fake_read_count': expected_reads, 'viewport': {'width': 390, 'height': 844},
            'frontend_assets_sha256': frontend_assets_sha256,
            'assertions': {name: True for name in ASSERTIONS[case]}}
    if paused:
        checked['short_pause'] = short_pause
    return checked


def browser_script(case, url, *, assets_sha256):
    if case not in ASSERTIONS:
        raise VerificationError('browser_script')
    return r'''async page => {
  const scenario = __CASE__, base = __URL__, hash = __HASH__, assertions = {};
  const deadline = Date.now()+42000, request = page.context().request;
  let stage = 'startup', nonce, actionPosts = 0, externalRequests = 0, shortPause;
  const require = value => {if (!value) throw new Error('acceptance');};
  page.on('request', req => {if (req.url() === base+'api/actions' && req.method() === 'POST') actionPosts += 1;});
  await page.context().route('**/*', async route => {
    if (route.request().url().startsWith(base)) await route.continue();
    else {externalRequests += 1; await route.abort();}
  });
  const get = async path => {
    const response = await request.get(base+path, {headers:{'X-Organizer-Session':nonce},timeout:2000});
    require(response.status() === 200); return await response.json();
  };
  const readNonce = async () => {
    nonce = await page.locator('meta[name="organizer-session"]').getAttribute('content');
    require(typeof nonce === 'string' && /^[a-f0-9]{64}$/.test(nonce));
  };
  const proof = async () => {
    const state = await get('fixture/playlist-read');
    require(state.kind === 'fake_playlist_read_state' && state.real_account_calls === 0 &&
      state.fake_write_count === 0 && state.forbidden_command_count === 0 &&
      state.directory_unchanged === true && state.frontend_assets_sha256 === hash);
    return state;
  };
  const waitDone = async id => {
    while (Date.now() < deadline) {
      const state = await get('api/state');
      if (state.job?.id === id && !['queued','running','pause_requested'].includes(state.job.status)) return state;
      await new Promise(resolve => setTimeout(resolve,100));
    } throw new Error('timeout');
  };
  const waitGate = async () => {
    while (Date.now() < deadline) {
      if ((await proof()).gate_entered === true) return;
      await new Promise(resolve => setTimeout(resolve,100));
    } throw new Error('gate timeout');
  };
  const openDrawer = async () => {
    await page.getByRole('navigation',{name:'主导航'}).getByRole('link',{name:/我的歌单/}).click();
    await page.getByRole('button',{name:'打开英语 🎵的歌曲明细',exact:true}).click();
    const drawer = page.getByRole('dialog',{name:'英语 🎵的歌曲明细',exact:true});
    await drawer.getByRole('table',{name:'已保存的歌曲',exact:true}).waitFor();
    return drawer;
  };
  try {
    page.setDefaultTimeout(6000); await page.setViewportSize({width:390,height:844}); await readNonce();
    await page.getByRole('heading',{name:/让每一份喜欢/}).waitFor();
    const initial = await get('api/state');
    require(initial.job === null && (await proof()).fake_read_count === 0 && actionPosts === 0);
    assertions.startup_has_no_action = true;
    stage = 'layout';
    require(await page.evaluate(() => innerWidth === 390 && document.documentElement.scrollWidth <= 391));
    assertions.narrow_no_horizontal_overflow = true;
    stage = 'open';
    let drawer = await openDrawer();
    await drawer.getByRole('table',{name:'已保存的歌曲',exact:true}).getByText('旧记录验收曲目甲',{exact:true}).waitFor();
    stage = 'search';
    const search = drawer.getByRole('textbox',{name:'搜索曲名或歌手',exact:true});
    await search.fill('旧资料歌手');
    await drawer.getByText('匹配 2 首已保存歌曲',{exact:true}).waitFor();
    await search.fill(''); await drawer.getByText('共 2 首已保存歌曲',{exact:true}).waitFor();
    require((await proof()).fake_read_count === 0 && actionPosts === 0);
    assertions.opening_and_search_are_local = true;
    stage = 'submit';
    const responsePromise = page.waitForResponse(response => response.url() === base+'api/actions' && response.request().method() === 'POST');
    await drawer.getByRole('button',{name:'更新此歌单明细',exact:true}).click();
    const response = await responsePromise, accepted = await response.json();
    const submitted = response.request().postDataJSON();
    require(response.status() === 202 && accepted.accepted === true && typeof accepted.job_id === 'string' &&
      submitted.action === 'read_playlist' && JSON.stringify(submitted.payload) === '{"key":"9001"}' && actionPosts === 1);
    assertions.one_explicit_read = true;
    if (scenario === 'pause-retains-old') {
      stage = 'gate'; await waitGate(); assertions.first_page_gate_confirmed = true;
      stage = 'pause';
      await page.setViewportSize({width:820,height:360});
      const pause = drawer.getByRole('region',{name:'歌单明细读取任务',exact:true}).getByRole('button',{name:'暂停任务',exact:true});
      await pause.waitFor(); await pause.scrollIntoViewIfNeeded();
      const box = await pause.boundingBox();
      const geometry = await pause.evaluate(button => {
        const content = button.closest('.details-content'), task = button.closest('.details-read-task');
        if (!content || !task) return null;
        const frame = content.getBoundingClientRect(), region = task.getBoundingClientRect();
        return {viewport:{width:innerWidth,height:innerHeight},
          scroller:{x:frame.left+content.clientLeft,y:frame.top+content.clientTop,
            width:content.clientWidth,height:content.clientHeight,
            overflow_y:getComputedStyle(content).overflowY,client_height:content.clientHeight,
            scroll_height:content.scrollHeight,scroll_top:content.scrollTop},
          task:{x:region.left,y:region.top,width:region.width,height:region.height}};
      });
      require(box && geometry && geometry.viewport.width === 820 && geometry.viewport.height === 360);
      const scroll = geometry.scroller;
      require(['auto','scroll'].includes(scroll.overflow_y) && scroll.client_height > 0 &&
        scroll.scroll_height > scroll.client_height && scroll.scroll_top >= 0);
      const clips = [scroll,geometry.task,{x:0,y:0,width:820,height:360}];
      const left = Math.max(...clips.map(rect => rect.x)), top = Math.max(...clips.map(rect => rect.y));
      const right = Math.min(...clips.map(rect => rect.x+rect.width)), bottom = Math.min(...clips.map(rect => rect.y+rect.height));
      require(box.width > 0 && box.height > 0 && box.x >= left-1 && box.y >= top-1 &&
        box.x+box.width <= right+1 && box.y+box.height <= bottom+1);
      shortPause = {viewport:geometry.viewport,button:box,scroller:scroll,task:geometry.task};
      assertions.pause_reachable = true; assertions.short_viewport_pause_reachable = true;
      await pause.click();
    }
    stage = 'result';
    const final = await waitDone(accepted.job_id), result = final.job.result;
    require(final.job.action === 'read_playlist' && final.job.playlist_key === '9001' && result.playlist_key === '9001');
    assertions.matching_target_job = true;
    stage = 'details';
    if (scenario === 'pause-retains-old') {
      require(final.job.status === 'paused' && result.record_saved === false && (await proof()).pause_requested === true);
      await drawer.getByText('已暂停，本地明细保留原来的记录。',{exact:true}).waitFor();
      await drawer.getByRole('table',{name:'已保存的歌曲',exact:true}).getByText('旧记录验收曲目甲',{exact:true}).waitFor();
      require((await proof()).old_record_unchanged === true && (await proof()).fake_read_count === 3);
      assertions.pause_precedes_shutdown = true; assertions.old_record_bytes_and_time_unchanged = true;
      await page.setViewportSize({width:390,height:844});
    } else {
      const partial = scenario === 'filtered-partial';
      require(final.job.status === (partial ? 'partial':'completed') && result.record_saved === true &&
        result.expected_count === (partial ? 501:2) && result.count === (partial ? 500:2) &&
        result.missing_count === (partial ? 1:0) && result.missing_metadata_count === (partial ? 2:0));
      const table = drawer.getByRole('table',{name:'已保存的歌曲',exact:true});
      await table.getByText('新读取验收曲目 001',{exact:true}).waitFor();
      await drawer.getByText(partial ? '已保存 500 / 501 首':'已保存 2 / 2 首',{exact:true}).waitFor();
      require((await proof()).old_record_unchanged === false);
      assertions.new_songs_visible = true;
      if (partial) {
        await drawer.getByText('缺失 1 首',{exact:true}).waitFor();
        await drawer.getByText('2 首资料不完整',{exact:true}).waitFor();
        const first = table.getByRole('row').filter({has:page.getByText('新读取验收曲目 001',{exact:true})});
        await first.getByText('验收歌手',{exact:true}).waitFor();
        const second = table.getByRole('row').filter({has:page.getByText('新读取验收曲目 002',{exact:true})});
        await second.getByText('歌手资料暂缺',{exact:true}).waitFor();
        assertions.partial_counts_honest = true; assertions.known_artists_retained = true;
      } else assertions.complete_counts_saved = true;
    }
    stage = 'refresh';
    require(await page.evaluate(() => innerWidth === 390 && innerHeight === 844 && document.documentElement.scrollWidth <= 391));
    const readCount = (await proof()).fake_read_count;
    await page.reload(); await readNonce(); await page.getByRole('heading',{name:'我的歌单',exact:true}).waitFor();
    require((await get('api/state')).job.id === accepted.job_id);
    drawer = await openDrawer();
    await drawer.getByRole('table',{name:'已保存的歌曲',exact:true})
      .getByText(scenario === 'pause-retains-old' ? '旧记录验收曲目甲':'新读取验收曲目 001',{exact:true}).waitFor();
    require(actionPosts === 1 && (await proof()).fake_read_count === readCount);
    assertions.reload_no_replay = true;
    stage = 'proof';
    const last = await proof();
    require(last.fake_read_count === (scenario === 'filtered-partial' ? 6:scenario === 'pause-retains-old' ? 3:5));
    require(externalRequests === 0 && page.url().startsWith(base));
    Object.assign(assertions,{directory_unchanged:true,zero_account_writes:true,served_assets_match:true,no_external_navigation:true});
    return {kind:'modern_playlist_read_browser_observation',case:scenario,verified:true,
            viewport:{width:390,height:844},frontend_assets_sha256:hash,assertions,
            ...(shortPause ? {short_pause:shortPause}: {})};
  } catch {return {kind:'modern_playlist_read_browser_observation',case:scenario,verified:false,failed_stage:stage};}
}'''.replace('__CASE__', json.dumps(case)).replace('__URL__', json.dumps(url)).replace('__HASH__', json.dumps(assets_sha256))


def run_case(case, node, entry, workspace, token, run_id, index_hash, assets_hash, deadline):
    path = Path(workspace)/'playlist-read-report.json'
    previous, started_ns = file_signature(path), time.time_ns()
    cli = Cli(node, entry, 'organizer-playlist-read-'+secrets.token_hex(6), workspace, deadline)
    process = None
    observed = error = None
    browser_attempted, cleaned, stopped = False, True, True
    stage = 'helper_start'
    try:
        process = subprocess.Popen([sys.executable, '-X', 'utf8', str(PROJECT/'scripts/serve_playlist_read_fixture.py'),
            '--workspace', str(workspace), '--owner-token', token, '--case', case], shell=False, cwd=workspace,
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            text=True, encoding='utf-8', creationflags=_NO_WINDOW)
        url = _announcement(process, deadline)
        stage = cli.stage = 'browser_open'; browser_attempted = True
        cli.command('open', url, '--browser=msedge', '--idle-timeout=60000', timeout=25)
        script = Path(workspace)/'playlist-read.js'
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
            if browser_attempted:
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
        run_id=run_id, expected_pid=process.pid, index_sha256=index_hash, frontend_assets_sha256=assets_hash)
    require_frontend_fresh(PROJECT/'frontend/dist', index_hash, assets_hash)
    result['assertions']['own_browser_and_helper_cleaned'] = True
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--cli-path')
    parser.add_argument('--timeout', type=int, default=180)
    options = parser.parse_args()
    started = time.monotonic()
    report = {'kind':'modern_playlist_read_fake_browser_verification','version':1,'verified':False,
              'real_account_calls':0,'scenarios':[],'failed_stage':'started',
              'tested_versions':{'python':platform.python_version()},'assertions':{}}
    destination = PROJECT/'artifacts/modern-playlist-read-verification.json'
    try:
        write_report(destination, report)
        if not 90 <= options.timeout <= 300:
            raise VerificationError('prerequisites')
        node = shutil.which('node')
        if not node:
            raise VerificationError('node_location')
        entry, versions = locate_cli(options.cli_path)
        _, index_hash, assets_hash = frontend_snapshot(PROJECT/'frontend/dist')
        report['tested_versions'].update(versions, browser='msedge', frontend_index_sha256=index_hash,
                                         frontend_assets_sha256=assets_hash)
        pids = set()
        for case in ASSERTIONS:
            with tempfile.TemporaryDirectory(prefix='organizer-restart-playlist-read-') as folder:
                workspace, token, run_id = Path(folder), secrets.token_hex(32), secrets.token_hex(16)
                mark_workspace(workspace, token, run_id)
                try:
                    result = run_case(case, node, entry, workspace, token, run_id,
                                      index_hash, assets_hash, started+options.timeout)
                except VerificationError as error:
                    raise VerificationError(case+':'+error.stage) from None
                if result['process_id'] in pids:
                    raise VerificationError('process_identity')
                pids.add(result['process_id']); report['scenarios'].append(result)
        report.update(verified=True, failed_stage=None)
        report['assertions'].update(all_owned_helpers_and_browsers_cleaned=True,
            temporary_workspaces_cleaned=True, frontend_assets_fresh=True)
    except VerificationError as error:
        report['failed_stage'] = error.stage
    except KeyboardInterrupt:
        report['failed_stage'] = 'cancelled'
    except Exception:
        report['failed_stage'] = 'verification'
    report['elapsed_seconds'] = round(time.monotonic()-started, 3)
    try:
        write_report(destination, report)
    except (OSError, ValueError):
        report.update(verified=False, failed_stage='report_save')
    print(json.dumps({'verified':report['verified'],'scenario_count':len(report['scenarios']),
                      'failed_stage':report['failed_stage'],'real_account_calls':0}))
    return 0 if report['verified'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
