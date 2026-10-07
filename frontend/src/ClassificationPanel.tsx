import { useEffect, useId, useRef, useState } from 'react';
import type { KeyboardEvent } from 'react';
import { fetchClassification, postClassificationDraft, readSession } from './api';
import { ClassificationEditor, ClassificationQualitySummary } from './ClassificationEditor';
import { REVIEW_LABELS } from './classificationQuality';
import { Icon } from './icons';
import type { ClassificationDimension, ClassificationDraftRequest, ClassificationPage } from './types';
import './classification.css';

type Selection = ClassificationPage['filters'] & { offset: number };
type Dimension = Exclude<ClassificationDimension, 'all'>;
type View = 'songs' | 'playlists';
const INITIAL: Selection = { dimension: 'all', tag: '', review: 'all', query: '', offset: 0 };
const DIMENSIONS = { scene: '场景', style: '风格', language: '语言', review: '待辨识' };
function dateText(value: string | null) {
  if (!value) return '未记录';
  const date = new Date(value);
  return Number.isFinite(date.valueOf())
    ? date.toLocaleString('zh-CN', { hour12: false })
    : '未记录';
}

export function ClassificationPanel({ active = true }: { active?: boolean }) {
  const [input, setInput] = useState('');
  const [selection, setSelection] = useState<Selection>(INITIAL);
  const [view, setView] = useState<View>('songs');
  const [saved, setSaved] = useState<{ page: ClassificationPage; selection: Selection } | null>(
    null,
  );
  const [loading, setLoading] = useState(true);
  const [failed, setFailed] = useState(false);
  const [retry, setRetry] = useState(0);
  const [pageNotice, setPageNotice] = useState('');
  const [draftSubmitting, setDraftSubmitting] = useState(false);
  const mutationLock = useRef(false);
  const generation = useRef(0);
  const refreshRequested = useRef(false);
  const resultsHeading = useRef<HTMLHeadingElement>(null);
  const songTab = useRef<HTMLButtonElement>(null);
  const playlistTab = useRef<HTMLButtonElement>(null);
  const pageIntent = useRef<{
    selection: Selection;
    source: HTMLButtonElement;
    focus: boolean;
    generation: number | null;
  } | null>(null);
  const id = useId();
  const searching = input.trim() !== selection.query;
  const waiting = loading || searching;
  const data = saved?.page ?? null;
  const currentData = saved?.selection === selection ? data : null;
  const available = currentData?.status === 'available';
  const summary = data?.status === 'available' ? data.summary : null;
  const stale = !!data && (failed || waiting);
  const draftBasis = selection.basis === 'draft';
  const applyingDraft = draftBasis && data?.quality?.draft_status === 'ready';
  const draftCounts = applyingDraft ? data?.quality?.draft_summary : null;

  useEffect(() => {
    if (!active) {
      pageIntent.current = null;
      return;
    }
    const query = input.trim();
    if (query === selection.query) return;
    const timer = window.setTimeout(
      () => setSelection((value) => ({ ...value, query, offset: 0 })),
      200,
    );
    return () => window.clearTimeout(timer);
  }, [active, input, selection.query]);

  useEffect(() => {
    if (!active || searching) return;
    const controller = new AbortController();
    const current = ++generation.current;
    const refresh = refreshRequested.current;
    refreshRequested.current = false;
    if (pageIntent.current?.selection === selection) pageIntent.current.generation = current;
    else pageIntent.current = null;
    let disposed = false;
    setLoading(true);
    setFailed(false);
    void (async () => {
      try {
        const result = await fetchClassification(
          readSession(),
          selection,
          controller.signal,
          refresh,
        );
        if (disposed || current !== generation.current) return;
        if (
          result.status === 'available' &&
          selection.offset > 0 &&
          selection.offset >= result.pagination.total
        ) {
          pageIntent.current = null;
          setPageNotice('记录数量已变化，已返回第 1 页。');
          setSelection((value) => ({ ...value, offset: 0 }));
          return;
        }
        setSaved({ page: result, selection });
      } catch {
        if (!disposed && current === generation.current) {
          pageIntent.current = null;
          setFailed(true);
        }
      } finally {
        if (!disposed && current === generation.current) setLoading(false);
      }
    })();
    return () => {
      disposed = true;
      controller.abort();
    };
  }, [active, searching, selection, retry]);

  useEffect(() => {
    if (!active) {
      pageIntent.current = null;
      return;
    }
    const cancelOnMove = (event: FocusEvent) => {
      if (pageIntent.current && event.target !== pageIntent.current.source)
        pageIntent.current = null;
    };
    document.addEventListener('focusin', cancelOnMove);
    return () => {
      document.removeEventListener('focusin', cancelOnMove);
      pageIntent.current = null;
    };
  }, [active]);

  useEffect(() => {
    const intent = pageIntent.current;
    if (
      !intent ||
      !active ||
      view !== 'songs' ||
      waiting ||
      failed ||
      !available ||
      intent.selection !== selection ||
      intent.generation !== generation.current
    )
      return;
    pageIntent.current = null;
    if (document.activeElement !== document.body && document.activeElement !== intent.source)
      return;
    if (intent.focus) resultsHeading.current?.focus({ preventScroll: true });
    resultsHeading.current?.scrollIntoView?.({ block: 'start' });
  }, [active, view, waiting, failed, available, currentData, selection]);

  function filter(change: Partial<Selection>) {
    pageIntent.current = null;
    refreshRequested.current = false;
    setPageNotice('');
    setSelection((value) => ({ ...value, ...change, query: input.trim(), offset: 0 }));
  }
  function clear(pending = false, basis?: Selection['basis']) {
    pageIntent.current = null;
    refreshRequested.current = false;
    setPageNotice('');
    setInput('');
    setSelection({ ...INITIAL, review: pending ? 'pending' : 'all', ...(basis ? { basis } : {}) });
    if (pending) setView('songs');
  }
  function openQualityReview(review: 'needs_review' | 'pilot' | 'draft') {
    pageIntent.current = null;
    refreshRequested.current = false;
    setPageNotice('');
    setInput('');
    setSelection({ ...INITIAL, basis: selection.basis ?? 'original', review });
    setView('songs');
  }
  function chooseTag(dimension: Dimension, tag: string) {
    if (!data?.options[dimension].includes(tag)) return;
    filter({ dimension, tag });
    setView('songs');
  }
  function changeView(next: View) {
    pageIntent.current = null;
    setView(next);
  }
  function tabKey(event: KeyboardEvent<HTMLButtonElement>) {
    if (!['ArrowLeft', 'ArrowRight', 'Home', 'End'].includes(event.key)) return;
    event.preventDefault();
    const next =
      event.key === 'Home'
        ? 'songs'
        : event.key === 'End'
          ? 'playlists'
          : view === 'songs'
            ? 'playlists'
            : 'songs';
    changeView(next);
    (next === 'songs' ? songTab : playlistTab).current?.focus();
  }
  function refresh() {
    pageIntent.current = null;
    refreshRequested.current = true;
    setRetry((value) => value + 1);
  }
  async function submitDraft(request: ClassificationDraftRequest) {
    if (mutationLock.current || !active || waiting || failed || currentData?.quality?.draft_status !== 'ready')
      throw new Error('分类记录正在更新，请重新读取后保存修正。');
    mutationLock.current = true;
    setDraftSubmitting(true);
    try {
      const result = await postClassificationDraft(readSession(), request);
      // A GET started while this POST was pending may contain an older draft.
      // Invalidate it before requesting the accepted revision from local records.
      generation.current += 1;
      setLoading(true);
      setPageNotice(result.message);
      refresh();
    } finally {
      mutationLock.current = false;
      setDraftSubmitting(false);
    }
  }
  function changePage(offset: number, source: HTMLButtonElement) {
    const next = { ...selection, offset };
    pageIntent.current = {
      selection: next,
      source,
      focus: document.activeElement === source,
      generation: null,
    };
    refreshRequested.current = false;
    setPageNotice('');
    setSelection(next);
  }
  function trackTag(dimension: Dimension, tag: string) {
    const text = `${DIMENSIONS[dimension]} · ${tag}`;
    return data?.options[dimension].includes(tag) ? (
      <button
        type="button"
        className={`${dimension}-tag`}
        key={text}
        onClick={() => chooseTag(dimension, tag)}
      >
        {text}
      </button>
    ) : (
      <span className={`${dimension}-tag`} key={text}>
        {text}
      </span>
    );
  }
  const tags = selection.dimension === 'all' ? [] : (data?.options[selection.dimension] ?? []);
  const removedTag = !!selection.tag && !tags.includes(selection.tag);
  const filterText = [
    ...(data?.quality ? [draftBasis ? '修正后分类' : '原分类'] : []),
    selection.dimension === 'all'
      ? '全部维度'
      : `${DIMENSIONS[selection.dimension]}${selection.tag ? ` · ${selection.tag}` : ' · 全部标签'}`,
    ...(selection.review !== 'all' ? [REVIEW_LABELS[selection.review]] : []),
    ...(input.trim() ? [`搜索“${input.trim()}”`] : []),
  ];
  const retryButton = (
    <button className="button secondary" disabled={waiting} onClick={refresh}>
      <Icon name="refresh" size={16} />
      重试读取分类成果
    </button>
  );
  const unavailable = !waiting && !failed && !available;

  return (
    <div className="classification-panel" hidden={!active}>
      <div className="page-heading">
        <div>
          <span className="eyebrow">CLASSIFICATION RESULTS</span>
          <h1 id={active ? 'page-title' : `${id}-inactive-title`}>分类成果</h1>
          <p>查询歌曲分类与判断依据，也可浏览分类歌单。</p>
        </div>
        <button className="button secondary" disabled={waiting} onClick={refresh}>
          <Icon name="refresh" size={16} />
          刷新分类记录
        </button>
      </div>
      <div className="classification-tabs" role="tablist" aria-label="分类成果视图">
        <button
          ref={songTab}
          id={`${id}-songs-tab`}
          role="tab"
          aria-selected={view === 'songs'}
          aria-controls={`${id}-songs`}
          tabIndex={view === 'songs' ? 0 : -1}
          onClick={() => changeView('songs')}
          onKeyDown={tabKey}
        >
          歌曲分类
        </button>
        <button
          ref={playlistTab}
          id={`${id}-playlists-tab`}
          role="tab"
          aria-selected={view === 'playlists'}
          aria-controls={`${id}-playlists`}
          tabIndex={view === 'playlists' ? 0 : -1}
          onClick={() => changeView('playlists')}
          onKeyDown={tabKey}
        >
          分类歌单
        </button>
      </div>
      <section className="classification-selection" aria-label="已选分类筛选">
        <div>
          <strong>当前筛选</strong>
          <span>
            {filterText.join(' · ')}
            {removedTag ? '（标签已移除）' : ''}
          </span>
          <span aria-live="polite">
            {searching || (loading && !currentData)
              ? '正在匹配…'
              : available
                ? `匹配 ${currentData.pagination.total} 条`
                : '匹配数量暂不可用'}
          </span>
        </div>
        <button className="button secondary" onClick={() => clear()}>
          清除全部筛选
        </button>
      </section>
      {summary && (
        <section className="classification-stats" aria-label="分类结果概览">
          {[
            [applyingDraft ? '原分类曲目' : '来源曲目', summary.source_count],
            [applyingDraft ? '原分类已覆盖' : '已覆盖', summary.covered_count],
            [applyingDraft ? '原分类歌单' : '分类歌单', summary.playlist_count],
            [applyingDraft ? '修正后待辨识' : '待辨识', applyingDraft ? draftCounts?.pending_count ?? '—' : summary.pending_count],
            [applyingDraft ? '修正后风格待辨识' : '风格待辨识', applyingDraft ? draftCounts?.unknown_style_count ?? '—' : summary.unknown_style_count],
            [applyingDraft ? '修正后语言待辨识' : '语言待辨识', applyingDraft ? draftCounts?.unknown_language_count ?? '—' : summary.unknown_language_count],
          ].map(([label, count]) => (
            <div key={label}>
              <span>{label}</span>
              <strong>{count === '—' ? count : Number(count).toLocaleString('zh-CN')}</strong>
              {(label === '待辨识' || label === '修正后待辨识') && (
                <button className="classification-inline" onClick={() => {
                  if (applyingDraft) clear(true, 'draft');
                  else clear(true);
                }}>
                  查看全部待辨识
                </button>
              )}
            </div>
          ))}
        </section>
      )}
      {data?.status === 'available' && data.quality && (
        <ClassificationQualitySummary quality={data.quality} disabled={waiting || failed || !active || draftSubmitting}
          onReview={openQualityReview} />
      )}
      {draftBasis && data?.quality && !applyingDraft && <div className="classification-basis-note" role="status">
        <p>修正草稿不可应用，当前回退原分类。请核对草稿来源后再使用修正后分类。</p>
        <button className="classification-inline" onClick={() => filter({ basis: 'original' })}>返回原分类</button>
      </div>}
      {draftBasis && applyingDraft && <p className="classification-basis-note">
        歌曲标签与待辨识筛选采用修正后分类；来源曲目、覆盖数和分类歌单数量仍为原统计，草稿尚未写入账号。
      </p>}
      {failed && data?.status === 'available' && (
        <div className="classification-stale" role="alert">
          <strong>重新读取未完成，当前保留上次读取的旧记录。</strong>
          <p>记录保存于 {dateText(data.updated_at)}。本机服务可能正在忙，请稍后重试。</p>
          {retryButton}
        </div>
      )}
      {waiting && currentData?.status === 'available' && (
        <p className="classification-loading-note" role="status">
          正在重新读取，暂时显示上次读取的记录…
        </p>
      )}
      {pageNotice && (
        <p className="classification-loading-note" role="status">
          {pageNotice}
        </p>
      )}
      <div
        id={`${id}-songs`}
        role="tabpanel"
        aria-labelledby={`${id}-songs-tab`}
        hidden={view !== 'songs'}
      >
        <section className="classification-records" aria-label="逐曲分类结果" aria-busy={waiting}>
          <div className="section-heading">
            <div>
              <span className="eyebrow">TRACK BY TRACK</span>
              <h2 ref={resultsHeading} tabIndex={-1}>
                逐曲分类结果
              </h2>
            </div>
            <span className="subtle small">每页 50 条</span>
          </div>
          <div className="classification-filters">
            <label className="classification-search">
              <Icon name="search" size={17} />
              <input
                aria-label="搜索歌曲或歌手"
                placeholder="搜索歌曲或歌手"
                maxLength={160}
                value={input}
                onChange={(event) => {
                  pageIntent.current = null;
                  setInput(event.target.value);
                }}
              />
              {input && (
                <button
                  className="icon-button"
                  aria-label="清除分类搜索"
                  onClick={() => {
                    pageIntent.current = null;
                    setInput('');
                  }}
                >
                  <Icon name="close" size={16} />
                </button>
              )}
            </label>
            {data?.quality && <label>分类依据
              <select aria-label="分类依据" value={selection.basis ?? 'original'}
                onChange={(event) => filter({ basis: event.target.value as Selection['basis'] })}>
                <option value="original">原分类</option><option value="draft">修正后分类</option>
              </select>
            </label>}
            <label>
              分类维度
              <select
                aria-label="分类维度"
                value={selection.dimension}
                onChange={(event) =>
                  filter({ dimension: event.target.value as Selection['dimension'], tag: '' })
                }
              >
                <option value="all">全部维度</option>
                <option value="scene">场景</option>
                <option value="style">风格</option>
                <option value="language">语言</option>
              </select>
            </label>
            <label>
              分类标签
              <select
                aria-label="分类标签"
                value={selection.tag}
                disabled={selection.dimension === 'all'}
                onChange={(event) => filter({ tag: event.target.value })}
              >
                <option value="">全部标签</option>
                {removedTag && (
                  <option value={selection.tag} disabled>
                    {selection.tag}（当前记录已无此标签）
                  </option>
                )}
                {tags.map((tag) => (
                  <option key={tag} value={tag}>
                    {tag}
                  </option>
                ))}
              </select>
            </label>
            <button
              className={`button ${selection.review === 'pending' ? 'primary' : 'secondary'}`}
              aria-pressed={selection.review === 'pending'}
              onClick={() => filter({ review: selection.review === 'pending' ? 'all' : 'pending' })}
            >
              只看待辨识
            </button>
            {data?.quality && <label>
              复核筛选
              <select aria-label="复核筛选" value={selection.review} onChange={(event) => filter({ review: event.target.value as Selection['review'] })}>
                {Object.entries(REVIEW_LABELS).map(([value, label]) => <option key={value} value={value}>{label}</option>)}
              </select>
            </label>}
          </div>
          {waiting && !available ? (
            <p className="classification-empty" role="status">
              正在读取本地分类成果…
            </p>
          ) : failed && !available ? (
            <div
              className="classification-empty"
              role={data?.status === 'available' ? undefined : 'alert'}
            >
              <h3>分类成果暂时无法读取</h3>
              <p>请检查本机服务后重试。读取分类记录不会修改账号。</p>
              {data?.status !== 'available' && retryButton}
            </div>
          ) : unavailable ? (
            <div className="classification-empty">
              <h3>
                {currentData?.status === 'not_loaded' ? '还没有本地分类成果' : '本地分类记录不可用'}
              </h3>
              <p>
                {currentData?.status === 'not_loaded'
                  ? '本页展示分类工具保存的结果，暂未提供网页分类执行入口。已有记录时可重新读取。'
                  : '记录可能缺失或格式不完整。请检查分类工具保存的记录后重试。'}
              </p>
              {retryButton}
            </div>
          ) : (
            available && (
              <>
                <p className="classification-range" role="status">
                  {currentData.records.length
                    ? `${currentData.pagination.offset + 1}–${currentData.pagination.offset + currentData.records.length} / ${currentData.pagination.total} 条`
                    : '没有匹配的曲目'}
                </p>
                {currentData.records.length ? (
                  <div className="classification-track-list">
                    {currentData.records.map((track) => {
                      const displayed = applyingDraft && track.draft ? track.draft : track;
                      const pending = applyingDraft && track.draft
                        ? track.draft.styles.includes('待辨识') || track.draft.language === '待辨识'
                        : track.pending_reasons.length > 0;
                      return (
                      <article className="classification-track" key={currentData.quality
                        ? `${currentData.quality.source_version}:${currentData.quality.revision}:${track.record_key}`
                        : track.position}>
                        <span className="classification-position">{track.position}</span>
                        <div className="classification-track-content">
                          <h3>{track.name}</h3>
                          <p className="classification-artists">
                            {track.artists || '歌手资料未记录'}
                          </p>
                          <div className="classification-tags">
                            {displayed.scenes.map((tag) => trackTag('scene', tag))}
                            {displayed.styles.map((tag) => trackTag('style', tag))}
                            {trackTag('language', displayed.language || '待辨识')}
                            {pending && (
                              <span className="pending-tag">待辨识</span>
                            )}
                          </div>
                          {track.needs_review && <div className="classification-review-reasons">
                            <strong>{applyingDraft && track.draft ? '原分类复核提示' : '需复核'}</strong>
                            <ul>{track.review_reasons?.map((reason) => <li key={reason}>{reason}</li>)}</ul>
                          </div>}
                          {track.style_judgment_score !== undefined && <p className="classification-score">
                            模型自评分：{track.style_judgment_score === null ? '未记录' : track.style_judgment_score.toFixed(2)}（未经校准）
                          </p>}
                          {!!track.recording_hints?.length && <div className="classification-recording-hints">
                            <p>标题线索，尚未确认录音版本：</p>
                            <ul>{track.recording_hints.map((hint) => <li key={hint}>{hint}</li>)}</ul>
                          </div>}
                          <details className="classification-evidence">
                            <summary>查看分类依据</summary>
                            <p>{track.evidence_note || '未记录风格或场景判断依据。'}</p>
                            <p>{track.language_evidence_note || '未记录语言判断依据。'}</p>
                            {track.pending_reasons.length > 0 && (
                              <ul>
                                {track.pending_reasons.map((reason, index) => (
                                  <li key={index}>{reason}</li>
                                ))}
                              </ul>
                            )}
                          </details>
                          {currentData.quality && track.record_key && <ClassificationEditor
                            track={track} quality={currentData.quality} disabled={waiting || failed || draftSubmitting || !active}
                            onSubmit={submitDraft} />}
                        </div>
                      </article>
                    ); })}
                  </div>
                ) : (
                  <div className="classification-empty">
                    <p>试试其他歌曲、歌手或分类标签，或清除全部筛选。</p>
                  </div>
                )}
                <nav className="classification-pagination" aria-label="分类曲目分页">
                  <button
                    className="button secondary"
                    disabled={currentData.pagination.offset === 0 || waiting}
                    onClick={(event) =>
                      changePage(
                        Math.max(0, currentData.pagination.offset - 50),
                        event.currentTarget,
                      )
                    }
                  >
                    上一页
                  </button>
                  <span>
                    第 {Math.floor(currentData.pagination.offset / 50) + 1} 页，共{' '}
                    {Math.max(1, Math.ceil(currentData.pagination.total / 50))} 页
                  </span>
                  <button
                    className="button secondary"
                    disabled={currentData.pagination.next_offset === null || waiting}
                    onClick={(event) => {
                      if (currentData.pagination.next_offset !== null)
                        changePage(currentData.pagination.next_offset, event.currentTarget);
                    }}
                  >
                    下一页
                  </button>
                </nav>
              </>
            )
          )}
        </section>
      </div>
      <div
        id={`${id}-playlists`}
        role="tabpanel"
        aria-labelledby={`${id}-playlists-tab`}
        hidden={view !== 'playlists'}
      >
        <section className="classification-playlists" aria-label="分类歌单" aria-busy={waiting}>
          <div className="section-heading">
            <div>
              <span className="eyebrow">ORGANIZED COLLECTION</span>
              <h2>分类歌单</h2>
            </div>
            <span className="subtle small">{data?.playlists.length ?? 0} 项本地记录</span>
          </div>
          {data?.status === 'available' ? (
            <div className="classification-card-grid">
              {data.playlists.map((playlist) => {
                const dimension = playlist.dimension;
                const tag =
                  dimension === 'review'
                    ? null
                    : data.options[dimension].find(
                        (label) => playlist.name === `${DIMENSIONS[dimension]} · ${label}`,
                      );
                return (
                  <article
                    className={`classification-card dimension-${dimension}`}
                    key={playlist.name}
                  >
                    <span className="classification-dimension">{DIMENSIONS[dimension]}</span>
                    <h3>{playlist.name}</h3>
                    <p>{playlist.count.toLocaleString('zh-CN')} 首歌曲</p>
                    <button
                      className="classification-inline"
                      disabled={dimension !== 'review' && !tag}
                      title={
                        dimension !== 'review' && !tag
                          ? '当前记录没有可精确匹配的分类标签'
                          : undefined
                      }
                      onClick={() => {
                        if (dimension === 'review') clear(true);
                        else if (tag) chooseTag(dimension, tag);
                      }}
                    >
                      {dimension === 'review' ? '查看全部待辨识' : '查看分类曲目'}
                    </button>
                    {playlist.key && /^[1-9][0-9]{0,19}$/.test(playlist.key) && (
                      <a
                        className="classification-open"
                        href={`https://music.163.com/#/playlist?id=${playlist.key}`}
                        target="_blank"
                        rel="noopener noreferrer"
                        aria-label={`在网易云打开${playlist.name}`}
                      >
                        打开歌单 <Icon name="arrow" size={13} />
                      </a>
                    )}
                  </article>
                );
              })}
            </div>
          ) : (
            <div className="classification-empty">
              <p>
                {waiting
                  ? '正在读取本地分类成果…'
                  : failed
                    ? '分类成果暂时无法读取，请稍后重试。'
                    : currentData?.status === 'not_loaded'
                      ? '还没有本地分类成果'
                      : '本地分类记录不可用'}
              </p>
              {!waiting && retryButton}
            </div>
          )}
        </section>
      </div>
      <section className="classification-source" aria-label="分类记录来源">
        <div>
          <Icon name="file" size={19} />
          <strong>{draftBasis ? '原分类记录来源' : '本地分类记录'}</strong>
          <span
            className={`status-pill ${!stale && data?.verification === 'verified' ? 'success' : ''}`}
          >
            {stale
              ? failed
                ? '旧记录 · 等待重新读取'
                : '正在重新读取 · 旧记录'
              : data?.verification === 'verified'
                ? '已有账号核验记录'
                : '仅本地分类记录'}
          </span>
        </div>
        <p>分类记录更新：{dateText(data?.updated_at ?? null)}</p>
        <p>
          {data?.verification === 'verified' && data.verified_at
            ? `上次账号核验：${dateText(data.verified_at)}；浏览本页不会重新核验账号。`
            : '尚未记录账号核验；本地分类结果不代表账号已完成写入。'}
        </p>
        <p className="classification-evidence-note">
          标签来自已有资料和逐曲判断，不能替代完整听音判断。待辨识条目保留证据缺口。
        </p>
      </section>
    </div>
  );
}
