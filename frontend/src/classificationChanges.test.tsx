import { act, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { ClassificationChanges } from './ClassificationChanges';
import { fetchClassificationChanges } from './api';
import type { ClassificationChangesPage, ClassificationQuality } from './types';

vi.mock('./api', async (original) => ({ ...(await original<typeof import('./api')>()), fetchClassificationChanges: vi.fn() }));
const quality: ClassificationQuality = { source_version: 'a'.repeat(64), revision: 2, draft_status: 'ready', changed_count: 51,
  review_count: 0, pilot_positions: [], rules: [], playlist_changes: [{ name: '语言 · 英语', added_count: 50, removed_count: 1 }] };
function page(offset = 0, change = 'all'): ClassificationChangesPage {
  const total = change === 'all' ? 51 : change === 'added' ? 50 : 1;
  return { status: 'available', source: 'local_correction_draft', source_version: quality.source_version, revision: quality.revision,
    draft_status: 'ready', playlist: '语言 · 英语', filters: { change }, counts: { added: 50, removed: 1 },
    pagination: { offset, limit: 50, total, next_offset: offset + 50 < total ? offset + 50 : null },
    records: Array.from({ length: Math.min(50, Math.max(0, total - offset)) }, (_, index) => ({ position: offset + index + 1,
      name: `待核对歌曲 ${offset + index + 1}`, artists: '测试歌手', record_key: (offset + index + 1).toString(16).padStart(32, '0'),
      change: offset + index === 50 || change === 'removed' ? 'removed' : 'added', before: { styles: ['流行抒情'], scenes: [], language: '国语' },
      after: { styles: ['流行抒情'], scenes: [], language: '英语' }, reason: `核对理由 ${offset + index + 1}`, recording_note: '专辑版本' })),
  } as ClassificationChangesPage;
}
beforeEach(() => {
  document.head.innerHTML = '<meta name="organizer-session" content="changes-test" />';
  vi.mocked(fetchClassificationChanges).mockReset();
  vi.mocked(fetchClassificationChanges).mockImplementation(async (_session, request) => page(request.offset, request.change));
});
describe('lazy classification playlist changes', () => {
  it('focuses and scrolls the detail heading after keyboard expansion and successful keyboard paging', async () => {
    let complete!: (value: ClassificationChangesPage) => void;
    vi.mocked(fetchClassificationChanges).mockImplementationOnce(() => new Promise((resolve) => { complete = resolve; }));
    render(<ClassificationChanges quality={quality} disabled={false} />);
    const open = screen.getByRole('button', { name: '逐曲核对语言 · 英语' });
    open.focus(); fireEvent.click(open, { detail: 0 });
    const heading = screen.getByRole('heading', { name: '语言 · 英语 · 逐曲变动' });
    const scroll = vi.fn(); heading.scrollIntoView = scroll;
    await act(async () => { complete(page()); });
    expect(document.activeElement).toBe(heading);
    expect(scroll).toHaveBeenCalledWith({ block: 'start' });
    const next = screen.getByRole('button', { name: '下一页变动' });
    next.focus(); fireEvent.click(next, { detail: 0 });
    await screen.findByText('待核对歌曲 51');
    await waitFor(() => expect(document.activeElement).toBe(heading));
    expect(scroll).toHaveBeenCalledTimes(2);
  });

  it('does not move focus or scroll after the user leaves the initiating control while a read is pending', async () => {
    let complete!: (value: ClassificationChangesPage) => void;
    vi.mocked(fetchClassificationChanges).mockImplementationOnce(() => new Promise((resolve) => { complete = resolve; }));
    render(<><button>其他操作</button><ClassificationChanges quality={quality} disabled={false} /></>);
    const open = screen.getByRole('button', { name: '逐曲核对语言 · 英语' });
    open.focus(); fireEvent.click(open, { detail: 0 });
    const heading = screen.getByRole('heading', { name: '语言 · 英语 · 逐曲变动' });
    const scroll = vi.fn(); heading.scrollIntoView = scroll;
    const other = screen.getByRole('button', { name: '其他操作' }); other.focus();
    await act(async () => { complete(page()); });
    expect(document.activeElement).toBe(other); expect(scroll).not.toHaveBeenCalled();
  });

  it('does not restore paging focus after the user moves to another control while the next page loads', async () => {
    render(<><button>其他操作</button><ClassificationChanges quality={quality} disabled={false} /></>);
    const open = screen.getByRole('button', { name: '逐曲核对语言 · 英语' }); open.focus(); fireEvent.click(open, { detail: 0 });
    await screen.findByText('待核对歌曲 1');
    let complete!: (value: ClassificationChangesPage) => void;
    vi.mocked(fetchClassificationChanges).mockImplementationOnce(() => new Promise((resolve) => { complete = resolve; }));
    const heading = screen.getByRole('heading', { name: '语言 · 英语 · 逐曲变动' });
    const focus = vi.spyOn(heading, 'focus'), scroll = vi.fn(); heading.scrollIntoView = scroll;
    const next = screen.getByRole('button', { name: '下一页变动' }); next.focus(); fireEvent.click(next, { detail: 0 });
    const other = screen.getByRole('button', { name: '其他操作' }); other.focus();
    await act(async () => { complete(page(50)); });
    expect(screen.getByText('待核对歌曲 51')).toBeTruthy();
    expect(document.activeElement).toBe(other); expect(focus).not.toHaveBeenCalled(); expect(scroll).not.toHaveBeenCalled();
  });

  it.each(['PageDown', 'ArrowDown', ' '] as const)('does not restore paging focus after keyboard scroll %j while focus is on the body', async (key) => {
    render(<ClassificationChanges quality={quality} disabled={false} />);
    const open = screen.getByRole('button', { name: '逐曲核对语言 · 英语' }); open.focus(); fireEvent.click(open, { detail: 0 });
    await screen.findByText('待核对歌曲 1');
    let complete!: (value: ClassificationChangesPage) => void;
    vi.mocked(fetchClassificationChanges).mockImplementationOnce(() => new Promise((resolve) => { complete = resolve; }));
    const heading = screen.getByRole('heading', { name: '语言 · 英语 · 逐曲变动' });
    const focus = vi.spyOn(heading, 'focus'), scroll = vi.fn(); heading.scrollIntoView = scroll;
    const next = screen.getByRole('button', { name: '下一页变动' }); next.focus(); fireEvent.click(next, { detail: 0 });
    expect(next.isConnected).toBe(false); expect(document.activeElement).toBe(document.body);
    fireEvent.keyDown(document.body, { key });
    await act(async () => { complete(page(50)); });
    expect(screen.getByText('待核对歌曲 51')).toBeTruthy();
    expect(document.activeElement).toBe(document.body); expect(focus).not.toHaveBeenCalled(); expect(scroll).not.toHaveBeenCalled();
  });

  it('keeps normal Enter activation and its successful paging focus', async () => {
    render(<ClassificationChanges quality={quality} disabled={false} />);
    const open = screen.getByRole('button', { name: '逐曲核对语言 · 英语' }); open.focus();
    fireEvent.keyDown(open, { key: 'Enter' }); fireEvent.click(open, { detail: 0 });
    await screen.findByText('待核对歌曲 1');
    let complete!: (value: ClassificationChangesPage) => void;
    vi.mocked(fetchClassificationChanges).mockImplementationOnce(() => new Promise((resolve) => { complete = resolve; }));
    const heading = screen.getByRole('heading', { name: '语言 · 英语 · 逐曲变动' });
    const scroll = vi.fn(); heading.scrollIntoView = scroll;
    const next = screen.getByRole('button', { name: '下一页变动' }); next.focus();
    fireEvent.keyDown(next, { key: 'Enter' }); fireEvent.click(next, { detail: 0 });
    fireEvent.keyDown(document.body, { key: 'Enter' });
    await act(async () => { complete(page(50)); });
    expect(document.activeElement).toBe(heading); expect(scroll).toHaveBeenCalledWith({ block: 'start' });
  });

  it.each(['blur', 'pointer', 'wheel'] as const)('does not reclaim focus or scroll after user %s during an expansion read', async (action) => {
    let complete!: (value: ClassificationChangesPage) => void;
    vi.mocked(fetchClassificationChanges).mockImplementationOnce(() => new Promise((resolve) => { complete = resolve; }));
    render(<><p>其他内容</p><ClassificationChanges quality={quality} disabled={false} /></>);
    const open = screen.getByRole('button', { name: '逐曲核对语言 · 英语' }); open.focus(); fireEvent.click(open, { detail: 0 });
    const heading = screen.getByRole('heading', { name: '语言 · 英语 · 逐曲变动' });
    const focus = vi.spyOn(heading, 'focus'), scroll = vi.fn(); heading.scrollIntoView = scroll;
    if (action === 'blur') open.blur();
    else if (action === 'pointer') fireEvent.pointerDown(screen.getByText('其他内容'));
    else fireEvent.wheel(document, { deltaY: 100 });
    await act(async () => { complete(page()); });
    expect(focus).not.toHaveBeenCalled(); expect(scroll).not.toHaveBeenCalled();
  });

  it('returns focus to the opener on close only when focus belongs to the detail', async () => {
    render(<><button>其他操作</button><ClassificationChanges quality={quality} disabled={false} /></>);
    const open = screen.getByRole('button', { name: '逐曲核对语言 · 英语' });
    open.focus(); fireEvent.click(open, { detail: 0 });
    await screen.findByText('待核对歌曲 1');
    const close = screen.getByRole('button', { name: '收起逐曲核对' }); close.focus();
    fireEvent.click(close, { detail: 0 });
    expect(document.activeElement).toBe(open);
    open.focus(); fireEvent.click(open, { detail: 0 }); await screen.findByText('待核对歌曲 1');
    const other = screen.getByRole('button', { name: '其他操作' }); other.focus();
    fireEvent.click(screen.getByRole('button', { name: '收起逐曲核对' }), { detail: 0 });
    expect(document.activeElement).toBe(other);
  });

  it('does not focus or scroll a detail whose read fails', async () => {
    vi.mocked(fetchClassificationChanges).mockRejectedValueOnce(new Error('变动读取失败'));
    render(<ClassificationChanges quality={quality} disabled={false} />);
    const open = screen.getByRole('button', { name: '逐曲核对语言 · 英语' }); open.focus();
    fireEvent.click(open, { detail: 0 });
    const heading = screen.getByRole('heading', { name: '语言 · 英语 · 逐曲变动' });
    const scroll = vi.fn(); heading.scrollIntoView = scroll;
    await screen.findByRole('alert');
    expect(document.activeElement).toBe(open); expect(scroll).not.toHaveBeenCalled();
  });

  it('returns keyboard paging to retry after a delayed failure without showing the old page', async () => {
    render(<ClassificationChanges quality={quality} disabled={false} />);
    fireEvent.click(screen.getByRole('button', { name: '逐曲核对语言 · 英语' }));
    await screen.findByText('待核对歌曲 1');
    let reject!: (cause: Error) => void;
    vi.mocked(fetchClassificationChanges).mockImplementationOnce(() => new Promise((_resolve, fail) => { reject = fail; }));
    const next = screen.getByRole('button', { name: '下一页变动' });
    next.focus(); fireEvent.click(next, { detail: 0 });
    expect(screen.queryByText('待核对歌曲 1')).toBeNull();
    await act(async () => { reject(new Error('变动读取失败')); });
    expect(document.activeElement).toBe(screen.getByRole('button', { name: '重试读取变动' }));
    expect(screen.queryByText('待核对歌曲 1')).toBeNull();
  });

  it('returns a successful keyboard retry to the detail heading', async () => {
    vi.mocked(fetchClassificationChanges).mockRejectedValueOnce(new Error('变动读取失败'));
    render(<ClassificationChanges quality={quality} disabled={false} />);
    fireEvent.click(screen.getByRole('button', { name: '逐曲核对语言 · 英语' }));
    const retry = await screen.findByRole('button', { name: '重试读取变动' });
    let complete!: (value: ClassificationChangesPage) => void;
    vi.mocked(fetchClassificationChanges).mockImplementationOnce(() => new Promise((resolve) => { complete = resolve; }));
    const heading = screen.getByRole('heading', { name: '语言 · 英语 · 逐曲变动' });
    const scroll = vi.fn(); heading.scrollIntoView = scroll;
    retry.focus(); fireEvent.click(retry, { detail: 0 });
    await act(async () => { complete(page()); });
    expect(screen.getByText('待核对歌曲 1')).toBeTruthy();
    expect(document.activeElement).toBe(heading);
    expect(scroll).toHaveBeenCalledWith({ block: 'start' });
  });

  it('keeps a failed keyboard retry reachable after its initiating button is replaced', async () => {
    vi.mocked(fetchClassificationChanges).mockRejectedValueOnce(new Error('首次读取失败'));
    render(<ClassificationChanges quality={quality} disabled={false} />);
    fireEvent.click(screen.getByRole('button', { name: '逐曲核对语言 · 英语' }));
    const retry = await screen.findByRole('button', { name: '重试读取变动' });
    let reject!: (cause: Error) => void;
    vi.mocked(fetchClassificationChanges).mockImplementationOnce(() => new Promise((_resolve, fail) => { reject = fail; }));
    retry.focus(); fireEvent.click(retry, { detail: 0 });
    await act(async () => { reject(new Error('再次读取失败')); });
    expect(screen.getByRole('alert').textContent).toContain('再次读取失败');
    expect(document.activeElement).toBe(screen.getByRole('button', { name: '重试读取变动' }));
  });

  describe.each(['paging failure', 'retry success'] as const)('%s focus ownership', (phase) => {
    it.each(['focus-blur', 'pointer', 'wheel', 'scroll-key', 'touch'] as const)(
      'does not restore focus after user %s while the request is pending', async (movement) => {
        if (phase === 'retry success') vi.mocked(fetchClassificationChanges).mockRejectedValueOnce(new Error('首次读取失败'));
        render(<><button>其他操作</button><p>其他内容</p><ClassificationChanges quality={quality} disabled={false} /></>);
        fireEvent.click(screen.getByRole('button', { name: '逐曲核对语言 · 英语' }));
        const source = await screen.findByRole('button', { name: phase === 'paging failure' ? '下一页变动' : '重试读取变动' });
        let complete!: (value: ClassificationChangesPage) => void, reject!: (cause: Error) => void;
        vi.mocked(fetchClassificationChanges).mockImplementationOnce(() => new Promise((resolve, fail) => { complete = resolve; reject = fail; }));
        const heading = screen.getByRole('heading', { name: '语言 · 英语 · 逐曲变动' });
        const focus = vi.spyOn(heading, 'focus'), scroll = vi.fn(); heading.scrollIntoView = scroll;
        source.focus(); fireEvent.click(source, { detail: 0 });
        expect(document.activeElement).toBe(document.body);
        if (movement === 'focus-blur') {
          const other = screen.getByRole('button', { name: '其他操作' }); other.focus(); other.blur();
        } else if (movement === 'pointer') fireEvent.pointerDown(screen.getByText('其他内容'));
        else if (movement === 'wheel') fireEvent.wheel(document, { deltaY: 100 });
        else if (movement === 'scroll-key') fireEvent.keyDown(document.body, { key: 'PageDown' });
        else fireEvent.touchStart(screen.getByText('其他内容'));
        await act(async () => {
          if (phase === 'paging failure') reject(new Error('变动读取失败'));
          else complete(page());
        });
        expect(document.activeElement).toBe(document.body);
        expect(focus).not.toHaveBeenCalled(); expect(scroll).not.toHaveBeenCalled();
      },
    );

    it('does not turn pointer activation into keyboard focus recovery', async () => {
      if (phase === 'retry success') vi.mocked(fetchClassificationChanges).mockRejectedValueOnce(new Error('首次读取失败'));
      render(<ClassificationChanges quality={quality} disabled={false} />);
      fireEvent.click(screen.getByRole('button', { name: '逐曲核对语言 · 英语' }));
      const source = await screen.findByRole('button', { name: phase === 'paging failure' ? '下一页变动' : '重试读取变动' });
      let complete!: (value: ClassificationChangesPage) => void, reject!: (cause: Error) => void;
      vi.mocked(fetchClassificationChanges).mockImplementationOnce(() => new Promise((resolve, fail) => { complete = resolve; reject = fail; }));
      source.focus(); fireEvent.click(source, { detail: 1 });
      await act(async () => {
        if (phase === 'paging failure') reject(new Error('变动读取失败'));
        else complete(page());
      });
      expect(document.activeElement).toBe(document.body);
    });
  });

  it.each(['source', 'revision', 'disabled'] as const)('ignores a pending retry after parent %s changes', async (kind) => {
    vi.mocked(fetchClassificationChanges).mockRejectedValueOnce(new Error('首次读取失败'));
    const view = render(<ClassificationChanges quality={quality} disabled={false} />);
    fireEvent.click(screen.getByRole('button', { name: '逐曲核对语言 · 英语' }));
    const retry = await screen.findByRole('button', { name: '重试读取变动' });
    let complete!: (value: ClassificationChangesPage) => void;
    vi.mocked(fetchClassificationChanges).mockImplementationOnce(() => new Promise((resolve) => { complete = resolve; }));
    const heading = screen.getByRole('heading', { name: '语言 · 英语 · 逐曲变动' });
    const focus = vi.spyOn(heading, 'focus'), scroll = vi.fn(); heading.scrollIntoView = scroll;
    retry.focus(); fireEvent.click(retry, { detail: 0 });
    const signal = vi.mocked(fetchClassificationChanges).mock.lastCall?.[2];
    const changed = { ...quality, ...(kind === 'source' ? { source_version: 'c'.repeat(64) } : kind === 'revision' ? { revision: 3 } : {}) };
    view.rerender(<ClassificationChanges quality={changed} disabled={kind === 'disabled'} />);
    expect(signal?.aborted).toBe(true);
    await act(async () => { complete(page()); });
    expect(screen.queryByText('待核对歌曲 1')).toBeNull();
    expect(focus).not.toHaveBeenCalled(); expect(scroll).not.toHaveBeenCalled();
  });

  it.each(['source', 'revision', 'disabled'] as const)('does not focus a late result after parent %s changes', async (kind) => {
    let complete!: (value: ClassificationChangesPage) => void;
    vi.mocked(fetchClassificationChanges).mockImplementationOnce(() => new Promise((resolve) => { complete = resolve; }));
    const view = render(<ClassificationChanges quality={quality} disabled={false} />);
    const open = screen.getByRole('button', { name: '逐曲核对语言 · 英语' }); open.focus(); fireEvent.click(open, { detail: 0 });
    const heading = screen.getByRole('heading', { name: '语言 · 英语 · 逐曲变动' });
    const focus = vi.spyOn(heading, 'focus'), scroll = vi.fn(); heading.scrollIntoView = scroll;
    const changed = { ...quality, ...(kind === 'source' ? { source_version: 'c'.repeat(64) } : kind === 'revision' ? { revision: 3 } : {}) };
    view.rerender(<ClassificationChanges quality={changed} disabled={kind === 'disabled'} />);
    await act(async () => { complete(page()); });
    expect(focus).not.toHaveBeenCalled(); expect(scroll).not.toHaveBeenCalled();
  });

  it('loads actual changed tracks only when opened and pages the same playlist', async () => {
    await act(async () => { render(<ClassificationChanges quality={quality} disabled={false} />); });
    expect(vi.mocked(fetchClassificationChanges)).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole('button', { name: '逐曲核对语言 · 英语' }));
    await screen.findByText('待核对歌曲 1');
    expect(screen.getByText('核对理由 1')).toBeTruthy();
    expect(screen.getAllByText('录音版本依据：专辑版本')).toHaveLength(50);
    fireEvent.click(screen.getByRole('button', { name: '下一页变动' }));
    await screen.findByText('待核对歌曲 51');
    expect(screen.queryByText('待核对歌曲 1')).toBeNull();
    expect(vi.mocked(fetchClassificationChanges).mock.lastCall?.[1]).toMatchObject({ playlist: '语言 · 英语', revision: 2, source_version: quality.source_version, offset: 50 });
    fireEvent.change(screen.getByLabelText('变动方向'), { target: { value: 'removed' } });
    await waitFor(() => expect(vi.mocked(fetchClassificationChanges).mock.lastCall?.[1]).toMatchObject({ change: 'removed', offset: 0 }));
  });
  it.each(['revision', 'source'] as const)('clears old details and aborts when the parent %s changes', async (identity) => {
    let complete!: (value: ClassificationChangesPage) => void;
    vi.mocked(fetchClassificationChanges).mockImplementationOnce(() => new Promise((resolve) => { complete = resolve; }));
    const view = render(<ClassificationChanges quality={quality} disabled={false} />);
    fireEvent.click(screen.getByRole('button', { name: '逐曲核对语言 · 英语' }));
    await waitFor(() => expect(vi.mocked(fetchClassificationChanges)).toHaveBeenCalledTimes(1));
    const signal = vi.mocked(fetchClassificationChanges).mock.lastCall?.[2];
    view.rerender(<ClassificationChanges quality={{ ...quality,
      ...(identity === 'revision' ? { revision: 3 } : { source_version: 'c'.repeat(64) }) }} disabled={false} />);
    expect(signal?.aborted).toBe(true);
    await act(async () => { complete(page()); });
    expect(screen.queryByText('待核对歌曲 1')).toBeNull();
    expect(screen.getByText(/父记录版本已变化.*重新展开核对/)).toBeTruthy();
  });
  it('reports read failures without displaying a previous page as current', async () => {
    render(<ClassificationChanges quality={quality} disabled={false} />);
    fireEvent.click(screen.getByRole('button', { name: '逐曲核对语言 · 英语' }));
    await screen.findByText('待核对歌曲 1');
    vi.mocked(fetchClassificationChanges).mockRejectedValueOnce(new Error('修订版本已变化，请刷新父记录。'));
    fireEvent.click(screen.getByRole('button', { name: '下一页变动' }));
    await screen.findByRole('alert');
    expect(screen.queryByText('待核对歌曲 1')).toBeNull();
    expect(screen.getByText('修订版本已变化，请刷新父记录。')).toBeTruthy();
  });
});
