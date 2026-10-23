import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';
import App from './App';
import { normalizeState } from './normalize';

const MESSAGE = '本地记录暂时无法完整刷新，仍显示上次读取的资料。';
function fixture(localRecords?: unknown) {
  return {
    connection: { installed: true, configured: true, authorized: null },
    data: {
      account: { nickname: '记录刷新测试用户' }, source: 'local_record',
      playlists: [{ key: '123', name: '上次读取的歌单', category: 'liked', source: 'local_record', track_count: 8 }],
      history: null, artists_completed: false,
    },
    job: {
      id: 'completed-preview', action: 'preview_names', label: '更新歌单清单', status: 'completed',
      result: { status: 'completed', message: '清单任务已完成。' }, logs: [],
    },
    ...(localRecords === undefined ? {} : { local_records: localRecords }),
  };
}
function prepare(read: () => unknown) {
  window.history.replaceState(null, '', '/#overview');
  document.head.innerHTML = '<meta name="organizer-session" content="test-session" />';
  const fetcher = vi.fn(async (url: string, _options?: RequestInit) => {
    if (url === '/api/state') return { ok: true, json: async () => read() };
    throw new Error('unexpected non-state request');
  });
  vi.stubGlobal('fetch', fetcher);
  return fetcher;
}

describe('retained local records status', () => {
  it.each(['current', 'stale'])('keeps only the public %s status', (status) => {
    const value = normalizeState(fixture({ status, message: 'PRIVATE-RAW', path: 'PRIVATE-RAW' }));
    expect(value.local_records).toEqual({ status });
    expect(JSON.stringify(value)).not.toContain('PRIVATE-RAW');
    expect(value.job?.result).toEqual({ status: 'completed', message: '清单任务已完成。' });
  });
  it('supports an older backend without a local records field', () => {
    expect(normalizeState(fixture()).local_records).toEqual({ status: 'current' });
  });
  it.each([null, [], 'stale', {}, { status: 'unknown' }, { status: null }].map((localRecords) => ({ localRecords })))(
    'rejects a malformed present status instead of silently clearing the stale warning (%j)', ({ localRecords }) => {
      expect(() => normalizeState(fixture(localRecords))).toThrow();
    },
  );
  it('keeps an accessible non-dismissible warning across navigation and only links to the existing reread flow', async () => {
    const fetcher = prepare(() => fixture({ status: 'stale', message: 'PRIVATE-RAW' }));
    render(<App />);
    await screen.findByText(MESSAGE);
    const banner = within(screen.getByRole('status', { name: '本地记录读取状态' }));
    expect(banner.queryByRole('button')).toBeNull();
    expect(banner.getByText(/刷新分类记录/)).toBeTruthy();
    expect(banner.getByRole('link', { name: '前往分类成果' }).getAttribute('href')).toBe('#classification');
    expect(screen.getByText('上次读取的歌单')).toBeTruthy();
    fireEvent.click(within(screen.getByRole('navigation', { name: '主导航' })).getByRole('link', { name: /我的歌单/ }));
    expect(screen.getByText(MESSAGE)).toBeTruthy();
    fireEvent.click(within(screen.getByRole('navigation', { name: '主导航' })).getByRole('link', { name: /任务记录/ }));
    await waitFor(() => expect(window.location.hash).toBe('#history'));
    expect(screen.getByText(MESSAGE)).toBeTruthy();
    expect(screen.queryByText('PRIVATE-RAW')).toBeNull();
    expect(fetcher.mock.calls.every((call) => call[0] === '/api/state' && call[1]?.method !== 'POST')).toBe(true);
  });
  it('retains stale records on a disconnected poll and clears the warning only after a current response', async () => {
    vi.useFakeTimers();
    let response: unknown = fixture({ status: 'stale' });
    const fetcher = prepare(() => {
      if (response instanceof Error) throw response;
      return response;
    });
    await act(async () => { render(<App />); });
    expect(screen.getByText(MESSAGE)).toBeTruthy();
    response = new Error('PRIVATE-RAW');
    await act(async () => { await vi.advanceTimersByTimeAsync(1000); });
    expect(screen.getByText(MESSAGE)).toBeTruthy();
    expect(screen.getByText('上次读取的歌单')).toBeTruthy();
    fireEvent.click(screen.getByRole('button', { name: '关闭提示' }));
    expect(screen.getByText(MESSAGE)).toBeTruthy();
    response = fixture({ status: 'current' });
    await act(async () => { await vi.advanceTimersByTimeAsync(1000); });
    expect(screen.queryByText(MESSAGE)).toBeNull();
    expect(screen.getByText('上次读取的歌单')).toBeTruthy();
    expect(fetcher.mock.calls.every((call) => call[0] === '/api/state')).toBe(true);
  });
  it('navigates to the existing refresh and waits for server confirmation before clearing the warning', async () => {
    let localRecords = { status: 'stale' };
    const originalJob = normalizeState(fixture(localRecords)).job;
    const fetcher = prepare(() => fixture(localRecords));
    fetcher.mockImplementation(async (url: string) => {
      if (url === '/api/state') return { ok: true, json: async () => fixture(localRecords) };
      if (!url.startsWith('/api/classification?')) throw new Error('unexpected request');
      if (url.includes('&refresh=local')) localRecords = { status: 'current' };
      return { ok: true, json: async () => ({
        status: 'not_loaded', source: 'local_record', verification: 'local_only', updated_at: null, verified_at: null,
        summary: null, options: { scene: [], style: [], language: [] }, playlists: [], records: [],
        filters: { query: '', dimension: 'all', tag: '', review: 'all' },
        pagination: { offset: 0, limit: 50, total: 0, next_offset: null },
      }) };
    });
    render(<App />);
    await screen.findByText(MESSAGE);
    fireEvent.click(screen.getByRole('link', { name: '前往分类成果' }));
    const refresh = await screen.findByRole('button', { name: '刷新分类记录' });
    await waitFor(() => expect(refresh.hasAttribute('disabled')).toBe(false));
    await waitFor(() => expect(window.location.hash).toBe('#classification'));
    expect(screen.getByText(MESSAGE)).toBeTruthy();
    expect(fetcher.mock.calls.some((call) => call[0].includes('&refresh=local'))).toBe(false);
    fireEvent.click(refresh);
    await waitFor(() => expect(fetcher.mock.calls.filter((call) => call[0].includes('&refresh=local'))).toHaveLength(1));
    await waitFor(() => expect(screen.queryByText(MESSAGE)).toBeNull(), { timeout: 3000 });
    expect(normalizeState(fixture(localRecords)).job).toEqual(originalJob);
    expect(fetcher.mock.calls.every((call) => call[1]?.method !== 'POST')).toBe(true);
  });
});
