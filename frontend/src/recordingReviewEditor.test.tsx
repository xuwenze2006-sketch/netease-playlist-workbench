import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { RecordingReviewsPanel } from './RecordingReviewsPanel';
import { fetchRecordingReview, fetchRecordingReviews, postRecordingReview } from './api';
import type { RecordingReviewDetail } from './recordingReviewData';

vi.mock('./api', () => ({ readSession: () => 'session', fetchRecordingReviews: vi.fn(),
  fetchRecordingReview: vi.fn(), postRecordingReview: vi.fn() }));
const catalog = { status: 'available' as const, bundles: [{ name: 'sample-a' }, { name: 'sample-b' }],
  limit: 100 as const, truncated: false as const };
function detail(bundle = 'sample-a'): RecordingReviewDetail {
  return { status: 'available', bundle, created_at: '2026-10-23T09:00:00+08:00', review_sha256: 'b'.repeat(64), preview: {
    kind: 'classification_scene_review_preview', source_sha256: 'a'.repeat(64),
    summary: { reviewed_records: 2, metadata_matched_records: 0, audio_verified_records: 0,
      scene_verified_records: 0, uncertain_records: 2, partial_scene_records: 0, added_count: 0, removed_count: 0 },
    records: [1, 2].map(position => ({ position, name: `测试歌曲${position}`, artists: '测试歌手', before_scenes: ['学习专注'],
      after_scenes: ['学习专注'], added_scenes: [], removed_scenes: [], scene_decisions: [],
      recording: { status: 'unresolved', version: '', note: '', evidence_sources: [], listened_segments: [] } })),
  } };
}
function deferred<T>() { let resolve!: (value: T) => void; let reject!: (reason: Error) => void;
  const promise = new Promise<T>((yes, no) => { resolve = yes; reject = no; }); return { promise, resolve, reject }; }
async function choose(bundle = 'sample-a') {
  const select = screen.getByRole('combobox', { name: '核验包' });
  await waitFor(() => expect(select).toHaveProperty('disabled', false));
  fireEvent.change(select, { target: { value: bundle } });
  await screen.findByRole('article', { name: '测试歌曲1' });
}
function card(position = 1) { return within(screen.getByRole('article', { name: `测试歌曲${position}` })); }
async function open(position = 1) {
  fireEvent.click(card(position).getByRole('button', { name: '填写核验' }));
  return card(position);
}
beforeEach(() => {
  vi.mocked(fetchRecordingReviews).mockReset().mockResolvedValue(catalog);
  vi.mocked(fetchRecordingReview).mockReset().mockImplementation(async (_session, bundle) => detail(bundle));
  vi.mocked(postRecordingReview).mockReset().mockImplementation(async (_session, request) => detail(request.target_bundle));
});

describe('recording review editor sessions', () => {
  it('starts six scenes as unfilled and keeps original membership without inventing confirmation', async () => {
    render(<RecordingReviewsPanel />); await choose(); const row = await open();
    expect(row.getByRole('combobox', { name: '学习专注判断' })).toHaveProperty('value', '');
    expect(row.getAllByRole('combobox', { name: /判断$/ })).toHaveLength(6);
    expect(row.getByLabelText('填写预览').textContent).toContain('新收录学习专注');
    expect(row.queryByRole('textbox', { name: '歌名' })).toBeNull();
  });
  it('preserves edits through collapse, filter, bundle changes, inactive navigation and refresh', async () => {
    const view = render(<RecordingReviewsPanel />); await choose(); let row = await open();
    fireEvent.change(row.getByLabelText('版本说明'), { target: { value: '保留我的版本' } });
    fireEvent.click(row.getByRole('button', { name: '收起填写' }));
    fireEvent.click(row.getByRole('button', { name: '继续填写核验' }));
    fireEvent.change(screen.getByRole('searchbox'), { target: { value: '无匹配' } });
    fireEvent.click(screen.getByRole('button', { name: '清除筛选' }));
    await choose('sample-b'); await choose('sample-a');
    view.rerender(<RecordingReviewsPanel active={false} />); view.rerender(<RecordingReviewsPanel active />);
    await waitFor(() => expect(screen.getByRole('button', { name: '刷新核验记录' })).toHaveProperty('disabled', false));
    fireEvent.click(screen.getByRole('button', { name: '刷新核验记录' }));
    await waitFor(() => expect(screen.getByRole('button', { name: '刷新核验记录' })).toHaveProperty('disabled', false));
    row = card(); expect(row.getByLabelText('版本说明')).toHaveProperty('value', '保留我的版本');
  });
  it('preserves old input but blocks save after review hash changes', async () => {
    render(<RecordingReviewsPanel />); await choose(); const row = await open();
    fireEvent.change(row.getByLabelText('版本说明'), { target: { value: '旧依据填写' } });
    const newer = detail(); newer.review_sha256 = 'c'.repeat(64);
    vi.mocked(fetchRecordingReview).mockResolvedValue(newer);
    fireEvent.click(screen.getByRole('button', { name: '刷新核验记录' }));
    await screen.findByText(/原始核验包或分类来源已变化/);
    expect(row.getByLabelText('版本说明')).toHaveProperty('value', '旧依据填写');
    expect(row.getByRole('button', { name: '另存新核验包' })).toHaveProperty('disabled', true);
  });
  it('freezes one target after unknown acceptance and reuses it even after another retry rejection', async () => {
    const unknown = Object.assign(new Error('网络中断'), { acceptanceUnknown: true });
    vi.mocked(postRecordingReview).mockRejectedValueOnce(unknown).mockRejectedValueOnce(new Error('来源暂不可用'));
    render(<RecordingReviewsPanel />); await choose(); const row = await open();
    fireEvent.change(row.getByLabelText('版本说明'), { target: { value: '固定内容' } });
    fireEvent.click(row.getByRole('button', { name: '另存新核验包' }));
    await row.findByRole('button', { name: '核对并重试保存' });
    expect(row.getByLabelText('版本说明').closest('fieldset')).toHaveProperty('disabled', true);
    expect(row.queryByRole('button', { name: '放弃本曲填写' })).toBeNull();
    const first = vi.mocked(postRecordingReview).mock.calls[0][1];
    fireEvent.click(row.getByRole('button', { name: '核对并重试保存' }));
    await row.findByText('来源暂不可用');
    expect(row.getByLabelText('版本说明').closest('fieldset')).toHaveProperty('disabled', true);
    fireEvent.click(row.getByRole('button', { name: '核对并重试保存' }));
    await waitFor(() => expect(screen.getByRole('combobox', { name: '核验包' })).toHaveProperty('value', first.target_bundle));
    expect(vi.mocked(postRecordingReview).mock.calls.map(call => call[1])).toEqual([first, first, first]);
  });
  it('guards double submit synchronously and keeps other song sessions on successful save', async () => {
    const pending = deferred<RecordingReviewDetail>(); vi.mocked(postRecordingReview).mockReturnValueOnce(pending.promise);
    render(<RecordingReviewsPanel />); await choose(); await open(2);
    fireEvent.change(card(2).getByLabelText('版本说明'), { target: { value: '另一首未保存' } });
    const row = await open(); const button = row.getByRole('button', { name: '另存新核验包' });
    fireEvent.click(button); fireEvent.click(button);
    expect(vi.mocked(postRecordingReview).mock.calls).toHaveLength(1);
    const target = vi.mocked(postRecordingReview).mock.calls[0][1].target_bundle;
    await act(async () => pending.resolve(detail(target)));
    await choose();
    expect(card(2).getByLabelText('版本说明')).toHaveProperty('value', '另一首未保存');
    expect(card().getByRole('button', { name: '填写核验' })).toBeTruthy();
  });
  it('requires inline second confirmation to discard', async () => {
    render(<RecordingReviewsPanel />); await choose(); const row = await open();
    fireEvent.change(row.getByLabelText('版本说明'), { target: { value: '仍保留' } });
    fireEvent.click(row.getByRole('button', { name: '放弃本曲填写' }));
    expect(row.getByLabelText('版本说明')).toHaveProperty('value', '仍保留');
    fireEvent.click(row.getByRole('button', { name: '确认放弃填写' }));
    await open(); expect(row.getByLabelText('版本说明')).toHaveProperty('value', '');
  });
  it('does not change selection after a save completes while inactive', async () => {
    const pending = deferred<RecordingReviewDetail>(); vi.mocked(postRecordingReview).mockReturnValueOnce(pending.promise);
    const view = render(<RecordingReviewsPanel />); await choose(); const row = await open();
    fireEvent.click(row.getByRole('button', { name: '另存新核验包' }));
    view.rerender(<RecordingReviewsPanel active={false} />);
    const target = vi.mocked(postRecordingReview).mock.calls[0][1].target_bundle;
    await act(async () => pending.resolve(detail(target)));
    view.rerender(<RecordingReviewsPanel active />);
    expect(screen.getByRole('combobox', { name: '核验包' })).toHaveProperty('value', 'sample-a');
    await screen.findByText(new RegExp(`已另存.*${target}`));
  });
  it('rejects unsafe evidence and invalid times, without deleting entered text', async () => {
    render(<RecordingReviewsPanel />); await choose(); const row = await open();
    fireEvent.click(row.getByRole('button', { name: '添加来源' }));
    fireEvent.change(row.getByLabelText('来源 1 链接'), { target: { value: 'javascript:alert(1)' } });
    expect(row.getByLabelText('来源 1 链接')).toHaveProperty('value', 'javascript:alert(1)');
    expect(row.getByRole('button', { name: '另存新核验包' })).toHaveProperty('disabled', true);
    fireEvent.click(row.getByRole('button', { name: '移除来源 1' }));
    expect(row.getByRole('button', { name: '另存新核验包' })).toHaveProperty('disabled', false);
  });
  it('keeps old server packages read only when no review hash exists', async () => {
    const old = detail(); delete old.review_sha256; vi.mocked(fetchRecordingReview).mockResolvedValue(old);
    render(<RecordingReviewsPanel />); await choose();
    expect(card().queryByRole('button', { name: '填写核验' })).toBeNull();
    expect(screen.getByText(/缺少核验文件校验值/)).toBeTruthy();
  });
  it('records audio evidence, validates empty seconds and previews explicit scene changes before keyboard save', async () => {
    render(<RecordingReviewsPanel />); await choose(); const row = await open();
    const input = (name: string, value: string) => fireEvent.change(row.getByLabelText(name), { target: { value } });
    input('录音核验状态', 'audio_verified'); input('版本说明', '专辑录音'); input('核验备注', '已人工对照试听\n合成测试');
    fireEvent.click(row.getByRole('button', { name: '添加来源' }));
    input('来源 1 类型', 'audio'); input('来源 1 链接', 'https://example.com/listen');
    input('来源 1 标题', '音频样本'); input('来源 1 说明', '测试用音频入口');
    fireEvent.click(row.getByRole('button', { name: '添加试听片段' }));
    input('片段 1 结束秒数', '35.5'); input('片段 1 音频链接', 'https://example.com/listen'); input('片段 1 说明', '节奏与动态');
    expect(row.getByRole('button', { name: '另存新核验包' })).toHaveProperty('disabled', true);
    input('片段 1 开始秒数', '0');
    input('学习专注判断', 'unsuitable'); input('学习专注理由', '合成测试：动态起伏影响专注');
    input('运动提神判断', 'suitable'); input('运动提神理由', '合成测试：节奏稳定');
    const preview = row.getByLabelText('填写预览');
    expect(preview.textContent).toContain('新收录运动提神');
    expect(preview.textContent).toContain('拟移除学习专注');
    fireEvent.keyDown(row.getByLabelText('运动提神理由'), { key: 'Enter', ctrlKey: true });
    await screen.findByText(/已另存并核对新核验包/);
    const request = vi.mocked(postRecordingReview).mock.calls[0][1];
    expect(request.recording.listened_segments).toEqual([{ start_seconds: 0, end_seconds: 35.5,
      source_url: 'https://example.com/listen', note: '节奏与动态' }]);
    expect(request.scene_decisions).toEqual([
      { scene: '学习专注', decision: 'unsuitable', reason: '合成测试：动态起伏影响专注' },
      { scene: '运动提神', decision: 'suitable', reason: '合成测试：节奏稳定' },
    ]);
  });
  it('keeps edits mutable after known rejection but requires rereading the current basis', async () => {
    vi.mocked(postRecordingReview).mockRejectedValueOnce(new Error('来源冲突'));
    render(<RecordingReviewsPanel />); await choose(); const row = await open();
    fireEvent.click(row.getByRole('button', { name: '另存新核验包' }));
    await row.findByText('来源冲突');
    expect(row.getByLabelText('版本说明').closest('fieldset')).toHaveProperty('disabled', false);
    fireEvent.change(row.getByLabelText('版本说明'), { target: { value: '修改后保留' } });
    expect(row.getByRole('button', { name: '另存新核验包' })).toHaveProperty('disabled', true);
    fireEvent.click(screen.getByRole('button', { name: '刷新核验记录' }));
    await waitFor(() => expect(row.getByRole('button', { name: '另存新核验包' })).toHaveProperty('disabled', false));
    expect(row.getByLabelText('版本说明')).toHaveProperty('value', '修改后保留');
  });
  it('retains a pending request through refresh and refuses programmatic input changes', async () => {
    vi.mocked(postRecordingReview).mockRejectedValueOnce(Object.assign(new Error('unknown'), { acceptanceUnknown: true }));
    render(<RecordingReviewsPanel />); await choose(); const row = await open();
    fireEvent.change(row.getByLabelText('版本说明'), { target: { value: '冻结内容' } });
    fireEvent.click(row.getByRole('button', { name: '另存新核验包' }));
    await row.findByRole('button', { name: '核对并重试保存' });
    const original = vi.mocked(postRecordingReview).mock.calls[0][1];
    fireEvent.click(screen.getByRole('button', { name: '刷新核验记录' }));
    await waitFor(() => expect(screen.getByRole('button', { name: '刷新核验记录' })).toHaveProperty('disabled', false));
    fireEvent.change(row.getByLabelText('版本说明'), { target: { value: '不应改变' } });
    expect(row.getByLabelText('版本说明')).toHaveProperty('value', '冻结内容');
    fireEvent.click(row.getByRole('button', { name: '核对并重试保存' }));
    await screen.findByText(/已另存并核对新核验包/);
    expect(vi.mocked(postRecordingReview).mock.calls[1][1]).toEqual(original);
  });
  it('makes saved input accessible when the selected package disappears from the catalog', async () => {
    render(<RecordingReviewsPanel />); await choose(); const row = await open();
    fireEvent.change(row.getByLabelText('版本说明'), { target: { value: '找回填写' } });
    vi.mocked(fetchRecordingReviews).mockResolvedValue({ ...catalog, bundles: [{ name: 'sample-b' }] });
    fireEvent.click(screen.getByRole('button', { name: '刷新核验记录' }));
    await screen.findByText(/所选核验包已不在列表中/);
    vi.mocked(fetchRecordingReview).mockRejectedValue(new Error('包不存在'));
    fireEvent.click(screen.getByRole('button', { name: 'sample-a · 第 1 首' }));
    await screen.findByText('包不存在');
    expect(card().getByLabelText('版本说明')).toHaveProperty('value', '找回填写');
    expect(card().getByRole('button', { name: '另存新核验包' })).toHaveProperty('disabled', true);
  });
  it('reopens the current bundle draft from the retained-input list without leaving the reader loading', async () => {
    render(<RecordingReviewsPanel />); await choose(); const row = await open();
    fireEvent.change(row.getByLabelText('版本说明'), { target: { value: '当前包草稿' } });
    fireEvent.change(screen.getByRole('searchbox'), { target: { value: '过滤隐藏全部' } });
    fireEvent.click(screen.getByRole('button', { name: 'sample-a · 第 1 首' }));
    await waitFor(() => expect(screen.getByRole('button', { name: '刷新核验记录' })).toHaveProperty('disabled', false));
    expect(card().getByLabelText('版本说明')).toHaveProperty('value', '当前包草稿');
    expect(card().getByRole('button', { name: '另存新核验包' })).toHaveProperty('disabled', false);
  });
  it.each([{ isComposing: true }, { repeat: true }])('ignores composing or repeated keyboard saves (%j)', async flags => {
    render(<RecordingReviewsPanel />); await choose(); const row = await open();
    fireEvent.keyDown(row.getByLabelText('版本说明'), { key: 'Enter', ctrlKey: true, ...flags });
    expect(postRecordingReview).not.toHaveBeenCalled();
    expect(row.getByRole('button', { name: '另存新核验包' })).toHaveProperty('disabled', false);
    fireEvent.keyDown(row.getByLabelText('版本说明'), { key: 'Enter', metaKey: true });
    await screen.findByText(/已另存并核对新核验包/);
  });
  it('reports oversized UTF8 payloads without submitting or clearing valid fields', async () => {
    const large = detail(); large.preview.records[0].recording.evidence_sources = Array.from({ length: 12 }, (_, index) => ({
      url: `https://example.com/${index}`, title: `来源${index}`, kind: 'catalog', note: '测'.repeat(2000),
    }));
    vi.mocked(fetchRecordingReview).mockResolvedValue(large);
    render(<RecordingReviewsPanel />); await choose(); const row = await open();
    fireEvent.click(row.getByRole('button', { name: '另存新核验包' }));
    await row.findByText(/超过 64 KiB/);
    expect(postRecordingReview).not.toHaveBeenCalled();
    expect(row.getByLabelText('来源 12 说明')).toHaveProperty('value', '测'.repeat(2000));
    expect(row.getByLabelText('版本说明').closest('fieldset')).toHaveProperty('disabled', false);
  });
});
