"""Offline fake read evidence and own helper HTTP; no browser is started."""

import copy
import json
from pathlib import Path
import re
import secrets
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import Mock, patch
from urllib.request import ProxyHandler, Request, build_opener
from urllib.parse import quote

from scripts import verify_playlist_read as verification
from scripts.verify_web import Cli, VerificationError, _NO_WINDOW, _announcement, stop_helper
from scripts.verify_restart import mark_workspace


class PlaylistReadEvidenceTests(unittest.TestCase):
    def evidence(self, case):
        paused, partial = case == 'pause-retains-old', case == 'filtered-partial'
        count = 6 if partial else 3 if paused else 5
        observed = {'kind': 'modern_playlist_read_browser_observation', 'case': case, 'verified': True,
                    'viewport': {'width': 390, 'height': 844}, 'frontend_assets_sha256': 'c'*64,
                    'assertions': dict.fromkeys(verification.ASSERTIONS[case], True)}
        if paused:
            observed['short_pause'] = {
                'viewport': {'width': 820, 'height': 360},
                'button': {'x': 610, 'y': 130, 'width': 130, 'height': 44},
                'task': {'x': 248, 'y': 115, 'width': 544, 'height': 85},
                'scroller': {'x': 220, 'y': 90, 'width': 600, 'height': 190,
                             'overflow_y': 'auto', 'client_height': 190,
                             'scroll_height': 650, 'scroll_top': 80},
            }
        labels = ['user info', 'playlist get', 'playlist tracks']
        if partial:
            labels += ['playlist tracks']
        if not paused:
            labels += ['playlist get', 'user info']
        report = {'kind': 'modern_playlist_read_fake_account_report', 'version': 1, 'case': case,
                  'run_id': 'a'*32, 'process_id': 123, 'index_sha256': 'b'*64,
                  'frontend_assets_sha256': 'c'*64, 'real_account_calls': 0,
                  'fake_write_count': 0, 'forbidden_command_count': 0,
                  'startup': {'job_null': True, 'fake_read_count': 0},
                  'end': {'fake_read_count': count, 'command_labels': labels,
                          'track_offsets': [0, 500] if partial else [0],
                          'old_record_unchanged': paused, 'directory_unchanged': True,
                          'job_status': 'paused' if paused else 'partial' if partial else 'completed',
                          'pause_requested': paused,
                          'result': {'status': 'paused' if paused else 'partial' if partial else 'completed',
                                     'playlist_key': '9001', 'record_saved': not paused,
                                     'expected_count': 501 if paused or partial else 2,
                                     'count': 0 if paused else 500 if partial else 2,
                                     'missing_count': 501 if paused else 1 if partial else 0,
                                     'missing_metadata_count': 0 if paused else 2 if partial else 0}}}
        return json.dumps({'result': observed}), report

    def check(self, stdout, report, case):
        return verification.check_evidence(stdout, report, case, run_id='a'*32, expected_pid=123,
                                           index_sha256='b'*64, frontend_assets_sha256='c'*64)

    def test_three_cases_require_precise_counts_commands_and_local_persistence(self):
        for case in verification.ASSERTIONS:
            stdout, report = self.evidence(case)
            report['raw_response'] = 'PRIVATE-NO-ECHO'
            checked = self.check(stdout, report, case)
            self.assertTrue(checked['verified'])
            self.assertEqual(checked['fake_write_count'], 0)
            self.assertNotIn('PRIVATE-NO-ECHO', json.dumps(checked))

    def test_normal_reload_may_request_idle_pause_without_replaying_the_read(self):
        stdout, report = self.evidence('normal-complete')
        report['end']['pause_requested'] = True
        self.assertTrue(self.check(stdout, report, 'normal-complete')['verified'])
        report['end']['pause_requested'] = 1
        with self.assertRaises(VerificationError):
            self.check(stdout, report, 'normal-complete')
        stdout, report = self.evidence('pause-retains-old')
        report['end']['pause_requested'] = False
        with self.assertRaises(VerificationError):
            self.check(stdout, report, 'pause-retains-old')

    def test_short_pause_evidence_is_required_and_rechecks_the_scroller_clip(self):
        stdout, report = self.evidence('pause-retains-old')
        observed = json.loads(stdout)['result']
        self.assertEqual(self.check(stdout, report, 'pause-retains-old')['short_pause'],
                         observed['short_pause'])
        mutations = [
            lambda r: r.pop('short_pause'),
            lambda r: r['assertions'].pop('short_viewport_pause_reachable'),
            lambda r: r['short_pause'].update(viewport={'width': 390, 'height': 844}),
            lambda r: r['short_pause']['scroller'].update(height=40, client_height=40),
            lambda r: r['short_pause']['scroller'].update(overflow_y='hidden'),
            lambda r: r['short_pause']['scroller'].update(scroll_height=190),
            lambda r: r['short_pause']['task'].update(height=20),
            lambda r: r['short_pause']['button'].update(y=350),
            lambda r: r['short_pause']['button'].update(width=True),
            lambda r: r['short_pause']['button'].update(x=float('nan')),
        ]
        for change in mutations:
            altered = copy.deepcopy(observed)
            change(altered)
            with self.subTest(change=change), self.assertRaises(VerificationError):
                self.check(json.dumps({'result': altered}), report, 'pause-retains-old')

    def test_normal_and_partial_cannot_claim_short_pause_acceptance(self):
        paused, _ = self.evidence('pause-retains-old')
        short = json.loads(paused)['result']['short_pause']
        for case in ('normal-complete', 'filtered-partial'):
            stdout, report = self.evidence(case)
            for extra in ('flag', 'geometry'):
                observed = json.loads(stdout)['result']
                if extra == 'flag':
                    observed['assertions']['short_viewport_pause_reachable'] = True
                else:
                    observed['short_pause'] = short
                with self.subTest(case=case, extra=extra), self.assertRaises(VerificationError):
                    self.check(json.dumps({'result': observed}), report, case)

    def test_missing_flag_replayed_read_bad_identity_and_stale_assets_never_pass(self):
        case = 'normal-complete'
        stdout, report = self.evidence(case)
        for field, bad in [('run_id', 'd'*32), ('process_id', 999), ('real_account_calls', True),
                           ('fake_write_count', 1), ('frontend_assets_sha256', 'd'*64)]:
            with self.subTest(field=field), self.assertRaises(VerificationError):
                self.check(stdout, {**report, field: bad}, case)
        for change in (lambda r: r['end'].update(fake_read_count=10),
                       lambda r: r['end'].update(directory_unchanged=False),
                       lambda r: r['end']['result'].update(playlist_key='9002'),
                       lambda r: r['end']['result'].update(record_saved=False)):
            altered = copy.deepcopy(report); change(altered)
            with self.subTest(change=change), self.assertRaises(VerificationError):
                self.check(stdout, altered, case)
        observed = json.loads(stdout)
        del observed['result']['assertions']['opening_and_search_are_local']
        with self.assertRaises(VerificationError):
            self.check(json.dumps(observed), report, case)
        with self.assertRaises(VerificationError):
            self.check(stdout, report, [])

    def test_cleanup_interruption_retries_own_close_deletes_and_never_claims_pass(self):
        with tempfile.TemporaryDirectory() as folder:
            process = Mock(pid=123)
            cli = Cli('node', Path(folder)/'cli.js', 'own-playlist-read-session', folder, time.monotonic()+60)
            closes = 0
            def command(name, *args, **kwargs):
                nonlocal closes
                if name == 'close':
                    closes += 1
                    if closes == 1:
                        raise KeyboardInterrupt()
                return '{}'
            cli.command = Mock(side_effect=command)
            with patch.object(verification, 'Cli', return_value=cli), \
                    patch.object(verification.subprocess, 'Popen', return_value=process), \
                    patch.object(verification, '_announcement', return_value='http://127.0.0.1:12345/'), \
                    patch.object(verification, 'stop_helper', return_value=True) as stop, \
                    patch.object(verification, 'load_fresh_report') as load, self.assertRaises(VerificationError) as error:
                verification.run_case('normal-complete', 'node', Path(folder)/'cli.js', folder,
                    'a'*64, 'a'*32, 'b'*64, 'c'*64, time.monotonic()+60)
            self.assertEqual(error.exception.stage, 'cleanup')
            self.assertEqual([call.args[0] for call in cli.command.call_args_list],
                             ['open', 'run-code', 'close', 'close', 'delete-data'])
            stop.assert_called_once_with(process, timeout=12)
            load.assert_not_called()

    def test_callback_failure_still_stops_helper_and_deletes_owned_browser(self):
        with tempfile.TemporaryDirectory() as folder:
            process = Mock(pid=123)
            cli = Cli('node', Path(folder)/'cli.js', 'own-playlist-read-session', folder, time.monotonic()+60)
            def command(name, *args, **kwargs):
                if name == 'run-code':
                    raise VerificationError('browser_callback')
                return '{}'
            cli.command = Mock(side_effect=command)
            with patch.object(verification, 'Cli', return_value=cli), \
                    patch.object(verification.subprocess, 'Popen', return_value=process), \
                    patch.object(verification, '_announcement', return_value='http://127.0.0.1:12345/'), \
                    patch.object(verification, 'stop_helper', return_value=True) as stop, self.assertRaises(VerificationError) as error:
                verification.run_case('normal-complete', 'node', Path(folder)/'cli.js', folder,
                    'a'*64, 'a'*32, 'b'*64, 'c'*64, time.monotonic()+60)
            self.assertEqual(error.exception.stage, 'browser_callback')
            self.assertEqual([call.args[0] for call in cli.command.call_args_list],
                             ['open', 'run-code', 'close', 'delete-data'])
            stop.assert_called_once_with(process, timeout=12)

    def test_preflight_failure_overwrites_old_pass_without_starting_a_process(self):
        with tempfile.TemporaryDirectory() as folder:
            project = Path(folder)
            target = project/'artifacts/modern-playlist-read-verification.json'
            verification.write_report(target, {'verified': True, 'old_pass': True})
            with patch.object(verification, 'PROJECT', project), patch.object(sys, 'argv', ['verify_playlist_read']), \
                    patch.object(verification.shutil, 'which', return_value=None), \
                    patch.object(verification.subprocess, 'Popen') as spawn, patch('builtins.print'):
                self.assertEqual(verification.main(), 1)
                spawn.assert_not_called()
            report = json.loads(target.read_text(encoding='utf-8'))
            self.assertFalse(report['verified'])
            self.assertEqual(report['failed_stage'], 'node_location')
            self.assertNotIn('old_pass', report)

    def test_direct_script_import_does_not_depend_on_cwd_or_pythonpath(self):
        with tempfile.TemporaryDirectory() as folder:
            assets = Path(folder)/'dist'; assets.mkdir()
            (assets/'index.html').write_bytes(b'<meta content="__ORGANIZER_SESSION__">')
            script = verification.PROJECT/'scripts/verify_playlist_read.py'
            code = ('import runpy,sys; '+f'sys.path.insert(0,{str(script.parent)!r}); '
                    +f'entry=runpy.run_path({str(script)!r},run_name="verification_module"); '
                    +f'entry["frontend_snapshot"]({str(assets)!r}); print("snapshot-ready")')
            completed = subprocess.run([sys.executable, '-I', '-X', 'utf8', '-c', code], cwd=folder,
                shell=False, capture_output=True, text=True, encoding='utf-8', timeout=5, creationflags=_NO_WINDOW)
            self.assertEqual(completed.returncode, 0, 'Direct entry failed; raw output is not echoed')
            self.assertEqual(completed.stdout.strip(), 'snapshot-ready')

    @unittest.skipUnless(shutil.which('node'), 'Local Node is needed for offline callback syntax')
    def test_all_callbacks_parse_without_a_browser(self):
        with tempfile.TemporaryDirectory() as folder:
            for case in verification.ASSERTIONS:
                script = Path(folder)/(case+'.js')
                script.write_text('('+verification.browser_script(case, 'http://127.0.0.1:12345/',
                                  assets_sha256='c'*64)+');', encoding='utf-8')
                completed = subprocess.run([shutil.which('node'), '--check', str(script)], shell=False,
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=5, creationflags=_NO_WINDOW)
                self.assertEqual(completed.returncode, 0)

    @unittest.skipUnless(shutil.which('node'), 'Local Node is needed for offline callback behavior')
    def test_pause_scrolls_in_short_viewport_and_rejects_a_clipped_button_before_click(self):
        code = r'''const fs=require('fs');const source=JSON.parse(fs.readFileSync(0,'utf8')).script;
async function run(clipped) {
  let started=false, done=false, reloaded=false, scrolled=false, listener;
  let viewport={width:390,height:844};const steps=[];
  const rect=(x,y,width,height)=>({left:x,top:y,right:x+width,bottom:y+height,width,height});
  const content={clientLeft:1,clientTop:1,clientWidth:598,clientHeight:clipped?35:188,
    scrollHeight:650,scrollTop:80,getBoundingClientRect:()=>rect(220,80,600,190)};
  const task={getBoundingClientRect:()=>rect(248,100,544,100)};
  const button={closest:selector=>selector==='.details-content'?content:selector==='.details-read-task'?task:null};
  global.getComputedStyle=()=>({overflowY:'auto'});
  const request={url:()=> 'http://127.0.0.1:12345/api/actions',method:()=> 'POST',
    postDataJSON:()=>({action:'read_playlist',payload:{key:'9001'}})};
  const response={status:()=>202,json:async()=>({accepted:true,job_id:'new-read'}),request:()=>request};
  const state=()=>({job:started?{id:'new-read',action:'read_playlist',playlist_key:'9001',
    status:done?'paused':'running',result:done?{status:'paused',playlist_key:'9001',record_saved:false}:null}:null});
  function loc(name,role){return {getByRole:(nextRole,args)=>loc(args.name,nextRole),getByText:text=>loc(text),filter:()=>loc(name),
    waitFor:async()=>{if(reloaded&&role==='heading'&&name!=='我的歌单')throw new Error('wrong refreshed page');},
    fill:async()=>{},getAttribute:async()=> 'a'.repeat(64),
    scrollIntoViewIfNeeded:async()=>{if(name!=='暂停任务'||viewport.width!==820||viewport.height!==360)throw new Error('wrong scroll');scrolled=true;steps.push('scroll');},
    boundingBox:async()=>{if(!scrolled)throw new Error('not scrolled');steps.push('bounds');return {x:610,y:130,width:130,height:44};},
    evaluate:async fn=>{steps.push('clip');return fn(button);},
    click:async()=>{if(name==='更新此歌单明细'){started=true;listener(request);}if(name==='暂停任务'){if(!scrolled)throw new Error('not scrolled');done=true;steps.push('pause');}}};}
  const page={on:(name,fn)=>{if(name==='request')listener=fn;},setDefaultTimeout:()=>{},
    setViewportSize:async next=>{viewport=next;global.innerWidth=next.width;global.innerHeight=next.height;steps.push(next.width+'x'+next.height);},
    getByRole:(role,args)=>loc(args.name,role),locator:()=>loc(''),evaluate:async()=>true,
    reload:async()=>{if(!done||viewport.width!==390||viewport.height!==844)throw new Error('not restored');reloaded=true;steps.push('reload');},
    waitForResponse:async()=>response,url:()=> 'http://127.0.0.1:12345/',
    context:()=>({route:async()=>{},request:{get:async url=>({status:()=>200,json:async()=>url.endsWith('api/state')?state():{
      kind:'fake_playlist_read_state',real_account_calls:0,fake_write_count:0,forbidden_command_count:0,
      directory_unchanged:true,frontend_assets_sha256:'c'.repeat(64),fake_read_count:started?3:0,
      old_record_unchanged:true,gate_entered:started,pause_requested:done}})}})};
  const result=await eval('('+source+')')(page);return {result,steps};
}
(async()=>console.log(JSON.stringify([await run(false),await run(true)])))().catch(()=>process.exit(1));'''
        completed = subprocess.run([shutil.which('node'), '-e', code], shell=False, capture_output=True,
            input=json.dumps({'script': verification.browser_script('pause-retains-old',
                             'http://127.0.0.1:12345/', assets_sha256='c'*64)}),
            text=True, encoding='utf-8', timeout=5, creationflags=_NO_WINDOW)
        self.assertEqual(completed.returncode, 0, 'Offline callback failed; raw output is not echoed')
        good, clipped = json.loads(completed.stdout)
        self.assertTrue(good['result']['verified'])
        self.assertTrue(good['result']['assertions']['short_viewport_pause_reachable'])
        self.assertEqual(good['result']['short_pause']['viewport'], {'width': 820, 'height': 360})
        self.assertEqual(good['steps'], ['390x844', '820x360', 'scroll', 'bounds', 'clip',
                                         'pause', '390x844', 'reload'])
        self.assertFalse(clipped['result']['verified'])
        self.assertEqual(clipped['result']['failed_stage'], 'pause')
        self.assertNotIn('pause', clipped['steps'])

    @unittest.skipUnless(shutil.which('node'), 'Local Node is needed for offline callback behavior')
    def test_callback_rejects_a_wrong_target_or_unsaved_success(self):
        code = r'''const fs=require('fs');const source=JSON.parse(fs.readFileSync(0,'utf8')).script;
async function run(bad) {
  let done=false, reloaded=false, listener;
  const request={url:()=> 'http://127.0.0.1:12345/api/actions',method:()=> 'POST',
    postDataJSON:()=>({action:'read_playlist',payload:{key:'9001'}})};
  const response={status:()=>202,json:async()=>({accepted:true,job_id:'new-read'}),request:()=>request};
  const result={status:'completed',playlist_key:'9001',record_saved:bad!=='unsaved',
    expected_count:2,count:2,missing_count:0,missing_metadata_count:0};
  const state=()=>({job:done?{id:'new-read',action:'read_playlist',playlist_key:bad==='wrong-key'?'9002':'9001',
    status:'completed',result}:null});
  function loc(name,role){return {getByRole:(nextRole,args)=>loc(args.name,nextRole),getByText:(text)=>loc(text),filter:()=>loc(name),
    waitFor:async()=>{if(reloaded && role==='heading' && name!=='我的歌单')throw new Error('wrong refreshed page');},fill:async()=>{},getAttribute:async()=> 'a'.repeat(64),
    click:async()=>{if(name==='更新此歌单明细'){done=true;listener(request);}}};}
  const page={on:(name,fn)=>{if(name==='request')listener=fn;},setDefaultTimeout:()=>{},setViewportSize:async()=>{},
    getByRole:(role,args)=>loc(args.name,role),locator:()=>loc(''),evaluate:async()=>true,reload:async()=>{reloaded=true;},
    waitForResponse:async()=>response,url:()=> 'http://127.0.0.1:12345/',
    context:()=>({route:async()=>{},request:{get:async url=>({status:()=>200,json:async()=>url.endsWith('api/state')?state():{
      kind:'fake_playlist_read_state',real_account_calls:0,fake_write_count:0,forbidden_command_count:0,
      directory_unchanged:true,frontend_assets_sha256:'c'.repeat(64),fake_read_count:done?5:0,old_record_unchanged:!done}})}})};
  return await eval('('+source+')')(page);
}
(async()=>console.log(JSON.stringify([await run('none'),await run('wrong-key'),await run('unsaved')])) )().catch(()=>process.exit(1));'''
        completed = subprocess.run([shutil.which('node'), '-e', code], shell=False, capture_output=True,
            input=json.dumps({'script': verification.browser_script('normal-complete',
                             'http://127.0.0.1:12345/', assets_sha256='c'*64)}),
            text=True, encoding='utf-8', timeout=5, creationflags=_NO_WINDOW)
        self.assertEqual(completed.returncode, 0)
        observations = json.loads(completed.stdout)
        self.assertTrue(observations[0]['verified'])
        self.assertFalse(observations[1]['verified'])
        self.assertEqual(observations[1]['failed_stage'], 'result')
        self.assertFalse(observations[2]['verified'])
        self.assertEqual(observations[2]['failed_stage'], 'details')


class PlaylistReadHelperTests(unittest.TestCase):
    def start(self, case):
        temp = tempfile.TemporaryDirectory(prefix='organizer-restart-playlist-read-test-')
        self.addCleanup(temp.cleanup)
        workspace = Path(temp.name)
        token, run_id = secrets.token_hex(32), secrets.token_hex(16)
        mark_workspace(workspace, token, run_id)
        assets = workspace/'dist'; assets.mkdir()
        (assets/'index.html').write_text('<meta name="organizer-session" content="__ORGANIZER_SESSION__">', encoding='utf-8')
        process = subprocess.Popen([sys.executable, '-X', 'utf8',
            str(verification.PROJECT/'scripts/serve_playlist_read_fixture.py'), '--workspace', str(workspace),
            '--owner-token', token, '--case', case, '--assets', str(assets)], cwd=workspace,
            shell=False, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            text=True, encoding='utf-8', creationflags=_NO_WINDOW)
        def cleanup():
            try:
                if process.poll() is None:
                    self.assertTrue(stop_helper(process, timeout=8))
                else:
                    self.assertEqual(process.returncode, 0)
            finally:
                for stream in (process.stdin, process.stdout):
                    if stream:
                        stream.close()
        self.addCleanup(cleanup)
        base = _announcement(process, time.monotonic()+8)
        opener = build_opener(ProxyHandler({}))
        with opener.open(base, timeout=3) as response:
            html = response.read().decode('utf-8')
        nonce = re.search(r'content="([a-f0-9]{64})"', html).group(1)
        def get(path):
            with opener.open(Request(base+quote(path, safe='/?:=&'),
                    headers={'X-Organizer-Session': nonce}), timeout=3) as response:
                return json.loads(response.read())
        def post(path, payload):
            request = Request(base+path, data=json.dumps(payload).encode('utf-8'), method='POST',
                headers={'X-Organizer-Session': nonce, 'Content-Type': 'application/json', 'Origin': base.rstrip('/')})
            with opener.open(request, timeout=3) as response:
                return json.loads(response.read())
        return workspace, process, get, post

    def wait_done(self, get):
        deadline = time.monotonic()+5
        while time.monotonic() < deadline:
            state = get('api/state')
            if state['job'] is not None and state['job']['status'] not in ('queued', 'running', 'pause_requested'):
                return state
        self.fail('Fake readonly job did not reach a bounded terminal state')

    def test_real_helper_complete_partial_and_pause_use_the_actual_service_without_writes(self):
        for case in verification.ASSERTIONS:
            with self.subTest(case=case):
                workspace, process, get, post = self.start(case)
                old = workspace/'artifacts/歌单明细/9001.json'
                before = old.read_bytes(), old.stat().st_mtime_ns
                self.assertIsNone(get('api/state')['job'])
                self.assertEqual(get('fixture/playlist-read')['fake_read_count'], 0)
                self.assertEqual(get('api/playlists/9001/tracks')['tracks'][0]['name'], '旧记录验收曲目甲')
                get('api/playlists/9001/tracks?q=旧资料歌手')
                self.assertEqual(get('fixture/playlist-read')['fake_read_count'], 0)
                self.assertTrue(post('api/actions', {'action': 'read_playlist', 'payload': {'key': '9001'}})['accepted'])
                if case == 'pause-retains-old':
                    deadline = time.monotonic()+5
                    while time.monotonic() < deadline and not get('fixture/playlist-read')['gate_entered']:
                        pass
                    self.assertTrue(get('fixture/playlist-read')['gate_entered'])
                    post('api/pause', {})
                final = self.wait_done(get)
                result, page = final['job']['result'], get('api/playlists/9001/tracks')
                self.assertEqual(result['playlist_key'], '9001')
                self.assertEqual(result['record_saved'], case != 'pause-retains-old')
                if case == 'pause-retains-old':
                    self.assertEqual(before, (old.read_bytes(), old.stat().st_mtime_ns))
                    self.assertEqual(page['tracks'][0]['name'], '旧记录验收曲目甲')
                else:
                    self.assertEqual(page['tracks'][0]['name'], '新读取验收曲目 001')
                    self.assertEqual(page['counts'], {'expected': 501 if case == 'filtered-partial' else 2,
                        'observed': 500 if case == 'filtered-partial' else 2,
                        'missing': 1 if case == 'filtered-partial' else 0,
                        'metadata_missing': 2 if case == 'filtered-partial' else 0})
                self.assertTrue(stop_helper(process, timeout=8))
                report = json.loads((workspace/'playlist-read-report.json').read_text(encoding='utf-8'))
                self.assertEqual(report['fake_write_count'], 0)
                self.assertEqual(report['end']['fake_read_count'], 6 if case == 'filtered-partial' else 3 if case == 'pause-retains-old' else 5)
                self.assertTrue(report['end']['directory_unchanged'])
                labels = ['user info', 'playlist get', 'playlist tracks']
                if case == 'filtered-partial':
                    labels.append('playlist tracks')
                if case != 'pause-retains-old':
                    labels += ['playlist get', 'user info']
                self.assertEqual(report['end']['command_labels'], labels)
                self.assertEqual(report['startup'], {'job_null': True, 'fake_read_count': 0})
                self.assertEqual(report['real_account_calls'], 0)
                self.assertEqual(report['forbidden_command_count'], 0)
                for private in ('A'*32, 'B'*32, 'E'*32, 'X-Organizer-Session', 'http://'):
                    self.assertNotIn(private, json.dumps(report))

    def test_wrong_account_and_save_failure_preserve_the_existing_record(self):
        for case in ('wrong-account', 'save-failure'):
            with self.subTest(case=case):
                workspace, process, get, post = self.start(case)
                old = workspace/'artifacts/歌单明细/9001.json'
                before = old.read_bytes(), old.stat().st_mtime_ns
                self.assertTrue(post('api/actions', {'action': 'read_playlist', 'payload': {'key': '9001'}})['accepted'])
                final = self.wait_done(get)
                self.assertEqual(final['job']['result']['record_saved'], False)
                self.assertEqual(before, (old.read_bytes(), old.stat().st_mtime_ns))
                self.assertEqual(get('api/playlists/9001/tracks')['tracks'][0]['name'], '旧记录验收曲目甲')
                self.assertTrue(stop_helper(process, timeout=8))
                report = json.loads((workspace/'playlist-read-report.json').read_text(encoding='utf-8'))
                self.assertEqual(report['end']['fake_read_count'], 1 if case == 'wrong-account' else 5)
                self.assertEqual(report['fake_write_count'], 0)
                self.assertTrue(report['end']['old_record_unchanged'])


if __name__ == '__main__':
    unittest.main()
