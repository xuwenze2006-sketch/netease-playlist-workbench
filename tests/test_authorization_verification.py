"""Offline evidence validation and real fake-helper HTTP; never start a browser."""

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
from urllib.error import HTTPError
from urllib.request import ProxyHandler, Request, build_opener

from scripts import verify_authorization as verification
from scripts.verify_web import Cli, VerificationError, _NO_WINDOW, _announcement, stop_helper
from scripts.verify_restart import mark_workspace


PENDING = {'status': 'review_required', 'operation': 'renames', 'can_reconcile': True}
CLEAR = {'status': 'clear', 'operation': 'unknown', 'can_reconcile': False}


class AuthorizationEvidenceTests(unittest.TestCase):
    def evidence(self, case):
        healthy = case == 'renew-and-reconcile'
        wrong = case == 'wrong-account'
        observed = {'kind': 'modern_authorization_browser_observation', 'case': case, 'verified': True,
                    'viewport': {'width': 390, 'height': 844}, 'frontend_assets_sha256': 'c'*64,
                    'assertions': dict.fromkeys(verification.ASSERTIONS[case], True)}
        report = {'kind': 'modern_authorization_fake_account_report', 'version': 1, 'case': case,
                  'run_id': 'a'*32, 'process_id': 123, 'index_sha256': 'b'*64,
                  'frontend_assets_sha256': 'c'*64, 'real_account_calls': 0,
                  'fake_writes': ['updateName'], 'remaining_jobs_not_executed': True,
                  'actual_rename_intent_used': True,
                  'startup': {'job_null': True, 'authorized_unknown': True, 'fake_call_count': 0,
                              'recovery': PENDING},
                  'authorization': {'expired_check_observed': True, 'background_count': 1,
                                    'scan_count': 1, 'authorized_check_count': 1, 'signature_changed': True},
                  'end': {'recovery': CLEAR if healthy else PENDING,
                          'reconcile': {'status': 'uncertain' if wrong else 'completed',
                                        'completed_count': 0 if wrong else 1, 'write_attempted': False,
                                        'outcome_known': not wrong, 'record_saved': True if healthy else False if not wrong else None}}}
        return json.dumps({'result': observed}), report

    def check(self, stdout, report, case):
        return verification.check_evidence(stdout, report, case, run_id='a'*32, expected_pid=123,
                                           index_sha256='b'*64, frontend_assets_sha256='c'*64)

    def test_all_three_cases_need_independent_authorization_and_reconcile_proofs(self):
        for case in verification.ASSERTIONS:
            stdout, report = self.evidence(case)
            report['raw_url'] = 'PRIVATE-DO-NOT-REPORT'
            checked = self.check(stdout, report, case)
            self.assertTrue(checked['verified'])
            self.assertEqual(checked['fake_write_count'], 1)
            self.assertNotIn('PRIVATE-DO-NOT-REPORT', json.dumps(checked))

    def test_missing_freeze_or_false_account_count_or_stale_asset_never_passes(self):
        case = 'renew-and-reconcile'
        stdout, report = self.evidence(case)
        for field, value in [('run_id', 'd'*32), ('frontend_assets_sha256', 'd'*64),
                             ('real_account_calls', True), ('fake_writes', []), ('process_id', 999)]:
            with self.subTest(field=field), self.assertRaises(VerificationError):
                self.check(stdout, {**report, field: value}, case)
        outer = json.loads(stdout)
        del outer['result']['assertions']['authorized_still_freezes_writes']
        with self.assertRaises(VerificationError):
            self.check(json.dumps(outer), report, case)
        with self.assertRaises(VerificationError):
            self.check(stdout, {**report, 'authorization': {**report['authorization'], 'scan_count': 2}}, case)
        with self.assertRaises(VerificationError):
            self.check(stdout, report, 'not-a-case')

    def test_preflight_failure_replaces_old_pass_without_any_process_or_browser(self):
        with tempfile.TemporaryDirectory() as folder:
            project = Path(folder)
            path = project / 'artifacts/modern-authorization-verification.json'
            verification.write_report(path, {'verified':True,'old_pass':True})
            with patch.object(verification,'PROJECT',project), patch.object(sys,'argv',['verify_authorization']), \
                    patch.object(verification.shutil,'which',return_value=None), \
                    patch.object(verification.subprocess,'Popen') as spawn, patch('builtins.print'):
                self.assertEqual(verification.main(),1)
                spawn.assert_not_called()
            report = json.loads(path.read_text(encoding='utf-8'))
            self.assertFalse(report['verified'])
            self.assertEqual(report['failed_stage'],'node_location')
            self.assertNotIn('old_pass',report)

    def test_interrupted_close_retries_and_deletes_then_rejects_pass_and_stops_helper(self):
        stdout, _ = self.evidence('renew-and-reconcile')
        with tempfile.TemporaryDirectory() as folder:
            process = Mock(pid=123)
            cli = Cli('node',Path(folder)/'cli.js','own-authorization-session',folder,time.monotonic()+60)
            closes = 0
            def command(name,*args,**kwargs):
                nonlocal closes
                if name == 'close':
                    closes += 1
                    if closes == 1:
                        raise KeyboardInterrupt()
                return stdout
            cli.command = Mock(side_effect=command)
            with patch.object(verification,'Cli',return_value=cli), \
                    patch.object(verification.subprocess,'Popen',return_value=process), \
                    patch.object(verification,'_announcement',return_value='http://127.0.0.1:12345/'), \
                    patch.object(verification,'stop_helper',return_value=True) as stop, \
                    patch.object(verification,'load_fresh_report') as load, \
                    self.assertRaises(VerificationError) as failure:
                verification.run_case('renew-and-reconcile','node',Path(folder)/'cli.js',folder,
                                      'a'*64,'a'*32,'b'*64,'c'*64,time.monotonic()+60)
            self.assertEqual(failure.exception.stage,'cleanup')
            self.assertEqual([call.args[0] for call in cli.command.call_args_list],
                             ['open','run-code','close','close','delete-data'])
            stop.assert_called_once_with(process,timeout=12)
            load.assert_not_called()

    def test_failed_callback_always_cleans_session_and_own_helper(self):
        with tempfile.TemporaryDirectory() as folder:
            process = Mock(pid=123)
            cli = Cli('node',Path(folder)/'cli.js','own-authorization-session',folder,time.monotonic()+60)
            def command(name,*args,**kwargs):
                if name == 'run-code':
                    raise VerificationError('browser_callback')
                return '{}'
            cli.command = Mock(side_effect=command)
            with patch.object(verification,'Cli',return_value=cli), \
                    patch.object(verification.subprocess,'Popen',return_value=process), \
                    patch.object(verification,'_announcement',return_value='http://127.0.0.1:12345/'), \
                    patch.object(verification,'stop_helper',return_value=True) as stop, \
                    self.assertRaises(VerificationError) as failure:
                verification.run_case('renew-and-reconcile','node',Path(folder)/'cli.js',folder,
                                      'a'*64,'a'*32,'b'*64,'c'*64,time.monotonic()+60)
            self.assertEqual(failure.exception.stage,'browser_callback')
            self.assertEqual([call.args[0] for call in cli.command.call_args_list],
                             ['open','run-code','close','delete-data'])
            stop.assert_called_once_with(process,timeout=12)

    def test_direct_entry_imports_do_not_depend_on_cwd_or_pythonpath(self):
        with tempfile.TemporaryDirectory() as folder:
            assets = Path(folder) / 'dist'
            assets.mkdir()
            (assets / 'index.html').write_bytes(b'<meta content="__ORGANIZER_SESSION__">')
            script = verification.PROJECT / 'scripts/verify_authorization.py'
            code = ('import runpy,sys; '
                    f'sys.path.insert(0,{str(script.parent)!r}); '
                    f'entry=runpy.run_path({str(script)!r},run_name="verification_module"); '
                    f'entry["frontend_snapshot"]({str(assets)!r}); print("snapshot-ready")')
            result = subprocess.run([sys.executable, '-I', '-X', 'utf8', '-c', code], cwd=folder,
                                    shell=False, capture_output=True, text=True, encoding='utf-8',
                                    timeout=5, creationflags=_NO_WINDOW)
            self.assertEqual(result.returncode, 0, 'Direct entry failed; raw output is not echoed')
            self.assertEqual(result.stdout.strip(), 'snapshot-ready')

    @unittest.skipUnless(shutil.which('node'), 'Local Node is needed for offline callback syntax checks')
    def test_callbacks_parse_without_browser_or_network(self):
        with tempfile.TemporaryDirectory() as folder:
            for case in verification.ASSERTIONS:
                script = Path(folder) / (case + '.js')
                script.write_text('(' + verification.browser_script(case, 'http://127.0.0.1:12345/',
                                  assets_sha256='c'*64) + ');', encoding='utf-8')
                result = subprocess.run([shutil.which('node'), '--check', str(script)], shell=False,
                                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                        timeout=5, creationflags=_NO_WINDOW)
                self.assertEqual(result.returncode, 0)

    @unittest.skipUnless(shutil.which('node'), 'Local Node is needed for offline callback behavior')
    def test_pending_probe_before_scan_confirmation_cannot_release_the_write_gate(self):
        code = r'''const fs = require('fs');
const source = JSON.parse(fs.readFileSync(0,'utf8')).script;
async function run(brokenGate) {
  let phase='startup', pendingReads=0;
  const recovery={status:'review_required',operation:'renames',can_reconcile:true};
  function state() {
    let action={check:'check',expired:'login_status',login:'login',pending:'authorization_probe',
      authorized:'authorization_probe',done:'reconcile_renames',reload:'reconcile_renames'}[phase];
    const authorized=['authorized','done','reload'].includes(phase);
    const result={message:'fixed',write_attempted:false};
    if(phase==='expired') result.status='authorization_required';
    if(phase==='login') result.status='authorization_pending';
    if(phase==='pending') {result.status='authorization_pending';result.authorized=false;}
    if(phase==='authorized') {result.status='authorized';result.authorized=true;}
    if(['done','reload'].includes(phase)) Object.assign(result,{status:'completed',outcome_known:true,record_saved:true});
    const current={connection:{authorized:phase==='startup'?null:authorized},recovery:['done','reload'].includes(phase)?{status:'clear'}:recovery,
      job:action?{id:action,action,status:'completed',result}:null};
    if(phase==='pending' && ++pendingReads===1) phase='authorized';
    return current;
  }
  function loc(name) {return {
    waitFor:async()=>{},check:async()=>{},filter:()=>loc(name),locator:()=>loc(name),
    getByRole:(_,args)=>loc(args.name),all:async()=>[loc('resume')],count:async()=>1,
    getAttribute:async()=> 'a'.repeat(64),evaluate:async()=>true,
    isDisabled:async()=>!(['done','reload'].includes(phase)||(brokenGate&&phase==='authorized')),
    click:async()=>{if(name==='检查本地接入')phase='check';if(name==='验证账号授权')phase='expired';
      if(name==='重新扫码后只读核对')phase='login';if(name==='只读核对上次改名')phase='done';}
  };}
  const page={setDefaultTimeout:()=>{},setViewportSize:async()=>{},getByRole:(_,args)=>loc(args.name),
    locator:()=>loc(''),evaluate:async()=>true,reload:async()=>{phase='reload';},url:()=> 'http://127.0.0.1:12345/',
    context:()=>({route:async()=>{},request:{
      get:async url=>({status:()=>200,json:async()=>url.endsWith('api/state')?state():{
        kind:'fake_authorization_state',real_account_calls:0,fake_writes:['updateName'],remaining_jobs_not_executed:true,
        frontend_assets_sha256:'c'.repeat(64),fake_call_count:0,background_count:1,scan_count:1,authorized_check_count:1}}),
      post:async()=>{phase='pending';return{status:()=>200,json:async()=>({accepted:true})};}
    }})};
  return await eval('('+source+')')(page);
}
(async()=>console.log(JSON.stringify([await run(false),await run(true)])))().catch(()=>process.exit(1));'''
        result = subprocess.run([shutil.which('node'), '-e', code], shell=False, capture_output=True,
                                input=json.dumps({'script':verification.browser_script('renew-and-reconcile',
                                'http://127.0.0.1:12345/',assets_sha256='c'*64)}),
                                text=True, encoding='utf-8', timeout=5, creationflags=_NO_WINDOW)
        self.assertEqual(result.returncode, 0)
        observations = json.loads(result.stdout)
        self.assertTrue(observations[0]['verified'], 'A stale completed pending probe must be skipped')
        self.assertFalse(observations[1]['verified'], 'Authorized alone must never release writes')
        self.assertEqual(observations[1]['failed_stage'], 'authorized')


class AuthorizationHelperTests(unittest.TestCase):
    def start(self, workspace, token, assets, case):
        process = subprocess.Popen([sys.executable, '-X', 'utf8',
            str(verification.PROJECT / 'scripts/serve_authorization_fixture.py'),
            '--workspace', str(workspace), '--owner-token', token, '--assets', str(assets), '--case', case],
            cwd=workspace, shell=False, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, text=True, encoding='utf-8', creationflags=_NO_WINDOW)
        self.addCleanup(self.cleanup, process)
        url = _announcement(process, time.monotonic()+12)
        opener = build_opener(ProxyHandler({}))
        with opener.open(url, timeout=3) as response:
            nonce = re.search(r'content="([a-f0-9]{64})"', response.read(65536).decode())[1]
        return process, opener, url, nonce

    def cleanup(self, process):
        if process.poll() is None:
            self.assertTrue(stop_helper(process, timeout=5))
        for stream in (process.stdin, process.stdout):
            if stream:
                stream.close()

    def request(self, opener, url, nonce, path='api/state', body=None):
        data = json.dumps(body).encode() if body is not None else None
        request = Request(url+path, data, {'X-Organizer-Session': nonce, 'Content-Type': 'application/json'})
        with opener.open(request, timeout=3) as response:
            return json.load(response)

    def action(self, opener, url, nonce, action):
        self.request(opener, url, nonce, 'api/actions', {'action': action, 'payload': {}})
        deadline = time.monotonic()+5
        while time.monotonic() < deadline:
            state = self.request(opener, url, nonce)
            if state['job']['action'] == action and state['job']['status'] not in ('queued', 'running', 'pause_requested'):
                return state
        self.fail('Fake action did not reach a terminal state')

    def test_real_service_login_probe_and_reconcile_all_cases_keep_single_original_write(self):
        for case in verification.ASSERTIONS:
            with self.subTest(case=case):
                temporary = tempfile.TemporaryDirectory(prefix='organizer-restart-authorization-')
                self.addCleanup(temporary.cleanup)
                folder = temporary.name
                workspace = Path(folder)
                token, run_id = secrets.token_hex(32), secrets.token_hex(16)
                mark_workspace(workspace, token, run_id)
                assets = workspace / 'dist'
                assets.mkdir()
                (assets / 'index.html').write_bytes(b'<meta name="organizer-session" content="__ORGANIZER_SESSION__">')
                process, opener, url, nonce = self.start(workspace, token, assets, case)
                initial = self.request(opener, url, nonce)
                self.assertIsNone(initial['job'])
                self.assertIsNone(initial['connection']['authorized'])
                self.assertEqual(initial['recovery'], PENDING)
                with self.assertRaises(HTTPError) as premature:
                    self.request(opener, url, nonce, 'fixture/scan', {})
                self.assertEqual(premature.exception.code, 409)
                checked = self.action(opener, url, nonce, 'login_status')
                self.assertFalse(checked['connection']['authorized'])
                logged = self.action(opener, url, nonce, 'login')
                self.assertEqual(logged['job']['result']['status'], 'authorization_pending')
                self.assertTrue(logged['job']['result']['qr_png_base64'])
                with self.assertRaises(HTTPError):
                    self.request(opener, url, '0'*64, 'fixture/scan', {})
                self.request(opener, url, nonce, 'fixture/scan', {})
                authorized = self.action(opener, url, nonce, 'authorization_probe')
                self.assertTrue(authorized['connection']['authorized'])
                self.assertEqual(authorized['recovery'], PENDING)
                final = self.action(opener, url, nonce, 'reconcile_renames')
                self.assertEqual(final['recovery'], CLEAR if case == 'renew-and-reconcile' else PENDING)
                self.assertFalse(final['job']['result']['write_attempted'])
                self.assertTrue(stop_helper(process, timeout=5))
                report = json.loads((workspace / 'authorization-report.json').read_text(encoding='utf-8'))
                self.assertEqual(report['fake_writes'], ['updateName'])
                self.assertTrue(report['remaining_jobs_not_executed'])
                self.assertEqual(report['authorization']['background_count'], 1)
                self.assertEqual(report['authorization']['authorized_check_count'], 1)
                self.assertNotIn('https://', json.dumps(report))
                self.assertNotIn('qr_png_base64', json.dumps(report))


if __name__ == '__main__':
    unittest.main()
