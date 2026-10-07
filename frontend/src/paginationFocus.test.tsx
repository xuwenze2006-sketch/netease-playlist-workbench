import { act, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { PlaylistDetails } from './PlaylistDetails';
import type { Playlist } from './types';

const playlist: Playlist = {
  key: '9000',
  name: '键盘分页验收',
  track_count: 120,
  category: 'liked',
  source: 'local_record',
};
function page(key = '9000', offset = 0, total = 120, metadata = 'all', query = '') {
  const all = Array.from({ length: total }, (_, index) => ({
    key: `track-${index + 1}`,
    position: index + 1,
    name: `歌曲 ${index + 1}`,
    artists: ['本地歌手'],
    artist_count: 1,
    metadata_available: index !== 0,
  }));
  const matches = all.filter(
    (track) =>
      (metadata === 'all' || !track.metadata_available) && (!query || track.name.includes(query)),
  );
  return {
    status: 'available',
    source: 'local_record',
    metadata_filter: metadata,
    playlist: { ...playlist, key },
    updated_at: '2026-10-04T00:00:00+00:00',
    counts: { expected: total, observed: total, missing: 0, metadata_missing: 1 },
    pagination: {
      offset,
      limit: 50,
      total: matches.length,
      next_offset: offset + 50 < matches.length ? offset + 50 : null,
    },
    tracks: matches.slice(offset, offset + 50),
  };
}
const response = (value: unknown) => ({ ok: true, json: async () => value });
function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (reason: unknown) => void;
  const promise = new Promise<T>((done, fail) => {
    resolve = done;
    reject = fail;
  });
  return { promise, resolve, reject };
}
function setup(total = 120) {
  const next = deferred<ReturnType<typeof response>>();
  let hold = true;
  const fetcher = vi.fn((url: string, _init: RequestInit) => {
    const parsed = new URL(url, 'http://localhost');
    const offset = Number(parsed.searchParams.get('offset'));
    const key = parsed.pathname.split('/')[3];
    if (hold && offset === 50) {
      hold = false;
      return next.promise;
    }
    return Promise.resolve(
      response(
        page(
          key,
          offset,
          total,
          parsed.searchParams.get('metadata') || 'all',
          parsed.searchParams.get('q') || '',
        ),
      ),
    );
  });
  vi.stubGlobal('fetch', fetcher);
  return { fetcher, next };
}
function focusedClick(name: string) {
  const button = screen.getByRole('button', { name });
  button.focus();
  fireEvent.click(button);
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

describe('keyboard focus after asynchronous local pagination', () => {
  it('returns focus to the same available direction on a middle page and to the other direction at each edge', async () => {
    const { fetcher, next } = setup();
    render(<PlaylistDetails playlist={playlist} onClose={() => {}} />);
    await screen.findByRole('table');
    focusedClick('下一页');
    expect(screen.queryByRole('button', { name: '下一页' })).toBeNull();
    await act(async () => {
      next.resolve(response(page('9000', 50)));
    });
    expect(screen.getByText('第 2 页，共 3 页')).toBeTruthy();
    expect(document.activeElement).toBe(screen.getByRole('button', { name: '下一页' }));
    focusedClick('下一页');
    await screen.findByText('第 3 页，共 3 页');
    // The page text can commit before the passive effect restores keyboard focus.
    await waitFor(() =>
      expect(document.activeElement).toBe(screen.getByRole('button', { name: '上一页' })),
    );
    focusedClick('上一页');
    await screen.findByText('第 2 页，共 3 页');
    await waitFor(() =>
      expect(document.activeElement).toBe(screen.getByRole('button', { name: '上一页' })),
    );
    focusedClick('上一页');
    await screen.findByText('第 1 页，共 3 页');
    await waitFor(() =>
      expect(document.activeElement).toBe(screen.getByRole('button', { name: '下一页' })),
    );
    expect(fetcher.mock.calls.every(([, init]) => init.method === 'GET')).toBe(true);
  });
  it('uses search when a changed saved record no longer has an available page button', async () => {
    const { next } = setup();
    render(<PlaylistDetails playlist={playlist} onClose={() => {}} />);
    await screen.findByRole('table');
    focusedClick('下一页');
    await act(async () => {
      next.resolve(
        response({
          ...page('9000', 50),
          status: 'not_loaded',
          updated_at: null,
          counts: { expected: 120, observed: null, missing: null, metadata_missing: null },
          pagination: { offset: 50, limit: 50, total: 0, next_offset: null },
          tracks: [],
        }),
      );
    });
    expect(document.activeElement).toBe(screen.getByRole('textbox', { name: '搜索曲名或歌手' }));
  });
  it('moves focus to the fixed local retry after a failed focused page request', async () => {
    const { fetcher, next } = setup();
    render(<PlaylistDetails playlist={playlist} onClose={() => {}} />);
    await screen.findByRole('table');
    focusedClick('下一页');
    await act(async () => {
      next.reject(new Error('SECRET'));
    });
    expect(screen.getByRole('alert').textContent).toBe('本地歌曲资料暂时无法读取，请重试。');
    expect(document.activeElement).toBe(screen.getByRole('button', { name: '重试读取' }));
    expect(screen.queryByText(/SECRET/)).toBeNull();
    expect(fetcher.mock.calls.every(([, init]) => init.method === 'GET')).toBe(true);
  });
  it('completes the focused page failure and focused retry loop without losing the page direction', async () => {
    const failedPage = deferred<ReturnType<typeof response>>();
    const retryPage = deferred<ReturnType<typeof response>>();
    let pageAttempts = 0;
    const fetcher = vi.fn((url: string, _init: RequestInit) => {
      const offset = Number(new URL(url, 'http://localhost').searchParams.get('offset'));
      if (offset === 50) return ++pageAttempts === 1 ? failedPage.promise : retryPage.promise;
      return Promise.resolve(response(page('9000', offset)));
    });
    vi.stubGlobal('fetch', fetcher);
    render(<PlaylistDetails playlist={playlist} onClose={() => {}} />);
    await screen.findByRole('table');
    focusedClick('下一页');
    await act(async () => {
      failedPage.reject(new Error('SECRET'));
    });
    expect(document.activeElement === screen.getByRole('button', { name: '重试读取' })).toBe(true);
    focusedClick('重试读取');
    expect(screen.queryByRole('button', { name: '重试读取' })).toBeNull();
    await act(async () => {
      retryPage.resolve(response(page('9000', 50)));
    });
    expect(screen.getByText('第 2 页，共 3 页')).toBeTruthy();
    expect(document.activeElement === screen.getByRole('button', { name: '下一页' })).toBe(true);
    expect(fetcher).toHaveBeenCalledTimes(3);
    expect(fetcher.mock.calls.every(([, init]) => init.method === 'GET')).toBe(true);
  });
  it('focuses the local retry when the requested record becomes unavailable', async () => {
    const { next } = setup();
    render(<PlaylistDetails playlist={playlist} onClose={() => {}} />);
    await screen.findByRole('table');
    focusedClick('下一页');
    await act(async () => {
      next.resolve(
        response({
          ...page('9000', 50),
          status: 'unavailable',
          updated_at: null,
          counts: { expected: 120, observed: null, missing: null, metadata_missing: null },
          pagination: { offset: 50, limit: 50, total: 0, next_offset: null },
          tracks: [],
        }),
      );
    });
    expect(document.activeElement).toBe(screen.getByRole('button', { name: '重试读取' }));
  });
  it('does not move focus for a click whose page button was never focused', async () => {
    const { next } = setup();
    render(<PlaylistDetails playlist={playlist} onClose={() => {}} />);
    await screen.findByRole('table');
    const search = screen.getByRole('textbox', { name: '搜索曲名或歌手' });
    search.focus();
    fireEvent.click(screen.getByRole('button', { name: '下一页' }));
    await act(async () => {
      next.resolve(response(page('9000', 50)));
    });
    expect(document.activeElement).toBe(search);
  });
  it('cancels restoration when the user focuses another control during the request', async () => {
    const { next } = setup();
    render(<PlaylistDetails playlist={playlist} onClose={() => {}} />);
    await screen.findByRole('table');
    focusedClick('下一页');
    const close = screen.getByRole('button', { name: '关闭歌曲明细' });
    close.focus();
    close.blur();
    expect(document.activeElement).toBe(document.body);
    await act(async () => {
      next.resolve(response(page('9000', 50)));
    });
    expect(document.activeElement).toBe(document.body);
  });
  it('remembers a focus move outside the dialog even if that control later blurs', async () => {
    const { next } = setup();
    const outside = document.createElement('button');
    outside.textContent = '其他控件';
    document.body.append(outside);
    const view = render(<PlaylistDetails playlist={playlist} onClose={() => {}} />);
    await screen.findByRole('table');
    focusedClick('下一页');
    outside.focus();
    outside.blur();
    await act(async () => {
      next.resolve(response(page('9000', 50)));
    });
    expect(document.activeElement).toBe(document.body);
    view.unmount();
    outside.remove();
  });
  it.each(['query', 'filter'])(
    'cancels the old intent when %s changes even if the old transport ignores abort',
    async (change) => {
      const { next, fetcher } = setup();
      render(<PlaylistDetails playlist={playlist} onClose={() => {}} />);
      await screen.findByRole('table');
      focusedClick('下一页');
      if (change === 'filter') {
        fireEvent.click(screen.getByRole('button', { name: '只看资料不完整' }));
        await screen.findByText('符合筛选 1 首已保存歌曲');
      } else {
        vi.useFakeTimers();
        fireEvent.change(screen.getByRole('textbox', { name: '搜索曲名或歌手' }), {
          target: { value: '歌曲 1' },
        });
        await act(async () => {
          await vi.advanceTimersByTimeAsync(200);
        });
      }
      await act(async () => {
        next.resolve(response(page('9000', 50)));
      });
      expect(document.activeElement).toBe(document.body);
      expect(screen.getByText(/第 1 页/)).toBeTruthy();
      expect(fetcher.mock.calls[1][1].signal?.aborted).toBe(true);
    },
  );
  it('does not steal the trigger focus after closing with a page response still in flight', async () => {
    const { next } = setup();
    const trigger = document.createElement('button');
    trigger.textContent = '详情入口';
    document.body.append(trigger);
    trigger.focus();
    const onClose = vi.fn();
    const view = render(
      <PlaylistDetails playlist={playlist} returnFocus={trigger} onClose={onClose} />,
    );
    await screen.findByRole('table');
    focusedClick('下一页');
    fireEvent.click(screen.getByRole('button', { name: '关闭歌曲明细' }));
    expect(document.activeElement).toBe(trigger);
    await act(async () => {
      next.resolve(response(page('9000', 50)));
    });
    expect(document.activeElement).toBe(trigger);
    expect(onClose).toHaveBeenCalledTimes(1);
    view.unmount();
    trigger.remove();
  });
  it('does not restore an old page focus after changing the playlist key', async () => {
    const { next } = setup();
    const view = render(<PlaylistDetails playlist={playlist} onClose={() => {}} />);
    await screen.findByRole('table');
    focusedClick('下一页');
    view.rerender(
      <PlaylistDetails
        playlist={{ ...playlist, key: '9001', name: '另一个歌单' }}
        onClose={() => {}}
      />,
    );
    await screen.findByRole('table');
    const close = screen.getByRole('button', { name: '关闭歌曲明细' });
    expect(document.activeElement).toBe(close);
    await act(async () => {
      next.resolve(response(page('9000', 50)));
    });
    expect(document.activeElement).toBe(close);
    expect(screen.getByRole('dialog', { name: '另一个歌单的歌曲明细' })).toBeTruthy();
  });
});
