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
function moveWhileWaiting(movement: string) {
  const content = document.querySelector('.details-body')!;
  if (movement === 'wheel') fireEvent.wheel(content, { deltaY: 120 });
  else if (movement === 'pointer') fireEvent.pointerDown(content);
  else if (movement === 'touch') fireEvent.touchStart(content);
  else fireEvent.keyDown(document.activeElement!, { key: movement });
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
  it.each([
    { total: 1, moveFocus: false }, { total: 0, moveFocus: false },
    { total: 1, moveFocus: true }, { total: 0, moveFocus: true },
  ])(
    'recovers a shrinking saved page while respecting keyboard focus: %j',
    async ({ total, moveFocus }) => {
      const next = deferred<ReturnType<typeof response>>();
      const first = deferred<ReturnType<typeof response>>();
      let shrinking = false;
      const savedPage = (offset: number, count: number, metadata: string) => {
        const result = page('9000', offset, count);
        return {
          ...result,
          metadata_filter: metadata,
          counts: { ...result.counts, metadata_missing: count },
          tracks: result.tracks.map((track) => ({ ...track, metadata_available: false })),
        };
      };
      const fetcher = vi.fn((url: string, _init: RequestInit) => {
        const parsed = new URL(url, 'http://localhost');
        const offset = Number(parsed.searchParams.get('offset'));
        if (offset === 50) {
          shrinking = true;
          return next.promise;
        }
        if (shrinking) return first.promise;
        return Promise.resolve(
          response(savedPage(offset, 120, parsed.searchParams.get('metadata') || 'all')),
        );
      });
      vi.stubGlobal('fetch', fetcher);
      render(<PlaylistDetails playlist={playlist} onClose={() => {}} />);
      await screen.findByRole('table');
      const search = screen.getByRole('textbox', { name: '搜索曲名或歌手' });
      fireEvent.change(search, { target: { value: '歌曲' } });
      await waitFor(() => expect(screen.getByText('匹配 120 首已保存歌曲')).toBeTruthy());
      fireEvent.click(screen.getByRole('button', { name: '只看资料不完整' }));
      await screen.findByText('符合筛选 120 首已保存歌曲');
      focusedClick('下一页');
      await act(async () => next.resolve(response(savedPage(50, total, 'incomplete'))));
      await waitFor(() =>
        expect(fetcher.mock.lastCall?.[0]).toBe(
          '/api/playlists/9000/tracks?offset=0&limit=50&q=%E6%AD%8C%E6%9B%B2&metadata=incomplete',
        ),
      );
      expect(screen.getByText('记录数量已变化，已返回第 1 页。').getAttribute('role')).toBe('status');
      if (moveFocus) {
        const close = screen.getByRole('button', { name: '关闭歌曲明细' });
        close.focus();
        close.blur();
      }
      await act(async () => first.resolve(response(savedPage(0, total, 'incomplete'))));
      expect(screen.getByText('第 1 页，共 1 页')).toBeTruthy();
      expect(search).toHaveProperty('value', '歌曲');
      expect(
        screen.getByRole('button', { name: '只看资料不完整' }).getAttribute('aria-pressed'),
      ).toBe('true');
      expect(document.activeElement).toBe(moveFocus ? document.body : search);
      if (total) {
        expect(screen.getByRole('table')).toBeTruthy();
        expect(screen.queryByText('这份本地记录没有歌曲')).toBeNull();
      } else {
        expect(screen.getByText('没有符合筛选的已保存歌曲')).toBeTruthy();
      }
      expect(fetcher).toHaveBeenCalledTimes(5);
      expect(fetcher.mock.calls.every(([, init]) => init.method === 'GET')).toBe(true);
    },
  );
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
  it.each(
    ['wheel', 'pointer', 'touch', 'PageDown'].flatMap((movement) =>
      ['page success', 'page failure', 'retry success'].map((phase) => ({ movement, phase })),
    ),
  )('cancels late $phase focus after $movement without a focus move', async ({ movement, phase }) => {
    const pageRead = deferred<ReturnType<typeof response>>();
    const retryRead = deferred<ReturnType<typeof response>>();
    let attempts = 0;
    const fetcher = vi.fn((url: string, _init: RequestInit) => {
      const offset = Number(new URL(url, 'http://localhost').searchParams.get('offset'));
      if (offset === 50) return ++attempts === 1 ? pageRead.promise : retryRead.promise;
      return Promise.resolve(response(page()));
    });
    vi.stubGlobal('fetch', fetcher);
    render(<PlaylistDetails playlist={playlist} onClose={() => {}} />);
    await screen.findByRole('table');
    focusedClick('下一页');
    if (phase === 'retry success') {
      await act(async () => pageRead.reject(new Error('local read failed')));
      expect(document.activeElement).toBe(screen.getByRole('button', { name: '重试读取' }));
      focusedClick('重试读取');
    }
    const beforeMovement = document.activeElement;
    moveWhileWaiting(movement);
    const afterMovement = document.activeElement;
    expect(afterMovement).toBe(beforeMovement);
    await act(async () => {
      if (phase === 'page failure') pageRead.reject(new Error('local read failed'));
      else (phase === 'retry success' ? retryRead : pageRead).resolve(response(page('9000', 50)));
    });
    expect(document.activeElement).toBe(afterMovement);
    if (phase === 'page failure') expect(screen.getByRole('alert')).toBeTruthy();
    else expect(screen.getByText('第 2 页，共 3 页')).toBeTruthy();
    expect(fetcher).toHaveBeenCalledTimes(phase === 'retry success' ? 3 : 2);
    expect(fetcher.mock.calls.every(([, init]) => init.method === 'GET')).toBe(true);
  });
  it.each(['ArrowUp', 'ArrowDown', 'ArrowLeft', 'ArrowRight', 'PageUp', 'Home', 'End', ' ', 'Spacebar'])(
    'cancels a delayed page focus after the %j scrolling key', async (key) => {
      const { next } = setup();
      render(<PlaylistDetails playlist={playlist} onClose={() => {}} />);
      await screen.findByRole('table');
      focusedClick('下一页');
      moveWhileWaiting(key);
      const afterMovement = document.activeElement;
      await act(async () => next.resolve(response(page('9000', 50))));
      expect(document.activeElement).toBe(afterMovement);
      expect(screen.getByText('第 2 页，共 3 页')).toBeTruthy();
    },
  );
  it('keeps normal page focus recovery after a key that does not scroll', async () => {
    const { next } = setup();
    render(<PlaylistDetails playlist={playlist} onClose={() => {}} />);
    await screen.findByRole('table');
    focusedClick('下一页');
    fireEvent.keyDown(document.activeElement!, { key: 'Shift' });
    await act(async () => next.resolve(response(page('9000', 50))));
    expect(document.activeElement).toBe(screen.getByRole('button', { name: '下一页' }));
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
