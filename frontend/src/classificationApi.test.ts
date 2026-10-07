import { afterEach, describe, expect, it, vi } from 'vitest';
import { fetchClassification, fetchState } from './api';
import { normalizeClassification } from './classificationData';

const request = { offset: 0, query: '', dimension: 'all' as const, tag: '', review: 'all' as const };
function page() {
  return {
    status: 'available', source: 'local_record', verification: 'verified',
    updated_at: '2026-10-04T15:01:11+08:00', verified_at: '2026-10-04T16:29:09+08:00',
    summary: { source_count: 2, covered_count: 2, playlist_count: 2, pending_count: 1,
      unknown_style_count: 1, unknown_language_count: 0 },
    options: { scene: ['通勤散步'], style: ['流行抒情', '待辨识'], language: ['国语'] },
    playlists: [{ name: '场景 · 通勤散步', dimension: 'scene', count: 2, key: '123' },
      { name: '分类 · 待辨识', dimension: 'review', count: 1, key: '124' }],
    filters: { ...request, offset: undefined },
    pagination: { offset: 0, limit: 50, total: 2, next_offset: null },
    records: [1, 2].map((position) => ({ position, name: `歌曲${position}`, artists: '歌手',
      styles: [position === 1 ? '流行抒情' : '待辨识'], scenes: ['通勤散步'], language: '国语',
      pending_reasons: position === 2 ? ['风格待辨识'] : [], evidence_note: '风格依据', language_evidence_note: '语言依据' })),
  };
}
afterEach(() => vi.unstubAllGlobals());
describe('classification local data boundary', () => {
  it('whitelists public records and validates pagination', () => {
    const raw = { ...page(), token: 'SECRET', records: page().records.map((r) => ({ ...r, id: 'PRIVATE', raw_lyrics: 'SECRET' })) };
    const parsed = normalizeClassification(raw, request);
    expect(parsed.records).toHaveLength(2);
    expect(JSON.stringify(parsed)).not.toMatch(/SECRET|PRIVATE|raw_lyrics/);
  });
  it.each([
    (p: ReturnType<typeof page>) => { p.pagination.next_offset = 1 as never; },
    (p: ReturnType<typeof page>) => { p.summary.pending_count = 3; },
    (p: ReturnType<typeof page>) => { p.records[1].position = 1; },
    (p: ReturnType<typeof page>) => { p.playlists[0].key = 'A'.repeat(32); },
    (p: ReturnType<typeof page>) => { p.filters.dimension = 'style' as never; },
    (p: ReturnType<typeof page>) => { p.records[0].name = 'bad\u0000'; },
    (p: ReturnType<typeof page>) => { p.verified_at = null as never; },
  ])('rejects damaged or mismatched public response', (mutate) => {
    const raw = page(); mutate(raw);
    expect(() => normalizeClassification(raw, request)).toThrow();
  });
  it('sends a same-session local GET without a write body', async () => {
    const raw = page(); raw.filters.query = '歌手';
    const fetcher = vi.fn().mockResolvedValue({ ok: true, json: async () => raw });
    vi.stubGlobal('fetch', fetcher);
    await fetchClassification('session', { ...request, query: '歌手' });
    expect(fetcher).toHaveBeenCalledTimes(1);
    const [path, config] = fetcher.mock.calls[0];
    expect(path).toBe('/api/classification?offset=0&limit=50&q=%E6%AD%8C%E6%89%8B&dimension=all&tag=&review=all');
    expect(config.method).toBe('GET');
    expect(config.headers['X-Organizer-Session']).toBe('session');
    expect(config.body).toBeUndefined();
  });
  it('rejects bad queries before fetch and does not retry failures', async () => {
    const fetcher = vi.fn().mockRejectedValue(new Error('SECRET-RAW'));
    vi.stubGlobal('fetch', fetcher);
    await expect(fetchClassification('s', { ...request, query: '\u0000' })).rejects.toThrow('查询条件');
    expect(fetcher).not.toHaveBeenCalled();
    await expect(fetchClassification('s', request)).rejects.toMatchObject({ acceptanceUnknown: false });
    expect(fetcher).toHaveBeenCalledTimes(1);
  });
  it('refreshes local records only on an explicit GET and keeps filters unchanged', async () => {
    const fetcher = vi.fn().mockResolvedValue({ ok: true, json: async () => page() });
    vi.stubGlobal('fetch', fetcher);
    await fetchClassification('s', request, undefined, true);
    const [path, config] = fetcher.mock.calls[0];
    expect(path).toBe('/api/classification?offset=0&limit=50&q=&dimension=all&tag=&review=all&refresh=local');
    expect(config.method).toBe('GET');
    expect(config.body).toBeUndefined();
    expect(config.headers['X-Organizer-Session']).toBe('s');
    expect(fetcher).toHaveBeenCalledTimes(1);
  });
  it('rejects nonboolean refresh flags before issuing any request', async () => {
    const fetcher = vi.fn(); vi.stubGlobal('fetch', fetcher);
    await expect(fetchClassification('s', request, undefined, 'account' as never)).rejects.toThrow('查询条件');
    expect(fetcher).not.toHaveBeenCalled();
  });
  it('recognizes classification recovery and never grants ordinary resume', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue({ ok: true, json: async () => ({
      connection: { installed: true, configured: true, authorized: true },
      data: { source: 'local_record', account: null, playlists: [], artists_completed: false,
        history: { operation: 'classification', status: 'paused', completed_count: 1, items: [], resumable: true } },
      recovery: { status: 'review_required', operation: 'classification', can_reconcile: false }, job: null,
    }) }));
    const state = await fetchState('s');
    expect(state.recovery?.operation).toBe('classification');
    expect(state.data.history?.operation).toBe('classification');
    expect(state.data.history?.resumable).toBe(false);
  });
});
