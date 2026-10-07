import { useEffect, useRef, useState, type MouseEvent } from 'react';
import { fetchClassificationChanges, readSession } from './api';
import type { ClassificationChangesPage, ClassificationChangesQuery, ClassificationQuality } from './types';

type Selection = Pick<ClassificationChangesQuery, 'playlist' | 'offset' | 'change'> & { identity: string };
export function ClassificationChanges({ quality, disabled }: { quality: ClassificationQuality; disabled: boolean }) {
  const [selection, setSelection] = useState<Selection | null>(null);
  const [saved, setSaved] = useState<{ selection: Selection; page: ClassificationChangesPage } | null>(null);
  const [failure, setFailure] = useState<{ selection: Selection; message: string } | null>(null);
  const [retry, setRetry] = useState(0);
  const generation = useRef(0);
  const detailHeading = useRef<HTMLHeadingElement>(null);
  const detailSection = useRef<HTMLElement>(null);
  const openers = useRef(new Map<string, HTMLButtonElement>());
  const detailIntent = useRef<{
    selection: Selection;
    source: HTMLButtonElement;
    focus: boolean;
    generation: number | null;
  } | null>(null);
  const identity = `${quality.source_version}:${quality.revision}`;
  const currentSelection = selection?.identity === identity ? selection : null;
  const ready = quality.draft_status === 'ready';
  const current = ready && !disabled && saved?.selection === currentSelection ? saved?.page : null;
  const error = failure?.selection === currentSelection ? failure?.message : null;

  useEffect(() => {
    if (!currentSelection || !ready || disabled) {
      detailIntent.current = null;
      return;
    }
    const controller = new AbortController();
    const currentGeneration = ++generation.current;
    if (detailIntent.current?.selection === currentSelection) detailIntent.current.generation = currentGeneration;
    else detailIntent.current = null;
    let disposed = false;
    setSaved(null); setFailure(null);
    void (async () => {
      try {
        const page = await fetchClassificationChanges(readSession(), {
          playlist: currentSelection.playlist, offset: currentSelection.offset, change: currentSelection.change,
          source_version: quality.source_version, revision: quality.revision,
        }, controller.signal);
        if (!disposed && currentGeneration === generation.current) setSaved({ selection: currentSelection, page });
      } catch (cause) {
        if (!disposed && currentGeneration === generation.current) {
          detailIntent.current = null;
          setFailure({ selection: currentSelection,
            message: cause instanceof Error ? cause.message : '歌单变动暂时无法读取，请刷新分类记录后核对。' });
        }
      }
    })();
    return () => { disposed = true; controller.abort(); };
  }, [currentSelection, ready, disabled, retry, quality.source_version, quality.revision]);

  useEffect(() => {
    const cancelOnFocus = (event: FocusEvent) => {
      if (detailIntent.current && event.target !== detailIntent.current.source) detailIntent.current = null;
    };
    const cancelOnPointer = (event: PointerEvent) => {
      if (detailIntent.current && !detailIntent.current.source.contains(event.target as Node)) detailIntent.current = null;
    };
    const cancelOnScroll = () => { detailIntent.current = null; };
    const cancelOnScrollKey = (event: KeyboardEvent) => {
      if (['ArrowDown', 'ArrowUp', 'ArrowLeft', 'ArrowRight', 'PageDown', 'PageUp', 'Home', 'End', ' ', 'Spacebar'].includes(event.key))
        detailIntent.current = null;
    };
    document.addEventListener('focusin', cancelOnFocus);
    document.addEventListener('pointerdown', cancelOnPointer);
    document.addEventListener('wheel', cancelOnScroll, { passive: true });
    document.addEventListener('keydown', cancelOnScrollKey);
    return () => {
      document.removeEventListener('focusin', cancelOnFocus);
      document.removeEventListener('pointerdown', cancelOnPointer);
      document.removeEventListener('wheel', cancelOnScroll);
      document.removeEventListener('keydown', cancelOnScrollKey);
      detailIntent.current = null;
    };
  }, []);

  useEffect(() => {
    const intent = detailIntent.current;
    if (!intent || !current || disabled || !ready || intent.selection !== currentSelection ||
      intent.selection.identity !== identity || intent.generation !== generation.current) return;
    detailIntent.current = null;
    const ownsFocus = document.activeElement === intent.source ||
      (!intent.source.isConnected && document.activeElement === document.body);
    if (current.status !== 'available' || !ownsFocus) return;
    if (intent.focus) detailHeading.current?.focus({ preventScroll: true });
    detailHeading.current?.scrollIntoView?.({ block: 'start' });
  }, [current, currentSelection, disabled, ready, identity]);

  function showSelection(next: Selection, event: MouseEvent<HTMLButtonElement>) {
    detailIntent.current = { selection: next, source: event.currentTarget,
      focus: event.detail === 0 && document.activeElement === event.currentTarget, generation: null };
    setSelection(next);
  }
  function closeDetails() {
    const returnFocus = !disabled && ready && currentSelection && detailSection.current?.contains(document.activeElement);
    detailIntent.current = null;
    setSelection(null);
    if (returnFocus) {
      const opener = openers.current.get(currentSelection.playlist);
      opener?.focus({ preventScroll: true });
      opener?.scrollIntoView?.({ block: 'nearest' });
    }
  }
  return <div className="classification-change-preview">
    <ul className="classification-change-counts">{quality.playlist_changes.map((change) => <li key={change.name}>
      <strong>{change.name}</strong><span>加入 {change.added_count} 首</span><span>移出 {change.removed_count} 首</span>
      <button className="classification-inline" disabled={disabled || !ready}
        ref={(element) => { if (element) openers.current.set(change.name, element); else openers.current.delete(change.name); }}
        aria-expanded={currentSelection?.playlist === change.name}
        onClick={(event) => showSelection({ playlist: change.name, offset: 0, change: 'all', identity }, event)}>
        逐曲核对{change.name}
      </button>
    </li>)}</ul>
    {selection && !currentSelection && <p role="status">父记录版本已变化，旧变动明细已清除，请重新展开核对。</p>}
    {currentSelection && <section ref={detailSection} className="classification-change-details" aria-label={`歌单变动：${currentSelection.playlist}`}>
      <div className="classification-change-heading"><h3 ref={detailHeading} tabIndex={-1}>{currentSelection.playlist} · 逐曲变动</h3>
        <button className="classification-inline" onClick={closeDetails}>收起逐曲核对</button>
      </div>
      <label>变动方向<select aria-label="变动方向" value={currentSelection.change} disabled={disabled || !ready}
        onChange={(event) => {
          detailIntent.current = null;
          setSelection({ ...currentSelection, offset: 0, change: event.target.value as Selection['change'] });
        }}>
        <option value="all">全部变动</option><option value="added">加入歌单</option><option value="removed">移出歌单</option>
      </select></label>
      {disabled ? <p role="status">父分类记录正在更新，变动明细暂停显示。</p>
        : !ready ? <p role="status">草稿当前不可用于逐曲核对，请刷新分类记录后检查。</p>
          : error ? <div role="alert"><p>{error}</p><button className="classification-inline" onClick={() => setRetry((v) => v + 1)}>重试读取变动</button></div>
            : !current ? <p role="status">正在读取歌单变动…</p>
              : current.status !== 'available' ? <p role="status">本地修正变动资料不可用，请刷新分类记录后重新核对。</p>
                : <>
                  <p>共加入 {current.counts.added} 首、移出 {current.counts.removed} 首；当前方向匹配 {current.pagination.total} 首。</p>
                  {current.records.length === 0 ? <p>当前方向没有变动曲目。</p> : <ol className="classification-changed-tracks">{current.records.map((track) => <li key={track.record_key}>
                    <h4>{track.change === 'added' ? '加入' : '移出'} · <span>{track.name}</span></h4><p>{track.artists || '歌手资料未记录'} · 原序号 {track.position}</p>
                    <dl><dt>风格</dt><dd>{track.before.styles.join('、')} → {track.after.styles.join('、')}</dd>
                      <dt>场景</dt><dd>{track.before.scenes.join('、') || '未指定'} → {track.after.scenes.join('、') || '未指定'}</dd>
                      <dt>语言</dt><dd>{track.before.language} → {track.after.language}</dd></dl>
                    <p>{track.reason}</p><p>录音版本依据：{track.recording_note || '未记录'}</p>
                  </li>)}</ol>}
                  <nav className="classification-pagination" aria-label="歌单变动分页">
                    <button className="button secondary" disabled={current.pagination.offset === 0}
                      onClick={(event) => showSelection({ ...currentSelection, offset: Math.max(0, current.pagination.offset - 50) }, event)}>上一页变动</button>
                    <span>第 {Math.floor(current.pagination.offset / 50) + 1} 页，共 {Math.max(1, Math.ceil(current.pagination.total / 50))} 页</span>
                    <button className="button secondary" disabled={current.pagination.next_offset === null}
                      onClick={(event) => current.pagination.next_offset !== null && showSelection({ ...currentSelection, offset: current.pagination.next_offset }, event)}>下一页变动</button>
                  </nav>
                </>}
    </section>}
  </div>;
}
