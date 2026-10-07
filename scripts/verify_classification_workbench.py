"""Real browser acceptance against owned temporary classification records only.

No account URL, credentials, real launcher, or dependency installation is used.
The public report is PASS only after browser assertions and all owned cleanup.
"""

import argparse
import copy
import hashlib
import json
import os
from pathlib import Path
import secrets
import shutil
import sys
import threading
import time
from urllib.parse import parse_qsl, urlsplit


PROJECT = Path(__file__).resolve().parents[1]
if str(PROJECT) not in sys.path:
    sys.path.insert(0, str(PROJECT))

from scripts.verify_web import Cli, VerificationError, _json, locate_cli, write_report
from scripts.verify_restart import _cleanup_browser, frontend_snapshot, require_frontend_fresh
from netease_organizer.web_server import create_server, shutdown_server
from netease_organizer.web_state import build_local_state
from netease_organizer.classification_execution import _encoded
from tests.test_web_classification import WebClassificationTests
from tests.test_web_server import FakeController


ASSETS = PROJECT / "frontend" / "dist"
REPORT = PROJECT / "artifacts" / "分类成果工作台-浏览器验收.json"
_COMMON = ("wide_no_horizontal_overflow", "mobile_no_horizontal_overflow", "reload_has_no_job",
           "zero_action_posts", "zero_external_requests", "default_track_tab",
           "search_initially_reachable_wide", "search_initially_reachable_mobile", "no_local_storage_state")
_ASSERTIONS = {
    "filters": _COMMON + ("ordinary_startup_local_only", "verified_record_label", "seven_playlist_cards",
                          "four_original_positions", "artist_unicode_search", "scene_tag_filter",
                          "style_tag_filter", "language_tag_filter", "pending_filter_intersection",
                          "evidence_visible", "refresh_local_only", "classification_history_no_resume",
                          "playlist_tab_cards", "card_to_track_filter", "clickable_tag_filters",
                          "pending_quick_clear", "navigation_keeps_selection", "inactive_page_no_classification_query",
                          "return_only_local_get", "refresh_keeps_selection", "refresh_updates_cached_history"),
    "pagination": _COMMON + ("local_only_record_label", "first_fifty_positions", "last_thirteen_positions",
                             "previous_page_original_positions", "keyboard_pagination_focus", "page_first_row_visible"),
}
_STAGES = frozenset(("overview", "classification", "search", "scene", "style", "language", "pending",
                     "evidence", "refresh", "history", "pagination", "reload", "wide", "mobile", "proof",
                     "tabs", "card", "labels", "memory", "pagination_focus", "initial_search"))


def fixture_history_revision(fixture, intent, receipt):
    """A fixed change to two owned fake records; no user path or account input."""
    intent, receipt = copy.deepcopy(intent), copy.deepcopy(receipt)
    intent.update(status="paused", phase="paused")
    receipt.update(status="paused", intent_digest=hashlib.sha256(_encoded(intent)).hexdigest())
    fixture.write("分类整理执行进度.json", intent, 106)
    fixture.write("分类整理执行结果.json", receipt, 107)


def install_fixture_refresh(server, fixture, intent, receipt):
    """Revise fake history once on a guarded, well-shaped explicit local refresh."""
    handler, revision_lock = server.RequestHandlerClass, threading.Lock()
    server.fixture_history_revisions = 0

    class FixtureRefreshHandler(handler):
        def do_GET(self):
            try:
                url = urlsplit(self.path)
                pairs = parse_qsl(url.query, strict_parsing=True, keep_blank_values=True,
                                  encoding="utf-8", errors="strict", max_num_fields=7)
                parameters = dict(pairs)
                revise = (url.path == "/api/classification" and len(pairs) == len(parameters)
                          and set(parameters) == {"offset", "limit", "q", "dimension", "tag", "review", "refresh"}
                          and parameters.get("refresh") == "local")
            except (ValueError, UnicodeError):
                revise = False
            if revise:
                if not self._guard():
                    return
                with revision_lock:
                    if self.server.fixture_history_revisions == 0:
                        try:
                            fixture_history_revision(fixture, intent, receipt)
                            self.server.fixture_history_revisions = 1
                        except Exception:
                            self._json(503, {"accepted": False, "message": "模拟记录更新失败。"})
                            return
            super().do_GET()

    server.RequestHandlerClass = FixtureRefreshHandler


def browser_script(case, url, run_id, assets_hash, screenshot, mobile_screenshot):
    if case not in _ASSERTIONS:
        raise VerificationError("browser_script")
    source = r'''async (page) => {
  const scenario = __CASE__, base = __URL__, runId = __RUN__, assetsHash = __HASH__;
  const screenshot = __SCREEN__, mobileScreenshot = __MOBILE__;
  const assertions = {};
  const deadline = Date.now() + 40000;
  let stage = 'overview', actionPosts = 0, externalRequests = 0, classificationGets = 0, refreshGets = 0;
  const require = condition => { if (!condition) throw new Error('acceptance failed'); };
  const origin = new URL(base).origin;
  page.setDefaultTimeout(4000);
  page.on('request', request => {
    const url = new URL(request.url());
    if (url.origin !== origin && url.protocol !== 'data:' && url.protocol !== 'about:') externalRequests += 1;
    if (url.pathname === '/api/actions' && request.method() === 'POST') actionPosts += 1;
    if (url.pathname === '/api/classification' && request.method() === 'GET') {
      classificationGets += 1;
      if (url.searchParams.get('refresh') === 'local') refreshGets += 1;
    }
  });
  await page.route('**/*', route => {
    const url = new URL(route.request().url());
    return url.origin === origin || url.protocol === 'data:' ? route.continue() : route.abort();
  });
  const nav = () => page.getByRole('navigation', {name: '主导航', exact: true});
  const records = () => page.getByRole('region', {name: '逐曲分类结果', exact: true});
  const tabs = () => page.getByRole('tablist', {name: '分类成果视图', exact: true});
  const search = () => records().getByRole('textbox', {name: '搜索歌曲或歌手', exact: true});
  const dimension = () => records().getByRole('combobox', {name: '分类维度', exact: true});
  const tag = () => records().getByRole('combobox', {name: '分类标签', exact: true});
  const pending = () => records().getByRole('button', {name: '只看待辨识', exact: true});
  const range = (first, last) => Array.from({length: last - first + 1}, (_, index) => first + index);
  const waitFor = async predicate => {
    while (Date.now() < deadline) {
      if (await predicate()) return;
      await page.waitForTimeout(50);
    }
    throw new Error('condition timeout');
  };
  const waitPositions = async expected => {
    while (Date.now() < deadline) {
      // One synchronous DOM snapshot avoids reading removed nth rows after a render.
      const actual = await records().getByRole('article').evaluateAll(rows => rows.map(row =>
        Number(row.querySelector('.classification-position')?.textContent?.trim())));
      if (await records().getAttribute('aria-busy') === 'false' && JSON.stringify(actual) === JSON.stringify(expected)) return;
      await page.waitForTimeout(50);
    }
    throw new Error('positions timeout');
  };
  const readState = async () => {
    return await page.evaluate(async () => {
      const nonce = document.querySelector('meta[name="organizer-session"]')?.getAttribute('content');
      if (!nonce) throw new Error('missing session');
      const response = await fetch('/api/state', {headers: {'X-Organizer-Session': nonce}, cache: 'no-store'});
      if (!response.ok) throw new Error('state unavailable');
      return await response.json();
    });
  };
  const noOverflow = async () => {
    await page.evaluate(() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve))));
    require(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1 &&
      document.body.scrollWidth <= innerWidth + 1));
  };
  const inViewport = async locator => {
    const box = await locator.boundingBox(), viewport = page.viewportSize();
    return !!box && box.width > 0 && box.height > 0 && box.x >= -1 && box.y >= -1 &&
      box.x + box.width <= viewport.width + 1 && box.y + box.height <= viewport.height + 1;
  };
  const selected = async (dim, label, query = '', review = false) => {
    require(await dimension().inputValue() === dim && await tag().inputValue() === label &&
      await search().inputValue() === query && await pending().getAttribute('aria-pressed') === String(review));
  };
  const historyNoResume = async expectedStatus => {
    require(await page.locator('.history-row').count() === 7);
    const resume = page.getByRole('button', {name: '继续上次任务', exact: true});
    require(await resume.count() > 0);
    for (const button of await resume.all()) require(await button.isDisabled());
    const state = await readState();
    require(state.data.history?.operation === 'classification' && state.data.history.status === expectedStatus &&
      state.data.history.completed_count === 7 && state.data.history.resumable === false && state.job === null);
  };
  try {
    await page.setViewportSize({width: 1440, height: 1000});
    await page.goto(base, {waitUntil: 'domcontentloaded'});
    await nav().getByRole('link', {name: '分类成果', exact: true}).waitFor({state: 'visible'});
    require((await readState()).job === null && classificationGets === 0);
    assertions.ordinary_startup_local_only = true;
    stage = 'classification';
    await nav().getByRole('link', {name: '分类成果', exact: true}).click();
    await page.getByRole('heading', {name: '分类成果', level: 1, exact: true}).waitFor({state: 'visible'});
    await waitPositions(scenario === 'filters' ? [1, 2, 3, 4] : range(1, 50));
    stage = 'initial_search';
    require(await tabs().getByRole('tab', {name: '歌曲分类', exact: true}).getAttribute('aria-selected') === 'true' &&
      await page.getByRole('region', {name: '分类歌单', exact: true}).count() === 0);
    assertions.default_track_tab = true;
    require(await inViewport(search()));
    assertions.search_initially_reachable_wide = true;
    await page.setViewportSize({width: 390, height: 844});
    await noOverflow();
    require(await inViewport(search()));
    assertions.search_initially_reachable_mobile = true;
    await page.setViewportSize({width: 1440, height: 1000});
    if (scenario === 'filters') {
      await waitPositions([1, 2, 3, 4]);
      const source = page.getByRole('region', {name: '分类记录来源', exact: true});
      await source.getByText('已有账号核验记录', {exact: true}).waitFor({state: 'visible'});
      await source.getByText(/上次账号核验：.*浏览本页不会重新核验账号/).waitFor({state: 'visible'});
      assertions.verified_record_label = true;
      stage = 'tabs';
      await tabs().getByRole('tab', {name: '分类歌单', exact: true}).click();
      require(await tabs().getByRole('tab', {name: '分类歌单', exact: true}).getAttribute('aria-selected') === 'true');
      const playlists = page.getByRole('region', {name: '分类歌单', exact: true});
      require(await playlists.getByRole('article').count() === 7);
      assertions.seven_playlist_cards = true;
      assertions.playlist_tab_cards = true;
      assertions.four_original_positions = true;
      stage = 'card';
      await playlists.getByRole('article').filter({has: page.getByRole('heading', {name: '风格 · 摇滚与独立', exact: true})})
        .getByRole('button', {name: '查看分类曲目', exact: true}).click();
      await waitPositions([4]);
      await selected('style', '摇滚与独立');
      require(await tabs().getByRole('tab', {name: '歌曲分类', exact: true}).getAttribute('aria-selected') === 'true');
      assertions.card_to_track_filter = true;
      await page.getByRole('button', {name: '清除全部筛选', exact: true}).click();
      await waitPositions([1, 2, 3, 4]);
      stage = 'search';
      await search().fill('ＴＡＹＬＯＲ');
      await waitPositions([1]);
      await records().getByRole('heading', {name: 'Rain · 雨の音', exact: true}).waitFor({state: 'visible'});
      assertions.artist_unicode_search = true;
      await records().getByRole('button', {name: '清除分类搜索', exact: true}).click();
      await waitPositions([1, 2, 3, 4]);
      stage = 'scene';
      await records().getByRole('button', {name: '场景 · 通勤散步', exact: true}).first().click();
      await waitPositions([1, 2]);
      await selected('scene', '通勤散步');
      assertions.scene_tag_filter = true;
      await page.getByRole('button', {name: '清除全部筛选', exact: true}).click();
      await waitPositions([1, 2, 3, 4]);
      stage = 'style';
      await records().getByRole('button', {name: '风格 · 摇滚与独立', exact: true}).click();
      await waitPositions([4]);
      await selected('style', '摇滚与独立');
      assertions.style_tag_filter = true;
      await page.getByRole('button', {name: '清除全部筛选', exact: true}).click();
      await waitPositions([1, 2, 3, 4]);
      stage = 'language';
      await records().getByRole('button', {name: '语言 · 英语', exact: true}).click();
      await waitPositions([1]);
      await selected('language', '英语');
      assertions.language_tag_filter = true;
      assertions.clickable_tag_filters = true;
      stage = 'pending';
      await pending().click();
      await waitPositions([]);
      require(await pending().getAttribute('aria-pressed') === 'true');
      await records().getByText('没有匹配的曲目', {exact: true}).waitFor({state: 'visible'});
      await search().fill('不存在的测试歌曲');
      await waitPositions([]);
      await page.getByRole('region', {name: '分类结果概览', exact: true})
        .getByRole('button', {name: '查看全部待辨识', exact: true}).click();
      await waitPositions([3, 4]);
      await selected('all', '', '', true);
      assertions.pending_quick_clear = true;
      await dimension().selectOption('style');
      await waitPositions([3, 4]);
      await tag().selectOption('摇滚与独立');
      await waitPositions([4]);
      await search().fill('测试歌手');
      await waitPositions([4]);
      await selected('style', '摇滚与独立', '测试歌手', true);
      assertions.pending_filter_intersection = true;
      stage = 'evidence';
      const track = records().getByRole('article');
      await track.locator('summary').click();
      await track.getByText('具体录音及风格判断', {exact: true}).waitFor({state: 'visible'});
      await track.getByText('原歌词文字补充核对', {exact: true}).waitFor({state: 'visible'});
      await track.getByText('语言待辨识', {exact: true}).waitFor({state: 'visible'});
      assertions.evidence_visible = true;
      stage = 'memory';
      const beforeHide = classificationGets;
      await nav().getByRole('link', {name: '任务记录', exact: true}).click();
      await page.getByRole('heading', {name: '任务记录', level: 1, exact: true}).waitFor({state: 'visible'});
      await page.getByText('分类任务 · 本地保存的核对结果', {exact: true}).waitFor({state: 'visible'});
      await historyNoResume('completed');
      await page.waitForTimeout(350);
      require(classificationGets === beforeHide && refreshGets === 0);
      require(await page.evaluate(() => localStorage.length === 0));
      assertions.inactive_page_no_classification_query = true;
      assertions.classification_history_no_resume = true;
      await nav().getByRole('link', {name: '分类成果', exact: true}).click();
      await waitFor(() => classificationGets === beforeHide + 1);
      await waitPositions([4]);
      await selected('style', '摇滚与独立', '测试歌手', true);
      require(refreshGets === 0 && actionPosts === 0 && (await readState()).job === null);
      assertions.navigation_keeps_selection = true;
      assertions.return_only_local_get = true;
      stage = 'refresh';
      const reads = classificationGets;
      require((await readState()).data.history?.status === 'completed');
      await page.getByRole('button', {name: '刷新分类记录', exact: true}).click();
      await waitFor(() => classificationGets === reads + 1);
      await waitPositions([4]);
      await selected('style', '摇滚与独立', '测试歌手', true);
      require(refreshGets === 1 && (await readState()).job === null);
      assertions.refresh_keeps_selection = true;
      assertions.refresh_local_only = true;
      await waitFor(async () => (await readState()).data.history?.status === 'paused');
      await page.getByRole('region', {name: '分类记录来源', exact: true})
        .getByText('仅本地分类记录', {exact: true}).waitFor({state: 'visible'});
      stage = 'history';
      await nav().getByRole('link', {name: '任务记录', exact: true}).click();
      await page.getByRole('heading', {name: '任务记录', level: 1, exact: true}).waitFor({state: 'visible'});
      await historyNoResume('paused');
      await page.locator('.history-card').getByText('已暂停', {exact: true}).waitFor({state: 'visible'});
      assertions.refresh_updates_cached_history = true;
      const returnReads = classificationGets;
      await nav().getByRole('link', {name: '分类成果', exact: true}).click();
      await waitFor(() => classificationGets === returnReads + 1);
      await waitPositions([4]);
      await selected('style', '摇滚与独立', '测试歌手', true);
    } else {
      stage = 'pagination';
      await waitPositions(range(1, 50));
      await page.getByRole('region', {name: '分类记录来源', exact: true})
        .getByText('仅本地分类记录', {exact: true}).waitFor({state: 'visible'});
      assertions.local_only_record_label = true;
      assertions.first_fifty_positions = true;
      const pages = records().getByRole('navigation', {name: '分类曲目分页', exact: true});
      const next = pages.getByRole('button', {name: '下一页', exact: true});
      const previous = pages.getByRole('button', {name: '上一页', exact: true});
      require(await previous.isDisabled() && await next.isEnabled());
      await next.focus();
      await next.press('Enter');
      await waitPositions(range(51, 63));
      require(await next.isDisabled() && await previous.isEnabled());
      assertions.last_thirteen_positions = true;
      stage = 'pagination_focus';
      const resultHeading = records().getByRole('heading', {name: '逐曲分类结果', level: 2, exact: true});
      await waitFor(() => resultHeading.evaluate(node => document.activeElement === node));
      require(await inViewport(records().getByRole('article').first()));
      await previous.focus();
      await previous.press('Enter');
      await waitPositions(range(1, 50));
      await waitFor(() => resultHeading.evaluate(node => document.activeElement === node));
      require(await inViewport(records().getByRole('article').first()));
      assertions.previous_page_original_positions = true;
      assertions.keyboard_pagination_focus = true;
      assertions.page_first_row_visible = true;
    }
    stage = 'reload';
    await page.reload({waitUntil: 'domcontentloaded'});
    await page.getByRole('heading', {name: '分类成果', level: 1, exact: true}).waitFor({state: 'visible'});
    await waitPositions(scenario === 'filters' ? [1, 2, 3, 4] : range(1, 50));
    require((await readState()).job === null);
    assertions.reload_has_no_job = true;
    require(await tabs().getByRole('tab', {name: '歌曲分类', exact: true}).getAttribute('aria-selected') === 'true');
    await selected('all', '');
    require(await page.evaluate(() => localStorage.length === 0));
    assertions.no_local_storage_state = true;
    stage = 'wide';
    await noOverflow();
    assertions.wide_no_horizontal_overflow = true;
    if (scenario === 'filters') await page.screenshot({path: screenshot, fullPage: true});
    stage = 'mobile';
    await page.setViewportSize({width: 390, height: 844});
    await noOverflow();
    await page.getByRole('heading', {name: '分类成果', level: 1, exact: true}).waitFor({state: 'visible'});
    assertions.mobile_no_horizontal_overflow = true;
    if (scenario === 'filters') await page.screenshot({path: mobileScreenshot, fullPage: true});
    stage = 'proof';
    require(actionPosts === 0 && externalRequests === 0 && (await readState()).job === null);
    assertions.zero_action_posts = true;
    assertions.zero_external_requests = true;
    require(!/RAW-SECRET|RECEIPT-SECRET|PRIVATE-TOKEN-DO-NOT-ECHO/.test(await page.locator('body').innerText()));
    return {kind: 'classification_workbench_browser_observation', scenario, run_id: runId,
      frontend_assets_sha256: assetsHash, verified: true, assertions,
      viewports: [{width: 1440, height: 1000}, {width: 390, height: 844}],
      metrics: {action_posts: actionPosts, external_requests: externalRequests,
        classification_gets: classificationGets, explicit_local_refresh_gets: refreshGets}};
  } catch { return {kind: 'classification_workbench_browser_observation', scenario, verified: false, failed_stage: stage}; }
}'''
    replacements = {"__CASE__": case, "__URL__": url, "__RUN__": run_id, "__HASH__": assets_hash,
                    "__SCREEN__": str(screenshot), "__MOBILE__": str(mobile_screenshot)}
    for marker, value in replacements.items():
        source = source.replace(marker, json.dumps(value, ensure_ascii=False))
    return source


def check_observation(stdout, case, run_id, assets_hash):
    outer = _json(stdout, "browser_callback")
    raw = outer.get("result")
    observed = _json(raw, "browser_callback") if isinstance(raw, str) else raw
    if (outer.get("isError") is True or not isinstance(observed, dict) or observed.get("verified") is not True):
        stage = observed.get("failed_stage") if isinstance(observed, dict) else None
        raise VerificationError(stage if isinstance(stage, str) and stage in _STAGES else "browser_callback")
    assertions, metrics = observed.get("assertions"), observed.get("metrics")
    if (case not in _ASSERTIONS or observed.get("kind") != "classification_workbench_browser_observation"
            or observed.get("scenario") != case or observed.get("run_id") != run_id
            or observed.get("frontend_assets_sha256") != assets_hash
            or observed.get("viewports") != [{"width": 1440, "height": 1000}, {"width": 390, "height": 844}]
            or not isinstance(assertions, dict) or any(assertions.get(key) is not True for key in _ASSERTIONS[case])
            or not isinstance(metrics, dict) or type(metrics.get("action_posts")) is not int or metrics["action_posts"] != 0
            or type(metrics.get("external_requests")) is not int or metrics["external_requests"] != 0
            or type(metrics.get("classification_gets")) is not int or not 1 <= metrics["classification_gets"] <= 100
            or type(metrics.get("explicit_local_refresh_gets")) is not int
            or metrics["explicit_local_refresh_gets"] != (1 if case == "filters" else 0)):
        raise VerificationError("browser_callback")
    return {"scenario": case, "verified": True, "assertions": {name: True for name in _ASSERTIONS[case]},
            "metrics": {name: metrics[name] for name in ("action_posts", "external_requests", "classification_gets", "explicit_local_refresh_gets")},
            "viewports": observed["viewports"]}


def run_case(case, node, entry, snapshot, run_id, index_hash, assets_hash, deadline, browser):
    fixture = WebClassificationTests(methodName="runTest")
    server = thread = cli = None
    result = failure = None
    cleanup_ok = True
    screenshots = {}
    stage = "fixture"
    try:
        fixture.setUp()
        if case == "filters":
            intent, receipt, _ = fixture.complete_evidence()
        elif case == "pagination":
            # Reuse this fixture's actual 63-record seed, then prove paging again in the browser.
            fixture.test_pagination_default_fifty_and_preserves_global_summary()
        else:
            raise VerificationError("fixture")
        controller = FakeController(fixture.project)
        server = create_server(controller, assets=ASSETS, asset_snapshot=snapshot,
                               state_provider=lambda: build_local_state(fixture.project))
        if case == "filters":
            install_fixture_refresh(server, fixture, intent, receipt)
        thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": .01}, daemon=True)
        thread.start()
        workspace = fixture.project / "owned-browser"
        workspace.mkdir()
        cli = Cli(node, entry, "classification-" + secrets.token_hex(10), workspace, deadline)
        stage = cli.stage = "browser_open"
        cli.command("open", "about:blank", "--browser=" + browser, "--idle-timeout=60000", timeout=25)
        callback = workspace / "classification-callback.js"
        wide, mobile = workspace / "classification-wide.png", workspace / "classification-mobile.png"
        callback.write_text(browser_script(case, f"http://127.0.0.1:{server.server_address[1]}/", run_id,
                                           assets_hash, wide, mobile), encoding="utf-8")
        stage = cli.stage = "browser_callback"
        observed = cli.command("run-code", "--filename=" + str(callback), timeout=50)
        result = check_observation(observed, case, run_id, assets_hash)
        stage = "fixture_postconditions"
        revisions = getattr(server, "fixture_history_revisions", 0)
        if (controller.calls != [("doctor", {})] or controller.prepared or server.application.state()["job"] is not None
                or revisions != (1 if case == "filters" else 0)):
            raise VerificationError(stage)
        result["assertions"].update(fake_controller_only_local_doctor=True, no_prepared_account_task=True,
                                     background_job_absent=True, real_account_calls_zero=True)
        result["metrics"].update(fake_account_calls=0, fake_writes=0, real_account_calls=0,
                                 fixture_history_revisions=revisions, server_process_id=os.getpid())
        if case == "filters":
            for label, path in (("wide", wide), ("mobile", mobile)):
                raw = path.read_bytes()
                if not raw.startswith(b"\x89PNG\r\n\x1a\n") or not 32 <= len(raw) <= 16 * 1024 * 1024:
                    raise VerificationError("screenshot")
                screenshots[label] = raw
        require_frontend_fresh(ASSETS, index_hash, assets_hash)
    except (Exception, KeyboardInterrupt) as error:
        failure = error.stage if isinstance(error, VerificationError) else stage
    finally:
        try:
            if cli is not None:
                cleanup_ok = _cleanup_browser(cli) and cleanup_ok
        except (Exception, KeyboardInterrupt):
            cleanup_ok = False
        finally:
            try:
                if server is not None:
                    if not shutdown_server(server, timeout=5):
                        cleanup_ok = False
                        server.shutdown()
                        server.server_close()
                if thread is not None:
                    thread.join(3)
                    cleanup_ok = not thread.is_alive() and cleanup_ok
            except (Exception, KeyboardInterrupt):
                cleanup_ok = False
            finally:
                try:
                    fixture.doCleanups()
                    cleanup_ok = not getattr(fixture, "project", PROJECT).exists() and cleanup_ok
                except (Exception, KeyboardInterrupt):
                    cleanup_ok = False
    if not cleanup_ok:
        raise VerificationError("cleanup")
    if failure is not None or result is None:
        raise VerificationError(failure or "verification")
    result["assertions"]["owned_browser_server_and_temporary_directory_cleaned"] = True
    return result, screenshots


def main():
    parser = argparse.ArgumentParser(description="仅对临时模拟分类资料运行真实浏览器验收。")
    parser.add_argument("--browser", choices=("msedge", "chrome"), default="msedge")
    parser.add_argument("--cli-path")
    parser.add_argument("--timeout", type=int, default=180)
    options = parser.parse_args()
    started, run_id = time.monotonic(), secrets.token_hex(16)
    report = {"kind": "classification_workbench_fake_browser_verification", "version": 2,
              "run_id": run_id, "started_at_ns": time.time_ns(), "status": "FAIL", "verified": False,
              "real_account_calls": 0, "cases": []}
    stage = "configuration"
    try:
        # A new run invalidates an old PASS before any browser can be opened.
        write_report(REPORT, {**report, "failed_stage": "in_progress"})
        if not 60 <= options.timeout <= 300:
            raise VerificationError(stage)
        node = shutil.which("node")
        if node is None:
            raise VerificationError("node")
        entry, versions = locate_cli(options.cli_path)
        snapshot, index_hash, assets_hash = frontend_snapshot(ASSETS)
        report.update(frontend_index_sha256=index_hash, frontend_assets_sha256=assets_hash,
                      tested_versions={**versions, "browser": options.browser})
        screens = {}
        for case in _ASSERTIONS:
            stage = case
            result, captured = run_case(case, node, entry, snapshot, run_id, index_hash, assets_hash,
                                        started + options.timeout, options.browser)
            report["cases"].append(result)
            screens.update(captured)
        require_frontend_fresh(ASSETS, index_hash, assets_hash)
        stage = "screenshots"
        for label, filename in (("wide", "现代前端-分类成果.png"), ("mobile", "现代前端-分类成果-窄屏.png")):
            path = REPORT.parent / filename
            path.parent.mkdir(parents=True, exist_ok=True)
            raw = screens[label]
            path.write_bytes(raw)
            report.setdefault("screenshots", {})[label] = {"path": str(path), "sha256": hashlib.sha256(raw).hexdigest()}
        report.update(status="PASS", verified=True, elapsed_seconds=round(time.monotonic() - started, 2),
                      assertions={"all_browser_steps_verified": True, "all_owned_resources_cleaned": True,
                                  "frontend_assets_fresh": True, "real_account_calls_zero": True})
    except (Exception, KeyboardInterrupt) as error:
        report["failed_stage"] = error.stage if isinstance(error, VerificationError) else stage
    report["finished_at_ns"] = time.time_ns()
    try:
        write_report(REPORT, report)
    except (Exception, KeyboardInterrupt):
        print("FAIL report_save")
        return 1
    print("PASS classification_workbench" if report["verified"] else "FAIL " + report["failed_stage"])
    return 0 if report["verified"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
