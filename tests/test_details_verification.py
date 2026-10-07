"""Offline evidence and owned-helper tests; never launch a real browser."""

import copy
import importlib
import importlib.util
import json
import os
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
from urllib.request import Request, urlopen

from scripts.verify_web import VerificationError, _NO_WINDOW, _announcement, load_fresh_report, stop_helper


class DetailsEvidenceTests(unittest.TestCase):
    def driver(self):
        name = 'scripts.verify_details'
        self.assertIsNotNone(importlib.util.find_spec(name), 'The details evidence driver must exist')
        return importlib.import_module(name)

    def evidence(self):
        assertions = {name: True for name in (
            'startup_job_null', 'served_assets_match', 'narrow_no_horizontal_overflow',
            'wide_no_horizontal_overflow', 'historical_partial_counts', 'first_page_order',
            'next_page_order', 'previous_page_order', 'song_search_original_position',
            'keyboard_pagination_focus',
            'artist_search_original_position', 'no_match_distinct', 'keyboard_focus_trap',
            'escape_restores_card_focus', 'reopen_clears_search', 'normal_details_not_loaded',
            'reload_has_no_job_or_account_call', 'zero_fake_writes',
            'metadata_cross_page_positions', 'metadata_search_intersection',
            'metadata_clear_keeps_filter', 'metadata_counts_and_time_unchanged',
            'reopen_resets_metadata', 'complete_metadata_filter_empty',
            'playlist_name_unicode_search', 'zero_action_posts',
            'short_portrait_content_scroll', 'short_portrait_controls_in_view',
            'short_portrait_keyboard', 'short_landscape_content_scroll',
            'short_landscape_controls_in_view', 'short_landscape_keyboard',
            'short_viewport_no_horizontal_overflow', 'short_viewport_filter_and_pagination',
        )}
        browser = {'kind': 'modern_details_browser_observation', 'verified': True,
                   'viewport': {'width': 390, 'height': 844}, 'frontend_assets_sha256': 'c'*64,
                   'short_viewports': [{'width': 390, 'height': 500}, {'width': 820, 'height': 360}],
                   'assertions': assertions, 'private': 'PRIVATE-NOT-REPORTABLE'}
        report = {'kind': 'modern_details_fake_account_report', 'version': 1, 'run_id': 'a'*32,
                  'process_id': 101, 'index_sha256': 'b'*64, 'frontend_assets_sha256': 'c'*64,
                  'real_account_calls': 0, 'fake_writes': [], 'fake_write_count': 0,
                  'startup': {'job_null': True, 'fake_call_count': 0},
                  'end': {'job_null': True, 'fake_call_count': 0},
                  'saved_counts': {'expected': 67, 'observed': 62, 'missing': 5, 'metadata_missing': 2}}
        return json.dumps({'result': browser}), report

    def check(self, stdout, report):
        return self.driver().check_evidence(stdout, report, run_id='a'*32, index_sha256='b'*64,
                                           assets_sha256='c'*64, expected_pid=101)

    def test_complete_independent_browser_and_helper_evidence_only_returns_public_summary(self):
        stdout, report = self.evidence()
        result = self.check(stdout, report)
        self.assertIs(result['verified'], True)
        self.assertEqual(result['fake_write_count'], 0)
        self.assertNotIn('PRIVATE-NOT-REPORTABLE', json.dumps(result))

    def test_false_missing_assertion_or_bool_call_count_never_passes(self):
        stdout, report = self.evidence()
        observed = json.loads(stdout)
        observed['result']['assertions'].pop('keyboard_focus_trap')
        with self.assertRaises(VerificationError):
            self.check(json.dumps(observed), report)
        changes = [('fake_write_count', 1), ('fake_write_count', False), ('fake_writes', ['updateName']),
                   ('real_account_calls', 1), ('process_id', 102), ('run_id', 'd'*32),
                   ('frontend_assets_sha256', 'd'*64)]
        for field, value in changes:
            with self.subTest(field=field), self.assertRaises(VerificationError):
                self.check(stdout, {**report, field: value})
        for field in ('startup', 'end'):
            for value in ({'job_null': False, 'fake_call_count': 0},
                          {'job_null': True, 'fake_call_count': 1}):
                with self.subTest(field=field, value=value), self.assertRaises(VerificationError):
                    self.check(stdout, {**report, field: value})
        damaged = copy.deepcopy(report)
        damaged['saved_counts']['observed'] = 67
        with self.assertRaises(VerificationError):
            self.check(stdout, damaged)

    def test_callback_failure_and_malicious_stage_cannot_become_pass_or_leak(self):
        _, report = self.evidence()
        for stage in ('PRIVATE-NOT-REPORTABLE', [], {'token': 'PRIVATE'}):
            with self.subTest(stage=stage), self.assertRaises(VerificationError) as failure:
                self.check(json.dumps({'result': {'verified': False, 'failed_stage': stage}}), report)
            self.assertEqual(failure.exception.stage, 'browser_callback')

    def test_boolean_metadata_count_cannot_masquerade_as_one_missing_record(self):
        stdout, report = self.evidence()
        report['saved_counts']['metadata_missing'] = True
        with self.assertRaises(VerificationError):
            self.check(stdout, report)

    def test_missing_or_false_search_filter_assertions_cannot_pass(self):
        stdout, report = self.evidence()
        for field in ('metadata_cross_page_positions', 'metadata_search_intersection',
                      'metadata_clear_keeps_filter', 'metadata_counts_and_time_unchanged',
                      'reopen_resets_metadata', 'complete_metadata_filter_empty',
                      'playlist_name_unicode_search', 'zero_action_posts'):
            for value in (None, False):
                observed = json.loads(stdout)
                if value is None:
                    observed['result']['assertions'].pop(field)
                else:
                    observed['result']['assertions'][field] = value
                with self.subTest(field=field, value=value), self.assertRaises(VerificationError):
                    self.check(json.dumps(observed), report)

    def test_missing_or_false_keyboard_pagination_focus_cannot_pass(self):
        stdout, report = self.evidence()
        for value in (None, False):
            observed = json.loads(stdout)
            if value is None:
                observed['result']['assertions'].pop('keyboard_pagination_focus')
            else:
                observed['result']['assertions']['keyboard_pagination_focus'] = value
            with self.subTest(value=value), self.assertRaises(VerificationError):
                self.check(json.dumps(observed), report)

    def test_missing_short_window_geometry_or_keyboard_proof_cannot_pass(self):
        stdout, report = self.evidence()
        for field in ('short_portrait_content_scroll', 'short_portrait_controls_in_view',
                      'short_portrait_keyboard', 'short_landscape_content_scroll',
                      'short_landscape_controls_in_view', 'short_landscape_keyboard',
                      'short_viewport_no_horizontal_overflow', 'short_viewport_filter_and_pagination'):
            for value in (None, False):
                observed = json.loads(stdout)
                if value is None:
                    observed['result']['assertions'].pop(field)
                else:
                    observed['result']['assertions'][field] = value
                with self.subTest(field=field, value=value), self.assertRaises(VerificationError):
                    self.check(json.dumps(observed), report)
        for windows in (None, [], [{'width': 390, 'height': 844}],
                        [{'width': 390, 'height': 500}, {'width': 820, 'height': 900}]):
            observed = json.loads(stdout)
            observed['result']['short_viewports'] = windows
            with self.subTest(windows=windows), self.assertRaises(VerificationError):
                self.check(json.dumps(observed), report)

    def test_preflight_failure_invalidates_old_pass_before_launching_anything(self):
        driver = self.driver()
        with tempfile.TemporaryDirectory() as folder:
            project = Path(folder)
            path = project / 'artifacts/modern-details-verification.json'
            driver.write_report(path, {'verified': True, 'stale': True})
            with patch.object(driver, 'PROJECT', project), patch.object(sys, 'argv', ['verify_details']), \
                    patch.object(driver.shutil, 'which', return_value=None), \
                    patch.object(driver.subprocess, 'Popen') as spawn, patch('builtins.print'):
                self.assertEqual(driver.main(), 1)
                spawn.assert_not_called()
            report = json.loads(path.read_text(encoding='utf-8'))
            self.assertIs(report['verified'], False)
            self.assertEqual(report['failed_stage'], 'node_location')
            self.assertNotIn('stale', report)

    def test_callback_and_cleanup_failure_still_stop_owned_helper_and_close_streams(self):
        driver = self.driver()
        for fail_at in ('run-code', 'close'):
            with self.subTest(fail_at=fail_at), tempfile.TemporaryDirectory() as folder:
                process = Mock(pid=101)
                cli = Mock()
                def command(name, *args, **kwargs):
                    if name == fail_at:
                        raise KeyboardInterrupt()
                    return '{}'
                cli.command.side_effect = command
                with patch.object(driver, 'Cli', return_value=cli), \
                        patch.object(driver.subprocess, 'Popen', return_value=process), \
                        patch.object(driver, '_announcement', return_value='http://127.0.0.1:12345/'), \
                        patch.object(driver, 'stop_helper', return_value=True) as stop, \
                        self.assertRaises(VerificationError) as failure:
                    driver.run_case('node', Path(folder)/'cli.js', folder, 'a'*64, 'a'*32,
                                    'b'*64, 'c'*64, time.monotonic()+60)
                self.assertEqual(failure.exception.stage, 'cancelled' if fail_at == 'run-code' else 'cleanup')
                self.assertIn('delete-data', [call.args[0] for call in cli.command.call_args_list])
                stop.assert_called_once_with(process, timeout=12)
                process.stdin.close.assert_called_once()
                process.stdout.close.assert_called_once()

    def test_unchanged_old_report_is_never_fresh(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder)/'report.json'
            path.write_text(self.evidence()[0], encoding='utf-8')
            signature = (path.stat().st_mtime_ns, path.stat().st_size)
            with self.assertRaises(VerificationError):
                load_fresh_report(path, signature, time.time_ns())

    @unittest.skipUnless(shutil.which('node'), 'A local Node executable is required')
    def test_keyboard_pagination_contract_waits_for_focus_and_rejects_body(self):
        code = r'''const fs=require('fs'), source=JSON.parse(fs.readFileSync(0,'utf8')).script;
const helper=source.match(/const waitFocus = async locator => \{[\s\S]*?\n  \};/);
const flow=source.match(/    stage = 'pagination';([\s\S]*?)    stage = 'song_search';/);
if(!helper || !flow){console.log(JSON.stringify({available:false}));process.exit(0);}
async function run(restore){
  let clock=0, current=1, pending=null, waits=0;
  const presses=[], assertions={}, nextElement={}, previousElement={};
  global.document={activeElement:{body:true}};
  const require=value=>{if(!value)throw new Error('acceptance');};
  const page={waitForTimeout:async milliseconds=>{clock+=milliseconds;waits+=1;
    if(restore && pending){document.activeElement=pending;pending=null;}}};
  function button(element,target,replacement){return {
    focus:async()=>{document.activeElement=element;},
    press:async key=>{require(key==='Enter' && document.activeElement===element);
      presses.push(target);current=target;document.activeElement={body:true};pending=replacement;},
    evaluate:async callback=>callback(element),
    click:async()=>{throw new Error('mouse pagination is not keyboard evidence');}
  };}
  const next=button(nextElement,2,previousElement), previous=button(previousElement,1,nextElement);
  const dialog={getByRole:(_role,options)=>options.name==='上一页'?previous:next};
  const range=(first,last)=>Array.from({length:last-first+1},(_,index)=>first+index);
  const waitPositions=async(_dialog,wanted)=>require(JSON.stringify(wanted)===
    JSON.stringify(current===1?range(1,50):range(51,62)));
  const waitFocus=new Function('page','deadline','require','Date',helper[0]+'\nreturn waitFocus;')(
    page,1000,require,{now:()=>clock});
  const execute=new Function('next','dialog','waitPositions','range','assertions','require','waitFocus',
    'return (async()=>{let stage;'+flow[0]+'})();');
  try{await execute(next,dialog,waitPositions,range,assertions,require,waitFocus);
    return {ok:true,presses,waits,focusNext:document.activeElement===nextElement,assertions};}
  catch{return {ok:false,presses,waits,assertions};}
}
(async()=>console.log(JSON.stringify({available:true,restored:await run(true),lost:await run(false)})))()
  .catch(()=>process.exit(1));'''
        result = subprocess.run([shutil.which('node'), '-e', code], shell=False, capture_output=True,
            input=json.dumps({'script': self.driver().browser_script('http://127.0.0.1:12345/',
                              assets_sha256='c'*64)}), text=True, encoding='utf-8',
            timeout=5, creationflags=_NO_WINDOW)
        self.assertEqual(result.returncode, 0)
        value = json.loads(result.stdout)
        self.assertTrue(value['available'])
        self.assertTrue(value['restored']['ok'])
        self.assertEqual(value['restored']['presses'], [2, 1])
        self.assertEqual(value['restored']['waits'], 2)
        self.assertTrue(value['restored']['focusNext'])
        self.assertTrue(value['restored']['assertions']['keyboard_pagination_focus'])
        self.assertFalse(value['lost']['ok'])
        self.assertEqual(value['lost']['presses'], [2])
        self.assertNotIn('keyboard_pagination_focus', value['lost']['assertions'])

    @unittest.skipUnless(shutil.which('node'), 'A local Node executable is required')
    def test_callback_parses_in_node_without_starting_browser(self):
        driver = self.driver()
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder)/'callback.js'
            path.write_text('('+driver.browser_script('http://127.0.0.1:12345/', assets_sha256='c'*64)+');', encoding='utf-8')
            result = subprocess.run([shutil.which('node'), '--check', str(path)], shell=False,
                                    capture_output=True, timeout=5, creationflags=_NO_WINDOW)
            self.assertEqual(result.returncode, 0)

    @unittest.skipUnless(shutil.which('node'), 'A local Node executable is required')
    def test_navigation_accepts_accessible_playlist_count_but_not_another_link(self):
        driver = self.driver()
        with tempfile.TemporaryDirectory() as folder:
            for name, expected in [('我的歌单', True), ('我的歌单3', True),
                                   ('我的歌单 20', True), ('他人的歌单3', False)]:
                code = r'''
const callback = __CALLBACK__, accessibleName = __NAME__;
let selected = false;
const page = {
  on: () => {},
  context: () => ({request: {}}), setViewportSize: async () => {},
  locator: () => ({getAttribute: async () => 'a'.repeat(64)}),
  getByRole: (role, options) => role === 'navigation' ? {
    getByRole: (_role, opts) => {
      const found = opts.name instanceof RegExp ? opts.name.test(accessibleName) : opts.name === accessibleName;
      if (!found) throw new Error('not found');
      return {click: async () => {selected = true;}};
    }
  } : {waitFor: async () => {throw new Error('stop after navigation');}}
};
callback(page).then(() => process.stdout.write(JSON.stringify({selected}))).catch(() => process.exit(2));
'''.replace('__CALLBACK__', '('+driver.browser_script('http://127.0.0.1:12345/', assets_sha256='c'*64)+')') \
                   .replace('__NAME__', json.dumps(name))
                path = Path(folder)/'navigation.js'
                path.write_text(code, encoding='utf-8')
                result = subprocess.run([shutil.which('node'), str(path)], shell=False, capture_output=True,
                                        text=True, encoding='utf-8', timeout=5, creationflags=_NO_WINDOW)
                self.assertEqual(result.returncode, 0)
                with self.subTest(name=name):
                    self.assertIs(json.loads(result.stdout)['selected'], expected)

    @unittest.skipUnless(shutil.which('node'), 'A local Node executable is required')
    def test_callback_rejects_an_unexpected_action_post_before_claiming_local_startup(self):
        code = r'''const fs=require('fs'), source=JSON.parse(fs.readFileSync(0,'utf8')).script;
async function run(injectPost) {
  let listener;
  function locator(role, options={}) {
    return {getByRole:(next,args)=>locator(next,args),getAttribute:async()=> 'a'.repeat(64),
      waitFor:async()=>{},click:async()=>{if(role==='link' && injectPost)listener({
        method:()=> 'POST',url:()=> 'http://127.0.0.1:12345/api/actions'});},
      getByText:()=>({waitFor:async()=>{throw new Error('stop before details');}})};
  }
  const page={on:(name,callback)=>{if(name==='request')listener=callback;},
    setViewportSize:async()=>{},locator:()=>locator('meta'),getByRole:(role,args)=>locator(role,args),
    evaluate:async()=>true,context:()=>({request:{get:async url=>({status:()=>200,json:async()=>
      url.endsWith('api/state')?{job:null}:{kind:'fake_details_account_state',real_account_calls:0,
        fake_write_count:0,fake_call_count:0,fake_writes:[],frontend_assets_sha256:'c'.repeat(64)}})}})};
  return await eval('('+source+')')(page);
}
(async()=>console.log(JSON.stringify([await run(false),await run(true)])))().catch(()=>process.exit(1));'''
        result = subprocess.run([shutil.which('node'), '-e', code], shell=False, capture_output=True,
            input=json.dumps({'script': self.driver().browser_script('http://127.0.0.1:12345/',
                              assets_sha256='c'*64)}), text=True, encoding='utf-8',
            timeout=5, creationflags=_NO_WINDOW)
        self.assertEqual(result.returncode, 0)
        control, rejected = json.loads(result.stdout)
        self.assertEqual(control['failed_stage'], 'counts')
        self.assertFalse(rejected['verified'])
        self.assertEqual(rejected['failed_stage'], 'startup')

    @unittest.skipUnless(shutil.which('node'), 'A local Node executable is required')
    def test_position_wait_takes_atomic_snapshots_when_the_table_shrinks_between_renders(self):
        code = r'''const fs=require('fs'), source=JSON.parse(fs.readFileSync(0,'utf8')).script;
const match=source.match(/const waitPositions = async \(dialog, wanted\) => \{[\s\S]*?\n  \};/);
if(!match) process.exit(2);
let render=0, snapshots=0, obsoleteLocatorReads=0, waits=0;
const page={waitForTimeout:async()=>{waits+=1;render=1;}};
const dialog={
  evaluate:async callback=>{
    snapshots+=1;
    // One DOM callback sees one complete render. The old page has twelve
    // rows; the replacement has two. Reading either snapshot is safe.
    const positions=render===0?Array.from({length:12},(_,i)=>51+i):[1,55];
    const rows=positions.map(position=>({querySelector:()=>({textContent:String(position)})}));
    const table={querySelectorAll:()=>rows};
    return callback({querySelectorAll:()=>[table]});
  },
  getByRole:()=>({count:async()=>1,getByRole:()=>({all:async()=>{
    // The old implementation enumerates old locators, then the render
    // changes before a subsequent nth row is dereferenced.
    render=1;
    return Array.from({length:12},(_,index)=>({getByRole:()=>({count:async()=>1,
      first:()=>({innerText:async()=>{obsoleteLocatorReads+=1;
        if(index>1)throw new Error('removed nth row');return String([1,55][index]);}})})}));
  }})})
};
const waitPositions=new Function('page','deadline',match[0]+'\nreturn waitPositions;')(page,Date.now()+1000);
(async()=>{try{await waitPositions(dialog,[1,55]);console.log(JSON.stringify({ok:true,snapshots,waits,obsoleteLocatorReads}));}
catch{console.log(JSON.stringify({ok:false,snapshots,waits,obsoleteLocatorReads}));}})();'''
        result = subprocess.run([shutil.which('node'), '-e', code], shell=False, capture_output=True,
            input=json.dumps({'script': self.driver().browser_script('http://127.0.0.1:12345/',
                              assets_sha256='c'*64)}), text=True, encoding='utf-8',
            timeout=5, creationflags=_NO_WINDOW)
        self.assertEqual(result.returncode, 0)
        observed = json.loads(result.stdout)
        self.assertTrue(observed['ok'])
        self.assertEqual(observed['snapshots'], 2)
        self.assertEqual(observed['waits'], 1)
        self.assertEqual(observed['obsoleteLocatorReads'], 0)

    @unittest.skipUnless(shutil.which('node'), 'A local Node executable is required')
    def test_visibility_guard_rejects_clipped_covered_and_offscreen_elements_after_scroll(self):
        code = r'''const fs=require('fs'),source=JSON.parse(fs.readFileSync(0,'utf8')).script;
const match=source.match(/const fullyVisible = async \(locator, viewport, scroll = true\) => \{[\s\S]*?\n  \};/);
if(!match){console.log(JSON.stringify({found:false}));process.exit(0);}
const assert=value=>{if(!value)throw new Error('acceptance');};
const fullyVisible=new Function('require',match[0]+'\nreturn fullyVisible;')(assert);
async function run(mode){
  let scrolls=0;
  const box=mode==='offscreen'?{x:20,y:498,width:200,height:40}:
    mode==='horizontal'?{x:350,y:100,width:100,height:40}:{x:20,y:100,width:200,height:40};
  const element={contains:()=>false,getBoundingClientRect:()=>({left:box.x,top:box.y,width:box.width,height:box.height})};
  global.document={elementFromPoint:()=>mode==='covered'?{}:element};
  global.IntersectionObserver=class{constructor(callback){this.callback=callback;}disconnect(){}
    observe(){queueMicrotask(()=>this.callback([{isIntersecting:true,intersectionRatio:mode==='clipped'?0.55:1}]));}};
  const locator={scrollIntoViewIfNeeded:async()=>{scrolls++;},boundingBox:async()=>box,evaluate:async cb=>cb(element)};
  try{await fullyVisible(locator,{width:390,height:500});return {mode,ok:true,scrolls};}
  catch{return {mode,ok:false,scrolls};}
}
(async()=>console.log(JSON.stringify({found:true,results:await Promise.all([]).then(async()=>{
  const values=[];for(const mode of ['good','offscreen','clipped','covered','horizontal'])values.push(await run(mode));return values;})})))();'''
        result = subprocess.run([shutil.which('node'), '-e', code], shell=False, capture_output=True,
            input=json.dumps({'script': self.driver().browser_script('http://127.0.0.1:12345/',
                              assets_sha256='c'*64)}), text=True, encoding='utf-8',
            timeout=5, creationflags=_NO_WINDOW)
        self.assertEqual(result.returncode, 0)
        observed = json.loads(result.stdout)
        self.assertTrue(observed['found'])
        self.assertEqual([value['ok'] for value in observed['results']], [True, False, False, False, False])
        self.assertEqual([value['scrolls'] for value in observed['results']], [1, 1, 1, 1, 1])

    def test_direct_isolated_entry_can_load_assets_and_build_callback_without_pythonpath(self):
        driver = self.driver()
        with tempfile.TemporaryDirectory() as folder:
            assets = Path(folder)/'dist'; assets.mkdir()
            (assets/'index.html').write_text('<meta content="__ORGANIZER_SESSION__">', encoding='utf-8')
            script = driver.PROJECT/'scripts/verify_details.py'
            code = ('import runpy,sys; '+f'sys.path.insert(0,{str(script.parent)!r}); '
                    +f'm=runpy.run_path({str(script)!r},run_name="details_module"); '
                    +f'm["frontend_snapshot"]({str(assets)!r}); '
                    +'m["browser_script"]("http://127.0.0.1:12345/",assets_sha256="c"*64); print("ready")')
            result = subprocess.run([sys.executable, '-I', '-X', 'utf8', '-c', code], cwd=folder,
                                    shell=False, capture_output=True, text=True, encoding='utf-8',
                                    timeout=5, creationflags=_NO_WINDOW)
            self.assertEqual(result.returncode, 0)
            self.assertEqual(result.stdout.strip(), 'ready')


class DetailsFixtureTests(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(importlib.util.find_spec('scripts.serve_details_fixture'), 'The owned fake helper must exist')
        self.fixture = importlib.import_module('scripts.serve_details_fixture')
        self.driver = importlib.import_module('scripts.verify_details')
        self.temp = tempfile.TemporaryDirectory(prefix='organizer-details-test-')
        self.addCleanup(self.temp.cleanup)
        self.workspace = Path(self.temp.name)
        self.token, self.run_id = secrets.token_hex(64//2), secrets.token_hex(16)
        self.driver.mark_workspace(self.workspace, self.token, self.run_id)
        self.assets = self.workspace/'dist'; self.assets.mkdir()
        (self.assets/'index.html').write_text('<meta name="organizer-session" content="__ORGANIZER_SESSION__">', encoding='utf-8')

    def test_owner_marker_rejects_wrong_token_missing_marker_parent_and_real_project(self):
        with self.assertRaises(VerificationError):
            self.fixture.validate_workspace(self.driver.PROJECT, self.token)
        with self.assertRaises(VerificationError):
            self.fixture.validate_workspace(self.workspace, 'e'*64)
        with patch.object(self.fixture.os, 'getppid', return_value=os.getpid()+1), self.assertRaises(VerificationError):
            self.fixture.validate_workspace(self.workspace, self.token, require_parent=True)
        (self.workspace/self.fixture.MARKER).unlink()
        with self.assertRaises(VerificationError):
            self.fixture.validate_workspace(self.workspace, self.token)

    def test_fixture_has_two_cross_page_gaps_and_an_independent_complete_normal_record(self):
        from netease_organizer.playlist_details import load_record
        server = self.fixture.saved_server(self.workspace, self.assets)
        try:
            snapshot = json.loads((self.workspace/'artifacts/在线整理快照.json').read_text(encoding='utf-8'))
            tracks = snapshot['liked']['tracks']
            self.assertEqual([index for index, track in enumerate(tracks, 1)
                              if track['metadata_available'] is False], [1, 55])
            self.assertEqual(tracks[0]['artists'], [])
            self.assertEqual(tracks[54]['artists'][0]['name'], '蔡依林')
            self.assertEqual(tracks[54]['name'], '蔡依林 · 验收曲目 9')
            self.assertEqual(snapshot['liked']['missing_metadata_track_ids'],
                             [tracks[0]['original_id'], tracks[54]['original_id']])
            normal = next(row for row in snapshot['playlists'] if row['name'] == '夜晚的纯音乐')
            saved, _ = load_record(self.workspace, normal['original_id'])
            self.assertTrue(saved['complete'])
            self.assertEqual(len(saved['playlist']['tracks']), 5)
            self.assertTrue(all(track['metadata_available'] for track in saved['playlist']['tracks']))
            self.assertEqual(server.fake_cli.calls, [])
            self.assertEqual(server.fake_cli.writes, [])
        finally:
            server.server_close()

    def test_owned_helper_serves_partial_saved_pages_and_eof_exits_without_account_calls(self):
        args = [sys.executable, '-X', 'utf8', str(self.driver.PROJECT/'scripts/serve_details_fixture.py'),
                '--workspace', str(self.workspace), '--owner-token', self.token, '--assets', str(self.assets)]
        process = subprocess.Popen(args, cwd=self.workspace, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                   stderr=subprocess.DEVNULL, text=True, encoding='utf-8', creationflags=_NO_WINDOW)
        self.addCleanup(lambda: stop_helper(process, timeout=3) if process.poll() is None else None)
        self.addCleanup(process.stdout.close)
        self.addCleanup(lambda: process.stdin.close() if not process.stdin.closed else None)
        url = _announcement(process, time.monotonic()+10)
        def get(path, nonce=None):
            request = Request(url+path, headers={'X-Organizer-Session': nonce} if nonce else {})
            with urlopen(request, timeout=3) as response:
                return response.read(100000)
        html = get('').decode('utf-8')
        nonce = re.search(r'content="([a-f0-9]{64})"', html).group(1)
        first = json.loads(get('api/playlists/9000/tracks?offset=0&limit=50', nonce))
        self.assertEqual(first['counts'], {'expected': 67, 'observed': 62, 'missing': 5, 'metadata_missing': 2})
        self.assertEqual(first['metadata_filter'], 'all')
        self.assertEqual([t['position'] for t in first['tracks']], list(range(1,51)))
        following = json.loads(get('api/playlists/9000/tracks?offset=50&limit=50', nonce))
        self.assertEqual([t['position'] for t in following['tracks']], list(range(51,63)))
        found = json.loads(get('api/playlists/9000/tracks?q=summer', nonce))
        self.assertEqual([t['position'] for t in found['tracks']], [7])
        from urllib.parse import quote
        found_artist = json.loads(get('api/playlists/9000/tracks?q='+quote('林俊杰'), nonce))
        self.assertEqual([t['position'] for t in found_artist['tracks']], list(range(1,22)))
        self.assertEqual(found_artist['tracks'][0]['artists'], [])
        self.assertIn('林俊杰', found_artist['tracks'][0]['name'])
        incomplete = json.loads(get('api/playlists/9000/tracks?metadata=incomplete', nonce))
        self.assertEqual(incomplete['metadata_filter'], 'incomplete')
        self.assertEqual([track['position'] for track in incomplete['tracks']], [1, 55])
        self.assertEqual(incomplete['counts'], first['counts'])
        self.assertEqual(incomplete['updated_at'], first['updated_at'])
        self.assertEqual(incomplete['pagination']['total'], 2)
        self.assertIsNone(incomplete['pagination']['next_offset'])
        query = quote('蔡依林 · 验收曲目 9')
        intersection = json.loads(get('api/playlists/9000/tracks?metadata=incomplete&q='+query, nonce))
        self.assertEqual([track['position'] for track in intersection['tracks']], [55])
        self.assertEqual(intersection['tracks'][0]['artists'], ['蔡依林'])
        self.assertFalse(intersection['tracks'][0]['metadata_available'])
        snapshot = json.loads((self.workspace/'artifacts/在线整理快照.json').read_text(encoding='utf-8'))
        normal = next(row for row in snapshot['playlists'] if row['name'] == '夜晚的纯音乐')
        empty = json.loads(get(f"api/playlists/{normal['original_id']}/tracks?metadata=incomplete", nonce))
        self.assertEqual(empty['status'], 'available')
        self.assertEqual(empty['counts'], {'expected': 5, 'observed': 5, 'missing': 0, 'metadata_missing': 0})
        self.assertEqual(empty['pagination']['total'], 0)
        self.assertEqual(empty['tracks'], [])
        state = json.loads(get('api/state', nonce))
        self.assertIsNone(state['job'])
        proof = json.loads(get('fixture/details', nonce))
        self.assertEqual(proof['fake_call_count'], 0)
        self.assertEqual(proof['fake_write_count'], 0)
        process.stdin.close()  # EOF, rather than the normal newline control.
        self.assertEqual(process.wait(timeout=6), 0)
        report = json.loads((self.workspace/'details-report.json').read_text(encoding='utf-8'))
        self.assertEqual(report['process_id'], process.pid)
        self.assertEqual(report['run_id'], self.run_id)
        self.assertEqual(report['startup'], {'job_null': True, 'fake_call_count': 0})
        self.assertEqual(report['end'], {'job_null': True, 'fake_call_count': 0})
        self.assertEqual(report['fake_writes'], [])


if __name__ == '__main__':
    unittest.main()
