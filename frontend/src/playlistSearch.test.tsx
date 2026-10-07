import { fireEvent, render, screen, within } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';
import App from './App';

const playlists = [
  { key: '9000', name: '我喜欢的音乐', track_count: 3, category: 'liked', source: 'local_record' },
  { key: '9001', name: 'Funk', track_count: 10, category: 'normal', source: 'local_record' },
  {
    key: '9002',
    name: 'Taylor Swift · 红心精选',
    track_count: 10,
    category: 'artist',
    source: 'local_record',
  },
  { key: '9003', name: '中文歌单', track_count: 4, category: 'normal', source: 'local_record' },
  { key: '9004', name: 'Ｆｕｎｋ现场', track_count: 2, category: 'normal', source: 'local_record' },
];

async function openPlaylists() {
  document.head.innerHTML = '<meta name="organizer-session" content="search-test-session" />';
  const fetcher = vi.fn(async (url: string, _init?: RequestInit) => {
    if (url !== '/api/state') throw new Error('Unexpected local request');
    return {
      ok: true,
      json: async () => ({
        connection: { installed: true, configured: true, authorized: null },
        update: { status: 'none' },
        recovery: { status: 'clear', operation: 'unknown', can_reconcile: false },
        data: {
          account: { nickname: '本地搜索验收用户' },
          source: 'local_record',
          playlists,
          history: null,
          artists_completed: false,
        },
        job: null,
      }),
    };
  });
  vi.stubGlobal('fetch', fetcher);
  render(<App />);
  await screen.findByText('本地搜索验收用户');
  fireEvent.click(
    within(screen.getByRole('navigation', { name: '主导航' })).getByRole('link', {
      name: /我的歌单/,
    }),
  );
  await screen.findByRole('heading', { name: '我的歌单' });
  return fetcher;
}

function visibleNames() {
  return screen.queryAllByRole('heading', { level: 3 }).map((heading) => heading.textContent);
}

function expectOnlyLocalStateReads(fetcher: Awaited<ReturnType<typeof openPlaylists>>) {
  expect(fetcher.mock.calls.length).toBeGreaterThan(0);
  expect(
    fetcher.mock.calls.every(
      ([url, init]) => url === '/api/state' && (init?.method ?? 'GET') === 'GET',
    ),
  ).toBe(true);
}

describe('local playlist name search', () => {
  it.each([
    [' Ｆｕｎｋ ', ['Funk', 'Ｆｕｎｋ现场']],
    ['Ｔａｙｌｏｒ', ['Taylor Swift · 红心精选']],
    ['fUnK', ['Funk', 'Ｆｕｎｋ现场']],
    ['tAyLoR', ['Taylor Swift · 红心精选']],
    ['中文', ['中文歌单']],
  ])('matches %s without sending an account action', async (query, expected) => {
    const fetcher = await openPlaylists();
    fireEvent.change(screen.getByRole('textbox', { name: '搜索歌单名称' }), {
      target: { value: query },
    });
    expect(visibleNames()).toEqual(expected);
    expectOnlyLocalStateReads(fetcher);
  });

  it('preserves original titles, categories and order while filtering and clearing', async () => {
    const original = structuredClone(playlists);
    const fetcher = await openPlaylists();
    const names = original.map((playlist) => playlist.name);
    expect(visibleNames()).toEqual(names);

    fireEvent.change(screen.getByRole('textbox', { name: '搜索歌单名称' }), {
      target: { value: 'ＦｕＮＫ' },
    });
    expect(visibleNames()).toEqual(['Funk', 'Ｆｕｎｋ现场']);
    fireEvent.click(screen.getByRole('button', { name: '清除搜索' }));
    expect(visibleNames()).toEqual(names);

    const categoryNames = { liked: '红心歌单', artist: '歌手精选', normal: '我的分类' };
    for (const playlist of original) {
      const heading = screen.getByRole('heading', { name: playlist.name });
      const card = heading.closest('article');
      expect(card).not.toBeNull();
      expect(
        within(card!).getByText(categoryNames[playlist.category as keyof typeof categoryNames]),
      ).toBeTruthy();
      expect(heading.getAttribute('title')).toBe(playlist.name);
    }
    fireEvent.click(
      within(screen.getByRole('group', { name: '歌单分类' })).getByRole('button', {
        name: '我的分类',
      }),
    );
    expect(visibleNames()).toEqual(['Funk', '中文歌单', 'Ｆｕｎｋ现场']);
    expect(playlists).toEqual(original);
    expectOnlyLocalStateReads(fetcher);
  });
});
