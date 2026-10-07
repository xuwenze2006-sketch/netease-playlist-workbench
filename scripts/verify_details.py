"""Repeatable local song-details acceptance using an owned fake helper only.

Importing this driver never starts a process or browser. Explicit runs use an
already installed Playwright CLI; no npm, installation, real account or URL is accepted.
"""

import argparse
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

try:
    from .verify_web import (PROJECT, Cli, VerificationError, _NO_WINDOW, _announcement,
                            _json, file_signature, load_fresh_report, locate_cli, stop_helper, write_report)
    from .verify_restart import _cleanup_browser, frontend_snapshot, require_frontend_fresh
except ImportError:
    from verify_web import (PROJECT, Cli, VerificationError, _NO_WINDOW, _announcement,
                           _json, file_signature, load_fresh_report, locate_cli, stop_helper, write_report)
    from verify_restart import _cleanup_browser, frontend_snapshot, require_frontend_fresh

if str(PROJECT) not in sys.path:
    sys.path.insert(0, str(PROJECT))


_HELPER = PROJECT/'scripts/serve_details_fixture.py'
_MARKER = 'details-fixture-owner.json'
_SHORT_VIEWPORTS = [{'width': 390, 'height': 500}, {'width': 820, 'height': 360}]
ASSERTIONS = (
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
)
_STAGES = frozenset(('layout', 'startup', 'counts', 'pagination', 'song_search', 'artist_search',
                     'no_match', 'keyboard', 'reopen', 'normal', 'reload', 'wide', 'proof',
                     'metadata', 'metadata_search', 'metadata_clear', 'complete_filter', 'name_search',
                     'short_portrait', 'short_landscape'))


def mark_workspace(workspace, owner_token, run_id):
    path = Path(workspace)
    if (not path.is_absolute() or path.is_symlink() or not path.is_dir()
            or not path.name.startswith('organizer-details-')
            or path.resolve().is_relative_to(PROJECT) or PROJECT.is_relative_to(path.resolve())
            or any(parent.is_symlink() for parent in path.parents)
            or not isinstance(owner_token, str) or not re.fullmatch(r'[a-f0-9]{64}', owner_token)
            or not isinstance(run_id, str) or not re.fullmatch(r'[a-f0-9]{32}', run_id)
            or (path/_MARKER).exists()):
        raise VerificationError('workspace_ownership')
    with (path/_MARKER).open('x', encoding='utf-8') as stream:
        json.dump({'kind': 'organizer_details_fixture_owner', 'version': 1, 'owner_token': owner_token,
                   'owner_pid': os.getpid(), 'run_id': run_id}, stream)
        stream.flush()
        os.fsync(stream.fileno())


def check_evidence(stdout, fixture, *, run_id, index_sha256, assets_sha256, expected_pid):
    outer = _json(stdout, 'browser_callback')
    raw = outer.get('result')
    observed = _json(raw, 'browser_callback') if isinstance(raw, str) else raw
    if (outer.get('isError') is True or not isinstance(observed, dict)
            or observed.get('kind') != 'modern_details_browser_observation' or observed.get('verified') is not True):
        stage = observed.get('failed_stage') if isinstance(observed, dict) else None
        raise VerificationError(stage if isinstance(stage, str) and stage in _STAGES else 'browser_callback')
    assertions = observed.get('assertions')
    if (not isinstance(assertions, dict) or any(assertions.get(name) is not True for name in ASSERTIONS)
            or observed.get('viewport') != {'width': 390, 'height': 844}
            or observed.get('short_viewports') != _SHORT_VIEWPORTS
            or observed.get('frontend_assets_sha256') != assets_sha256
            or not isinstance(fixture, dict) or fixture.get('kind') != 'modern_details_fake_account_report'
            or type(fixture.get('version')) is not int or fixture['version'] != 1
            or fixture.get('run_id') != run_id or fixture.get('index_sha256') != index_sha256
            or fixture.get('frontend_assets_sha256') != assets_sha256
            or type(expected_pid) is not int or expected_pid <= 0
            or type(fixture.get('process_id')) is not int or fixture['process_id'] != expected_pid
            or type(fixture.get('real_account_calls')) is not int or fixture['real_account_calls'] != 0
            or type(fixture.get('fake_write_count')) is not int or fixture['fake_write_count'] != 0
            or fixture.get('fake_writes') != []
            or fixture.get('saved_counts') != {'expected': 67, 'observed': 62, 'missing': 5, 'metadata_missing': 2}
            or not isinstance(fixture.get('saved_counts'), dict)
            or any(type(value) is not int for value in fixture['saved_counts'].values())):
        raise VerificationError('fixture_report')
    for name in ('startup', 'end'):
        value = fixture.get(name)
        if (not isinstance(value, dict) or value.get('job_null') is not True
                or type(value.get('fake_call_count')) is not int or value['fake_call_count'] != 0):
            raise VerificationError('fixture_report')
    return {'verified': True, 'process_id': expected_pid, 'fake_write_count': 0,
            'viewport': {'width': 390, 'height': 844}, 'wide_viewport': {'width': 1280, 'height': 900},
            'short_viewports': [dict(viewport) for viewport in _SHORT_VIEWPORTS],
            'assertions': {name: True for name in ASSERTIONS}}


def browser_script(url, *, assets_sha256):
    """Use only the component's public roles, labels and visible song positions."""
    if (not isinstance(url, str) or not re.fullmatch(r'http://127\.0\.0\.1:[1-9][0-9]{0,4}/', url)
            or not isinstance(assets_sha256, str) or not re.fullmatch(r'[a-f0-9]{64}', assets_sha256)):
        raise VerificationError('browser_script')
    return r'''async page => {
  const base = __URL__, assetsHash = __HASH__, shortViewports = __SHORT_WINDOWS__, assertions = {};
  const deadline = Date.now() + 42000;
  let stage = 'layout', nonce, actionPosts = 0;
  const require = value => { if (!value) throw new Error('acceptance'); };
  const request = page.context().request;
  page.on('request', value => {
    if (value.method() === 'POST' && value.url() === base + 'api/actions') actionPosts += 1;
  });
  const get = async path => {
    const response = await request.get(base + path, {headers: {'X-Organizer-Session': nonce}, timeout: 2000});
    require(response.status() === 200);
    return await response.json();
  };
  const readNonce = async () => {
    nonce = await page.locator('meta[name="organizer-session"]').getAttribute('content');
    require(typeof nonce === 'string' && /^[a-f0-9]{64}$/.test(nonce));
  };
  const proof = async () => {
    const value = await get('fixture/details');
    require(value.kind === 'fake_details_account_state' && value.real_account_calls === 0 &&
            value.fake_write_count === 0 && value.fake_call_count === 0 &&
            Array.isArray(value.fake_writes) && value.fake_writes.length === 0 &&
            value.frontend_assets_sha256 === assetsHash && actionPosts === 0);
  };
  const noOverflow = async () => require(await page.evaluate(() =>
    document.documentElement.scrollWidth <= window.innerWidth + 1));
  const likedButton = () => page.getByRole('button', {name: '打开我喜欢的音乐的歌曲明细', exact: true});
  const likedDialog = () => page.getByRole('dialog', {name: '我喜欢的音乐的歌曲明细', exact: true});
  const waitPositions = async (dialog, wanted) => {
    while (Date.now() < deadline) {
      // Read one synchronous DOM snapshot from the stable dialog. A filter
      // may remove old nth-row locators between separate awaited operations.
      const positions = await dialog.evaluate(element => {
        const tables = element.querySelectorAll('table[aria-label="已保存的歌曲"]');
        if (tables.length !== 1) return null;
        return Array.from(tables[0].querySelectorAll('tbody > tr'), row =>
          Number(row.querySelector('td')?.textContent?.trim()));
      });
      if (positions !== null && JSON.stringify(positions) === JSON.stringify(wanted)) return;
      await page.waitForTimeout(80);
    }
    throw new Error('acceptance');
  };
  const waitFocus = async locator => {
    while (Date.now() < deadline) {
      if (await locator.evaluate(element => element === document.activeElement)) return;
      await page.waitForTimeout(80);
    }
    throw new Error('acceptance');
  };
  const range = (first, last) => Array.from({length: last-first+1}, (_, i) => first+i);
  const fullyVisible = async (locator, viewport, scroll = true) => {
    if (scroll) await locator.scrollIntoViewIfNeeded();
    const box = await locator.boundingBox();
    require(box && box.width > 0 && box.height > 0 && box.x >= -1 && box.y >= -1 &&
            box.x + box.width <= viewport.width + 1 && box.y + box.height <= viewport.height + 1);
    const visible = await locator.evaluate(element => new Promise(resolve => {
      const observer = new IntersectionObserver(entries => {
        const entry = entries[0], rect = element.getBoundingClientRect();
        const hit = document.elementFromPoint(rect.left + rect.width / 2, rect.top + rect.height / 2);
        observer.disconnect();
        resolve(entry.isIntersecting && entry.intersectionRatio >= 0.99 &&
                !!hit && (element === hit || element.contains(hit)));
      });
      observer.observe(element);
    }));
    require(visible);
  };
  const scrollSample = async dialog => await dialog.locator('.details-content').evaluate(element => ({
    top: element.scrollTop, height: element.clientHeight, extent: element.scrollHeight,
    width: element.scrollWidth, clientWidth: element.clientWidth,
  }));
  try {
    await page.setViewportSize({width: 390, height: 844});
    await readNonce();
    await page.getByRole('navigation', {name: '主导航'}).getByRole('link', {name: /^我的歌单(?:\s*\d+)?$/}).click();
    await likedButton().waitFor({state: 'visible', timeout: 7000});
    await noOverflow();
    assertions.narrow_no_horizontal_overflow = true;
    stage = 'startup';
    require((await get('api/state')).job === null);
    await proof();
    assertions.startup_job_null = true;
    assertions.served_assets_match = true;
    await likedButton().click();
    const dialog = likedDialog();
    await dialog.waitFor({state: 'visible', timeout: 4000});
    stage = 'counts';
    for (const text of ['已保存 62 / 67 首', '缺失 5 首', '2 首资料不完整'])
      await dialog.getByText(text, {exact: true}).waitFor({state: 'visible', timeout: 4000});
    require(await dialog.locator('time').getAttribute('datetime') === '2023-11-14T22:13:20+00:00');
    await waitPositions(dialog, range(1, 50));
    await noOverflow();
    assertions.historical_partial_counts = true;
    assertions.first_page_order = true;
    stage = 'keyboard';
    const close = dialog.getByRole('button', {name: '关闭歌曲明细', exact: true});
    const next = dialog.getByRole('button', {name: '下一页', exact: true});
    require(await close.evaluate(element => element === document.activeElement));
    await page.keyboard.press('Shift+Tab');
    require(await next.evaluate(element => element === document.activeElement));
    await page.keyboard.press('Tab');
    require(await close.evaluate(element => element === document.activeElement));
    assertions.keyboard_focus_trap = true;
    stage = 'pagination';
    await next.focus();
    await next.press('Enter');
    await waitPositions(dialog, range(51, 62));
    const previous = dialog.getByRole('button', {name: '上一页', exact: true});
    await waitFocus(previous);
    assertions.next_page_order = true;
    await previous.press('Enter');
    await waitPositions(dialog, range(1, 50));
    await waitFocus(next);
    assertions.previous_page_order = true;
    assertions.keyboard_pagination_focus = true;
    stage = 'song_search';
    const search = dialog.getByRole('textbox', {name: '搜索曲名或歌手', exact: true});
    await search.fill(' summer ');
    await waitPositions(dialog, [7]);
    await dialog.getByText('ＳＵＭＭＥＲ · 离线验收', {exact: true}).waitFor({state: 'visible'});
    assertions.song_search_original_position = true;
    stage = 'artist_search';
    await search.fill('林俊杰');
    // The first song's title also contains the query, despite missing artist metadata.
    await waitPositions(dialog, range(1, 21));
    assertions.artist_search_original_position = true;
    stage = 'no_match';
    await search.fill('这首歌曲绝不存在-fixture');
    await dialog.getByRole('heading', {name: '没有匹配的已保存歌曲', exact: true}).waitFor({state: 'visible', timeout: 4000});
    for (const table of await dialog.getByRole('table', {name: '已保存的歌曲', exact: true}).all())
      for (const row of await table.getByRole('row').all()) require(await row.getByRole('cell').count() === 0);
    assertions.no_match_distinct = true;
    stage = 'metadata';
    await dialog.getByRole('button', {name: '清除歌曲搜索', exact: true}).click();
    await waitPositions(dialog, range(1, 50));
    await next.click();
    await waitPositions(dialog, range(51, 62));
    const filters = dialog.getByRole('group', {name: '歌曲资料筛选', exact: true});
    const all = filters.getByRole('button', {name: '全部已保存歌曲', exact: true});
    const incomplete = filters.getByRole('button', {name: '只看资料不完整', exact: true});
    require(await all.getAttribute('aria-pressed') === 'true');
    await incomplete.click();
    await waitPositions(dialog, [1, 55]);
    require(await incomplete.getAttribute('aria-pressed') === 'true' &&
            await all.getAttribute('aria-pressed') === 'false');
    assertions.metadata_cross_page_positions = true;
    for (const text of ['已保存 62 / 67 首', '缺失 5 首', '2 首资料不完整'])
      await dialog.getByText(text, {exact: true}).waitFor({state: 'visible'});
    require(await dialog.locator('time').getAttribute('datetime') === '2023-11-14T22:13:20+00:00');
    assertions.metadata_counts_and_time_unchanged = true;
    await noOverflow();
    stage = 'metadata_search';
    await search.fill('蔡依林 · 验收曲目 9');
    await waitPositions(dialog, [55]);
    await dialog.getByRole('table', {name: '已保存的歌曲', exact: true})
      .getByText('蔡依林', {exact: true}).waitFor({state: 'visible'});
    require(await incomplete.getAttribute('aria-pressed') === 'true');
    assertions.metadata_search_intersection = true;
    stage = 'metadata_clear';
    await dialog.getByRole('button', {name: '清除歌曲搜索', exact: true}).click();
    await waitPositions(dialog, [1, 55]);
    require(await search.inputValue() === '' && await incomplete.getAttribute('aria-pressed') === 'true');
    assertions.metadata_clear_keeps_filter = true;
    // Keep a nonempty search and the filter selected when closing. Reopening
    // must reset both instead of merely displaying the same old empty input.
    await search.fill('资料不完整-无交集-fixture');
    await dialog.getByRole('heading', {name: '没有符合筛选的已保存歌曲', exact: true})
      .waitFor({state: 'visible', timeout: 4000});
    stage = 'keyboard';
    await page.keyboard.press('Escape');
    await dialog.waitFor({state: 'hidden', timeout: 2000});
    require(await likedButton().evaluate(element => element === document.activeElement));
    assertions.escape_restores_card_focus = true;
    stage = 'reopen';
    await likedButton().click();
    await likedDialog().waitFor({state: 'visible'});
    require(await likedDialog().getByRole('textbox', {name: '搜索曲名或歌手', exact: true}).inputValue() === '');
    await waitPositions(likedDialog(), range(1, 50));
    const reopenedFilters = likedDialog().getByRole('group', {name: '歌曲资料筛选', exact: true});
    require(await reopenedFilters.getByRole('button', {name: '全部已保存歌曲', exact: true})
              .getAttribute('aria-pressed') === 'true' &&
            await reopenedFilters.getByRole('button', {name: '只看资料不完整', exact: true})
              .getAttribute('aria-pressed') === 'false');
    assertions.reopen_clears_search = true;
    assertions.reopen_resets_metadata = true;
    await page.keyboard.press('Escape');
    await likedDialog().waitFor({state: 'hidden'});
    stage = 'normal';
    await page.getByRole('button', {name: '打开funk的歌曲明细', exact: true}).click();
    const normal = page.getByRole('dialog', {name: 'funk的歌曲明细', exact: true});
    await normal.getByRole('heading', {name: '尚未保存歌曲明细', exact: true}).waitFor({state: 'visible', timeout: 4000});
    require(await normal.getByRole('table', {name: '已保存的歌曲', exact: true}).count() === 0);
    assertions.normal_details_not_loaded = true;
    await page.keyboard.press('Escape');
    await normal.waitFor({state: 'hidden'});
    stage = 'complete_filter';
    await page.getByRole('button', {name: '打开夜晚的纯音乐的歌曲明细', exact: true}).click();
    const complete = page.getByRole('dialog', {name: '夜晚的纯音乐的歌曲明细', exact: true});
    await waitPositions(complete, range(1, 5));
    await complete.getByRole('group', {name: '歌曲资料筛选', exact: true})
      .getByRole('button', {name: '只看资料不完整', exact: true}).click();
    await complete.getByRole('heading', {name: '已保存歌曲的资料均完整', exact: true})
      .waitFor({state: 'visible', timeout: 4000});
    await complete.getByText('已保存 5 / 5 首', {exact: true}).waitFor({state: 'visible'});
    require(await complete.getByRole('table', {name: '已保存的歌曲', exact: true}).count() === 0);
    require(await complete.locator('time').getAttribute('datetime') === '2023-11-14T22:13:20+00:00');
    await complete.getByRole('group', {name: '歌曲资料筛选', exact: true})
      .getByRole('button', {name: '全部已保存歌曲', exact: true}).click();
    await waitPositions(complete, range(1, 5));
    assertions.complete_metadata_filter_empty = true;
    await page.keyboard.press('Escape');
    await complete.waitFor({state: 'hidden'});
    stage = 'name_search';
    const nameSearch = page.getByRole('textbox', {name: '搜索歌单名称', exact: true});
    await nameSearch.fill('Ｆｕｎｋ');
    await likedButton().waitFor({state: 'hidden', timeout: 4000});
    await page.getByRole('button', {name: '打开funk的歌曲明细', exact: true}).waitFor({state: 'visible'});
    require(await likedButton().count() === 0);
    await page.getByRole('button', {name: '清除搜索', exact: true}).click();
    await likedButton().waitFor({state: 'visible'});
    require(await nameSearch.inputValue() === '');
    assertions.playlist_name_unicode_search = true;
    stage = 'reload';
    await page.reload({waitUntil: 'domcontentloaded'});
    await readNonce();
    await likedButton().waitFor({state: 'visible', timeout: 5000});
    require((await get('api/state')).job === null);
    await proof();
    assertions.reload_has_no_job_or_account_call = true;
    stage = 'wide';
    await page.setViewportSize({width: 1280, height: 900});
    await likedButton().click();
    await likedDialog().waitFor({state: 'visible'});
    await waitPositions(likedDialog(), range(1, 50));
    await noOverflow();
    assertions.wide_no_horizontal_overflow = true;
    await page.keyboard.press('Escape');
    await likedDialog().waitFor({state: 'hidden'});
    for (let index = 0; index < shortViewports.length; index += 1) {
      const viewport = shortViewports[index], prefix = index === 0 ? 'short_portrait' : 'short_landscape';
      stage = prefix;
      await page.setViewportSize(viewport);
      require(await page.evaluate(size => innerWidth === size.width && innerHeight === size.height, viewport));
      await likedButton().click();
      const small = likedDialog();
      await small.waitFor({state: 'visible'});
      await waitPositions(small, range(1, 50));
      const group = small.getByRole('group', {name: '歌曲资料筛选', exact: true});
      const incompleteButton = group.getByRole('button', {name: '只看资料不完整', exact: true});
      await fullyVisible(incompleteButton, viewport);
      await incompleteButton.click();
      await waitPositions(small, [1, 55]);
      const partialRow = small.getByRole('table', {name: '已保存的歌曲', exact: true})
        .getByRole('row').filter({has: page.getByText('蔡依林 · 验收曲目 9', {exact: true})});
      await fullyVisible(partialRow, viewport);
      await partialRow.getByText('蔡依林', {exact: true}).waitFor({state: 'visible'});
      for (const text of ['已保存 62 / 67 首', '缺失 5 首', '2 首资料不完整'])
        await small.getByText(text, {exact: true}).waitFor({state: 'visible'});
      require(await small.locator('time').getAttribute('datetime') === '2023-11-14T22:13:20+00:00');
      const allButton = group.getByRole('button', {name: '全部已保存歌曲', exact: true});
      await fullyVisible(allButton, viewport);
      await allButton.click();
      await waitPositions(small, range(1, 50));
      const nextButton = small.getByRole('button', {name: '下一页', exact: true});
      await fullyVisible(nextButton, viewport);
      await nextButton.click();
      await waitPositions(small, range(51, 62));
      await fullyVisible(small.getByRole('textbox', {name: '搜索曲名或歌手', exact: true}), viewport);
      const beforeScroll = await scrollSample(small);
      const lastRow = small.getByRole('table', {name: '已保存的歌曲', exact: true})
        .getByRole('row').filter({has: page.getByText('Taylor Swift · 验收曲目 5', {exact: true})});
      await fullyVisible(lastRow, viewport);
      const afterScroll = await scrollSample(small);
      require(afterScroll.top > beforeScroll.top + 5 && afterScroll.extent > afterScroll.height);
      assertions[prefix + '_content_scroll'] = true;
      const previousButton = small.getByRole('button', {name: '上一页', exact: true});
      const closeButton = small.getByRole('button', {name: '关闭歌曲明细', exact: true});
      await fullyVisible(previousButton, viewport);
      await fullyVisible(closeButton, viewport);
      // Both fixed controls must remain reachable at the same scroll position.
      await fullyVisible(previousButton, viewport, false);
      await fullyVisible(closeButton, viewport, false);
      assertions[prefix + '_controls_in_view'] = true;
      await noOverflow();
      require(afterScroll.width <= afterScroll.clientWidth + 1);
      await closeButton.focus();
      await page.keyboard.press('Shift+Tab');
      require(await previousButton.evaluate(element => element === document.activeElement));
      await page.keyboard.press('Tab');
      require(await closeButton.evaluate(element => element === document.activeElement));
      await page.keyboard.press('Escape');
      await small.waitFor({state: 'hidden'});
      require(await likedButton().evaluate(element => element === document.activeElement));
      assertions[prefix + '_keyboard'] = true;
      await proof();
      require((await get('api/state')).job === null);
    }
    assertions.short_viewport_no_horizontal_overflow = true;
    assertions.short_viewport_filter_and_pagination = true;
    stage = 'proof';
    await proof();
    require((await get('api/state')).job === null);
    assertions.zero_fake_writes = true;
    require(actionPosts === 0);
    assertions.zero_action_posts = true;
    return {kind: 'modern_details_browser_observation', verified: true,
            viewport: {width: 390, height: 844}, short_viewports: shortViewports,
            frontend_assets_sha256: assetsHash, assertions};
  } catch {
    return {kind: 'modern_details_browser_observation', verified: false, failed_stage: stage};
  }
}'''.replace('__URL__', json.dumps(url)).replace('__HASH__', json.dumps(assets_sha256)) \
     .replace('__SHORT_WINDOWS__', json.dumps(_SHORT_VIEWPORTS))


def run_case(node, entry, workspace, token, run_id, index_hash, assets_hash, deadline, *, browser='msedge'):
    report_path = Path(workspace)/'details-report.json'
    previous, started_ns = file_signature(report_path), time.time_ns()
    cli = Cli(node, entry, 'organizer-details-'+secrets.token_hex(6), workspace, deadline)
    process, observed, error = None, None, None
    cleaned, stopped, stage = False, False, 'helper_start'
    try:
        process = subprocess.Popen([sys.executable, '-X', 'utf8', str(_HELPER), '--workspace', str(workspace),
            '--owner-token', token], shell=False, cwd=PROJECT, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, text=True, encoding='utf-8', creationflags=_NO_WINDOW)
        url = _announcement(process, deadline)
        stage = cli.stage = 'browser_open'
        cli.command('open', url, '--browser='+browser, '--idle-timeout=60000', timeout=25)
        callback = Path(workspace)/'details.js'
        callback.write_text(browser_script(url, assets_sha256=assets_hash), encoding='utf-8')
        stage = cli.stage = 'browser_callback'
        observed = cli.command('run-code', '--filename='+str(callback), timeout=45)
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
    if not cleaned or not stopped:
        raise VerificationError('cleanup')
    if error:
        raise error
    result = check_evidence(observed, load_fresh_report(report_path, previous, started_ns), run_id=run_id,
                            index_sha256=index_hash, assets_sha256=assets_hash, expected_pid=process.pid)
    require_frontend_fresh(PROJECT/'frontend/dist', index_hash, assets_hash)
    result['assertions']['own_browser_and_helper_cleaned'] = True
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--cli-path')
    parser.add_argument('--browser', choices=('msedge', 'chrome'), default='msedge')
    parser.add_argument('--timeout', type=int, default=150)
    options = parser.parse_args()
    started, run_id = time.monotonic(), secrets.token_hex(16)
    report = {'kind': 'modern_details_fake_browser_verification', 'version': 1, 'run_id': run_id,
              'verified': False, 'real_account_calls': 0, 'scenarios': [], 'failed_stage': 'started',
              'tested_versions': {'python': platform.python_version()}, 'assertions': {}}
    destination = PROJECT/'artifacts/modern-details-verification.json'
    try:
        write_report(destination, report)  # Invalidate old PASS before any prerequisite or process.
        if not 90 <= options.timeout <= 300:
            raise VerificationError('prerequisites')
        node = shutil.which('node')
        if not node:
            raise VerificationError('node_location')
        entry, versions = locate_cli(options.cli_path)
        report['tested_versions'].update(versions)
        _, index_hash, assets_hash = frontend_snapshot(PROJECT/'frontend/dist')
        report['tested_versions'].update(frontend_index_sha256=index_hash,
            frontend_assets_sha256=assets_hash, browser=options.browser)
        with tempfile.TemporaryDirectory(prefix='organizer-details-acceptance-') as folder:
            workspace, token = Path(folder), secrets.token_hex(32)
            mark_workspace(workspace, token, run_id)
            report['scenarios'].append(run_case(node, entry, workspace, token, run_id, index_hash, assets_hash,
                                               started+options.timeout, browser=options.browser))
        report['assertions'].update(temporary_workspace_cleaned=True, all_owned_helpers_and_browsers_cleaned=True,
                                     frontend_assets_fresh=True)
        report['verified'], report['failed_stage'] = True, None
    except VerificationError as failure:
        report['failed_stage'] = failure.stage
    except KeyboardInterrupt:
        report['failed_stage'] = 'cancelled'
    except Exception:
        report['failed_stage'] = 'verification'
    report['elapsed_seconds'] = round(time.monotonic()-started, 2)
    try:
        write_report(destination, report)
    except (OSError, VerificationError):
        report['verified'], report['failed_stage'] = False, 'report_save'
    print(json.dumps(report, ensure_ascii=False))
    return 0 if report['verified'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
