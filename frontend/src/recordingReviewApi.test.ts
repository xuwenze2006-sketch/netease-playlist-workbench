import { afterEach, describe, expect, it, vi } from 'vitest';
import { fetchRecordingReview, fetchRecordingReviews } from './api';

function detail() {
  return { status: 'available', bundle: 'sample-01', created_at: '2026-10-23T10:00:00+08:00', preview: {
    kind: 'classification_scene_review_preview', source_sha256: 'a'.repeat(64),
    summary: { reviewed_records: 1, metadata_matched_records: 0, audio_verified_records: 0,
      scene_verified_records: 0, uncertain_records: 1, partial_scene_records: 0, added_count: 0, removed_count: 0 },
    records: [{ position: 5, name: '歌名', artists: '歌手', before_scenes: ['放松睡前'], after_scenes: ['放松睡前'],
      added_scenes: [] as string[], removed_scenes: [] as string[], scene_decisions: [] as {scene: string; decision: string; reason: string}[],
      recording: { status: 'unresolved', version: '', note: '', evidence_sources: [] as {url: string; kind: string; title: string; note: string}[],
        listened_segments: [] as {start_seconds: number; end_seconds: number; source_url: string; note: string}[] } }] } };
}
function respond(value: unknown, ok = true, status = 200) {
  const fetcher = vi.fn().mockResolvedValue({ ok, status, json: async () => value });
  vi.stubGlobal('fetch', fetcher); return fetcher;
}
afterEach(() => vi.unstubAllGlobals());

describe('recording review API', () => {
  it('loads safe catalog and detail by GET with local session; strips unknown fields', async () => {
    const list = respond({ status: 'available', bundles: [{ name: 'sample-01', token: 'PRIVATE' }], limit: 100, truncated: false, token: 'PRIVATE' });
    expect(await fetchRecordingReviews('local')).toEqual({ status: 'available', bundles: [{ name: 'sample-01' }], limit: 100, truncated: false });
    expect(list.mock.calls[0][0]).toBe('/api/recording-reviews');
    const original = detail();
    const fetcher = respond({ ...original, token: 'PRIVATE', preview: { ...original.preview,
      records: original.preview.records.map(row => ({ ...row, private_id: 'PRIVATE', recording: { ...row.recording, token: 'PRIVATE' } })) } });
    expect(await fetchRecordingReview('local', 'sample-01')).toEqual(original);
    const [url, config] = fetcher.mock.calls[0];
    expect(url).toBe('/api/recording-reviews/detail?bundle=sample-01');
    expect(config.method).toBe('GET'); expect(config.headers['X-Organizer-Session']).toBe('local');
    expect(config.credentials).toBe('omit'); expect(config.body).toBeUndefined();
  });
  it('accepts an explicitly empty catalog', async () => {
    respond({ status: 'empty', bundles: [], limit: 100, truncated: false });
    expect((await fetchRecordingReviews('s')).status).toBe('empty');
  });
  it.each([
    { status: 'empty', bundles: [{ name: 'safe' }], limit: 100, truncated: false },
    { status: 'available', bundles: [], limit: 100, truncated: false },
    { status: 'empty', bundles: [], limit: 100, truncated: true },
    { status: 'available', bundles: [{ name: 'a' }, { name: 'a' }], limit: 100, truncated: false },
    { status: 'available', bundles: [{ name: '../a' }], limit: 100, truncated: false },
    { status: 'available', bundles: [{ name: 'CON' }], limit: 100, truncated: false },
    { status: 'available', bundles: Array.from({length: 101}, (_, i) => ({ name: `bundle-${i}` })), limit: 100, truncated: false },
  ])('rejects misleading or unsafe catalogs: %j', async value => {
    respond(value); await expect(fetchRecordingReviews('s')).rejects.toThrow();
  });
  it.each(['', '../a', 'a/b', 'a b', 'NUL', 'com1', 'a'.repeat(65)])('rejects unsafe requested bundle %s before fetch', async name => {
    const fetcher = respond(detail()); await expect(fetchRecordingReview('s', name)).rejects.toThrow();
    expect(fetcher).not.toHaveBeenCalled();
  });
  it.each([
    (d: ReturnType<typeof detail>) => { d.bundle = 'another'; },
    (d: ReturnType<typeof detail>) => { d.created_at = 'yesterday'; },
    (d: ReturnType<typeof detail>) => { d.created_at = '2026-10-23T10:00:00'; },
    (d: ReturnType<typeof detail>) => { d.created_at = '2026-02-30T10:00:00Z'; },
    (d: ReturnType<typeof detail>) => { d.preview.source_sha256 = 'private-path'; },
    (d: ReturnType<typeof detail>) => { d.preview.records.push(d.preview.records[0]); },
    (d: ReturnType<typeof detail>) => { d.preview.records[0].position = 0; },
    (d: ReturnType<typeof detail>) => { d.preview.records[0].name = 'x\u0000'; },
    (d: ReturnType<typeof detail>) => { d.preview.records[0].before_scenes = ['unknown']; },
    (d: ReturnType<typeof detail>) => { d.preview.records[0].after_scenes = []; },
    (d: ReturnType<typeof detail>) => { d.preview.records[0].added_scenes = ['运动提神']; },
    (d: ReturnType<typeof detail>) => { d.preview.summary.uncertain_records = 0; },
    (d: ReturnType<typeof detail>) => { d.preview.summary.scene_verified_records = 1; },
    (d: ReturnType<typeof detail>) => { d.preview.records[0].recording.status = 'audio_verified'; },
    (d: ReturnType<typeof detail>) => { d.preview.records[0].recording.status = 'metadata_matched'; },
    (d: ReturnType<typeof detail>) => { d.preview.records[0].scene_decisions = [{scene: '放松睡前', decision: 'suitable', reason: '仅凭标题'}]; },
  ])('rejects corrupted, mismatched or unsupported assertions', async mutate => {
    const value = detail(); mutate(value); respond(value);
    await expect(fetchRecordingReview('s', 'sample-01')).rejects.toThrow();
  });
  it.each(['javascript:alert(1)', 'https://user:pass@example.com/a', 'https://example.com\\x', ' https://example.com', 'https://example.com:99999'])('rejects unsafe source links %s', async url => {
    const value = detail(); value.preview.records[0].recording.evidence_sources = [{ url, kind: 'catalog', title: '来源', note: '目录'}];
    respond(value); await expect(fetchRecordingReview('s', 'sample-01')).rejects.toThrow();
  });
  it('accepts partial audio assertions but keeps omitted original scenes unfinished', async () => {
    const value = detail(), row = value.preview.records[0];
    row.recording = { status: 'audio_verified', version: '具体版本', note: '作者声明',
      evidence_sources: [{url: 'https://example.com/audio', kind: 'audio', title: '录音', note: '区间来源'}],
      listened_segments: [{source_url: 'https://example.com/audio', start_seconds: 1.5, end_seconds: 30, note: '节奏'}] };
    row.scene_decisions = [{scene: '通勤散步', decision: 'suitable', reason: '记录者的判断'}];
    row.after_scenes.push('通勤散步'); row.added_scenes = ['通勤散步'];
    Object.assign(value.preview.summary, {audio_verified_records: 1, partial_scene_records: 1, added_count: 1});
    respond(value); const parsed = await fetchRecordingReview('s', 'sample-01');
    expect(parsed.preview.summary.uncertain_records).toBe(1);
    expect(parsed.preview.summary.scene_verified_records).toBe(0);
    expect(parsed.preview.records[0].recording.listened_segments[0].start_seconds).toBe(1.5);
  });
  it('propagates unavailable reads instead of turning them into empty or old success', async () => {
    respond({ accepted: false, message: '记录损坏或分类来源变化，请检查本地文件。' }, false, 503);
    await expect(fetchRecordingReview('s', 'sample-01')).rejects.toThrow('记录损坏或分类来源变化');
  });
});
