import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import App from './App';

const playlists = [
  { key: '9000', name: '我喜欢的音乐', track_count: 1, category: 'liked', source: 'local_record' },
  { key: '9001', name: '英语', track_count: 72, category: 'normal', source: 'local_record' },
];
function prepare(recovery = false) {
  const state = {
    connection: { installed: true, configured: true, authorized: null },
    update: { status: recovery ? 'review_required' : 'none' },
    recovery: {
      status: recovery ? 'review_required' : 'clear',
      operation: 'unknown',
      can_reconcile: false,
    },
    data: {
      account: { nickname: '详情测试用户' },
      source: 'local_record',
      playlists,
      history: null,
      artists_completed: false,
    },
    job: null,
  };
  const fetcher = vi.fn(async (url: string, _init: RequestInit) => {
    if (url === '/api/state') return { ok: true, json: async () => state };
    const normal = url.startsWith('/api/playlists/9001/');
    return {
      ok: true,
      json: async () => ({
        status: normal ? 'not_loaded' : 'available',
        source: 'local_record',
        playlist: playlists[normal ? 1 : 0],
        updated_at: normal ? null : '2026-10-04T00:00:00+00:00',
        counts: normal
          ? { expected: 72, observed: null, missing: null, metadata_missing: null }
          : { expected: 1, observed: 1, missing: 0, metadata_missing: 0 },
        pagination: { offset: 0, limit: 50, total: normal ? 0 : 1, next_offset: null },
        tracks: normal
          ? []
          : [
              {
                key: 'track-1',
                position: 1,
                name: '本地歌曲',
                artists: ['本地歌手'],
                artist_count: 1,
                metadata_available: true,
              },
            ],
      }),
    };
  });
  vi.stubGlobal('fetch', fetcher);
  return fetcher;
}
beforeEach(() => {
  document.head.innerHTML = '<meta name="organizer-session" content="test-session" />';
  Object.defineProperty(HTMLDialogElement.prototype, 'showModal', {
    configurable: true,
    value() {
      this.open = true;
    },
  });
  Object.defineProperty(HTMLDialogElement.prototype, 'close', {
    configurable: true,
    value() {
      this.open = false;
    },
  });
});
describe('playlist cards open local details', () => {
  it('reads details only after an explicit overview card click and restores its focus on close', async () => {
    const fetcher = prepare();
    render(<App />);
    await screen.findByText('详情测试用户');
    expect(fetcher.mock.calls.every(([url]) => url === '/api/state')).toBe(true);
    const trigger = screen.getByRole('button', { name: '打开我喜欢的音乐的歌曲明细' });
    trigger.focus();
    fireEvent.click(trigger);
    await screen.findByText('本地歌曲');
    expect(screen.getByRole('dialog', { name: '我喜欢的音乐的歌曲明细' })).toBeTruthy();
    fireEvent.click(screen.getByRole('button', { name: '关闭歌曲明细' }));
    expect(screen.queryByRole('dialog')).toBeNull();
    expect(document.activeElement).toBe(trigger);
    expect(fetcher.mock.calls.filter(([url]) => url.includes('/tracks'))).toHaveLength(1);
    expect(fetcher.mock.calls.every(([, init]) => init.method === 'GET')).toBe(true);
  });
  it('opens filtered normal cards while account writes remain frozen without submitting an action', async () => {
    const fetcher = prepare(true);
    render(<App />);
    await screen.findByText('详情测试用户');
    fireEvent.click(
      within(screen.getByRole('navigation', { name: '主导航' })).getByRole('link', {
        name: /我的歌单/,
      }),
    );
    await waitFor(() => expect(window.location.hash).toBe('#playlists'));
    fireEvent.change(screen.getByPlaceholderText('搜索歌单名称'), { target: { value: '英语' } });
    fireEvent.click(screen.getByRole('button', { name: '打开英语的歌曲明细' }));
    await screen.findByText('尚未保存歌曲明细');
    expect(screen.getByRole('dialog', { name: '英语的歌曲明细' })).toBeTruthy();
    expect(fetcher.mock.calls.every(([, init]) => init.method === 'GET')).toBe(true);
  });
});
