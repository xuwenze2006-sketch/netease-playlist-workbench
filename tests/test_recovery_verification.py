import json
import shutil
import subprocess
import tempfile
import time
import unittest
from unittest.mock import Mock, patch

from scripts import verify_recovery as recovery
from scripts.verify_web import VerificationError


class RecoveryEvidenceTests(unittest.TestCase):
    def evidence(self, case):
        connection, failure, writes = recovery.CASES[case]
        observed = {'kind': 'modern_recovery_browser_observation', 'case': case,
                    'verified': True, 'assertions': {key: True for key in recovery.ASSERTIONS[case]},
                    'viewport': {'width': 390, 'height': 844}}
        fixture = {'kind': 'modern_frontend_fake_account_report', 'real_account_calls': 0,
                   'scenario': 'recovery', 'connection': connection, 'failure': failure,
                   'fake_writes': list(writes)}
        return json.dumps({'result': observed}), fixture

    def test_valid_evidence_is_reduced_to_fixed_assertions_and_fake_counts(self):
        for case in recovery.CASES:
            with self.subTest(case=case):
                raw, fixture = self.evidence(case)
                fixture['token'] = 'SECRET'
                result = recovery.check_evidence(raw, fixture, case)
                self.assertTrue(result['verified'])
                self.assertNotIn('SECRET', json.dumps(result))

    def test_missing_browser_assertion_cannot_pass(self):
        raw, fixture = self.evidence('unknown-write')
        parsed = json.loads(raw)
        parsed['result']['assertions']['write_buttons_frozen'] = False
        with self.assertRaises(VerificationError):
            recovery.check_evidence(json.dumps(parsed), fixture, 'unknown-write')

    def test_missing_cross_read_freeze_proof_cannot_pass(self):
        raw, fixture = self.evidence('unknown-write')
        parsed = json.loads(raw)
        parsed['result']['assertions'].pop('read_only_check_keeps_freeze', None)
        with self.assertRaises(VerificationError):
            recovery.check_evidence(json.dumps(parsed), fixture, 'unknown-write')

    def test_unknown_cases_and_wrong_case_evidence_are_rejected(self):
        raw, fixture = self.evidence('missing-cli')
        for case in ('unknown', 'missing-credentials'):
            with self.subTest(case=case), self.assertRaises(VerificationError):
                recovery.check_evidence(raw, fixture, case)

    def test_replayed_or_extra_fake_mutations_are_rejected(self):
        raw, fixture = self.evidence('unknown-write')
        for writes in ([], ['updateName', 'updateName'], ['create']):
            with self.subTest(writes=writes), self.assertRaises(VerificationError):
                recovery.check_evidence(raw, {**fixture, 'fake_writes': writes}, 'unknown-write')

    def test_fixture_connection_failure_and_real_call_claims_must_match(self):
        raw, fixture = self.evidence('cache')
        for field, value in (('connection', 'missing-cli'), ('failure', 'account'),
                             ('real_account_calls', True), ('real_account_calls', 1),
                             ('scenario', 'preview')):
            with self.subTest(field=field, value=value), self.assertRaises(VerificationError):
                recovery.check_evidence(raw, {**fixture, field: value}, 'cache')

    def test_failed_browser_observation_cannot_pass(self):
        raw, fixture = self.evidence('account')
        parsed = json.loads(raw)
        parsed['result']['verified'] = False
        parsed['result']['failed_stage'] = 'layout'
        with self.assertRaises(VerificationError) as error:
            recovery.check_evidence(json.dumps(parsed), fixture, 'account')
        self.assertEqual(error.exception.stage, 'layout')

    def test_callback_contains_only_fake_local_routes_and_no_automatic_replay(self):
        script = recovery.browser_script('unknown-write', 'http://127.0.0.1:12345/')
        self.assertIn('fixture/state', script)
        self.assertNotIn('music.163.com', script)
        self.assertNotIn('request.post', script)
        self.assertNotIn('request.delete', script)

    def run_cleanup_case(self, *, cleanup_error=None, stop_error=None, callback_error=None):
        process = Mock()
        cli = Mock()
        raw, fixture = self.evidence('cache')
        cli.command.side_effect = [None, callback_error or raw]
        cli.cleanup.side_effect = cleanup_error
        cli.cleanup.return_value = True
        with tempfile.TemporaryDirectory() as workspace, \
                patch.object(recovery.subprocess, 'Popen', return_value=process), \
                patch.object(recovery, '_announcement', return_value='http://127.0.0.1:12345/'), \
                patch.object(recovery, 'Cli', return_value=cli), \
                patch.object(recovery, 'stop_helper', side_effect=stop_error, return_value=True) as stop, \
                patch.object(recovery, 'load_fresh_report', return_value=fixture):
            with self.assertRaises(VerificationError):
                recovery.run_case('cache', 'fake-node', 'fake-cli', workspace, time.monotonic() + 60)
            stop.assert_called_once_with(process)
            process.stdin.close.assert_called_once()
            process.stdout.close.assert_called_once()

    def test_browser_cleanup_exception_still_stops_helper_and_closes_streams(self):
        self.run_cleanup_case(cleanup_error=RuntimeError('SECRET'))

    def test_interrupted_browser_cleanup_still_stops_helper_and_closes_streams(self):
        self.run_cleanup_case(cleanup_error=KeyboardInterrupt())

    def test_helper_stop_exception_still_closes_streams_and_cannot_pass(self):
        self.run_cleanup_case(stop_error=RuntimeError('SECRET'))

    def test_callback_exception_still_cleans_browser_and_helper(self):
        self.run_cleanup_case(callback_error=RuntimeError('SECRET'))

    @unittest.skipUnless(shutil.which('node'), 'Node is required to exercise the browser callback in memory')
    def test_opening_any_write_entry_cannot_pass_even_after_reload(self):
        script = r'''
const fs = require('fs');
const source = JSON.parse(fs.readFileSync(0, 'utf8')).script;
async function run(target, onlyReload = false, lagAfterCheck = false) {
  let started = false, reloaded = false, consent = false, checked = false, settled = false;
  const job = { id: 'one', action: 'rename', status: 'uncertain', result: { outcome_known: false, next_step: 'inspect_records' } };
  const checkJob = { id: 'two', action: 'check', status: 'completed', result: {message:'本机接入已检查'} };
  function loc(name = '') {
    const broken = () => started && (!onlyReload || reloaded) && name === target;
    return {
      waitFor: async () => { if (name === '本机接入已检查') settled = true; }, click: async () => {
        if (name === '整理现有歌单名称') started = true;
        if (name === '检查本地接入') checked = true;
      },
      check: async () => { consent = true; }, getAttribute: async () => 'a'.repeat(64),
      isEnabled: async () => !started && consent, isDisabled: async () => started && !broken(),
      count: async () => lagAfterCheck && checked && !settled ? 0 : 2,
      all: async () => [loc('resume#0'), loc('resume#1')],
      locator: () => loc(), filter: args => loc(args?.hasText || ''), getByRole: (_role, args) => loc(args?.name || ''),
    };
  }
  const page = {
    setDefaultTimeout: () => {}, setViewportSize: async () => {}, locator: () => loc(),
    getByRole: (_role, args) => loc(args?.name || ''),
    reload: async () => { reloaded = true; consent = false; }, evaluate: async () => true,
    context: () => ({ request: { get: async url => ({ status: () => 200,
      json: async () => url.endsWith('fixture/state') ? { kind: 'fake_account_state', count: 1 } : {
        job: started ? (checked ? checkJob : job) : null, update: { status: started ? 'review_required' : 'none' },
      },
    }) } }),
  };
  const result = await eval('(' + source + ')')(page);
  return { verified: result.verified, failed_stage: result.failed_stage };
}
(async () => {
  const out = [await run('none'), await run('none', false, true)];
  for (const target of ['整理现有歌单名称', '创建歌手精选', 'resume#0', 'resume#1']) out.push(await run(target));
  out.push(await run('创建歌手精选', true));
  out.push(await run('resume#1', true));
  console.log(JSON.stringify(out));
})().catch(() => process.exit(1));
'''
        result = subprocess.run([shutil.which('node'), '-e', script],
                                input=json.dumps({'script': recovery.browser_script('unknown-write', 'http://127.0.0.1:12345/')}),
                                text=True, encoding='utf-8', capture_output=True, timeout=10, shell=False)
        self.assertEqual(result.returncode, 0)
        observations = json.loads(result.stdout)
        self.assertTrue(observations[0]['verified'])
        self.assertTrue(observations[1]['verified'], 'Completed backend check must wait for the UI to settle')
        self.assertEqual(len(observations), 8)
        for index, observation in enumerate(observations[2:], 2):
            self.assertFalse(observation['verified'])
            self.assertEqual(observation['failed_stage'], 'result' if index < 6 else 'reload')


if __name__ == '__main__':
    unittest.main()
