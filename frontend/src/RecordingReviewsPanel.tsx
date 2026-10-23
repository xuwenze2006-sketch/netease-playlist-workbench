import { useEffect, useId, useRef, useState } from 'react';
import { fetchRecordingReview, fetchRecordingReviews, readSession } from './api';
import { isSceneComplete } from './recordingReviewData';
import type { RecordingReviewCatalog, RecordingReviewDetail, RecordingReviewRow } from './recordingReviewData';
import { useSearchInput } from './useSearchInput';
import './recordingReviews.css';

type Filter = 'all' | 'unfinished' | 'changed';
const FILTERS: { value: Filter; label: string }[] = [
  { value: 'all', label: '全部曲目' }, { value: 'unfinished', label: '场景待核验' },
  { value: 'changed', label: '有收录变化' },
];
const RECORDING_STATUS = { unresolved: '录音版本待确认', metadata_matched: '目录信息已匹配',
  audio_verified: '有试听记录（保存声明）' };
const SOURCE_KIND = { catalog: '目录', lyrics: '歌词', audio: '音频' };
const DECISION = { suitable: '适合收录', unsuitable: '不适合收录', uncertain: '待核验' };

function secondsText(value: number) {
  const minutes = Math.floor(value / 60);
  const seconds = value % 60;
  return `${minutes}:${seconds < 10 ? '0' : ''}${Number(seconds.toFixed(6))}`;
}
function sceneText(scenes: string[]) { return scenes.length ? scenes.join('、') : '无'; }
function dateText(value: string) { return new Date(value).toLocaleString('zh-CN', { hour12: false }); }
function normalizedSearch(value: string) { return value.normalize('NFKC').toLowerCase().replace(/ς/g, 'σ'); }
function failureText(error: unknown) {
  return error instanceof Error && error.message.trim()
    ? Array.from(error.message.trim()).slice(0, 500).join('') : '请重试读取。';
}

function ReviewCard({ row }: { row: RecordingReviewRow }) {
  const id = useId();
  const scope = [...new Set([...row.before_scenes, ...row.scene_decisions.map((item) => item.scene)])];
  return <article className="recording-review-card" aria-labelledby={`${id}-title`}>
    <header>
      <div><p className="recording-review-position">第 {row.position} 首 · {row.artists}</p>
        <h3 id={`${id}-title`}>{row.name}</h3></div>
      <span className={`recording-review-state ${isSceneComplete(row) ? 'complete' : ''}`}>
        {isSceneComplete(row) ? '涉及场景已填判断' : '场景待核验'}
      </span>
    </header>
    <p className="recording-review-recording-status">{RECORDING_STATUS[row.recording.status]}</p>
    <p>录音版本：{row.recording.version || '未确认'}</p>
    <dl className="recording-review-memberships">
      <dt>原收录</dt><dd>{sceneText(row.before_scenes)}</dd>
      <dt>预览收录</dt><dd>{sceneText(row.after_scenes)}</dd>
      <dt>拟新增</dt><dd className={row.added_scenes.length ? 'added' : ''}>{sceneText(row.added_scenes)}</dd>
      <dt>拟移除</dt><dd className={row.removed_scenes.length ? 'removed' : ''}>{sceneText(row.removed_scenes)}</dd>
    </dl>
    <section className="recording-review-decisions" aria-label="各场景判断">
      {scope.length ? scope.map((scene) => {
        const decision = row.scene_decisions.find((item) => item.scene === scene);
        return <div className="recording-review-decision" key={scene}>
          <p><strong>{scene}</strong><span>{DECISION[decision?.decision ?? 'uncertain']}</span></p>
          <p>{decision ? (decision.reason || '未填写判断理由。') : '未记录该场景的判断，保留原收录。'}</p>
        </div>;
      }) : <p>尚无场景判断，仍待核验。</p>}
    </section>
    <details className="recording-review-evidence">
      <summary>来源、试听区间与备注</summary>
      <h4>核验备注</h4><p>{row.recording.note || '未填写核验备注。'}</p>
      <h4>依据来源</h4>
      {row.recording.evidence_sources.length ? <ul>
        {row.recording.evidence_sources.map((source, index) => <li key={`${source.url}-${index}`}>
          <span>{SOURCE_KIND[source.kind]} · </span>
          <a href={source.url} target="_blank" rel="noopener noreferrer">{source.title}</a>
          <p>{source.note || '未填写来源说明。'}</p>
        </li>)}
      </ul> : <p>未记录依据来源。</p>}
      <h4>试听区间（保存声明）</h4>
      {row.recording.listened_segments.length ? <ul>
        {row.recording.listened_segments.map((segment, index) => <li key={index}>
          <a href={segment.source_url} target="_blank" rel="noopener noreferrer">
            {secondsText(segment.start_seconds)}–{secondsText(segment.end_seconds)}
          </a><p>{segment.note || '未填写区间说明。'}</p>
        </li>)}
      </ul> : <p>未记录试听区间。</p>}
    </details>
  </article>;
}

export function RecordingReviewsPanel({ active = true }: { active?: boolean }) {
  const id = useId();
  const [catalog, setCatalog] = useState<RecordingReviewCatalog | null>(null);
  const [catalogLoading, setCatalogLoading] = useState(false);
  const [catalogError, setCatalogError] = useState('');
  const catalogFailed = !!catalogError;
  const [selected, setSelected] = useState('');
  const selectedRef = useRef(selected);
  selectedRef.current = selected;
  const [detail, setDetail] = useState<RecordingReviewDetail | null>(null);
  const [detailLoading, setDetailLoading] = useState(false);
  const [detailError, setDetailError] = useState('');
  const detailFailed = !!detailError;
  const [refresh, setRefresh] = useState(0);
  const [detailRetry, setDetailRetry] = useState(0);
  const [notice, setNotice] = useState('');
  const [filter, setFilter] = useState<Filter>('all');
  const { value: queryInput, query, setValue: setQuery, ...searchHandlers } = useSearchInput();

  useEffect(() => {
    if (!active) return;
    const controller = new AbortController();
    let disposed = false;
    setCatalogLoading(true);
    setCatalogError('');
    void (async () => {
      try {
        const result = await fetchRecordingReviews(readSession(), controller.signal);
        if (disposed) return;
        setCatalog(result);
        if (selectedRef.current && !result.bundles.some((bundle) => bundle.name === selectedRef.current)) {
          setSelected('');
          setDetail(null);
          setDetailLoading(false);
          setDetailError('');
          setNotice('所选核验包已不在列表中，请重新选择。');
        }
      } catch (error) {
        if (!disposed) setCatalogError(failureText(error));
      } finally {
        if (!disposed) setCatalogLoading(false);
      }
    })();
    return () => { disposed = true; controller.abort(); };
  }, [active, refresh]);

  useEffect(() => {
    if (!active || !selected) return;
    const controller = new AbortController();
    let disposed = false;
    setDetailLoading(true);
    setDetailError('');
    void (async () => {
      try {
        const result = await fetchRecordingReview(readSession(), selected, controller.signal);
        if (!disposed) setDetail(result);
      } catch (error) {
        if (!disposed) setDetailError(failureText(error));
      } finally {
        if (!disposed) setDetailLoading(false);
      }
    })();
    return () => { disposed = true; controller.abort(); };
  }, [active, selected, refresh, detailRetry]);

  function selectBundle(bundle: string) {
    setSelected(bundle);
    setDetail(null);
    setDetailLoading(!!bundle);
    setDetailError('');
    setNotice('');
    setQuery('');
    setFilter('all');
  }
  function refreshAll() {
    setCatalogLoading(true);
    setDetailLoading(!!selected);
    setRefresh((value) => value + 1);
  }
  const current = detail?.bundle === selected ? detail : null;
  const preview = current?.preview;
  const stale = !!current && (detailLoading || detailFailed || !active);
  const normalizedQuery = normalizedSearch(query.trim());
  const rows = preview?.records.filter((row) =>
    (!normalizedQuery || normalizedSearch(`${row.name}\n${row.artists}`).includes(normalizedQuery)) &&
    (filter === 'all' || (filter === 'unfinished' ? !isSceneComplete(row) :
      !!(row.added_scenes.length || row.removed_scenes.length)))) ?? [];
  const summary = preview?.summary;
  const hasFilters = !!queryInput || filter !== 'all';

  return <div className="recording-reviews-panel" hidden={!active}>
    <div className="page-heading">
      <div><span className="eyebrow">RECORDING REVIEWS</span>
        <h1 id={active ? 'page-title' : `${id}-inactive-title`}>录音核验</h1>
        <p>查看保存的录音依据与场景收录变化。</p></div>
      <button type="button" className="button secondary" disabled={catalogLoading || detailLoading} onClick={refreshAll}>
        刷新核验记录
      </button>
    </div>
    <div className="recording-review-scope">
      <p>只读预览 · 各核验包独立展示，未自动合并，未改歌单。</p>
      <p>目录匹配不能代替试听；“有试听记录”是文件内保存的声明，本程序未独立验证试听是否完成。</p>
      <p>场景判断仅覆盖原收录及本包提及的场景；缺少判断时保留原收录。</p>
    </div>
    <section className="recording-review-selector" aria-label="选择核验包">
      <label htmlFor={`${id}-bundle`}>核验包</label>
      <select id={`${id}-bundle`} value={selected} disabled={catalogLoading || catalogFailed || !catalog?.bundles.length}
        onChange={(event) => selectBundle(event.currentTarget.value)}>
        <option value="">请选择核验包</option>
        {catalog?.bundles.map((bundle) => <option value={bundle.name} key={bundle.name}>{bundle.name}</option>)}
      </select>
      {catalogLoading && <p role="status">正在读取核验包列表…</p>}
      {catalog && (catalogLoading || catalogFailed) && <p className="recording-review-warning">上次读取的列表，仅供参考。</p>}
      {catalogFailed && <div className="recording-review-error" role="alert">
        <p>核验包列表读取失败，无法确认当前保存情况。</p>
        <p>{catalogError}</p>
        <button className="button secondary" type="button" onClick={refreshAll} disabled={catalogLoading}>重试读取列表</button>
      </div>}
      {catalog?.status === 'empty' && !catalogLoading && !catalogFailed && <p>尚无已保存的核验包。</p>}
      {notice && <p role="status">{notice}</p>}
    </section>
    {selected && <section className="recording-review-results" aria-label="核验结果" aria-busy={detailLoading}>
      {detailLoading && <p role="status">正在读取所选核验包…</p>}
      {detailFailed && <div className="recording-review-error" role="alert">
        <p>核验包读取失败，可能是文件损坏、来源报告已变化或服务暂不可用。来源不符时不能使用该包。</p>
        <p>{detailError}</p>
        <button className="button secondary" type="button" disabled={detailLoading}
          onClick={() => { setDetailLoading(true); setDetailRetry((value) => value + 1); }}>重试读取核验包</button>
      </div>}
      {stale && <p className="recording-review-warning" role="status">以下为上次读取的核验内容，仅供参考，不能作为当前核验结果。</p>}
      {current && summary && <>
        <div className="recording-review-bundle-heading"><h2>{current.bundle}</h2>
          <p>记录时间：{dateText(current.created_at)}</p></div>
        <dl className="recording-review-summary" aria-label="核验包汇总">
          {[
            ['核验曲目', summary.reviewed_records], ['目录匹配', summary.metadata_matched_records],
            ['有试听记录', summary.audio_verified_records], ['涉及场景已填判断', summary.scene_verified_records],
            ['场景待核验', summary.uncertain_records], ['部分场景已填判断', summary.partial_scene_records],
            ['拟新增', summary.added_count], ['拟移除', summary.removed_count],
          ].map(([label, count]) => <div key={label}><dt>{label}</dt><dd>{count}</dd></div>)}
        </dl>
        <div className="recording-review-search">
          <label htmlFor={`${id}-search`}>搜索歌名或歌手</label>
          <input id={`${id}-search`} type="search" value={queryInput} {...searchHandlers} placeholder="在当前核验包内搜索" />
        </div>
        <div className="recording-review-filters" role="group" aria-label="核验结果筛选">
          {FILTERS.map((item) => <button type="button" key={item.value} aria-pressed={filter === item.value}
            onClick={() => setFilter(item.value)}>{item.label}</button>)}
          {hasFilters && <button type="button" onClick={() => { setQuery(''); setFilter('all'); }}>清除筛选</button>}
        </div>
        <p className="recording-review-result-count" role="status">显示 {rows.length} / {summary.reviewed_records} 首</p>
        {rows.length ? rows.map((row) => <ReviewCard row={row} key={`${current.bundle}-${row.position}`} />) :
          <p className="recording-review-empty">没有符合当前筛选的曲目，可清除筛选后查看全部。</p>}
      </>}
    </section>}
  </div>;
}
