import { afterEach, describe, expect, it, vi } from 'vitest';
import { fetchClassification, postClassificationDraft } from './api';
import { normalizeClassification } from './classificationData';

const request = { offset: 0, query: '', dimension: 'all' as const, tag: '', review: 'conflict' as const };
const payload = { action: 'save' as const, source_version: 'a'.repeat(64), revision: 3, record_key: 'b'.repeat(32),
  styles: ['流行抒情'], scenes: ['学习专注'], language: '国语', reason: '试听确认', recording_note: '' };
function raw() {
  return { status: 'available', source: 'local_record', verification: 'local_only', updated_at: '2026-10-07T00:00:00Z', verified_at: null,
    summary: { source_count: 1, covered_count: 1, playlist_count: 0, pending_count: 0, unknown_style_count: 0, unknown_language_count: 0 },
    options: { style: ['流行抒情'], scene: [], language: ['国语'] }, playlists: [], filters: request,
    pagination: { offset: 0, limit: 50, total: 1, next_offset: null },
    quality: { source_version: 'a'.repeat(64), draft_status: 'ready', revision: 3, changed_count: 0, review_count: 1,
      pilot_positions: [1], playlist_changes: [], rules: [] },
    records: [{ position: 1, record_key: 'b'.repeat(32), name: '歌曲', artists: '歌手', styles: ['流行抒情'], scenes: [], language: '国语',
      pending_reasons: [], evidence_note: '', language_evidence_note: '', style_judgment_score: .7, review_note: true,
      needs_review: true, review_reasons: ['语言证据冲突'], draft: null }],
  };
}
afterEach(() => vi.unstubAllGlobals());
describe('classification quality API boundary', () => {
  it('echoes explicit draft basis and preserves draft summary and recording clues', () => {
    const value = raw() as ReturnType<typeof raw> & Record<string, unknown>;
    value.filters = { ...request, basis: 'draft' } as never;
    value.quality = { ...value.quality, draft_summary: { pending_count: 0, unknown_style_count: 0, unknown_language_count: 0 } } as never;
    value.records[0] = { ...value.records[0], recording_hints: ['现场版本线索'] } as never;
    const parsed = normalizeClassification(value, { ...request, basis: 'draft' });
    expect(parsed.filters.basis).toBe('draft');
    expect(parsed.quality!.draft_summary!.pending_count).toBe(0);
    expect(parsed.records[0].recording_hints).toEqual(['现场版本线索']);
  });
  it('does not accept an original projection in response to a draft basis request', () => {
    expect(() => normalizeClassification(raw(), { ...request, basis: 'draft' })).toThrow();
  });
  it('appends basis only for an explicit draft GET and accepts the version filter', async () => {
    const value = raw(); value.filters = { ...request, basis: 'draft', review: 'version' } as never;
    const fetcher = vi.fn().mockResolvedValue({ ok: true, json: async () => value }); vi.stubGlobal('fetch', fetcher);
    await fetchClassification('s', { ...request, basis: 'draft', review: 'version' });
    expect(fetcher.mock.calls[0][0]).toContain('&review=version&basis=draft');
  });
  it('retains quality provenance and review reasons while stripping private fields', () => {
    const value = raw();
    const parsed = normalizeClassification({ ...value, secret: 'PRIVATE', records: value.records.map((r) => ({ ...r, id: 'PRIVATE' })) }, request);
    expect(parsed.quality?.revision).toBe(3);
    expect(parsed.records[0].review_reasons).toEqual(['语言证据冲突']);
    expect(JSON.stringify(parsed)).not.toContain('PRIVATE');
  });
  it.each(['conflict', 'needs_review', 'low_confidence', 'weak_evidence', 'pilot', 'draft'] as const)('accepts the %s review query', (review) => {
    const value = raw(); value.filters = { ...request, review } as typeof request;
    expect(normalizeClassification(value, { ...request, review }).filters.review).toBe(review);
  });
  it.each([
    (p: ReturnType<typeof raw>) => { p.quality.source_version = 'bad'; },
    (p: ReturnType<typeof raw>) => { p.quality.draft_status = 'published'; },
    (p: ReturnType<typeof raw>) => { p.records[0].record_key = 'private-id'; },
    (p: ReturnType<typeof raw>) => { p.records[0].style_judgment_score = 1.01; },
    (p: ReturnType<typeof raw>) => { p.records[0].needs_review = 'true' as never; },
    (p: ReturnType<typeof raw>) => { p.quality.pilot_positions = [0]; },
  ])('rejects unsafe quality fields', (mutate) => {
    const value = raw(); mutate(value); expect(() => normalizeClassification(value, request)).toThrow();
  });
  it('submits one session-bound local draft and strictly validates its response', async () => {
    const fetcher = vi.fn().mockResolvedValue({ ok: true, status: 200, json: async () => ({ accepted: true, message: '已保存', revision: 4, changed_count: 1, secret: 'PRIVATE' }) });
    vi.stubGlobal('fetch', fetcher);
    expect(await postClassificationDraft('s', payload)).toEqual({ accepted: true, message: '已保存', revision: 4, changed_count: 1 });
    const [path, init] = fetcher.mock.calls[0];
    expect(path).toBe('/api/classification/draft');
    expect(init.headers['X-Organizer-Session']).toBe('s');
    expect(JSON.parse(init.body)).toEqual(payload);
  });
  it('does not repeat an accepted but unreadable local draft response', async () => {
    const fetcher = vi.fn().mockResolvedValue({ ok: true, status: 200, json: async () => ({ accepted: true, revision: '4' }) });
    vi.stubGlobal('fetch', fetcher);
    await expect(postClassificationDraft('s', payload)).rejects.toMatchObject({ acceptanceUnknown: true });
    expect(fetcher).toHaveBeenCalledTimes(1);
  });
  it('rejects invalid label cardinality before submitting', async () => {
    const fetcher = vi.fn(); vi.stubGlobal('fetch', fetcher);
    await expect(postClassificationDraft('s', { ...payload, styles: ['待辨识', '流行抒情'] })).rejects.toThrow('修正');
    expect(fetcher).not.toHaveBeenCalled();
  });
});
