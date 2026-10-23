import { afterEach, describe, expect, it, vi } from 'vitest';
import { LocalApiError, postRecordingReview } from './api';
import { createRecordingReviewSave, normalizeRecordingReview, previewRecordingReviewEdit, recordingReviewEditError } from './recordingReviewData';
import type { RecordingReviewDetail, RecordingReviewEdit, RecordingReviewSaveRequest } from './recordingReviewData';

const target = `review-${'b'.repeat(32)}`;
function base(): RecordingReviewDetail {
  return { status: 'available', bundle: 'original', review_sha256: 'c'.repeat(64), created_at: '2026-10-23T00:00:00Z', preview: {
    kind: 'classification_scene_review_preview', source_sha256: 'a'.repeat(64),
    summary: { reviewed_records: 2, metadata_matched_records: 0, audio_verified_records: 0, scene_verified_records: 0,
      uncertain_records: 2, partial_scene_records: 0, added_count: 0, removed_count: 0 },
    records: [1, 2].map(position => ({position, name: `歌曲${position}`, artists: '歌手', before_scenes: ['放松睡前'], after_scenes: ['放松睡前'],
      added_scenes: [], removed_scenes: [], scene_decisions: [], recording: {status: 'unresolved', version: '', note: '', evidence_sources: [], listened_segments: []}})) } };
}
function edit(): RecordingReviewEdit {
  return { recording: {status: 'metadata_matched', version: '专辑版', note: '目录候选，未试听',
    evidence_sources: [{url: 'https://example.com/catalog', title: '发行目录', kind: 'catalog', note: '同名版本信息'}], listened_segments: []},
    scene_decisions: [{scene: '放松睡前', decision: 'uncertain', reason: '尚未试听'}] };
}
function request(): RecordingReviewSaveRequest {
  return {base_bundle: 'original', base_sha256: 'c'.repeat(64), target_bundle: target, created_at: '2026-10-23T10:00:00.000Z',
    position: 1, ...edit()};
}
function success() {
  const detail = base(); detail.bundle = target; detail.review_sha256 = 'd'.repeat(64); detail.created_at = request().created_at;
  Object.assign(detail.preview.records[0], edit()); detail.preview.summary.metadata_matched_records = 1;
  return {status: 'saved', bundle: target, replayed: false, detail};
}
function respond(value: unknown, status = 200) {
  const fetcher = vi.fn().mockResolvedValue({ ok: status < 400, status, json: async () => value });
  vi.stubGlobal('fetch', fetcher); return fetcher;
}
afterEach(() => vi.unstubAllGlobals());
describe('recording review save boundary', () => {
  it('retains validated revision hashes and reads legacy responses without enabling writes', () => {
    expect(normalizeRecordingReview(base(), 'original').review_sha256).toBe('c'.repeat(64));
    const legacy = base(); delete legacy.review_sha256;
    expect(normalizeRecordingReview(legacy, 'original').review_sha256).toBeUndefined();
    expect(() => createRecordingReviewSave(legacy, 1, edit())).toThrow();
  });
  it('creates one detached fixed intent and leaves the source unchanged', () => {
    const source = base(), input = edit(); const before = JSON.stringify(source);
    const result = createRecordingReviewSave(source, 1, input);
    expect(result.target_bundle).toMatch(/^review-[a-f0-9]{32}$/);
    expect(result.created_at).toMatch(/Z$/); expect(result.base_sha256).toBe(source.review_sha256);
    input.recording.note = 'later edits'; expect(result.recording.note).toBe('目录候选，未试听');
    expect(JSON.stringify(source)).toBe(before);
  });
  it('sends a single-song POST and confirms the entire returned package', async () => {
    const fetcher = respond(success());
    expect(await postRecordingReview('local', request(), base())).toEqual(success().detail);
    const [url, config] = fetcher.mock.calls[0];
    expect(url).toBe('/api/recording-reviews/save'); expect(config.method).toBe('POST');
    expect(config.headers['X-Organizer-Session']).toBe('local'); expect(JSON.parse(config.body)).toEqual(request());
    expect(config.body).not.toContain('歌曲');
  });
  it('accepts exact idempotent recovery without making another target', async () => {
    const value = success(); value.replayed = true; respond(value);
    expect((await postRecordingReview('s', request(), base())).bundle).toBe(target);
  });
  it.each([
    (value: ReturnType<typeof success>) => { value.bundle = 'wrong'; },
    (value: ReturnType<typeof success>) => { value.detail.created_at = '2026-10-24T10:00:00Z'; },
    (value: ReturnType<typeof success>) => { value.detail.preview.source_sha256 = 'e'.repeat(64); },
    (value: ReturnType<typeof success>) => { value.detail.preview.records[1].recording.note = 'other song corrupted'; },
    (value: ReturnType<typeof success>) => { value.detail.preview.records[0].recording.note = 'not the submitted note'; },
    (value: ReturnType<typeof success>) => { delete value.detail.review_sha256; },
    (value: ReturnType<typeof success>) => { value.detail.review_sha256 = 'invalid'; },
    (value: ReturnType<typeof success>) => { value.detail.preview.summary.uncertain_records = 0; },
  ])('treats mismatched success as unknown instead of clearing the draft', async mutate => {
    const value = success(); mutate(value); respond(value);
    await expect(postRecordingReview('s', request(), base())).rejects.toMatchObject({acceptanceUnknown: true});
  });
  it.each([500, 503])('treats save error HTTP %i as possibly saved', async status => {
    respond({ accepted: false, message: '保存结果未确认' }, status);
    await expect(postRecordingReview('s', request(), base())).rejects.toMatchObject({acceptanceUnknown: true});
  });
  it.each([400, 409])('leaves known HTTP %i failure editable on first submission', async status => {
    respond({ accepted: false, message: '参数或基础记录不符' }, status);
    await expect(postRecordingReview('s', request(), base())).rejects.toMatchObject({acceptanceUnknown: false});
  });
  it('retains unknown outcome after a connection failure', async () => {
    vi.stubGlobal('fetch', vi.fn().mockRejectedValue(new Error('offline')));
    await expect(postRecordingReview('s', request(), base())).rejects.toBeInstanceOf(LocalApiError);
    await expect(postRecordingReview('s', request(), base())).rejects.toMatchObject({acceptanceUnknown: true});
  });
  it.each([
    (value: RecordingReviewSaveRequest) => { value.target_bundle = '../x'; },
    (value: RecordingReviewSaveRequest) => { value.base_sha256 = 'e'.repeat(64); },
    (value: RecordingReviewSaveRequest) => { value.position = 100; },
    (value: RecordingReviewSaveRequest) => { value.created_at = '2026-02-30T00:00:00Z'; },
    (value: RecordingReviewSaveRequest) => { value.recording.evidence_sources[0].url = 'javascript:alert(1)'; },
    (value: RecordingReviewSaveRequest) => { value.scene_decisions[0].decision = 'suitable'; },
  ])('rejects invalid intents before sending', async mutate => {
    const input = request(); mutate(input); const fetcher = respond(success());
    await expect(postRecordingReview('s', input, base())).rejects.toMatchObject({acceptanceUnknown: false});
    expect(fetcher).not.toHaveBeenCalled();
  });
  it('checks UTF-8 request size instead of JavaScript string length', async () => {
    const input = request(); input.recording.evidence_sources = Array.from({length: 12}, (_, index) => ({
      url: `https://example.com/${index}`, kind: 'catalog', title: '目录', note: '中'.repeat(2000) }));
    expect(JSON.stringify(input).length).toBeLessThan(65536);
    const fetcher = respond(success());
    await expect(postRecordingReview('s', input, base())).rejects.toThrow('64 KiB'); expect(fetcher).not.toHaveBeenCalled();
  });
  it('shows conservative membership and catches missing evidence before explicit decisions', () => {
    const row = base().preview.records[0];
    expect(previewRecordingReviewEdit(row, edit()).after_scenes).toEqual(['放松睡前']);
    const invalid = edit(); invalid.scene_decisions[0].decision = 'unsuitable';
    expect(recordingReviewEditError(row, invalid)).not.toBe('');
    expect(() => previewRecordingReviewEdit(row, invalid)).toThrow();
    expect(recordingReviewEditError(row, edit())).toBe('');
  });
});
