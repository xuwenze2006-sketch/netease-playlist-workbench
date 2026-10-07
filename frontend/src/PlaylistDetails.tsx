import { useEffect, useId, useRef, useState } from 'react';
import type { KeyboardEvent } from 'react';
import { fetchLocalTracks, readSession } from './api';
import { Icon } from './icons';
import type { Job, LocalTrackPage, MetadataFilter, Playlist, PlaylistReadIntent } from './types';

interface Props {
  playlist: Playlist;
  onClose: () => void;
  returnFocus?: HTMLElement | null;
  onRead?: () => Promise<boolean>;
  readBlockedReason?: string;
  readIntent?: PlaylistReadIntent | null;
  job?: Job | null;
  onPause?: () => Promise<void>;
  pauseDisabled?: boolean;
}
type TrackSelection = { query: string; offset: number; metadata: MetadataFilter };

export function PlaylistDetails(props: Props) {
  return <DetailsDrawer key={props.playlist.key} {...props} />;
}

function DetailsDrawer({
  playlist,
  onClose,
  returnFocus,
  onRead,
  readBlockedReason,
  readIntent,
  job,
  onPause,
  pauseDisabled,
}: Props) {
  const dialog = useRef<HTMLDialogElement>(null);
  const closeButton = useRef<HTMLButtonElement>(null);
  const searchInput = useRef<HTMLInputElement>(null);
  const previousButton = useRef<HTMLButtonElement>(null);
  const nextButton = useRef<HTMLButtonElement>(null);
  const retryButton = useRef<HTMLButtonElement>(null);
  const trigger = useRef<HTMLElement | null>(
    returnFocus ?? (document.activeElement as HTMLElement | null),
  );
  const onCloseRef = useRef(onClose);
  onCloseRef.current = onClose;
  const closed = useRef(false);
  const request = useRef<AbortController | null>(null);
  const generation = useRef(0);
  const completedGeneration = useRef(0);
  const lastFocusedDirection = useRef<'previous' | 'next'>('next');
  const paginationFocus = useRef<{
    direction: 'previous' | 'next';
    source: HTMLButtonElement;
    selection: TrackSelection;
    generation: number | null;
  } | null>(null);
  const [input, setInput] = useState('');
  const [selection, setSelection] = useState<TrackSelection>({
    query: '',
    offset: 0,
    metadata: 'all',
  });
  const [retry, setRetry] = useState(0);
  const [data, setData] = useState<LocalTrackPage | null>(null);
  const [loading, setLoading] = useState(true);
  const [failed, setFailed] = useState(false);
  const [readSubmitting, setReadSubmitting] = useState(false);
  const [readError, setReadError] = useState('');
  const readAttempt = useRef<{ before: number | null; sequence: number | null } | null>(null);
  const refreshedJob = useRef('');
  const headingId = useId();
  const searching = input.trim() !== selection.query;
  const waiting = loading || searching;

  function restoreFocus() {
    if (trigger.current?.isConnected) trigger.current.focus();
  }
  function dismiss() {
    if (closed.current) return;
    closed.current = true;
    paginationFocus.current = null;
    generation.current += 1;
    request.current?.abort();
    if (dialog.current?.open) dialog.current.close();
    restoreFocus();
    onCloseRef.current();
  }
  function containFocus(event: KeyboardEvent<HTMLDialogElement>) {
    if (event.key === 'Escape') {
      event.preventDefault();
      dismiss();
      return;
    }
    if (event.key !== 'Tab') return;
    const controls = Array.from(
      dialog.current?.querySelectorAll<HTMLElement>(
        'button:not(:disabled), input:not(:disabled), a[href], [tabindex="0"]',
      ) ?? [],
    );
    const first = controls[0];
    const last = controls.at(-1);
    if (event.shiftKey && document.activeElement === first) {
      event.preventDefault();
      last?.focus();
    } else if (!event.shiftKey && document.activeElement === last) {
      event.preventDefault();
      first?.focus();
    }
  }

  useEffect(() => {
    const element = dialog.current;
    element?.showModal();
    closeButton.current?.focus();
    const cancelFocusOnMove = (event: FocusEvent) => {
      const intent = paginationFocus.current;
      if (intent && event.target !== intent.source) paginationFocus.current = null;
    };
    document.addEventListener('focusin', cancelFocusOnMove);
    return () => {
      closed.current = true;
      paginationFocus.current = null;
      document.removeEventListener('focusin', cancelFocusOnMove);
      request.current?.abort();
      if (element?.open) element.close();
      restoreFocus();
    };
  }, []);

  useEffect(() => {
    const next = input.trim();
    if (next === selection.query) return;
    const timer = window.setTimeout(
      () => setSelection((value) => ({ ...value, query: next, offset: 0 })),
      200,
    );
    return () => window.clearTimeout(timer);
  }, [input, selection.query]);

  useEffect(() => {
    const controller = new AbortController();
    request.current = controller;
    const current = ++generation.current;
    const focusIntent = paginationFocus.current;
    if (focusIntent?.selection === selection) focusIntent.generation = current;
    else paginationFocus.current = null;
    let disposed = false;
    setLoading(true);
    setFailed(false);
    setData(null);
    void (async () => {
      try {
        const result = await fetchLocalTracks(
          readSession(),
          playlist.key,
          selection,
          controller.signal,
        );
        if (!disposed && current === generation.current && !closed.current) setData(result);
      } catch {
        if (!disposed && current === generation.current && !closed.current) setFailed(true);
      } finally {
        if (!disposed && current === generation.current && !closed.current) {
          completedGeneration.current = current;
          setLoading(false);
        }
      }
    })();
    return () => {
      disposed = true;
      controller.abort();
    };
  }, [playlist.key, selection, retry]);

  useEffect(() => {
    const intent = paginationFocus.current;
    if (
      !intent ||
      loading ||
      searching ||
      closed.current ||
      intent.selection !== selection ||
      intent.generation !== generation.current ||
      intent.generation !== completedGeneration.current
    )
      return;
    paginationFocus.current = null;
    if (document.activeElement !== document.body && document.activeElement !== intent.source)
      return;
    if (failed || data?.status === 'unavailable') {
      retryButton.current?.focus();
      return;
    }
    const preferred = intent.direction === 'next' ? nextButton.current : previousButton.current;
    const alternative = intent.direction === 'next' ? previousButton.current : nextButton.current;
    const destination =
      preferred && !preferred.disabled
        ? preferred
        : alternative && !alternative.disabled
          ? alternative
          : searchInput.current;
    destination?.focus();
  }, [data, failed, loading, searching, selection]);

  useEffect(() => {
    const attempt = readAttempt.current;
    if (
      !attempt ||
      !readIntent ||
      readIntent.key !== playlist.key ||
      readIntent.sequence === attempt.before
    )
      return;
    if (attempt.sequence === null) attempt.sequence = readIntent.sequence;
    if (
      attempt.sequence !== readIntent.sequence ||
      !job ||
      job.id !== readIntent.job_id ||
      job.action !== 'read_playlist' ||
      job.playlist_key !== playlist.key ||
      job.id === readIntent.baseline_id ||
      refreshedJob.current === job.id
    )
      return;
    setReadError('');
    if (
      !['completed', 'partial'].includes(job.status) ||
      !['completed', 'partial'].includes(job.result?.status ?? '') ||
      job.result?.playlist_key !== playlist.key ||
      job.result.record_saved !== true ||
      job.result.outcome_known !== true ||
      job.result.write_attempted !== false ||
      job.result.applied_to_account !== false ||
      closed.current
    )
      return;
    refreshedJob.current = job.id;
    setReadError('');
    setSelection((value) => ({ ...value, offset: 0 }));
    setRetry((value) => value + 1);
  }, [job, readIntent, playlist.key]);

  async function startRead() {
    if (
      !onRead ||
      readBlockedReason ||
      readSubmitting ||
      waiting ||
      data?.status === 'missing_playlist'
    )
      return;
    readAttempt.current = { before: readIntent?.sequence ?? null, sequence: null };
    setReadSubmitting(true);
    setReadError('');
    try {
      const started = await onRead();
      if (!started && !closed.current) setReadError('读取提交未确认，请查看任务状态，勿重复提交。');
    } finally {
      if (!closed.current) setReadSubmitting(false);
    }
  }

  function resetSearch() {
    paginationFocus.current = null;
    setInput('');
    searchInput.current?.focus();
  }
  function changePage(direction: 'previous' | 'next', offset: number, source: HTMLButtonElement) {
    const next = { ...selection, offset };
    if (document.activeElement === source) lastFocusedDirection.current = direction;
    paginationFocus.current =
      document.activeElement === source
        ? { direction, source, selection: next, generation: null }
        : null;
    setSelection(next);
  }
  function retryLocalRead(source: HTMLButtonElement) {
    paginationFocus.current =
      document.activeElement === source
        ? { direction: lastFocusedDirection.current, source, selection, generation: null }
        : null;
    setRetry((value) => value + 1);
  }
  const available = data?.status === 'available';
  const total = data?.pagination.total ?? 0;
  const currentPage = Math.floor(selection.offset / 50) + 1;
  const pageCount = Math.max(1, Math.ceil(total / 50));
  const incomplete = selection.metadata === 'incomplete';
  const emptyTitle = selection.query
    ? incomplete
      ? '没有符合筛选的已保存歌曲'
      : '没有匹配的已保存歌曲'
    : data?.counts.observed === 0
      ? '这份本地记录没有歌曲'
      : incomplete && data?.counts.metadata_missing === 0
        ? '已保存歌曲的资料均完整'
        : incomplete
          ? '没有符合筛选的已保存歌曲'
          : '这份本地记录没有歌曲';
  const activeJob = !!job && ['queued', 'running', 'pause_requested'].includes(job.status);
  const ownReadJob =
    !!job &&
    !!readIntent &&
    !!readAttempt.current &&
    readIntent.sequence !== readAttempt.current.before &&
    (readAttempt.current.sequence === null ||
      readAttempt.current.sequence === readIntent.sequence) &&
    job.id === readIntent.job_id &&
    job.action === 'read_playlist' &&
    job.playlist_key === playlist.key;
  const task = activeJob || ownReadJob ? job : null;
  const taskMessage =
    task?.status === 'paused'
      ? '已暂停，本地明细保留原来的记录。'
      : task?.result?.record_saved === false && !activeJob
        ? '新明细保存状态未确认，请查看本地记录后主动重试读取。'
        : task?.result?.message || '读取未完成，请查看任务记录。';

  return (
    <dialog
      ref={dialog}
      className="playlist-details"
      aria-labelledby={headingId}
      onCancel={(event) => {
        event.preventDefault();
        dismiss();
      }}
      onClose={dismiss}
      onKeyDown={containFocus}
    >
      <header className="details-header">
        <div>
          <span className="eyebrow">LOCAL COLLECTION</span>
          <h2 id={headingId} title={`${playlist.name}的歌曲明细`}>
            {playlist.name}的歌曲明细
          </h2>
        </div>
        <button
          ref={closeButton}
          type="button"
          className="icon-button details-close"
          aria-label="关闭歌曲明细"
          onClick={dismiss}
        >
          <Icon name="close" size={21} />
        </button>
      </header>
      <div className="details-content">
        <section className="details-context">
          <p className="details-source">
            <Icon name="file" size={15} /> 仅查看本地保存的资料
          </p>
          {available && data && (
            <>
              <div className="details-counts">
                <strong>
                  已保存 {data.counts.observed} / {data.counts.expected} 首
                </strong>
                {!!data.counts.missing && (
                  <span className="details-warning">缺失 {data.counts.missing} 首</span>
                )}
                {!!data.counts.metadata_missing && (
                  <span>{data.counts.metadata_missing} 首资料不完整</span>
                )}
              </div>
              <p className="details-date">
                本地明细保存于{' '}
                <time dateTime={data.updated_at!}>
                  {new Date(data.updated_at!).toLocaleString('zh-CN', {
                    year: 'numeric',
                    month: '2-digit',
                    day: '2-digit',
                    hour: '2-digit',
                    minute: '2-digit',
                    hour12: false,
                  })}
                </time>
              </p>
              {data.playlist?.track_count !== data.counts.expected && (
                <p className="details-history-note">
                  清单记录 {data.playlist?.track_count} 首；这份历史明细的总数为{' '}
                  {data.counts.expected} 首。
                </p>
              )}
            </>
          )}
        </section>
        {onRead && (
          <section className="details-read-actions" aria-label="读取本歌单明细">
            <button
              type="button"
              className="button primary"
              disabled={
                !!readBlockedReason ||
                readSubmitting ||
                waiting ||
                data?.status === 'missing_playlist'
              }
              onClick={() => {
                void startRead();
              }}
            >
              <Icon name="refresh" size={16} />
              {available ? '更新此歌单明细' : '读取此歌单明细'}
            </button>
            <p>{readBlockedReason || '主动读取账号中的歌曲，并保存到本地供之后浏览。'}</p>
            {readError && <p role="alert">{readError}</p>}
          </section>
        )}
        {task && (
          <section className="details-read-task" aria-label="歌单明细读取任务">
            <div>
              <strong>{task.label}</strong>
              <p aria-live="polite">
                {activeJob ? task.progress?.label || '正在准备读取' : taskMessage}
              </p>
              {activeJob && (
                <span>
                  {task.progress?.total_count
                    ? `${task.progress.completed_count ?? 0} / ${task.progress.total_count} 项 · `
                    : ''}
                  {Math.floor(task.progress?.elapsed_seconds ?? 0)} 秒
                </span>
              )}
            </div>
            {activeJob && onPause && (
              <button
                type="button"
                className="button secondary"
                disabled={pauseDisabled}
                onClick={() => {
                  void onPause();
                }}
              >
                <Icon name="pause" size={15} />
                {pauseDisabled ? '正在暂停…' : '暂停任务'}
              </button>
            )}
          </section>
        )}
        <div className="details-search-wrap">
          <label className="details-search">
            <Icon name="search" size={19} />
            <input
              ref={searchInput}
              type="text"
              aria-label="搜索曲名或歌手"
              placeholder="搜索已保存的曲名或歌手"
              maxLength={160}
              value={input}
              onChange={(event) => {
                paginationFocus.current = null;
                setInput(event.target.value);
              }}
            />
            {input && (
              <button
                type="button"
                className="icon-button"
                aria-label="清除歌曲搜索"
                onClick={resetSearch}
              >
                <Icon name="close" size={17} />
              </button>
            )}
          </label>
          <div className="details-filters" role="group" aria-label="歌曲资料筛选">
            <button
              type="button"
              aria-label="全部已保存歌曲"
              aria-pressed={!incomplete}
              onClick={() => {
                paginationFocus.current = null;
                setSelection((value) =>
                  value.metadata === 'all' ? value : { ...value, metadata: 'all', offset: 0 },
                );
              }}
            >
              全部已保存歌曲
            </button>
            <button
              type="button"
              aria-label="只看资料不完整"
              aria-pressed={incomplete}
              onClick={() => {
                paginationFocus.current = null;
                setSelection((value) =>
                  value.metadata === 'incomplete'
                    ? value
                    : { ...value, metadata: 'incomplete', offset: 0 },
                );
              }}
            >
              只看资料不完整
            </button>
          </div>
        </div>
        <div className="details-body" aria-busy={waiting}>
          {waiting ? (
            <div className="details-state" role="status">
              <span className="details-state-icon">
                <Icon name="refresh" size={25} />
              </span>
              <h3>{searching ? '正在筛选已保存歌曲…' : '正在读取本地歌曲资料…'}</h3>
            </div>
          ) : failed ? (
            <div className="details-state">
              <span className="details-state-icon">
                <Icon name="file" size={25} />
              </span>
              <p role="alert">本地歌曲资料暂时无法读取，请重试。</p>
              <button
                ref={retryButton}
                className="button secondary"
                onClick={(event) => retryLocalRead(event.currentTarget)}
              >
                重试读取
              </button>
            </div>
          ) : data?.status === 'not_loaded' ? (
            <div className="details-state">
              <span className="details-state-icon">
                <Icon name="music" size={27} />
              </span>
              <h3>尚未保存歌曲明细</h3>
              <p>这份歌单已有名称和数量记录，歌曲明细尚未保存到本地。</p>
              <p className="details-state-note">当前只展示已保存的资料。</p>
            </div>
          ) : data?.status === 'unavailable' ? (
            <div className="details-state">
              <span className="details-state-icon">
                <Icon name="file" size={25} />
              </span>
              <h3>本地明细暂不可用</h3>
              <p>保存的资料无法与当前歌单清单核对，请稍后重新读取。</p>
              <button
                ref={retryButton}
                className="button secondary"
                onClick={(event) => retryLocalRead(event.currentTarget)}
              >
                重试读取
              </button>
            </div>
          ) : data?.status === 'missing_playlist' ? (
            <div className="details-state">
              <span className="details-state-icon">
                <Icon name="music" size={27} />
              </span>
              <h3>歌单记录已更新</h3>
              <p>当前本地清单中已没有这份歌单，请关闭后重新选择。</p>
            </div>
          ) : available && data.tracks.length === 0 ? (
            <div className="details-state">
              <span className="details-state-icon">
                <Icon name="search" size={27} />
              </span>
              <h3>{emptyTitle}</h3>
              <p>
                {selection.query
                  ? '试试其他曲名或歌手；搜索范围为本地已保存的歌曲。'
                  : data.counts.observed === 0
                    ? '这份已保存的明细中，歌曲数量为 0。'
                    : '当前仅筛选本地已保存的歌曲资料。'}
              </p>
            </div>
          ) : (
            available && (
              <table className="details-table" aria-label="已保存的歌曲">
                <thead>
                  <tr>
                    <th scope="col">序号</th>
                    <th scope="col">歌曲</th>
                    <th scope="col">歌手</th>
                  </tr>
                </thead>
                <tbody>
                  {data.tracks.map((track) => (
                    <tr key={track.key}>
                      <td>{track.position}</td>
                      <td>
                        <strong>{track.name}</strong>
                        {!track.metadata_available && (
                          <span className="track-note">歌手资料不完整</span>
                        )}
                      </td>
                      <td>
                        <span>
                          {track.artists.length ? track.artists.join(' / ') : '歌手资料暂缺'}
                        </span>
                        {track.artist_count > track.artists.length && (
                          <span className="track-note">
                            另有 {track.artist_count - track.artists.length} 位已记录歌手
                          </span>
                        )}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            )
          )}
        </div>
      </div>
      {available && (
        <footer className="details-pagination">
          <div>
            <strong>
              第 {currentPage} 页，共 {pageCount} 页
            </strong>
            <span>
              {incomplete
                ? `符合筛选 ${total} 首已保存歌曲`
                : selection.query
                  ? `匹配 ${total} 首已保存歌曲`
                  : `共 ${total} 首已保存歌曲`}
            </span>
          </div>
          <div className="details-page-buttons">
            <button
              ref={previousButton}
              type="button"
              className="button secondary"
              disabled={waiting || selection.offset === 0}
              onClick={(event) =>
                changePage('previous', Math.max(0, selection.offset - 50), event.currentTarget)
              }
            >
              上一页
            </button>
            <button
              ref={nextButton}
              type="button"
              className="button secondary"
              disabled={waiting || data.pagination.next_offset === null}
              onClick={(event) =>
                changePage('next', data.pagination.next_offset!, event.currentTarget)
              }
            >
              下一页
            </button>
          </div>
        </footer>
      )}
    </dialog>
  );
}
