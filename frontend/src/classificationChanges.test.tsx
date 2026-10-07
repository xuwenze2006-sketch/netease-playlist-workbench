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
