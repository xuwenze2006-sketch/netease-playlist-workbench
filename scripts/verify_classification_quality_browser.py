"""Browser acceptance of review filters and local corrections on owned fake data.

Uses the installed Playwright CLI and the real local HTTP API. No package or
browser installation, real launcher, account credentials, or account task runs.
PASS is written only after evidence checks and cleanup of owned resources.
"""

import argparse
import hashlib
import json
from pathlib import Path
import secrets
import shutil
import sys
import threading
import time

PROJECT = Path(__file__).resolve().parents[1]
if str(PROJECT) not in sys.path:
    sys.path.insert(0, str(PROJECT))

from scripts.verify_web import Cli, VerificationError, _json, locate_cli, write_report
from scripts.verify_restart import _cleanup_browser, frontend_snapshot, require_frontend_fresh
from netease_organizer.web_server import create_server, shutdown_server
from netease_organizer.web_state import build_local_state
from tests.test_web_classification import WebClassificationTests
from tests.test_web_server import FakeController

ASSETS = PROJECT / "frontend" / "dist"
OUTPUT = PROJECT / "artifacts" / "classification-quality-20261007"
REPORT = OUTPUT / "browser-verification.json"
ASSERTIONS = (
    "review_reason_visible_without_pending", "conflict_filter", "low_confidence_filter",
    "local_correction_saved", "baseline_and_draft_separate", "playlist_changes_preview",
    "reload_keeps_reason_and_recording_note", "narrow_editor_no_horizontal_overflow",
    "wide_no_horizontal_overflow", "correction_reverted", "no_account_job",
    "zero_account_action_posts", "exactly_two_draft_posts", "zero_external_requests",
    "served_assets_match", "no_browser_storage_state", "version_hint_filter",
    "draft_basis_tag_filter", "per_song_added_preview", "per_song_removed_preview",
)
STAGES = frozenset(("startup", "review", "conflict", "low_score", "editing", "save",
                    "preview", "version", "basis", "changes", "reload", "narrow", "wide", "revert", "proof"))


def source_hashes(project):
    names = ("全库分类-逐曲结果.json", "分类整理计划.json", "在线名称整理快照.json")
    return {name: hashlib.sha256((project / "artifacts" / name).read_bytes()).hexdigest() for name in names}


def browser_script(url, run_id, assets_hash, manifest, wide, narrow):
    source = r'''async page => {
  const base = __URL__, runId = __RUN__, assetsHash = __HASH__, manifest = __MANIFEST__;
  const wide = __WIDE__, narrow = __NARROW__, assertions = {};
  let stage = 'startup', draftPosts = 0, actionPosts = 0, externalRequests = 0;
  const require = condition => { if (!condition) throw new Error('acceptance'); };
  const origin = new URL(base).origin;
  const deadline = Date.now() + 55000;
  page.setDefaultTimeout(7000);
  page.on('request', request => {
    const url = new URL(request.url());
    if (url.origin !== origin && !['data:', 'about:'].includes(url.protocol)) externalRequests += 1;
    if (url.pathname === '/api/actions' && request.method() === 'POST') actionPosts += 1;
    if (url.pathname === '/api/classification/draft' && request.method() === 'POST') draftPosts += 1;
  });
  // Real local requests continue unchanged; external requests cannot reach an account.
  await page.route('**/*', route => {
    const url = new URL(route.request().url());
    return url.origin === origin || url.protocol === 'data:' ? route.continue() : route.abort();
  });
  const records = () => page.getByRole('region', {name: '逐曲分类结果', exact: true});
  const first = () => records().getByRole('article').filter({has: page.getByRole('heading', {name: 'Rain · 雨の音', exact: true})});
  const review = () => records().getByRole('combobox', {name: '复核筛选', exact: true});
  const editor = () => first().getByRole('group', {name: '修正Rain · 雨の音', exact: true});
  const waitPositions = async expected => {
    while (Date.now() < deadline) {
      const actual = await records().getByRole('article').evaluateAll(rows => rows.map(row =>
        Number(row.querySelector('.classification-position')?.textContent?.trim())));
      if (await records().getAttribute('aria-busy') === 'false' && JSON.stringify(actual) === JSON.stringify(expected)) return;
      await page.waitForTimeout(50);
    }
    throw new Error('positions timeout');
  };
  const read = async path => page.evaluate(async path => {
    const session = document.querySelector('meta[name="organizer-session"]')?.getAttribute('content');
    const response = await fetch(path, {headers: {'X-Organizer-Session': session}, cache: 'no-store'});
    if (!response.ok) throw new Error('local read');
    return await response.json();
  }, path);
  const noOverflow = async () => {
    await page.evaluate(() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve))));
    require(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1 && document.body.scrollWidth <= innerWidth + 1));
  };
  const reason = '试听确认该专辑录音强度平稳，更适合睡前放松';
  const version = '模拟验收：专辑录音版本，已排除现场与混音';
  try {
    await page.setViewportSize({width: 1440, height: 1000});
    await page.goto(base + '#classification', {waitUntil: 'domcontentloaded'});
    await page.getByRole('heading', {name: '分类成果', level: 1, exact: true}).waitFor();
    await waitPositions([1, 2, 3, 4]);
    stage = 'review';
    require(await first().locator('.classification-review-reasons').innerText() !== '' &&
      (await read('/api/classification?limit=50')).records[0].pending_reasons.length === 0);
    require((await first().locator('.classification-score').innerText()).includes('未经校准'));
    assertions.review_reason_visible_without_pending = true;
    stage = 'conflict';
    await review().selectOption('conflict'); await waitPositions([1]);
    assertions.conflict_filter = true;
    stage = 'low_score';
    await review().selectOption('low_confidence'); await waitPositions([2]);
    require((await records().getByRole('article').locator('.classification-score').innerText()).includes('0.65'));
    assertions.low_confidence_filter = true;
    stage = 'version';
    await review().selectOption('version'); await waitPositions([2]);
    require((await records().innerText()).includes('加速版本'));
    assertions.version_hint_filter = true;
    await review().selectOption('all'); await waitPositions([1, 2, 3, 4]);
    stage = 'editing';
    await first().getByRole('button', {name: '修正分类', exact: true}).click();
    await editor().getByRole('checkbox', {name: '通勤散步', exact: true}).uncheck();
    await editor().getByRole('checkbox', {name: '放松睡前', exact: true}).check();
    await editor().getByRole('textbox', {name: '修正理由', exact: true}).fill(reason);
    await editor().getByRole('textbox', {name: '录音版本依据', exact: true}).fill(version);
    stage = 'save';
    await editor().getByRole('button', {name: '保存本地修正', exact: true}).click();
    await page.getByRole('region', {name: '分类质量与修正草稿', exact: true}).getByText('本地修正 · 1 首', {exact: true}).waitFor();
    await first().getByText(reason, {exact: true}).waitFor();
    let data = await read('/api/classification?limit=50');
    require(data.quality.revision === 1 && data.quality.changed_count === 1 &&
      JSON.stringify(data.records[0].scenes) === '["通勤散步"]' &&
      JSON.stringify(data.records[0].draft.scenes) === '["放松睡前"]' && data.records[0].draft.reason === reason &&
      data.records[0].draft.recording_note === version);
    assertions.local_correction_saved = true;
    require((await first().getByLabel('本地修正前后对照', {exact: true}).innerText()).includes('通勤散步 → 放松睡前'));
    assertions.baseline_and_draft_separate = true;
    stage = 'preview';
    const quality = page.getByRole('region', {name: '分类质量与修正草稿', exact: true});
    await quality.locator('summary').filter({hasText: '查看歌单增删预览'}).click();
    require(data.quality.playlist_changes.length === 2 && data.quality.playlist_changes.some(change =>
      change.name === '场景 · 通勤散步' && change.removed_count === 1 && change.added_count === 0) &&
      data.quality.playlist_changes.some(change => change.name === '场景 · 放松睡前' && change.added_count === 1 && change.removed_count === 0));
    await quality.getByText('移出 1 首', {exact: true}).waitFor();
    await quality.getByText('加入 1 首', {exact: true}).waitFor();
    assertions.playlist_changes_preview = true;
    stage = 'changes';
    await quality.getByRole('button', {name: '逐曲核对场景 · 放松睡前', exact: true}).click();
    let changes = page.getByRole('region', {name: '歌单变动：场景 · 放松睡前', exact: true});
    await changes.getByText(reason, {exact: true}).waitFor();
    require((await changes.innerText()).includes('Rain · 雨の音') && (await changes.innerText()).includes(version));
    await changes.getByRole('combobox', {name: '变动方向', exact: true}).selectOption('added');
    await changes.getByText(reason, {exact: true}).waitFor();
    assertions.per_song_added_preview = true;
    await quality.getByRole('button', {name: '逐曲核对场景 · 通勤散步', exact: true}).click();
    changes = page.getByRole('region', {name: '歌单变动：场景 · 通勤散步', exact: true});
    await changes.getByRole('combobox', {name: '变动方向', exact: true}).selectOption('removed');
    await changes.getByText(reason, {exact: true}).waitFor();
    require((await changes.innerText()).includes('Rain · 雨の音'));
    assertions.per_song_removed_preview = true;
    stage = 'basis';
    await records().getByRole('combobox', {name: '分类依据', exact: true}).selectOption('draft');
    await waitPositions([1, 2, 3, 4]);
    await first().getByRole('button', {name: '场景 · 放松睡前', exact: true}).click();
    await waitPositions([1]);
    require((await read('/api/classification?basis=draft&dimension=scene&tag=' + encodeURIComponent('放松睡前'))).pagination.total === 1);
    assertions.draft_basis_tag_filter = true;
    stage = 'wide';
    await noOverflow(); assertions.wide_no_horizontal_overflow = true;
    await page.screenshot({path: wide, fullPage: true});
    stage = 'reload';
    await page.reload(); await waitPositions([1, 2, 3, 4]);
    await first().getByText(reason, {exact: true}).waitFor();
    await first().getByText('录音版本依据：' + version, {exact: true}).waitFor();
    require(draftPosts === 1);
    assertions.reload_keeps_reason_and_recording_note = true;
    stage = 'narrow';
    await page.setViewportSize({width: 390, height: 844});
    await first().getByRole('button', {name: '编辑本地修正', exact: true}).click();
    require(await editor().getByRole('textbox', {name: '修正理由', exact: true}).inputValue() === reason);
    await noOverflow(); assertions.narrow_editor_no_horizontal_overflow = true;
    await editor().scrollIntoViewIfNeeded();
    await page.screenshot({path: narrow, fullPage: true});
    stage = 'revert';
    await first().getByRole('button', {name: '撤销本地修正', exact: true}).click();
    await quality.getByText('本地修正 · 0 首', {exact: true}).waitFor();
    await first().getByRole('button', {name: '修正分类', exact: true}).waitFor();
    data = await read('/api/classification?limit=50');
    require(data.quality.revision === 2 && data.quality.changed_count === 0 && data.records[0].draft === null &&
      data.quality.playlist_changes.length === 0 && JSON.stringify(data.records[0].scenes) === '["通勤散步"]');
    assertions.correction_reverted = true;
    stage = 'proof';
    require((await read('/api/state')).job === null); assertions.no_account_job = true;
    require(actionPosts === 0); assertions.zero_account_action_posts = true;
    require(draftPosts === 2); assertions.exactly_two_draft_posts = true;
    require(externalRequests === 0); assertions.zero_external_requests = true;
    require(await page.evaluate(() => localStorage.length === 0 && sessionStorage.length === 0));
    assertions.no_browser_storage_state = true;
    require(await page.evaluate(async manifest => {
      for (const [relative, expected] of Object.entries(manifest)) {
        const response = await fetch('/' + relative, {cache: 'no-store'});
        if (!response.ok) return false;
        const bytes = await response.arrayBuffer();
        const hash = Array.from(new Uint8Array(await crypto.subtle.digest('SHA-256', bytes)), value => value.toString(16).padStart(2, '0')).join('');
        if (hash !== expected) return false;
      }
      return true;
    }, manifest));
    assertions.served_assets_match = true;
    return {kind: 'classification_quality_browser_observation', verified: true, run_id: runId,
      frontend_assets_sha256: assetsHash, assertions,
      viewports: [{width:1440,height:1000},{width:390,height:844}],
      metrics: {draft_posts: draftPosts, account_action_posts: actionPosts, external_requests: externalRequests}};
  } catch { return {kind:'classification_quality_browser_observation', verified:false, failed_stage:stage}; }
}'''
    replacements = {"__URL__": url, "__RUN__": run_id, "__HASH__": assets_hash,
                    "__MANIFEST__": manifest, "__WIDE__": str(wide), "__NARROW__": str(narrow)}
    for marker, value in replacements.items():
        source = source.replace(marker, json.dumps(value, ensure_ascii=False))
    return source


def check_observation(stdout, run_id, assets_hash):
    outer = _json(stdout, "browser_callback")
    value = outer.get("result")
    observed = _json(value, "browser_callback") if isinstance(value, str) else value
    if outer.get("isError") is True or not isinstance(observed, dict) or observed.get("verified") is not True:
        stage = observed.get("failed_stage") if isinstance(observed, dict) else None
        raise VerificationError(stage if stage in STAGES else "browser_callback")
    assertions = observed.get("assertions")
    metrics = observed.get("metrics")
    if (observed.get("kind") != "classification_quality_browser_observation" or observed.get("run_id") != run_id
            or observed.get("frontend_assets_sha256") != assets_hash
            or observed.get("viewports") != [{"width": 1440, "height": 1000}, {"width": 390, "height": 844}]
            or not isinstance(assertions, dict) or any(assertions.get(name) is not True for name in ASSERTIONS)
            or metrics != {"draft_posts": 2, "account_action_posts": 0, "external_requests": 0}):
        raise VerificationError("browser_callback")
    return {"verified": True, "assertions": {name: True for name in ASSERTIONS},
            "viewports": observed["viewports"], "metrics": metrics}


def run_browser(node, entry, snapshot, index_hash, assets_hash, run_id, deadline, browser):
    fixture = WebClassificationTests(methodName="runTest")
    server = thread = cli = None
    result = failure = None
    cleanup_ok = True
    screens = {}
    stage = "fixture"
    try:
        fixture.setUp()
        fixture.report["records"][0]["review_note"] = True
        fixture.report["records"][1]["style_judgment_score"] = .65
        fixture.report["records"][1]["name"] = '喜欢 (Speed Up Version)'
        fixture.write("全库分类-逐曲结果.json", fixture.report, 101)
        before = source_hashes(fixture.project)
        controller = FakeController(fixture.project)
        server = create_server(controller, assets=ASSETS, asset_snapshot=snapshot,
                               state_provider=lambda: build_local_state(fixture.project))
        thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": .01}, daemon=True)
        thread.start()
        workspace = fixture.project / "owned-browser"
        workspace.mkdir()
        cli = Cli(node, entry, "classification-quality-" + secrets.token_hex(10), workspace, deadline)
        stage = cli.stage = "browser_open"
        cli.command("open", "about:blank", "--browser=" + browser, "--idle-timeout=60000", timeout=25)
        wide, narrow = workspace / "quality-wide.png", workspace / "quality-narrow.png"
        manifest = {name: hashlib.sha256(raw).hexdigest() for name, raw in snapshot.items() if name != "index.html"}
        callback = workspace / "quality-callback.js"
        callback.write_text(browser_script(f"http://127.0.0.1:{server.server_address[1]}/", run_id,
                                          assets_hash, manifest, wide, narrow), encoding="utf-8")
        stage = cli.stage = "browser_callback"
        result = check_observation(cli.command("run-code", "--filename=" + str(callback), timeout=65), run_id, assets_hash)
        stage = "fixture_postconditions"
        after = source_hashes(fixture.project)
        if (before != after or controller.calls != [("doctor", {})] or controller.prepared
                or server.application.state()["job"] is not None):
            raise VerificationError(stage)
        result["assertions"].update(source_fixture_hashes_unchanged=True, fake_controller_only_doctor=True,
                                     no_prepared_account_task=True, real_account_calls_zero=True)
        result["source_fixture_sha256"] = after
        result["metrics"].update(fake_account_calls=0, real_account_calls=0)
        for label, path in (("wide", wide), ("narrow", narrow)):
            raw = path.read_bytes()
            if not raw.startswith(b"\x89PNG\r\n\x1a\n") or not 32 <= len(raw) <= 16 * 1024 * 1024:
                raise VerificationError("screenshot")
            screens[label] = raw
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
                    workspace_path = getattr(fixture, "project", None)
                    if workspace_path is None or workspace_path == PROJECT or workspace_path.is_relative_to(PROJECT):
                        cleanup_ok = False
                    else:
                        fixture.doCleanups()
                        cleanup_ok = not workspace_path.exists() and cleanup_ok
                except (Exception, KeyboardInterrupt):
                    cleanup_ok = False
    if not cleanup_ok:
        raise VerificationError("cleanup")
    if failure is not None or result is None:
        raise VerificationError(failure or "verification")
    result["assertions"]["owned_browser_server_and_temporary_directory_cleaned"] = True
    return result, screens


def main():
    parser = argparse.ArgumentParser(description="对临时分类数据验证复核与本地修正真实浏览器流程。")
    parser.add_argument("--browser", choices=("msedge", "chrome"), default="msedge")
    parser.add_argument("--cli-path")
    parser.add_argument("--timeout", type=int, default=120)
    parser.add_argument("--output-dir", type=Path, default=OUTPUT,
                        help="本轮模拟验收报告与截图目录，可指定新目录保留既有证据。")
    options = parser.parse_args()
    output = options.output_dir.resolve()
    report_path = output / REPORT.name
    started, run_id = time.monotonic(), secrets.token_hex(16)
    report = {"kind": "classification_quality_fake_browser_verification", "version": 1,
              "run_id": run_id, "started_at_ns": time.time_ns(), "status": "FAIL", "verified": False,
              "real_account_calls": 0}
    stage = "configuration"
    try:
        write_report(report_path, {**report, "failed_stage": "in_progress"})
        if not 60 <= options.timeout <= 300:
            raise VerificationError(stage)
        node = shutil.which("node")
        if node is None:
            raise VerificationError("node")
        entry, versions = locate_cli(options.cli_path)
        snapshot, index_hash, assets_hash = frontend_snapshot(ASSETS)
        report.update(frontend_index_sha256=index_hash, frontend_assets_sha256=assets_hash,
                      tested_versions={**versions, "browser": options.browser, "headless": True})
        result, screens = run_browser(node, entry, snapshot, index_hash, assets_hash, run_id,
                                     started + options.timeout, options.browser)
        report.update(result)
        require_frontend_fresh(ASSETS, index_hash, assets_hash)
        stage = "screenshots"
        for label in ("wide", "narrow"):
            path = output / f"classification-quality-{label}.png"
            path.write_bytes(screens[label])
            report.setdefault("screenshots", {})[label] = {"path": str(path), "sha256": hashlib.sha256(screens[label]).hexdigest()}
        report.update(status="PASS", verified=True, elapsed_seconds=round(time.monotonic() - started, 2))
    except (Exception, KeyboardInterrupt) as error:
        report.update(status="FAIL", verified=False, failed_stage=error.stage if isinstance(error, VerificationError) else stage)
    report["finished_at_ns"] = time.time_ns()
    try:
        write_report(report_path, report)
    except (Exception, KeyboardInterrupt):
        print("FAIL report_save")
        return 1
    print("PASS classification_quality_browser" if report["verified"] else "FAIL " + report["failed_stage"])
    return 0 if report["verified"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
