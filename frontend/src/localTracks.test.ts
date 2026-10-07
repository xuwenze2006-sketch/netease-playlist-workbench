import { describe, expect, it, vi } from 'vitest';
import { fetchLocalTracks, fetchState } from './api';
import { normalizeLocalTracks } from './normalize';

const request = { key: '9000', offset: 0, limit: 50 };
function page() {
  return {
    status: 'available',
    source: 'local_record',
    playlist: {
      key: '9000',
      name: '我喜欢的音乐',
      track_count: 70,
      category: 'liked',
      source: 'local_record',
      updated_at: null,
    },
    updated_at: '2026-10-04T00:00:00+00:00',
    counts: { expected: 67, observed: 62, missing: 5, metadata_missing: 1 },
    pagination: { offset: 0, limit: 50, total: 62, next_offset: 50 },
    tracks: Array.from({ length: 50 }, (_, index) => ({
      key: `track-${index + 1}`,
      position: index + 1,
      name: `歌曲 ${index + 1}`,
      artists: index ? ['林俊杰'] : [],
      artist_count: index ? 1 : 0,
      metadata_available: index > 0,
    })),
  };
}

describe('local song page contract', () => {
  it('retains historical counts and original positions while dropping private fields', () => {
    const raw = page();
    Object.assign(raw, { owner: 'SECRET', batch_id: 'SECRET' });
    Object.assign(raw.tracks[0], { original_id: 'SECRET', private_key: 'SECRET' });
    const result = normalizeLocalTracks(raw, request);
    expect(result.counts).toEqual({ expected: 67, observed: 62, missing: 5, metadata_missing: 1 });
    expect(result.playlist?.track_count).toBe(70);
    expect(result.tracks[0]).toEqual({
      key: 'track-1',
      position: 1,
      name: '歌曲 1',
      artists: [],
      artist_count: 0,
      metadata_available: false,
    });
    expect(JSON.stringify(result)).not.toContain('SECRET');
  });
  it('preserves skipped original positions in a filtered page and valid emoji names', () => {
    const raw = page();
    raw.tracks = [
      {
        key: 'track-7',
        position: 7,
        name: 'Summer 🎵',
        artists: ['林俊杰'],
        artist_count: 1,
        metadata_available: true,
      },
    ];
    raw.pagination = { offset: 0, limit: 50, total: 1, next_offset: null as unknown as number };
    expect(normalizeLocalTracks(raw, request).tracks[0].position).toBe(7);
  });
  it('distinguishes a genuinely empty saved page from missing details', () => {
    const raw = page();
    raw.counts = { expected: 0, observed: 0, missing: 0, metadata_missing: 0 };
    raw.pagination = { offset: 0, limit: 50, total: 0, next_offset: null as unknown as number };
    raw.tracks = [];
    expect(normalizeLocalTracks(raw, request).status).toBe('available');
    expect(
      normalizeLocalTracks(
        {
          ...raw,
          status: 'not_loaded',
          updated_at: null,
          counts: { expected: 70, observed: null, missing: null, metadata_missing: null },
        },
        request,
      ).counts.observed,
    ).toBeNull();
  });
  it('accepts bounded additional artists but rejects a smaller or unbounded source count', () => {
    const raw = page();
    raw.tracks[0] = { ...raw.tracks[0], artists: ['已记录歌手'], artist_count: 12 };
    expect(normalizeLocalTracks(raw, request).tracks[0].artist_count).toBe(12);
    raw.tracks[0].artist_count = 0;
    expect(() => normalizeLocalTracks(raw, request)).toThrow();
    raw.tracks[0].artist_count = 101;
    expect(() => normalizeLocalTracks(raw, request)).toThrow();
  });
  it.each([
    'wrong key',
    'count mismatch',
    'duplicate position',
    'bad next page',
    'secret name',
    'invented status',
  ])('rejects an inconsistent DTO: %s', (fault) => {
    const raw = page();
    if (fault === 'wrong key') raw.playlist.key = '9001';
    if (fault === 'count mismatch') raw.counts.missing = 0;
    if (fault === 'duplicate position') raw.tracks[1] = raw.tracks[0];
    if (fault === 'bad next page') raw.pagination.next_offset = 51;
    if (fault === 'secret name') raw.tracks[0].name = 'bad\u0000name';
    if (fault === 'invented status') raw.status = 'live';
    expect(() => normalizeLocalTracks(raw, request)).toThrow();
  });
  it('uses a bounded authenticated GET with an encoded query and no account action', async () => {
    const fetcher = vi.fn().mockResolvedValue({ ok: true, json: async () => page() });
    vi.stubGlobal('fetch', fetcher);
    await fetchLocalTracks('test-session', '9000', { offset: 0, query: '林俊杰 / Summer' });
    expect(fetcher).toHaveBeenCalledTimes(1);
    const [url, init] = fetcher.mock.calls[0];
    expect(url).toBe(
      '/api/playlists/9000/tracks?offset=0&limit=50&q=%E6%9E%97%E4%BF%8A%E6%9D%B0%20%2F%20Summer',
    );
    expect(init.method).toBe('GET');
    expect(init.headers['X-Organizer-Session']).toBe('test-session');
    expect(init.body).toBeUndefined();
  });
  it('does not fetch invalid keys or unsafe search input', async () => {
    const fetcher = vi.fn();
    vi.stubGlobal('fetch', fetcher);
    await expect(
      fetchLocalTracks('test-session', '../actions', { offset: 0, query: '' }),
    ).rejects.toThrow();
    await expect(
      fetchLocalTracks('test-session', '9000', { offset: 0, query: '\u0000' }),
    ).rejects.toThrow();
    expect(fetcher).not.toHaveBeenCalled();
  });
  it('aborts local reads and never retries them as a POST', async () => {
    const fetcher = vi.fn(
      (_url, init) =>
        new Promise((_resolve, reject) => {
          init.signal.addEventListener('abort', () => reject(new Error('SECRET')), { once: true });
        }),
    );
    vi.stubGlobal('fetch', fetcher);
    const controller = new AbortController();
    const pending = fetchLocalTracks(
      'test-session',
      '9000',
      { offset: 0, query: '' },
      controller.signal,
    );
    controller.abort();
    await expect(pending).rejects.toThrow('取消');
    expect(fetcher).toHaveBeenCalledTimes(1);
    expect(fetcher.mock.calls[0][1].method).toBe('GET');
  });
  it('normalizes only the exact removed-playlist rejection and preserves the requested page', async () => {
    const fetcher = vi.fn().mockImplementation(
      async () =>
        new Response(
          JSON.stringify({
            accepted: false,
            message: '该歌单已不在本地目录中，请更新页面后重试。',
            owner: 'SECRET',
          }),
          { status: 404 },
        ),
    );
    vi.stubGlobal('fetch', fetcher);
    const result = await fetchLocalTracks('test-session', '9000', { offset: 50, query: '本地' });
    expect(result).toEqual({
      status: 'missing_playlist',
      source: 'local_record',
      metadata_filter: 'all',
      playlist: null,
      updated_at: null,
      counts: { expected: null, observed: null, missing: null, metadata_missing: null },
      pagination: { offset: 50, limit: 50, total: 0, next_offset: null },
      tracks: [],
    });
    expect(JSON.stringify(result)).not.toContain('SECRET');
    await expect(fetchState('test-session')).rejects.toThrow('该歌单已不在本地目录中');
    expect(fetcher).toHaveBeenCalledTimes(2);
  });

  it('accepts legacy all pages but binds an incomplete page to its exact filter echo', () => {
    expect(normalizeLocalTracks(page(), request)).toHaveProperty('metadata_filter', 'all');
    const raw = {
      ...page(),
      metadata_filter: 'incomplete',
      pagination: { offset: 0, limit: 50, total: 1, next_offset: null },
      tracks: [{ ...page().tracks[0], artists: ['已记录歌手'], artist_count: 1 }],
    };
    const result = normalizeLocalTracks(raw, { ...request, metadata: 'incomplete' });
    expect(result.metadata_filter).toBe('incomplete');
    expect(result.counts.observed).toBe(62);
    expect(result.tracks[0].artists).toEqual(['已记录歌手']);
    expect(() => normalizeLocalTracks(raw, request)).toThrow();
    expect(() =>
      normalizeLocalTracks(
        { ...raw, metadata_filter: undefined },
        { ...request, metadata: 'incomplete' },
      ),
    ).toThrow();
  });
  it.each([null, 'incomplete', 'SECRET', false])(
    'rejects an explicit invalid echo on an all request: %j',
    (metadata_filter) => {
      expect(() => normalizeLocalTracks({ ...page(), metadata_filter }, request)).toThrow();
    },
  );
  it.each(['all echo', 'bad echo', 'complete row', 'oversized total'])(
    'rejects an unsafe incomplete response: %s',
    (fault) => {
      const raw = {
        ...page(),
        metadata_filter: 'incomplete',
        pagination: { offset: 0, limit: 50, total: 1, next_offset: null },
        tracks: [page().tracks[0]],
      };
      if (fault === 'all echo') raw.metadata_filter = 'all';
      if (fault === 'bad echo') raw.metadata_filter = 'SECRET';
      if (fault === 'complete row') raw.tracks = [page().tracks[1]];
      if (fault === 'oversized total') {
        raw.pagination.total = 2;
        raw.tracks.push({ ...raw.tracks[0], position: 55, key: 'track-55' });
      }
      expect(() => normalizeLocalTracks(raw, { ...request, metadata: 'incomplete' })).toThrow();
    },
  );
  it.each(['not_loaded', 'unavailable', 'missing_playlist'])(
    'requires the filter echo on an unavailable incomplete page: %s',
    (status) => {
      const raw = {
        ...page(),
        status,
        playlist: status === 'missing_playlist' ? null : page().playlist,
        updated_at: null,
        counts: { expected: null, observed: null, missing: null, metadata_missing: null },
        pagination: { offset: 0, limit: 50, total: 0, next_offset: null },
        tracks: [],
      };
      expect(() => normalizeLocalTracks(raw, { ...request, metadata: 'incomplete' })).toThrow();
      expect(
        normalizeLocalTracks(
          { ...raw, metadata_filter: 'incomplete' },
          { ...request, metadata: 'incomplete' },
        ).status,
      ).toBe(status);
    },
  );
  it('adds only the explicit incomplete filter to the local GET and rejects other filters before fetching', async () => {
    const raw = {
      ...page(),
      metadata_filter: 'incomplete',
      pagination: { offset: 0, limit: 50, total: 1, next_offset: null },
      tracks: [page().tracks[0]],
    };
    const fetcher = vi.fn().mockResolvedValue({ ok: true, json: async () => raw });
    vi.stubGlobal('fetch', fetcher);
    await fetchLocalTracks('test-session', '9000', {
      offset: 0,
      query: '歌手',
      metadata: 'incomplete',
    });
    expect(fetcher.mock.calls[0][0]).toBe(
      '/api/playlists/9000/tracks?offset=0&limit=50&q=%E6%AD%8C%E6%89%8B&metadata=incomplete',
    );
    expect(fetcher.mock.calls[0][1].method).toBe('GET');
    await expect(
      fetchLocalTracks('test-session', '9000', {
        offset: 0,
        query: '',
        metadata: 'invalid' as 'all',
      }),
    ).rejects.toThrow();
    expect(fetcher).toHaveBeenCalledTimes(1);
  });
  it('preserves the requested incomplete filter in the exact removed-playlist HTTP 404 state', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn().mockResolvedValue(
        new Response(
          JSON.stringify({
            accepted: false,
            message: '该歌单已不在本地目录中，请更新页面后重试。',
          }),
          { status: 404 },
        ),
      ),
    );
    const result = await fetchLocalTracks('test-session', '9000', {
      offset: 50,
      query: '',
      metadata: 'incomplete',
    });
    expect(result.metadata_filter).toBe('incomplete');
    expect(result.pagination.offset).toBe(50);
  });
});
