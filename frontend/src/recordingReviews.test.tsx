import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { RecordingReviewsPanel } from './RecordingReviewsPanel';
import { fetchRecordingReview, fetchRecordingReviews } from './api';
import type { RecordingReviewDetail } from './recordingReviewData';

vi.mock('./api', () => ({
  readSession: () => 'review-test-session', fetchRecordingReviews: vi.fn(), fetchRecordingReview: vi.fn(),
}));

const catalog = { status: 'available' as const, bundles: [{ name: 'sample-a' }, { name: 'sample-b' }],
  limit: 100 as const, truncated: false as const };
function detail(bundle = 'sample-a'): RecordingReviewDetail {
  return { status: 'available', bundle, created_at: '2026-10-23T09:00:00+08:00', preview: {
    kind: 'classification_scene_review_preview', source_sha256: 'a'.repeat(64),
    summary: { reviewed_records: 3, metadata_matched_records: 1, audio_verified_records: 2,
      scene_verified_records: 1, uncertain_records: 2, partial_scene_records: 1, added_count: 1, removed_count: 1 },
    records: [
      { position: 1, name: '还未确认的歌', artists: '歌手甲', before_scenes: ['学习专注'], after_scenes: ['学习专注'],
        added_scenes: [], removed_scenes: [], scene_decisions: [], recording: {
          status: 'metadata_matched', version: '专辑版本', note: '只查了目录\n仍需核验录音', listened_segments: [],
          evidence_sources: [{ url: 'https://example.com/catalog', title: '官方目录', kind: 'catalog', note: '目录时长相符' }],
        } },
      { position: 2, name: '有变化的歌', artists: 'Band Beta', before_scenes: ['放松睡前', '通勤散步'],
        after_scenes: ['通勤散步', '运动提神'], added_scenes: ['运动提神'], removed_scenes: ['放松睡前'],
        scene_decisions: [{ scene: '放松睡前', decision: 'unsuitable', reason: '合成示例：音量变化明显' },
          { scene: '运动提神', decision: 'suitable', reason: '合成示例：节奏稳定' }], recording: {
          status: 'audio_verified', version: '测试版本', note: '仅用于测试的保存声明',
          evidence_sources: [{ url: 'https://example.com/audio', title: '试听来源', kind: 'audio', note: '合成音频链接' }],
          listened_segments: [{ start_seconds: 10, end_seconds: 35.5, source_url: 'https://example.com/audio', note: '合成区间说明' }],
        } },
      { position: 3, name: '已填场景的歌', artists: '歌手丙', before_scenes: ['独处释怀'], after_scenes: ['独处释怀'],
        added_scenes: [], removed_scenes: [], scene_decisions: [{ scene: '独处释怀', decision: 'suitable', reason: '合成示例：已填写判断' }],
        recording: { status: 'audio_verified', version: '另一个测试版本', note: '',
          evidence_sources: [{ url: 'https://example.com/third', title: '另一试听来源', kind: 'audio', note: '' }],
          listened_segments: [{ start_seconds: 0, end_seconds: 20, source_url: 'https://example.com/third', note: '' }],
        } },
    ],
  } };
}
function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (reason: Error) => void;
  const promise = new Promise<T>((yes, no) => { resolve = yes; reject = no; });
  return { promise, resolve, reject };
}
async function choose(bundle = 'sample-a') {
  const select = await screen.findByRole('combobox', { name: '核验包' });
  await waitFor(() => expect(select).toHaveProperty('disabled', false));
  fireEvent.change(select, { target: { value: bundle } });
}
async function open() {
  render(<RecordingReviewsPanel />);
  await choose();
  await screen.findByRole('article', { name: /还未确认的歌/ });
}
beforeEach(() => {
  vi.mocked(fetchRecordingReviews).mockReset().mockResolvedValue(catalog);
  vi.mocked(fetchRecordingReview).mockReset().mockImplementation(async (_session, bundle) => detail(bundle));
});

describe('saved recording review workbench', () => {
  it('requires explicit bundle selection and never implies the first bundle is authoritative', async () => {
    render(<RecordingReviewsPanel />);
    const select = await screen.findByRole('combobox', { name: '核验包' });
    await waitFor(() => expect(select).toHaveProperty('disabled', false));
    expect(select).toHaveProperty('value', '');
    expect(fetchRecordingReview).not.toHaveBeenCalled();
    expect(screen.getByText(/请选择核验包/)).toBeTruthy();
    expect(screen.getByText(/只读.*未自动合并.*未改歌单/)).toBeTruthy();
    expect(fetchRecordingReviews).toHaveBeenCalledWith('review-test-session', expect.any(AbortSignal));
  });
  it('shows full-bundle counts, all memberships and omitted original scene judgments', async () => {
    await open();
    expect(screen.getByLabelText('核验包汇总').textContent).toContain('场景待核验2');
    expect(screen.getByLabelText('核验包汇总').textContent).toContain('拟新增1');
    expect(screen.getByText('显示 3 / 3 首')).toBeTruthy();
    const changed = within(screen.getByRole('article', { name: /有变化的歌/ }));
    expect(changed.getByText('原收录')).toBeTruthy();
    expect(changed.getByText('预览收录')).toBeTruthy();
    expect(changed.getByText('合成示例：音量变化明显')).toBeTruthy();
    expect(changed.getByText(/未记录该场景的判断，保留原收录/)).toBeTruthy();
    expect(changed.getByText('场景待核验')).toBeTruthy();
    expect(changed.getByText('有试听记录（保存声明）')).toBeTruthy();
    expect(screen.getByText(/本程序未独立验证试听/)).toBeTruthy();
  });
  it('searches names and artists, combines filters, and keeps total summary unchanged', async () => {
    await open();
    fireEvent.click(screen.getByRole('button', { name: '场景待核验' }));
    expect(screen.getAllByRole('article')).toHaveLength(2);
    fireEvent.change(screen.getByRole('searchbox', { name: '搜索歌名或歌手' }), { target: { value: '  band BETA  ' } });
    expect(screen.getAllByRole('article')).toHaveLength(1);
    expect(screen.getByText('显示 1 / 3 首')).toBeTruthy();
    expect(screen.getByLabelText('核验包汇总').textContent).toContain('核验曲目3');
    fireEvent.click(screen.getByRole('button', { name: '有收录变化' }));
    expect(screen.getAllByRole('article')).toHaveLength(1);
    fireEvent.change(screen.getByRole('searchbox', { name: '搜索歌名或歌手' }), { target: { value: '不存在' } });
    expect(screen.queryAllByRole('article')).toHaveLength(0);
    fireEvent.click(screen.getByRole('button', { name: '清除筛选' }));
    expect(screen.getAllByRole('article')).toHaveLength(3);
  });
  it('keeps composition text from changing results until committed', async () => {
    await open();
    const input = screen.getByRole('searchbox', { name: '搜索歌名或歌手' });
    fireEvent.compositionStart(input);
    fireEvent.change(input, { target: { value: '歌手甲' } });
    expect(screen.getAllByRole('article')).toHaveLength(3);
    fireEvent.compositionEnd(input, { target: { value: '歌手甲' } });
    expect(screen.getAllByRole('article')).toHaveLength(1);
  });
  it('matches width variants and Greek final sigma while describing created_at as record time', async () => {
    const value = detail(); value.preview.records[1].artists = 'Ｂａｎｄ Ｂｅｔａ';
    value.preview.records[2].artists = 'Οδυσσεύς';
    vi.mocked(fetchRecordingReview).mockResolvedValue(value);
    await open();
    expect(screen.getByText(/^记录时间：/)).toBeTruthy();
    const input = screen.getByRole('searchbox', { name: '搜索歌名或歌手' });
    fireEvent.change(input, { target: { value: 'band beta' } });
    expect(screen.getAllByRole('article')).toHaveLength(1);
    expect(screen.getByRole('article', { name: /有变化的歌/ })).toBeTruthy();
    fireEvent.change(input, { target: { value: 'Οδυσσεύσ' } });
    expect(screen.getAllByRole('article')).toHaveLength(1);
    expect(screen.getByRole('article', { name: /已填场景的歌/ })).toBeTruthy();
  });
  it('reveals evidence and segments with safe links and escaped content', async () => {
    const value = detail(); value.preview.records[1].name = '<img src=x onerror=alert(1)>';
    value.preview.records[1].recording.note = '<script>unsafe()</script>\n保留换行';
    vi.mocked(fetchRecordingReview).mockResolvedValue(value);
    await open();
    const row = screen.getByRole('article', { name: /<img src=x/ });
    const summary = within(row).getByText('来源、试听区间与备注');
    // JSDOM does not implement native summary keyboard activation; browser acceptance covers it.
    await userEvent.click(summary);
    expect(summary.closest('details')).toHaveProperty('open', true);
    expect(within(row).getByText('<script>unsafe()</script> 保留换行')).toBeTruthy();
    const links = within(row).getAllByRole('link');
    expect(links).toHaveLength(2);
    for (const link of links) {
      expect(link).toHaveProperty('target', '_blank');
      expect(link.getAttribute('rel')).toBe('noopener noreferrer');
    }
    expect(within(row).getByText('0:10–0:35.5')).toBeTruthy();
    expect(row.querySelector('script, img, audio, iframe')).toBeNull();
    await userEvent.click(summary);
    expect(summary.closest('details')).toHaveProperty('open', false);
  });
  it('distinguishes an empty catalog from an unreadable one and retries the latter', async () => {
    vi.mocked(fetchRecordingReviews).mockRejectedValueOnce(new Error('broken'))
      .mockResolvedValueOnce({ status: 'empty', bundles: [], limit: 100, truncated: false });
    render(<RecordingReviewsPanel />);
    expect(await screen.findByRole('alert')).toHaveProperty('textContent', expect.stringContaining('核验包列表读取失败'));
    expect(screen.queryByText('尚无已保存的核验包。')).toBeNull();
    fireEvent.click(screen.getByRole('button', { name: '重试读取列表' }));
    expect(await screen.findByText('尚无已保存的核验包。')).toBeTruthy();
    expect(fetchRecordingReview).not.toHaveBeenCalled();
  });
  it('keeps catalog refresh failures distinct from an empty result', async () => {
    await open();
    vi.mocked(fetchRecordingReviews).mockRejectedValue(new Error('unreadable'));
    fireEvent.click(screen.getByRole('button', { name: '刷新核验记录' }));
    await screen.findByRole('alert');
    expect(screen.queryByText('尚无已保存的核验包。')).toBeNull();
    expect(screen.getByRole('combobox', { name: '核验包' })).toHaveProperty('disabled', true);
    expect(screen.getByText(/上次读取的列表，仅供参考/)).toBeTruthy();
  });
  it('preserves actionable service failure reasons as bounded plain text', async () => {
    vi.mocked(fetchRecordingReviews).mockRejectedValueOnce(new Error('核验包超过 100 个，请整理目录后重试。'));
    render(<RecordingReviewsPanel />);
    expect(await screen.findByText('核验包超过 100 个，请整理目录后重试。')).toBeTruthy();
    fireEvent.click(screen.getByRole('button', { name: '重试读取列表' }));
    vi.mocked(fetchRecordingReview).mockRejectedValueOnce(new Error('页面会话已失效，请重新打开工作台。'));
    await choose();
    expect(await screen.findByText('页面会话已失效，请重新打开工作台。')).toBeTruthy();
    const message = '<script>unsafe()</script>' + '边界'.repeat(1000);
    vi.mocked(fetchRecordingReview).mockRejectedValueOnce(new Error(message));
    fireEvent.click(screen.getByRole('button', { name: '重试读取核验包' }));
    const bounded = await screen.findByText(message.slice(0, 500));
    expect(bounded.textContent?.length).toBe(500);
    expect(bounded.querySelector('script')).toBeNull();
  });
  it('clears the previous bundle immediately, ignores delayed responses and aborts superseded reads', async () => {
    const first = deferred<RecordingReviewDetail>();
    const second = detail('sample-b'); second.preview.records[0].name = '另一个包的歌';
    vi.mocked(fetchRecordingReview).mockReturnValueOnce(first.promise).mockResolvedValueOnce(second);
    render(<RecordingReviewsPanel />);
    await choose('sample-a');
    const firstSignal = vi.mocked(fetchRecordingReview).mock.calls[0][2];
    await choose('sample-b');
    await screen.findByRole('article', { name: /另一个包的歌/ });
    expect(firstSignal?.aborted).toBe(true);
    await act(async () => first.resolve(detail('sample-a')));
    expect(screen.queryByRole('article', { name: /还未确认的歌/ })).toBeNull();
    expect(screen.getByRole('combobox', { name: '核验包' })).toHaveProperty('value', 'sample-b');
  });
  it('never shows a previously loaded bundle when another bundle is broken', async () => {
    await open();
    vi.mocked(fetchRecordingReview).mockRejectedValueOnce(new Error('source changed'));
    await choose('sample-b');
    expect(screen.queryAllByRole('article')).toHaveLength(0);
    expect(await screen.findByRole('alert')).toHaveProperty('textContent', expect.stringContaining('来源不符时不能使用'));
    expect(screen.queryAllByRole('article')).toHaveLength(0);
    fireEvent.click(screen.getByRole('button', { name: '重试读取核验包' }));
    await screen.findByRole('article', { name: /还未确认的歌/ });
    expect(vi.mocked(fetchRecordingReview).mock.lastCall?.[1]).toBe('sample-b');
  });
  it('labels retained details as old during refresh and after failure, then clears the label on recovery', async () => {
    await open();
    const pending = deferred<RecordingReviewDetail>();
    vi.mocked(fetchRecordingReview).mockReturnValueOnce(pending.promise);
    fireEvent.click(screen.getByRole('button', { name: '刷新核验记录' }));
    expect(screen.getByRole('article', { name: /还未确认的歌/ })).toBeTruthy();
    expect(screen.getByText(/上次读取的核验内容.*不能作为当前核验结果/)).toBeTruthy();
    await act(async () => pending.reject(new Error('source mismatch')));
    await screen.findByRole('alert');
    expect(screen.getByText(/上次读取的核验内容.*不能作为当前核验结果/)).toBeTruthy();
    fireEvent.click(screen.getByRole('button', { name: '重试读取核验包' }));
    await waitFor(() => expect(screen.queryByText(/上次读取的核验内容.*不能作为当前核验结果/)).toBeNull());
  });
  it('removes a selected bundle when a successful refreshed catalog no longer contains it', async () => {
    await open();
    vi.mocked(fetchRecordingReviews).mockResolvedValue({ ...catalog, bundles: [{ name: 'sample-b' }] });
    fireEvent.click(screen.getByRole('button', { name: '刷新核验记录' }));
    await screen.findByText(/所选核验包已不在列表中/);
    expect(screen.queryAllByRole('article')).toHaveLength(0);
    expect(screen.getByRole('combobox', { name: '核验包' })).toHaveProperty('value', '');
  });
  it('ends pending detail loading when the selected bundle disappears during refresh', async () => {
    await open();
    const pending = deferred<RecordingReviewDetail>();
    vi.mocked(fetchRecordingReviews).mockResolvedValue({ status: 'empty', bundles: [], limit: 100, truncated: false });
    vi.mocked(fetchRecordingReview).mockReturnValueOnce(pending.promise);
    fireEvent.click(screen.getByRole('button', { name: '刷新核验记录' }));
    await screen.findByText('尚无已保存的核验包。');
    expect(screen.queryAllByRole('article')).toHaveLength(0);
    expect(screen.getByRole('button', { name: '刷新核验记录' })).toHaveProperty('disabled', false);
    expect(vi.mocked(fetchRecordingReview).mock.lastCall?.[2]?.aborted).toBe(true);
    await act(async () => pending.resolve(detail()));
    expect(screen.queryAllByRole('article')).toHaveLength(0);
  });
  it('does not request while inactive and rereads while preserving the selection and filter after returning', async () => {
    const view = render(<RecordingReviewsPanel active={false} />);
    expect(fetchRecordingReviews).not.toHaveBeenCalled();
    view.rerender(<RecordingReviewsPanel active />);
    await choose();
    await screen.findByRole('article', { name: /还未确认的歌/ });
    fireEvent.change(screen.getByRole('searchbox', { name: '搜索歌名或歌手' }), { target: { value: '歌手甲' } });
    const detailSignal = vi.mocked(fetchRecordingReview).mock.calls[0][2];
    const catalogSignal = vi.mocked(fetchRecordingReviews).mock.calls[0][1];
    view.rerender(<RecordingReviewsPanel active={false} />);
    expect(detailSignal?.aborted).toBe(true);
    expect(catalogSignal?.aborted).toBe(true);
    expect(screen.queryAllByRole('article')).toHaveLength(0);
    const pending = deferred<RecordingReviewDetail>();
    vi.mocked(fetchRecordingReview).mockReturnValueOnce(pending.promise);
    view.rerender(<RecordingReviewsPanel active />);
    expect(screen.getByText(/上次读取的核验内容.*不能作为当前核验结果/)).toBeTruthy();
    expect(screen.getByRole('searchbox', { name: '搜索歌名或歌手' })).toHaveProperty('value', '歌手甲');
    await act(async () => pending.resolve(detail()));
    expect(fetchRecordingReviews).toHaveBeenCalledTimes(2);
    expect(fetchRecordingReview).toHaveBeenCalledTimes(2);
    expect(screen.getAllByRole('article')).toHaveLength(1);
  });
  it('ignores responses after leaving the page and aborts requests on unmount', async () => {
    const pending = deferred<RecordingReviewDetail>();
    vi.mocked(fetchRecordingReview).mockReturnValueOnce(pending.promise);
    const view = render(<RecordingReviewsPanel />);
    await choose();
    const signal = vi.mocked(fetchRecordingReview).mock.calls[0][2];
    view.rerender(<RecordingReviewsPanel active={false} />);
    expect(signal?.aborted).toBe(true);
    await act(async () => pending.resolve(detail()));
    expect(screen.queryAllByRole('article', { hidden: true })).toHaveLength(0);
    view.rerender(<RecordingReviewsPanel active />);
    await screen.findByRole('article', { name: /还未确认的歌/ });
    const currentSignal = vi.mocked(fetchRecordingReview).mock.lastCall?.[2];
    view.unmount();
    expect(currentSignal?.aborted).toBe(true);
  });
});
