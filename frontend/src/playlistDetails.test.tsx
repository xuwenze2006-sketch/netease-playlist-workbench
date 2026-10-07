import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { PlaylistDetails } from './PlaylistDetails';
import type { Playlist } from './types';

const liked: Playlist = {
  key: '9000',
  name: '我喜欢的音乐',
  track_count: 70,
  category: 'liked',
  source: 'local_record',
};
function page(key = '9000', offset = 0, query = '') {
  const positions = query
    ? query === '不存在'
      ? []
      : [7]
    : Array.from(
        { length: Math.min(50, Math.max(0, 62 - offset)) },
        (_, index) => offset + index + 1,
      );
  const total = query ? positions.length : 62;
  return {
    status: 'available',
    source: 'local_record',
    playlist: { ...liked, key },
    updated_at: '2026-10-04T00:00:00+00:00',
    counts: { expected: 67, observed: 62, missing: 5, metadata_missing: 1 },
    pagination: { offset, limit: 50, total, next_offset: offset + 50 < total ? offset + 50 : null },
    tracks: positions.map((position) => ({
      key: `track-${position}`,
      position,
      name: position === 7 ? 'ＳＵＭＭＥＲ · 离线验收' : `歌曲 ${position}`,
      artists: position === 1 ? [] : ['林俊杰'],
      artist_count: position === 1 ? 0 : 1,
      metadata_available: position !== 1,
    })),
  };
}
function response(value: unknown) {
  return { ok: true, json: async () => value };
}
function route() {
  return vi.fn((url: string, _init: RequestInit) => {
    const parsed = new URL(url, 'http://localhost');
    const key = parsed.pathname.split('/')[3];
    return Promise.resolve(
      response(
        page(key, Number(parsed.searchParams.get('offset')), parsed.searchParams.get('q') || ''),
      ),
    );
  });
}
function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((done) => {
    resolve = done;
  });
  return { promise, resolve };
}

beforeEach(() => {
  let meta = document.querySelector<HTMLMetaElement>('meta[name="organizer-session"]');
  if (!meta) {
    meta = document.createElement('meta');
    meta.name = 'organizer-session';
    document.head.append(meta);
  }
  meta.content = 'test-session';
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

describe('saved playlist details drawer', () => {
  it('shows historical completeness, safe metadata and original order in an accessible dialog', async () => {
    const fetcher = route();
    vi.stubGlobal('fetch', fetcher);
    render(<PlaylistDetails playlist={liked} onClose={() => {}} />);
    const dialog = screen.getByRole('dialog', { name: '我喜欢的音乐的歌曲明细' });
    await within(dialog).findByRole('table', { name: '已保存的歌曲' });
    expect(within(dialog).getByText('已保存 62 / 67 首')).toBeTruthy();
    expect(within(dialog).getByText('缺失 5 首')).toBeTruthy();
    expect(within(dialog).getByText('1 首资料不完整')).toBeTruthy();
    expect(within(dialog).getByText('歌手资料暂缺')).toBeTruthy();
    expect(within(dialog).getByText('清单记录 70 首；这份历史明细的总数为 67 首。')).toBeTruthy();
    expect(dialog.querySelector('time')?.dateTime).toBe('2026-10-04T00:00:00+00:00');
    const firstRow = within(dialog).getAllByRole('row')[1];
    expect(within(firstRow).getAllByRole('cell')[0].textContent).toBe('1');
    expect(fetcher.mock.calls.every(([, init]) => (init as RequestInit).method === 'GET')).toBe(
      true,
    );
  });
  it('does not describe an unread normal playlist as empty or missing zero songs', async () => {
    const normal = { ...liked, key: '9001', name: '英语', category: 'normal' as const };
    vi.stubGlobal(
      'fetch',
      vi.fn().mockResolvedValue(
        response({
          ...page('9001'),
          playlist: normal,
          status: 'not_loaded',
          updated_at: null,
          counts: { expected: 70, observed: null, missing: null, metadata_missing: null },
          pagination: { offset: 0, limit: 50, total: 0, next_offset: null },
          tracks: [],
        }),
      ),
    );
    render(<PlaylistDetails playlist={normal} onClose={() => {}} />);
    expect(await screen.findByText('尚未保存歌曲明细')).toBeTruthy();
    expect(screen.getByText('这份歌单已有名称和数量记录，歌曲明细尚未保存到本地。')).toBeTruthy();
    expect(screen.queryByText(/已保存 0|缺失 0/)).toBeNull();
    expect(screen.queryByRole('table')).toBeNull();
  });
  it('recognizes a truly empty available record separately from an unread record', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn().mockResolvedValue(
        response({
          ...page(),
          counts: { expected: 0, observed: 0, missing: 0, metadata_missing: 0 },
          pagination: { offset: 0, limit: 50, total: 0, next_offset: null },
          tracks: [],
        }),
      ),
    );
    render(<PlaylistDetails playlist={liked} onClose={() => {}} />);
    expect(await screen.findByText('这份本地记录没有歌曲')).toBeTruthy();
    expect(screen.getByText('已保存 0 / 0 首')).toBeTruthy();
    expect(screen.queryByText('尚未保存歌曲明细')).toBeNull();
  });
  it.each(['unavailable', 'missing_playlist'])(
    'shows a specific local recovery state for %s without zero-song claims',
    async (status) => {
      vi.stubGlobal(
        'fetch',
        vi.fn().mockResolvedValue(
          response({
            ...page(),
            status,
            playlist: status === 'missing_playlist' ? null : liked,
            updated_at: null,
            counts: {
              expected: status === 'missing_playlist' ? null : 70,
              observed: null,
              missing: null,
              metadata_missing: null,
            },
            pagination: { offset: 0, limit: 50, total: 0, next_offset: null },
            tracks: [],
          }),
        ),
      );
      render(<PlaylistDetails playlist={liked} onClose={() => {}} />);
      expect(
        await screen.findByText(status === 'unavailable' ? '本地明细暂不可用' : '歌单记录已更新'),
      ).toBeTruthy();
      expect(screen.queryByRole('table')).toBeNull();
      expect(screen.queryByText(/已保存 0|缺失 0/)).toBeNull();
      expect(screen.queryByRole('button', { name: '重试读取' }) !== null).toBe(
        status === 'unavailable',
      );
    },
  );
  it('shows a removed playlist after the backend HTTP 404 response instead of a generic retry', async () => {
    const fetcher = vi
      .fn()
      .mockResolvedValue(
        new Response(
          JSON.stringify({
            accepted: false,
            message: '该歌单已不在本地目录中，请更新页面后重试。',
            private_key: 'SECRET',
          }),
          { status: 404, headers: { 'Content-Type': 'application/json' } },
        ),
      );
    vi.stubGlobal('fetch', fetcher);
    render(<PlaylistDetails playlist={liked} onClose={() => {}} />);
    expect(await screen.findByText('歌单记录已更新')).toBeTruthy();
    expect(screen.getByText('当前本地清单中已没有这份歌单，请关闭后重新选择。')).toBeTruthy();
    expect(screen.queryByRole('button', { name: '重试读取' })).toBeNull();
    expect(screen.queryByText(/SECRET/)).toBeNull();
    expect(fetcher).toHaveBeenCalledTimes(1);
    expect(fetcher.mock.calls[0][1].method).toBe('GET');
  });
  it.each([
    { status: 404, body: { accepted: false, message: '其他路径不存在。SECRET' } },
    {
      status: 404,
      body: { accepted: true, message: '该歌单已不在本地目录中，请更新页面后重试。' },
    },
    {
      status: 500,
      body: { accepted: false, message: '该歌单已不在本地目录中，请更新页面后重试。' },
    },
  ])(
    'keeps unrelated HTTP errors on the fixed retry path: $status $body.accepted',
    async ({ status, body }) => {
      vi.stubGlobal(
        'fetch',
        vi.fn().mockResolvedValue(new Response(JSON.stringify(body), { status })),
      );
      render(<PlaylistDetails playlist={liked} onClose={() => {}} />);
      expect(await screen.findByRole('alert')).toHaveProperty(
        'textContent',
        '本地歌曲资料暂时无法读取，请重试。',
      );
      expect(screen.queryByText('歌单记录已更新')).toBeNull();
      expect(screen.queryByText(/SECRET/)).toBeNull();
    },
  );
  it('debounces search, retains matched original positions and resets pagination', async () => {
    const fetcher = route();
    vi.stubGlobal('fetch', fetcher);
    render(<PlaylistDetails playlist={liked} onClose={() => {}} />);
    await screen.findByRole('table');
    fireEvent.click(screen.getByRole('button', { name: '下一页' }));
    await screen.findByText('歌曲 51');
    expect(fetcher.mock.calls[1][0]).toContain('offset=50&limit=50');
    vi.useFakeTimers();
    fireEvent.change(screen.getByRole('textbox', { name: '搜索曲名或歌手' }), {
      target: { value: ' summer ' },
    });
    await act(async () => {
      await vi.advanceTimersByTimeAsync(199);
    });
    expect(fetcher).toHaveBeenCalledTimes(2);
    await act(async () => {
      await vi.advanceTimersByTimeAsync(1);
    });
    expect(fetcher.mock.calls[2][0]).toBe('/api/playlists/9000/tracks?offset=0&limit=50&q=summer');
    const rows = screen.getAllByRole('row');
    expect(rows).toHaveLength(2);
    expect(within(rows[1]).getAllByRole('cell')[0].textContent).toBe('7');
    expect(screen.getByText('第 1 页，共 1 页')).toBeTruthy();
  });
  it('shows a filtered empty state with an explicit local search reset', async () => {
    const fetcher = route();
    vi.stubGlobal('fetch', fetcher);
    render(<PlaylistDetails playlist={liked} onClose={() => {}} />);
    await screen.findByRole('table');
    vi.useFakeTimers();
    fireEvent.change(screen.getByRole('textbox', { name: '搜索曲名或歌手' }), {
      target: { value: '不存在' },
    });
    await act(async () => {
      await vi.advanceTimersByTimeAsync(200);
    });
    expect(screen.getByText('没有匹配的已保存歌曲')).toBeTruthy();
    fireEvent.click(screen.getByRole('button', { name: '清除歌曲搜索' }));
    await act(async () => {
      await vi.advanceTimersByTimeAsync(200);
    });
    expect(screen.getAllByRole('row')).toHaveLength(51);
    expect(fetcher.mock.calls.every(([, init]) => (init as RequestInit).method === 'GET')).toBe(
      true,
    );
  });
  it('ignores a late old selection response and aborts it without account actions', async () => {
    const old = deferred<ReturnType<typeof response>>();
    const fetcher = vi
      .fn()
      .mockReturnValueOnce(old.promise)
      .mockResolvedValue(response(page('9002')));
    vi.stubGlobal('fetch', fetcher);
    const view = render(<PlaylistDetails playlist={liked} onClose={() => {}} />);
    view.rerender(
      <PlaylistDetails
        playlist={{ ...liked, key: '9002', name: '另一个本地歌单' }}
        onClose={() => {}}
      />,
    );
    await screen.findByRole('table');
    expect(fetcher.mock.calls[0][1].signal.aborted).toBe(true);
    await act(async () => {
      old.resolve(
        response({
          ...page(),
          tracks: page().tracks.map((track) => ({ ...track, name: '旧请求不应覆盖' })),
        }),
      );
    });
    expect(screen.queryByText('旧请求不应覆盖')).toBeNull();
    expect(screen.getByRole('dialog', { name: '另一个本地歌单的歌曲明细' })).toBeTruthy();
  });
  it('ignores a late previous search response even when the transport ignores abort', async () => {
    const old = deferred<ReturnType<typeof response>>();
    const fetcher = vi
      .fn()
      .mockResolvedValueOnce(response(page()))
      .mockReturnValueOnce(old.promise)
      .mockResolvedValue(response(page('9000', 0, 'summer')));
    vi.stubGlobal('fetch', fetcher);
    render(<PlaylistDetails playlist={liked} onClose={() => {}} />);
    await screen.findByRole('table');
    vi.useFakeTimers();
    fireEvent.change(screen.getByRole('textbox', { name: '搜索曲名或歌手' }), {
      target: { value: 'first' },
    });
    await act(async () => {
      await vi.advanceTimersByTimeAsync(200);
    });
    fireEvent.change(screen.getByRole('textbox', { name: '搜索曲名或歌手' }), {
      target: { value: 'summer' },
    });
    await act(async () => {
      await vi.advanceTimersByTimeAsync(200);
    });
    await act(async () => {
      old.resolve(
        response({
          ...page('9000', 0, 'first'),
          tracks: [{ ...page().tracks[0], name: '旧搜索不应覆盖' }],
        }),
      );
    });
    expect(screen.queryByText('旧搜索不应覆盖')).toBeNull();
    expect(screen.getByText('ＳＵＭＭＥＲ · 离线验收')).toBeTruthy();
  });
  it('keeps errors fixed and retries only the local GET', async () => {
    const fetcher = vi
      .fn()
      .mockRejectedValueOnce(new Error('SECRET'))
      .mockResolvedValue(response(page()));
    vi.stubGlobal('fetch', fetcher);
    render(<PlaylistDetails playlist={liked} onClose={() => {}} />);
    expect(await screen.findByRole('alert')).toHaveProperty(
      'textContent',
      '本地歌曲资料暂时无法读取，请重试。',
    );
    expect(screen.queryByText(/SECRET/)).toBeNull();
    fireEvent.click(screen.getByRole('button', { name: '重试读取' }));
    await screen.findByRole('table');
    expect(fetcher.mock.calls.every(([, init]) => (init as RequestInit).method === 'GET')).toBe(
      true,
    );
  });
  it('rejects malformed pages without rendering raw fields', async () => {
    vi.stubGlobal(
      'fetch',
      vi
        .fn()
        .mockResolvedValue(
          response({ ...page(), playlist: { ...liked, key: '9001' }, private_key: 'SECRET' }),
        ),
    );
    render(<PlaylistDetails playlist={liked} onClose={() => {}} />);
    await screen.findByRole('alert');
    expect(screen.queryByRole('table')).toBeNull();
    expect(screen.queryByText(/SECRET/)).toBeNull();
  });
  it('shows additional saved artists without hiding truncation or metadata gaps', async () => {
    const raw = page();
    raw.tracks[0] = {
      ...raw.tracks[0],
      artists: ['歌手甲', '歌手乙'],
      artist_count: 10,
      metadata_available: false,
    };
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(response(raw)));
    render(<PlaylistDetails playlist={liked} onClose={() => {}} />);
    await screen.findByRole('table');
    expect(screen.getByText('另有 8 位已记录歌手')).toBeTruthy();
    expect(screen.getByText('歌手资料不完整')).toBeTruthy();
  });
  it('cycles keyboard focus within the native dialog and restores the trigger on Escape', async () => {
    vi.stubGlobal('fetch', route());
    const trigger = document.createElement('button');
    trigger.textContent = '歌曲明细入口';
    document.body.append(trigger);
    trigger.focus();
    const close = vi.fn();
    const view = render(<PlaylistDetails playlist={liked} onClose={close} returnFocus={trigger} />);
    await screen.findByRole('table');
    const first = screen.getByRole('button', { name: '关闭歌曲明细' });
    const last = screen.getByRole('button', { name: '下一页' });
    expect(document.activeElement).toBe(first);
    fireEvent.keyDown(first, { key: 'Tab', shiftKey: true });
    expect(document.activeElement).toBe(last);
    fireEvent.keyDown(last, { key: 'Tab' });
    expect(document.activeElement).toBe(first);
    fireEvent.keyDown(first, { key: 'Escape' });
    expect(close).toHaveBeenCalledTimes(1);
    expect(document.activeElement).toBe(trigger);
    view.unmount();
    trigger.remove();
  });
  it('aborts pending local reads when closed', async () => {
    const fetcher = vi.fn(
      (_url, init) =>
        new Promise((_resolve, reject) => {
          init.signal.addEventListener('abort', () => reject(new Error('cancel')), { once: true });
        }),
    );
    vi.stubGlobal('fetch', fetcher);
    const view = render(<PlaylistDetails playlist={liked} onClose={() => {}} />);
    await waitFor(() => expect(fetcher).toHaveBeenCalledTimes(1));
    view.unmount();
    expect(fetcher.mock.calls[0][1].signal.aborted).toBe(true);
    expect(fetcher).toHaveBeenCalledTimes(1);
  });
});
