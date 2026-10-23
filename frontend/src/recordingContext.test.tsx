import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { ClassificationPanel } from './ClassificationPanel';
import { fetchClassification, postClassificationDraft } from './api';
import { normalizeClassification } from './classificationData';
import type { ClassificationQuery } from './types';

vi.mock('./api', async (original) => ({
  ...(await original<typeof import('./api')>()), fetchClassification: vi.fn(), postClassificationDraft: vi.fn(),
}));

const request: ClassificationQuery = { offset: 0, query: '', dimension: 'all', tag: '', review: 'all' };
const context = { source: 'historical_snapshot', generated_at: '2026-10-01T09:00:00+08:00',
  album: '历史专辑甲', duration_ms: 202026 };
function raw(recordingContext: unknown = context) {
  return { status: 'available', source: 'local_record', verification: 'local_only',
    updated_at: '2026-10-07T00:00:00Z', verified_at: null,
    summary: { source_count: 2, covered_count: 2, playlist_count: 0, pending_count: 0,
      unknown_style_count: 0, unknown_language_count: 0 },
    options: { style: ['流行抒情'], scene: ['通勤散步'], language: ['国语'] }, playlists: [], filters: request,
    pagination: { offset: 0, limit: 50, total: 2, next_offset: null },
    quality: { source_version: 'a'.repeat(64), draft_status: 'ready', revision: 4, changed_count: 0,
      review_count: 0, pilot_positions: [], playlist_changes: [], rules: [] },
    records: [1, 2].map((position) => ({ position, record_key: (position === 1 ? 'b' : 'c').repeat(32),
      name: '刀剑如梦', artists: '周华健', styles: ['流行抒情'], scenes: ['通勤散步'], language: '国语',
      pending_reasons: [], evidence_note: '原分类依据', language_evidence_note: '',
      style_judgment_score: .7, review_note: false, needs_review: false, review_reasons: [], draft: null,
      recording_context: recordingContext })),
  };
}
function parsed(recordingContext: unknown = context) {
  return normalizeClassification(raw(recordingContext), request);
}
beforeEach(() => {
  document.head.innerHTML = '<meta name="organizer-session" content="recording-context-test" />';
  vi.mocked(fetchClassification).mockReset(); vi.mocked(postClassificationDraft).mockReset();
});

describe('optional historical recording context boundary', () => {
  it('whitelists context on records with and without quality while stripping private nested data', () => {
    const value = raw({ ...context, id: 'PRIVATE', audio_url: 'PRIVATE', token: 'PRIVATE' });
    const result = normalizeClassification(value, request);
    expect(result.records[0]).toHaveProperty('recording_context', context);
    expect(JSON.stringify(result)).not.toContain('PRIVATE');
    const { quality: _quality, ...legacy } = value;
    expect(normalizeClassification(legacy, request).records[0]).toHaveProperty('recording_context', context);
  });
  it.each([null, [], 123, 'bad', {}, { ...context, source: 'current_account' },
    { ...context, generated_at: null }, { ...context, generated_at: '2026-10-01' },
    { ...context, generated_at: '2026-10-01T09:00:00' }, { ...context, generated_at: '2026-02-30T09:00:00Z' },
    { ...context, generated_at: 'PRIVATE' }])('keeps the core page usable with unavailable provenance: %j', (value) => {
    const result = parsed(value);
    expect(result.records).toHaveLength(2);
    expect(result.records[0]).toHaveProperty('recording_context', null);
  });
  it('accepts an old response that entirely omits context', () => {
    const value = raw();
    const records = value.records.map(({ recording_context: _context, ...record }) => record);
    expect(normalizeClassification({ ...value, records }, request).records[0]).toHaveProperty('recording_context', null);
  });
  it.each([null, undefined, '', '   ', 7, true, ['专辑'], 'x'.repeat(513), '😀'.repeat(513),
    '专辑\u0000', '专辑\n', '专辑\u007f', '专辑\u0085', '专辑\ud800', '专辑\udfff'])
    ('drops only invalid album while retaining valid historical duration: %j', (album) => {
      expect(parsed({ ...context, album }).records[0]).toHaveProperty('recording_context', { ...context, album: null });
    });
  it.each([null, undefined, 0, -1, 86400001, .5, '202026', true, NaN, Infinity, {}, []])
    ('drops only invalid duration while retaining valid historical album: %j', (duration_ms) => {
      expect(parsed({ ...context, duration_ms }).records[0]).toHaveProperty('recording_context', { ...context, duration_ms: null });
    });
  it.each([1, 86400000])('accepts the inclusive duration boundary %s and a 512 code-point album', (duration_ms) => {
    const expected = { ...context, album: '😀'.repeat(512), duration_ms };
    expect(parsed(expected).records[0]).toHaveProperty('recording_context', expected);
  });
  it('retains provenance when both metadata values are unavailable', () => {
    const expected = { ...context, album: null, duration_ms: null };
    expect(parsed(expected).records[0]).toHaveProperty('recording_context', expected);
  });
  it('does not soften malformed core record validation', () => {
    const value = raw(null); value.records[0].name = 'bad\u0000';
    expect(() => normalizeClassification(value, request)).toThrow('invalid classification data');
  });
});

describe('read-only historical recording context cards', () => {
  it('distinguishes same-title rows once per card while making historical limitations explicit', async () => {
    const value = raw(); value.records[1].recording_context = { ...context, album: '历史专辑乙', duration_ms: 188000 };
    vi.mocked(fetchClassification).mockResolvedValue(normalizeClassification(value, request));
    await act(async () => { render(<ClassificationPanel />); });
    const rows = within(screen.getByRole('region', { name: '逐曲分类结果' })).getAllByRole('article');
    expect(within(rows[0]).getByText('历史专辑甲', { exact: false })).toBeTruthy();
    expect(within(rows[0]).getByText('时长：3:22')).toBeTruthy();
    expect(within(rows[1]).getByText('历史专辑乙', { exact: false })).toBeTruthy();
    expect(within(rows[1]).getByText('时长：3:08')).toBeTruthy();
    fireEvent.click(within(rows[0]).getByRole('button', { name: '修正分类' }));
    expect(within(rows[0]).getAllByRole('group', { name: '历史曲目资料' })).toHaveLength(1);
    expect(within(rows[0]).getByText(/历史本地快照.*不代表当前账号资料.*不能确认音频版本/)).toBeTruthy();
    expect(within(rows[0]).getByText(/快照生成：2026/)).toBeTruthy();
    expect(within(rows[0]).getByLabelText('录音版本依据')).toHaveProperty('value', '');
    expect(vi.mocked(postClassificationDraft)).not.toHaveBeenCalled();
  });
  it.each([
    [{ ...context, album: null }, '专辑：未记录', '资料不完整'],
    [{ ...context, duration_ms: null }, '时长：未记录', '资料不完整'],
    [{ ...context, album: null, duration_ms: null }, '专辑：未记录', '资料不完整'],
    [null, '历史曲目资料不可用', '不代表当前账号资料'],
  ])('explains partial and unavailable context without inventing metadata: %j', async (value, expected, note) => {
    vi.mocked(fetchClassification).mockResolvedValue(parsed(value));
    await act(async () => { render(<ClassificationPanel />); });
    const first = screen.getAllByRole('group', { name: '历史曲目资料' })[0];
    expect(within(first).getByText(expected as string, { exact: false })).toBeTruthy();
    expect(within(first).getByText(note as string, { exact: false })).toBeTruthy();
    if (value === null) expect(within(first).queryByText(/快照生成/)).toBeNull();
  });
  it('floors milliseconds without rolling up the displayed second', async () => {
    vi.mocked(fetchClassification).mockResolvedValue(parsed({ ...context, duration_ms: 59999 }));
    await act(async () => { render(<ClassificationPanel />); });
    expect(screen.getAllByText('时长：0:59')).toHaveLength(2);
  });
  it('preserves unsaved correction values across context-only explicit refresh and excludes context from saving', async () => {
    vi.mocked(fetchClassification).mockImplementation(async (_session, query) => ({ ...parsed(), filters: query }));
    await act(async () => { render(<ClassificationPanel />); });
    const row = screen.getAllByRole('article')[0];
    fireEvent.click(within(row).getByRole('button', { name: '修正分类' }));
    const form = within(row).getByRole('group', { name: '修正刀剑如梦' });
    const editor = within(form);
    fireEvent.click(editor.getByLabelText('摇滚与独立'));
    fireEvent.click(editor.getByLabelText('学习专注'));
    fireEvent.change(editor.getByLabelText('语言'), { target: { value: '英语' } });
    fireEvent.change(editor.getByLabelText('修正理由'), { target: { value: '未保存理由\n继续核对' } });
    fireEvent.change(editor.getByLabelText('录音版本依据'), { target: { value: '未保存版本依据\n尚未试听' } });
    const refreshed = parsed({ ...context, album: '😀'.repeat(512), duration_ms: 188000,
      generated_at: '2026-10-02T09:00:00+08:00' });
    vi.mocked(fetchClassification).mockImplementation(async (_session, query) => ({ ...refreshed, filters: query }));
    fireEvent.click(screen.getByRole('button', { name: '刷新分类记录' }));
    await waitFor(() => expect(within(row).getByText('时长：3:08')).toBeTruthy());
    expect(within(row).getByRole('group', { name: '修正刀剑如梦' })).toBe(form);
    expect(editor.getByLabelText('修正理由')).toHaveProperty('value', '未保存理由\n继续核对');
    expect(editor.getByLabelText('录音版本依据')).toHaveProperty('value', '未保存版本依据\n尚未试听');
    expect(editor.getByLabelText('摇滚与独立')).toHaveProperty('checked', true);
    expect(editor.getByLabelText('学习专注')).toHaveProperty('checked', true);
    expect(editor.getByLabelText('语言')).toHaveProperty('value', '英语');
    expect(within(row).getByText('有未保存修改')).toBeTruthy();
    expect(vi.mocked(fetchClassification).mock.lastCall?.[3]).toBe(true);
    expect(vi.mocked(postClassificationDraft)).not.toHaveBeenCalled();
    for (const optionalContext of [null, { ...context, album: '恢复的历史专辑' }]) {
      vi.mocked(fetchClassification).mockImplementation(async (_session, query) => ({ ...parsed(optionalContext), filters: query }));
      fireEvent.click(screen.getByRole('button', { name: '刷新分类记录' }));
      await waitFor(() => expect(within(row).getByText(optionalContext ? '专辑：恢复的历史专辑' : '历史曲目资料不可用')).toBeTruthy());
      expect(within(row).getByRole('group', { name: '修正刀剑如梦' })).toBe(form);
      expect(editor.getByLabelText('修正理由')).toHaveProperty('value', '未保存理由\n继续核对');
      expect(editor.getByLabelText('录音版本依据')).toHaveProperty('value', '未保存版本依据\n尚未试听');
      expect(vi.mocked(postClassificationDraft)).not.toHaveBeenCalled();
    }
    vi.mocked(postClassificationDraft).mockResolvedValue({ accepted: true, message: '已保存', revision: 5, changed_count: 1 });
    fireEvent.click(editor.getByRole('button', { name: '保存本地修正' }));
    await waitFor(() => expect(vi.mocked(postClassificationDraft)).toHaveBeenCalledTimes(1));
    expect(vi.mocked(postClassificationDraft).mock.lastCall?.[1]).toEqual({ action: 'save', source_version: 'a'.repeat(64),
      revision: 4, record_key: 'b'.repeat(32), styles: ['流行抒情', '摇滚与独立'], scenes: ['通勤散步', '学习专注'],
      language: '英语', reason: '未保存理由\n继续核对', recording_note: '未保存版本依据\n尚未试听' });
  });
  it('restores cached unsaved editing when returning to a row after a context-only refresh', async () => {
    vi.mocked(fetchClassification).mockImplementation(async (_session, query) => ({ ...parsed(), filters: query }));
    await act(async () => { render(<ClassificationPanel />); });
    const row = screen.getAllByRole('article')[0];
    fireEvent.click(within(row).getByRole('button', { name: '修正分类' }));
    const editor = within(within(row).getByRole('group', { name: '修正刀剑如梦' }));
    fireEvent.change(editor.getByLabelText('修正理由'), { target: { value: '筛选前的理由' } });
    fireEvent.change(editor.getByLabelText('录音版本依据'), { target: { value: '筛选前的版本依据' } });
    vi.mocked(fetchClassification).mockImplementation(async (_session, query) => {
      const result = parsed({ ...context, album: '更新的历史专辑' });
      return { ...result, filters: query, ...(query.review === 'pilot'
        ? { records: [], pagination: { ...result.pagination, total: 0 } } : {}) };
    });
    fireEvent.click(screen.getByRole('button', { name: '刷新分类记录' }));
    await waitFor(() => expect(within(row).getByText('专辑：更新的历史专辑')).toBeTruthy());
    fireEvent.change(screen.getByLabelText('复核筛选'), { target: { value: 'pilot' } });
    await screen.findByText('没有匹配的曲目');
    fireEvent.change(screen.getByLabelText('复核筛选'), { target: { value: 'all' } });
    await waitFor(() => expect(screen.getAllByRole('article')).toHaveLength(2));
    const restored = within(screen.getAllByRole('article')[0]);
    expect(restored.getByLabelText('修正理由')).toHaveProperty('value', '筛选前的理由');
    expect(restored.getByLabelText('录音版本依据')).toHaveProperty('value', '筛选前的版本依据');
    expect(restored.getByText('有未保存修改')).toBeTruthy();
    expect(restored.getByText('专辑：更新的历史专辑')).toBeTruthy();
    expect(vi.mocked(postClassificationDraft)).not.toHaveBeenCalled();
  });
});
