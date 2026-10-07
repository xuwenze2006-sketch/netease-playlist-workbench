import { act, fireEvent, render, screen, within } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { PlaylistDetails } from './PlaylistDetails';
import type { Job, Playlist, PlaylistReadIntent } from './types';

const playlist: Playlist = {
  key: '9000',
  name: '我喜欢的音乐',
  track_count: 70,
  category: 'liked',
  source: 'local_record',
};
function page(
  key = '9000',
  offset = 0,
  query = '',
  metadata = 'all',
  complete = false,
  empty = false,
) {
  const observed = empty ? 0 : 62;
  const all = Array.from({ length: observed }, (_, index) => {
    const position = index + 1;
    return {
      key: `track-${position}`,
      position,
      name: position === 55 ? '目标歌曲' : `歌曲 ${position}`,
      artists: position === 1 ? [] : ['蔡依林'],
      artist_count: position === 1 ? 0 : 1,
      metadata_available: complete || ![1, 55].includes(position),
    };
  }).map((track) => (complete ? { ...track, artists: ['蔡依林'], artist_count: 1 } : track));
  const matches = all.filter(
    (track) =>
      (metadata === 'all' || !track.metadata_available) &&
      (!query ||
        track.name.includes(query) ||
        track.artists.some((artist) => artist.includes(query))),
  );
  return {
    status: 'available',
    source: 'local_record',
    metadata_filter: metadata,
    playlist: { ...playlist, key },
    updated_at: '2026-10-04T00:00:00+00:00',
    counts: {
      expected: empty ? 0 : 67,
      observed,
      missing: empty ? 0 : 5,
      metadata_missing: complete || empty ? 0 : 2,
    },
    pagination: {
      offset,
      limit: 50,
      total: matches.length,
      next_offset: offset + 50 < matches.length ? offset + 50 : null,
    },
    tracks: matches.slice(offset, offset + 50),
  };
}
function route(options: { complete?: boolean; empty?: boolean; unread?: boolean } = {}) {
  return vi.fn(async (url: string, _init: RequestInit) => {
    const parsed = new URL(url, 'http://localhost');
    const key = parsed.pathname.split('/')[3];
    const metadata = parsed.searchParams.get('metadata') || 'all';
    const result = page(
      key,
      Number(parsed.searchParams.get('offset')),
      parsed.searchParams.get('q') || '',
      metadata,
      options.complete,
      options.empty,
    );
    return {
      ok: true,
      json: async () =>
        options.unread
          ? {
              ...result,
              status: 'not_loaded',
              updated_at: null,
              counts: { expected: 70, observed: null, missing: null, metadata_missing: null },
              pagination: { ...result.pagination, total: 0, next_offset: null },
              tracks: [],
            }
          : result,
    };
  });
}
function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((done) => {
    resolve = done;
  });
  return { promise, resolve };
}
async function search(value: string) {
  fireEvent.change(screen.getByRole('textbox', { name: '搜索曲名或歌手' }), { target: { value } });
  await act(async () => {
    await vi.advanceTimersByTimeAsync(200);
  });
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

describe('local saved-song metadata filter', () => {
  it('collects incomplete songs across original pages, retaining known artists and whole-record counts', async () => {
    const fetcher = route();
    vi.stubGlobal('fetch', fetcher);
    render(<PlaylistDetails playlist={playlist} onClose={() => {}} />);
    await screen.findByRole('table');
    fireEvent.click(screen.getByRole('button', { name: '下一页' }));
    await screen.findByText('目标歌曲');
    const group = screen.getByRole('group', { name: '歌曲资料筛选' });
    fireEvent.click(within(group).getByRole('button', { name: '只看资料不完整' }));
    await screen.findByText('符合筛选 2 首已保存歌曲');
    expect(screen.getByText('已保存 62 / 67 首')).toBeTruthy();
    expect(screen.getByText('缺失 5 首')).toBeTruthy();
    expect(screen.getByText('2 首资料不完整')).toBeTruthy();
    const rows = screen.getAllByRole('row').slice(1);
    expect(rows.map((row) => within(row).getAllByRole('cell')[0].textContent)).toEqual(['1', '55']);
    expect(within(rows[1]).getByText('蔡依林')).toBeTruthy();
    expect(
      within(group).getByRole('button', { name: '只看资料不完整' }).getAttribute('aria-pressed'),
    ).toBe('true');
    expect(
      within(group).getByRole('button', { name: '全部已保存歌曲' }).getAttribute('aria-pressed'),
    ).toBe('false');
    expect(fetcher.mock.calls[2][0]).toBe(
      '/api/playlists/9000/tracks?offset=0&limit=50&q=&metadata=incomplete',
    );
    expect(fetcher.mock.calls.every(([, init]) => init.method === 'GET')).toBe(true);
  });
  it('intersects search with incomplete metadata and preserves the filter when search is cleared', async () => {
    const fetcher = route();
    vi.stubGlobal('fetch', fetcher);
    render(<PlaylistDetails playlist={playlist} onClose={() => {}} />);
    await screen.findByRole('table');
    fireEvent.click(screen.getByRole('button', { name: '只看资料不完整' }));
    await screen.findByText('符合筛选 2 首已保存歌曲');
    vi.useFakeTimers();
    await search('目标');
    expect(screen.getAllByRole('row')).toHaveLength(2);
    expect(screen.getByText('符合筛选 1 首已保存歌曲')).toBeTruthy();
    expect(fetcher.mock.calls.at(-1)?.[0]).toContain('q=%E7%9B%AE%E6%A0%87&metadata=incomplete');
    await search('不存在');
    expect(screen.getByText('没有符合筛选的已保存歌曲')).toBeTruthy();
    fireEvent.click(screen.getByRole('button', { name: '清除歌曲搜索' }));
    await act(async () => {
      await vi.advanceTimersByTimeAsync(200);
    });
    expect(screen.getAllByRole('row')).toHaveLength(3);
    expect(
      screen.getByRole('button', { name: '只看资料不完整' }).getAttribute('aria-pressed'),
    ).toBe('true');
    expect(fetcher.mock.calls.at(-1)?.[0]).toContain('q=&metadata=incomplete');
    fireEvent.click(screen.getByRole('button', { name: '全部已保存歌曲' }));
    await act(async () => {});
    expect(screen.getAllByRole('row')).toHaveLength(51);
    expect(fetcher.mock.calls.at(-1)?.[0]).not.toContain('metadata=');
  });
  it('preserves a pending search word while switching filters before the debounce finishes', async () => {
    const fetcher = route();
    vi.stubGlobal('fetch', fetcher);
    render(<PlaylistDetails playlist={playlist} onClose={() => {}} />);
    await screen.findByRole('table');
    vi.useFakeTimers();
    fireEvent.change(screen.getByRole('textbox', { name: '搜索曲名或歌手' }), {
      target: { value: '目标' },
    });
    fireEvent.click(screen.getByRole('button', { name: '只看资料不完整' }));
    await act(async () => {
      await vi.advanceTimersByTimeAsync(200);
    });
    expect(screen.getByText('符合筛选 1 首已保存歌曲')).toBeTruthy();
    expect(screen.getByRole('textbox', { name: '搜索曲名或歌手' })).toHaveProperty('value', '目标');
    expect(fetcher.mock.calls.at(-1)?.[0]).toContain('q=%E7%9B%AE%E6%A0%87&metadata=incomplete');
  });
  it('paginates the filtered subset without renumbering original positions or replacing global counts', async () => {
    const fetcher = vi.fn(async (url: string, _init: RequestInit) => {
      const parsed = new URL(url, 'http://localhost');
      const offset = Number(parsed.searchParams.get('offset'));
      const metadata = parsed.searchParams.get('metadata') || 'all';
      const all = Array.from({ length: 120 }, (_, index) => ({
        key: `track-${index + 1}`,
        position: index + 1,
        name: `大歌单歌曲 ${index + 1}`,
        artists: ['已知歌手'],
        artist_count: 1,
        metadata_available: index === 0 || index > 65,
      }));
      const subset =
        metadata === 'incomplete' ? all.filter((track) => !track.metadata_available) : all;
      return {
        ok: true,
        json: async () => ({
          ...page(),
          metadata_filter: metadata,
          counts: { expected: 125, observed: 120, missing: 5, metadata_missing: 65 },
          pagination: {
            offset,
            limit: 50,
            total: subset.length,
            next_offset: offset + 50 < subset.length ? offset + 50 : null,
          },
          tracks: subset.slice(offset, offset + 50),
        }),
      };
    });
    vi.stubGlobal('fetch', fetcher);
    render(<PlaylistDetails playlist={playlist} onClose={() => {}} />);
    await screen.findByRole('table');
    fireEvent.click(screen.getByRole('button', { name: '只看资料不完整' }));
    await screen.findByText('符合筛选 65 首已保存歌曲');
    fireEvent.click(screen.getByRole('button', { name: '下一页' }));
    await screen.findByText('大歌单歌曲 52');
    expect(screen.getAllByRole('row')).toHaveLength(16);
    expect(screen.getByText('已保存 120 / 125 首')).toBeTruthy();
    expect(screen.getByText('第 2 页，共 2 页')).toBeTruthy();
    expect(fetcher.mock.calls.at(-1)?.[0]).toContain('offset=50&limit=50&q=&metadata=incomplete');
    fireEvent.click(screen.getByRole('button', { name: '上一页' }));
    await screen.findByText('大歌单歌曲 2');
    expect(screen.getAllByRole('row')).toHaveLength(51);
    expect(fetcher.mock.calls.at(-1)?.[0]).toContain('offset=0&limit=50&q=&metadata=incomplete');
  });
  it('shows a fixed local error instead of accepting all songs when an incomplete response omits its echo', async () => {
    const ordinary = route();
    const fetcher = vi.fn(async (url: string, init: RequestInit) => {
      if (!url.includes('metadata=incomplete')) return ordinary(url, init);
      return {
        ok: true,
        json: async () => ({ ...page(), metadata_filter: undefined, owner: 'SECRET' }),
      };
    });
    vi.stubGlobal('fetch', fetcher);
    render(<PlaylistDetails playlist={playlist} onClose={() => {}} />);
    await screen.findByRole('table');
    fireEvent.click(screen.getByRole('button', { name: '只看资料不完整' }));
    expect(await screen.findByRole('alert')).toHaveProperty(
      'textContent',
      '本地歌曲资料暂时无法读取，请重试。',
    );
    expect(screen.queryByRole('table')).toBeNull();
    expect(screen.queryByText(/SECRET/)).toBeNull();
    expect(fetcher.mock.calls.every(([, init]) => init.method === 'GET')).toBe(true);
  });
  it.each([
    [{ complete: true }, '已保存歌曲的资料均完整'],
    [{ empty: true }, '这份本地记录没有歌曲'],
    [{ unread: true }, '尚未保存歌曲明细'],
  ])(
    'distinguishes a complete record, a genuine empty record, and unread details: %j',
    async (options, title) => {
      vi.stubGlobal('fetch', route(options));
      render(<PlaylistDetails playlist={playlist} onClose={() => {}} />);
      if ('unread' in options) await screen.findByText('尚未保存歌曲明细');
      else if ('empty' in options) await screen.findByText('这份本地记录没有歌曲');
      else await screen.findByRole('table');
      const filter = screen.getByRole('button', { name: '只看资料不完整' });
      expect(filter).toHaveProperty('disabled', false);
      fireEvent.click(filter);
      expect(await screen.findByText(title)).toBeTruthy();
      expect(screen.queryByRole('table')).toBeNull();
    },
  );
  it('ignores a late incomplete response after returning to all songs', async () => {
    const old = deferred<{ ok: boolean; json: () => Promise<ReturnType<typeof page>> }>();
    const ordinary = route();
    const fetcher = vi.fn((url: string, init: RequestInit) =>
      url.includes('metadata=incomplete') ? old.promise : ordinary(url, init),
    );
    vi.stubGlobal('fetch', fetcher);
    render(<PlaylistDetails playlist={playlist} onClose={() => {}} />);
    await screen.findByRole('table');
    fireEvent.click(screen.getByRole('button', { name: '只看资料不完整' }));
    fireEvent.click(screen.getByRole('button', { name: '全部已保存歌曲' }));
    await screen.findByRole('table');
    expect(fetcher.mock.calls[1][1].signal?.aborted).toBe(true);
    await act(async () => {
      old.resolve({ ok: true, json: async () => page('9000', 0, '', 'incomplete') });
    });
    expect(screen.getAllByRole('row')).toHaveLength(51);
    expect(
      screen.getByRole('button', { name: '全部已保存歌曲' }).getAttribute('aria-pressed'),
    ).toBe('true');
  });
  it('starts in all mode after a different key or a closed drawer is opened', async () => {
    const fetcher = route();
    vi.stubGlobal('fetch', fetcher);
    const view = render(<PlaylistDetails playlist={playlist} onClose={() => {}} />);
    await screen.findByRole('table');
    fireEvent.click(screen.getByRole('button', { name: '只看资料不完整' }));
    await screen.findByText('符合筛选 2 首已保存歌曲');
    view.rerender(<PlaylistDetails playlist={{ ...playlist, key: '9001' }} onClose={() => {}} />);
    await screen.findByRole('table');
    expect(
      screen.getByRole('button', { name: '全部已保存歌曲' }).getAttribute('aria-pressed'),
    ).toBe('true');
    expect(fetcher.mock.calls.at(-1)?.[0]).toBe('/api/playlists/9001/tracks?offset=0&limit=50&q=');
    view.unmount();
    render(<PlaylistDetails playlist={playlist} onClose={() => {}} />);
    await screen.findByRole('table');
    expect(fetcher.mock.calls.at(-1)?.[0]).toBe('/api/playlists/9000/tracks?offset=0&limit=50&q=');
  });
  it('keeps the selected filter when the exact active read saves and refreshes local details once', async () => {
    const fetcher = route();
    vi.stubGlobal('fetch', fetcher);
    const onRead = vi.fn().mockResolvedValue(true);
    const view = render(<PlaylistDetails playlist={playlist} onClose={() => {}} onRead={onRead} />);
    await screen.findByRole('table');
    fireEvent.click(screen.getByRole('button', { name: '更新此歌单明细' }));
    await act(async () => {});
    const intent: PlaylistReadIntent = {
      key: '9000',
      baseline_id: null,
      job_id: 'new-read',
      sequence: 1,
      acceptance: 'accepted',
    };
    const job: Job = {
      id: 'new-read',
      action: 'read_playlist',
      playlist_key: '9000',
      label: '读取歌单明细',
      status: 'running',
      logs: [],
    };
    view.rerender(
      <PlaylistDetails
        playlist={playlist}
        onClose={() => {}}
        onRead={onRead}
        readIntent={intent}
        job={job}
      />,
    );
    fireEvent.click(screen.getByRole('button', { name: '只看资料不完整' }));
    await screen.findByText('符合筛选 2 首已保存歌曲');
    const before = fetcher.mock.calls.length;
    const complete = {
      ...job,
      status: 'partial',
      result: {
        status: 'partial',
        playlist_key: '9000',
        record_saved: true,
        outcome_known: true,
        write_attempted: false,
        applied_to_account: false,
      },
    };
    view.rerender(
      <PlaylistDetails
        playlist={playlist}
        onClose={() => {}}
        onRead={onRead}
        readIntent={intent}
        job={complete}
      />,
    );
    await screen.findByText('符合筛选 2 首已保存歌曲');
    expect(fetcher).toHaveBeenCalledTimes(before + 1);
    expect(fetcher.mock.calls.at(-1)?.[0]).toContain('metadata=incomplete');
    expect(
      screen.getByRole('button', { name: '只看资料不完整' }).getAttribute('aria-pressed'),
    ).toBe('true');
    view.rerender(
      <PlaylistDetails
        playlist={playlist}
        onClose={() => {}}
        onRead={onRead}
        readIntent={intent}
        job={{ ...complete }}
      />,
    );
    expect(fetcher).toHaveBeenCalledTimes(before + 1);
    expect(onRead).toHaveBeenCalledTimes(1);
  });
});
