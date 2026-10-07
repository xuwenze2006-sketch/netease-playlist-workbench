import { afterEach, describe, expect, it, vi } from 'vitest';
import { fetchClassificationChanges } from './api';
const query = { playlist: '语言 · 英语', source_version: 'a'.repeat(64), revision: 2, offset: 0, change: 'all' as const };
function page() {
  return { status: 'available', source: 'local_correction_draft', source_version: query.source_version, revision: 2,
    draft_status: 'ready', playlist: query.playlist, filters: { change: 'all' }, counts: { added: 1, removed: 0 },
    pagination: { offset: 0, limit: 50, total: 1, next_offset: null }, records: [{ position: 1, name: '歌曲', artists: '歌手',
      record_key: 'b'.repeat(32), change: 'added', before: { styles: ['流行抒情'], scenes: [], language: '国语' },
      after: { styles: ['流行抒情'], scenes: [], language: '英语' }, reason: '核对录音', recording_note: '' }] };
}
afterEach(() => vi.unstubAllGlobals());
describe('classification change read boundary', () => {
  it('preserves newlines only in per-song correction evidence', async () => {
    const value = page(); value.records[0].reason = '人声依据\n场景依据'; value.records[0].recording_note = '专辑版\n已核对';
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue({ ok: true, json: async () => value }));
    const parsed = await fetchClassificationChanges('s', query);
    expect(parsed.records[0].reason).toBe('人声依据\n场景依据');
    expect(parsed.records[0].recording_note).toBe('专辑版\n已核对');
    value.records[0].name = '不合法\n标题';
    await expect(fetchClassificationChanges('s', query)).rejects.toThrow();
  });
  it('reads the exact parent identity and whitelists changed records', async () => {
    const value = page();
    const fetcher = vi.fn().mockResolvedValue({ ok: true, json: async () => ({ ...value, token: 'PRIVATE', records: value.records.map((row) => ({ ...row, id: 'PRIVATE' })) }) });
    vi.stubGlobal('fetch', fetcher);
    const parsed = await fetchClassificationChanges('s', query);
    expect(parsed.records[0].reason).toBe('核对录音');
    expect(JSON.stringify(parsed)).not.toContain('PRIVATE');
    const [url, config] = fetcher.mock.calls[0];
    expect(url).toContain('/api/classification/changes?');
    expect(url).toContain('source_version=' + query.source_version);
    expect(url).toContain('revision=2');
    expect(config.method).toBe('GET');
    expect(config.headers['X-Organizer-Session']).toBe('s');
  });
  it.each([
    (value: ReturnType<typeof page>) => { value.revision = 3; },
    (value: ReturnType<typeof page>) => { value.source_version = 'c'.repeat(64); },
    (value: ReturnType<typeof page>) => { value.playlist = '语言 · 国语'; },
    (value: ReturnType<typeof page>) => { value.records[0].record_key = 'private-id'; },
    (value: ReturnType<typeof page>) => { value.pagination.total = 2; },
  ])('rejects stale or damaged detail data', async (mutate) => {
    const value = page(); mutate(value);
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue({ ok: true, json: async () => value }));
    await expect(fetchClassificationChanges('s', query)).rejects.toThrow();
  });
  it('returns unavailable explicitly without presenting an empty list as a verified result', async () => {
    const value = { ...page(), status: 'unavailable', source_version: null, revision: 0, draft_status: 'unavailable',
      counts: { added: 0, removed: 0 }, pagination: { offset: 0, limit: 50, total: 0, next_offset: null }, records: [] };
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue({ ok: true, json: async () => value }));
    const parsed = await fetchClassificationChanges('s', query);
    expect(parsed.status).toBe('unavailable'); expect(parsed.records).toEqual([]);
  });
  it('rejects duplicate opaque record keys even when positions are distinct', async () => {
    const value = page(); value.records.push({ ...value.records[0], position: 2 });
    value.counts.added = 2; value.pagination.total = 2;
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue({ ok: true, json: async () => value }));
    await expect(fetchClassificationChanges('s', query)).rejects.toThrow();
  });
});
