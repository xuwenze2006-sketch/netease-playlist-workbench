import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { ClassificationPanel } from './ClassificationPanel';
import { fetchClassification, postClassificationDraft } from './api';
import type { ClassificationPage, ClassificationQuery } from './types';

vi.mock('./api', async (original) => ({
  ...(await original<typeof import('./api')>()),
  fetchClassification: vi.fn(),
  postClassificationDraft: vi.fn(),
}));

const query: ClassificationQuery = { offset: 0, query: '', dimension: 'all', tag: '', review: 'all' };
function page(revision = 0, draft: unknown = null, status = 'ready'): ClassificationPage {
  return {
    status: 'available', source: 'local_record', verification: 'local_only',
    updated_at: '2026-10-07T10:00:00+08:00', verified_at: null,
    summary: { source_count: 1, covered_count: 1, playlist_count: 1, pending_count: 0,
      unknown_style_count: 0, unknown_language_count: 0 },
    options: { scene: ['通勤散步'], style: ['流行抒情'], language: ['国语'] },
    playlists: [{ name: '场景 · 通勤散步', dimension: 'scene', count: 1, key: null }],
    filters: query, pagination: { offset: 0, limit: 50, total: 1, next_offset: null },
    quality: { source_version: 'a'.repeat(64), draft_status: status, revision, changed_count: draft ? 1 : 0,
      review_count: 1, pilot_positions: [1], playlist_changes: draft ? [{ name: '场景 · 通勤散步', added_count: 0, removed_count: 1 }] : [],
      rules: [{ scene: '学习专注', include: ['低干扰、强度平稳'], exclude: ['突兀段落、强烈人声'] }] },
    records: [{ position: 1, record_key: 'b'.repeat(32), name: '版本有疑问的歌', artists: '示例歌手',
      styles: ['流行抒情'], scenes: ['通勤散步'], language: '国语', pending_reasons: [],
      evidence_note: '已有曲目资料', language_evidence_note: '歌词与演唱判断冲突',
      style_judgment_score: 0.65, review_note: true, needs_review: true,
      review_reasons: ['语言证据冲突'], draft }],
  } as ClassificationPage;
}
async function open(initial = page()) {
  vi.mocked(fetchClassification).mockImplementation(async (_session, request) => ({ ...initial, filters: request }));
  await act(async () => { render(<ClassificationPanel />); });
}
function editor() { return within(screen.getByRole('group', { name: '修正版本有疑问的歌' })); }
async function edit() {
  fireEvent.click(screen.getByRole('button', { name: '修正分类' }));
  await screen.findByRole('group', { name: '修正版本有疑问的歌' });
}
beforeEach(() => {
  document.head.innerHTML = '<meta name="organizer-session" content="quality-test" />';
  vi.mocked(fetchClassification).mockReset();
  vi.mocked(postClassificationDraft).mockReset();
});

describe('classification review and local corrections', () => {
  it('uses ready corrections as displayed labels only in the explicit draft basis and separates unknown counts', async () => {
    const draft = { styles: ['摇滚与独立'], scenes: ['放松睡前'], language: '英语', reason: '核对录音后修正', recording_note: '专辑版' };
    const data = page(1, draft);
    data.records[0].styles = ['待辨识']; data.records[0].language = '待辨识'; data.records[0].pending_reasons = ['风格待辨识', '语言待辨识'];
    data.summary!.pending_count = 1; data.summary!.unknown_style_count = 1; data.summary!.unknown_language_count = 1;
    data.quality!.draft_summary = { pending_count: 0, unknown_style_count: 0, unknown_language_count: 0 };
    data.options = { scene: ['通勤散步', '放松睡前'], style: ['待辨识', '摇滚与独立'], language: ['待辨识', '英语'] };
    await open(data);
    fireEvent.change(screen.getByLabelText('分类依据'), { target: { value: 'draft' } });
    await waitFor(() => expect(vi.mocked(fetchClassification).mock.lastCall?.[1].basis).toBe('draft'));
    await screen.findByRole('button', { name: '语言 · 英语' });
    const track = within(screen.getByRole('region', { name: '逐曲分类结果' }).querySelector('article')!);
    expect(track.getByRole('button', { name: '语言 · 英语' })).toBeTruthy();
    expect(track.queryByText('待辨识', { selector: '.pending-tag' })).toBeNull();
    expect(screen.getByText('修正后待辨识')).toBeTruthy();
    expect(screen.getByText(/刷新、筛选、分页或保存.*关闭未保存编辑/)).toBeTruthy();
    expect(data.records[0].language).toBe('待辨识');
  });

  it('falls back to the original labels when corrections are stale and offers returning to the original basis', async () => {
    const data = page(1, { styles: ['流行抒情'], scenes: [], language: '英语', reason: '旧判断', recording_note: '' }, 'stale');
    await open(data);
    fireEvent.change(screen.getByLabelText('分类依据'), { target: { value: 'draft' } });
    await waitFor(() => expect(vi.mocked(fetchClassification).mock.lastCall?.[1].basis).toBe('draft'));
    expect(screen.getByText(/当前回退原分类/)).toBeTruthy();
    expect(screen.getByRole('button', { name: '语言 · 国语' })).toBeTruthy();
    fireEvent.click(screen.getByRole('button', { name: '返回原分类' }));
    await waitFor(() => expect(vi.mocked(fetchClassification).mock.lastCall?.[1].basis).toBe('original'));
  });

  it('returns from playlists, clears searches and retains draft basis when opening all corrected unknown tracks', async () => {
    const data = page(); data.quality!.draft_summary = { pending_count: 0, unknown_style_count: 0, unknown_language_count: 0 };
    await open(data);
    fireEvent.change(screen.getByLabelText('分类依据'), { target: { value: 'draft' } });
    await screen.findByText('修正后待辨识');
    fireEvent.change(screen.getByLabelText('搜索歌曲或歌手'), { target: { value: '旧搜索' } });
    fireEvent.click(screen.getByRole('tab', { name: '分类歌单' }));
    expect(screen.getByRole('tab', { name: '分类歌单' }).getAttribute('aria-selected')).toBe('true');
    fireEvent.click(screen.getByRole('button', { name: '查看全部待辨识' }));
    await waitFor(() => expect(vi.mocked(fetchClassification).mock.lastCall?.[1]).toMatchObject({ basis: 'draft', query: '', review: 'pending', offset: 0 }));
    expect(screen.getByLabelText('搜索歌曲或歌手')).toHaveProperty('value', '');
    expect(screen.getByRole('tab', { name: '歌曲分类' }).getAttribute('aria-selected')).toBe('true');
    expect(vi.mocked(fetchClassification).mock.calls.some((call) => call[1].query === '旧搜索')).toBe(false);
  });

  it.each([
    ['needs_review', '查看全部需复核'], ['pilot', '查看 1 首试点样本'], ['draft', '查看本地修正'],
  ] as const)('opens the %s shortcut without old query or tag conditions and retains draft basis', async (review, name) => {
    const data = page(1, { styles: ['流行抒情'], scenes: ['通勤散步'], language: '国语', reason: '已保存的依据', recording_note: '' });
    data.quality!.draft_summary = { pending_count: 0, unknown_style_count: 0, unknown_language_count: 0 };
    await open(data);
    fireEvent.change(screen.getByLabelText('分类依据'), { target: { value: 'draft' } });
    await screen.findByRole('button', { name: '编辑本地修正' });
    fireEvent.change(screen.getByLabelText('分类维度'), { target: { value: 'scene' } });
    fireEvent.change(screen.getByLabelText('分类标签'), { target: { value: '通勤散步' } });
    await waitFor(() => expect(screen.getByRole('region', { name: '逐曲分类结果' }).getAttribute('aria-busy')).toBe('false'));
    fireEvent.change(screen.getByLabelText('搜索歌曲或歌手'), { target: { value: '旧搜索' } });
    fireEvent.click(screen.getByRole('tab', { name: '分类歌单' }));
    const boundary = vi.mocked(fetchClassification).mock.calls.length;
    fireEvent.click(screen.getByRole('button', { name }));
    await waitFor(() => expect(vi.mocked(fetchClassification).mock.lastCall?.[1]).toMatchObject({
      basis: 'draft', review, query: '', dimension: 'all', tag: '', offset: 0,
    }));
    expect(screen.getByRole('tab', { name: '歌曲分类' }).getAttribute('aria-selected')).toBe('true');
    expect(screen.getByLabelText('搜索歌曲或歌手')).toHaveProperty('value', '');
    expect(vi.mocked(fetchClassification).mock.calls.slice(boundary).every((call) =>
      call[1].query === '' && call[1].dimension === 'all' && call[1].tag === '')).toBe(true);
  });

  it('displays title version cues as clues and lets the user filter them independently', async () => {
    const data = page(); data.records[0].recording_hints = ['现场版本线索'];
    await open(data);
    expect(screen.getByText('现场版本线索')).toBeTruthy();
    expect(screen.getByText(/标题线索.*尚未确认录音版本/)).toBeTruthy();
    fireEvent.change(screen.getByLabelText('复核筛选'), { target: { value: 'version' } });
    await waitFor(() => expect(vi.mocked(fetchClassification).mock.lastCall?.[1].review).toBe('version'));
  });

  it('shows conflicts even when the old pending flag is empty and supports independent review filters', async () => {
    await open();
    expect(screen.getByText('语言证据冲突')).toBeTruthy();
    expect(screen.getByText(/模型自评分.*未经校准/)).toBeTruthy();
    fireEvent.change(screen.getByLabelText('复核筛选'), { target: { value: 'conflict' } });
    await waitFor(() => expect(vi.mocked(fetchClassification).mock.lastCall?.[1].review).toBe('conflict'));
    fireEvent.click(screen.getByRole('button', { name: '查看 1 首试点样本' }));
    await waitFor(() => expect(vi.mocked(fetchClassification).mock.lastCall?.[1].review).toBe('pilot'));
  });

  it('saves a local correction then reads a fresh projection with its reason and playlist changes', async () => {
    await open(); await edit();
    fireEvent.click(editor().getByLabelText('通勤散步'));
    fireEvent.click(editor().getByLabelText('学习专注'));
    fireEvent.change(editor().getByLabelText('修正理由'), { target: { value: '试听后确认强度平稳，人声干扰较少' } });
    fireEvent.change(editor().getByLabelText('录音版本依据'), { target: { value: '专辑录音，非现场版' } });
    const draft = { styles: ['流行抒情'], scenes: ['学习专注'], language: '国语',
      reason: '试听后确认强度平稳，人声干扰较少', recording_note: '专辑录音，非现场版' };
    vi.mocked(postClassificationDraft).mockResolvedValue({ accepted: true, message: '已保存本地修正', revision: 1, changed_count: 1 });
    vi.mocked(fetchClassification).mockImplementation(async (_session, request) => ({ ...page(1, draft), filters: request }));
    fireEvent.click(editor().getByRole('button', { name: '保存本地修正' }));
    await waitFor(() => expect(screen.getByText('本地修正 · 1 首')).toBeTruthy());
    expect(vi.mocked(postClassificationDraft).mock.lastCall?.[1]).toEqual({ action: 'save', source_version: 'a'.repeat(64),
      revision: 0, record_key: 'b'.repeat(32), ...draft });
    expect(vi.mocked(fetchClassification).mock.lastCall?.[3]).toBe(true);
    expect(screen.getByText(draft.reason)).toBeTruthy();
    expect(screen.getByText('移出 1 首')).toBeTruthy();
    expect(screen.getByText(/草稿尚未写入网易云歌单/)).toBeTruthy();
  });

  it('removes a saved correction and restores the baseline without a save replay', async () => {
    const draft = { styles: ['流行抒情'], scenes: [], language: '国语', reason: '不适合通勤', recording_note: '' };
    await open(page(1, draft));
    vi.mocked(postClassificationDraft).mockResolvedValue({ accepted: true, message: '已撤销本地修正', revision: 2, changed_count: 0 });
    vi.mocked(fetchClassification).mockImplementation(async (_session, request) => ({ ...page(2), filters: request }));
    fireEvent.click(screen.getByRole('button', { name: '撤销本地修正' }));
    await waitFor(() => expect(screen.getByText('本地修正 · 0 首')).toBeTruthy());
    expect(vi.mocked(postClassificationDraft).mock.lastCall?.[1]).toEqual({ action: 'remove', source_version: 'a'.repeat(64), revision: 1, record_key: 'b'.repeat(32) });
    expect(screen.queryByText('不适合通勤')).toBeNull();
  });

  it('blocks editing when the draft belongs to a different source version', async () => {
    await open(page(0, null, 'stale'));
    expect(screen.getByRole('button', { name: '修正分类' }).hasAttribute('disabled')).toBe(true);
    expect(screen.getByText(/来源记录已变化/)).toBeTruthy();
    expect(vi.mocked(postClassificationDraft)).not.toHaveBeenCalled();
  });

  it('retains the entered reason after a failed submit and does not retry automatically', async () => {
    await open(); await edit();
    fireEvent.change(editor().getByLabelText('修正理由'), { target: { value: '需要保留的试听判断' } });
    vi.mocked(postClassificationDraft).mockRejectedValue(new Error('本地草稿保存失败，请重试。'));
    fireEvent.click(editor().getByRole('button', { name: '保存本地修正' }));
    expect(await screen.findByRole('alert')).toHaveProperty('textContent', '本地草稿保存失败，请重试。');
    expect(editor().getByLabelText('修正理由')).toHaveProperty('value', '需要保留的试听判断');
    expect(vi.mocked(postClassificationDraft)).toHaveBeenCalledTimes(1);
  });

  it('keeps all fixed labels available even if no current record has those labels', async () => {
    await open(); await edit();
    expect(editor().getByLabelText('古典与合唱')).toBeTruthy();
    expect(editor().getByLabelText('运动提神')).toBeTruthy();
    expect(editor().getByRole('option', { name: '器乐或配乐录音' })).toBeTruthy();
    fireEvent.click(editor().getByLabelText('风格待辨识'));
    expect(editor().getByLabelText('流行抒情')).toHaveProperty('checked', false);
  });

  it('discards an older filtered GET after a correction has been accepted', async () => {
    await open(); await edit();
    fireEvent.change(editor().getByLabelText('修正理由'), { target: { value: '接受的新判断' } });
    let accept!: (value: Awaited<ReturnType<typeof postClassificationDraft>>) => void;
    vi.mocked(postClassificationDraft).mockImplementation(() => new Promise((resolve) => { accept = resolve; }));
    let oldResult!: (value: ClassificationPage) => void;
    vi.mocked(fetchClassification).mockImplementationOnce(() => new Promise((resolve) => { oldResult = resolve; }));
    fireEvent.click(editor().getByRole('button', { name: '保存本地修正' }));
    fireEvent.change(screen.getByLabelText('复核筛选'), { target: { value: 'conflict' } });
    const draft = { styles: ['流行抒情'], scenes: ['通勤散步'], language: '国语', reason: '接受的新判断', recording_note: '' };
    vi.mocked(fetchClassification).mockImplementation(async (_session, request) => ({ ...page(1, draft), filters: request }));
    await act(async () => { accept({ accepted: true, message: '已保存', revision: 1, changed_count: 1 }); });
    await screen.findByText('接受的新判断');
    await act(async () => { oldResult(page()); });
    expect(screen.getByText('本地修正 · 1 首')).toBeTruthy();
    expect(screen.getByText('接受的新判断')).toBeTruthy();
  });

  it('keeps an open editor disabled while refreshed records are pending', async () => {
    await open(); await edit();
    let complete!: (value: ClassificationPage) => void;
    vi.mocked(fetchClassification).mockImplementationOnce(() => new Promise((resolve) => { complete = resolve; }));
    fireEvent.click(screen.getByRole('button', { name: '刷新分类记录' }));
    expect(editor().getByLabelText('修正理由').closest('fieldset')?.disabled).toBe(true);
    expect(vi.mocked(postClassificationDraft)).not.toHaveBeenCalled();
    await act(async () => { complete(page()); });
  });

  it.each(['source', 'record', 'revision'])('does not carry an unsaved editor into a changed %s identity', async (kind) => {
    await open(); await edit();
    fireEvent.change(editor().getByLabelText('修正理由'), { target: { value: '仅属于旧资料的判断' } });
    const updated = page(kind === 'revision' ? 1 : 0);
    if (kind === 'source') updated.quality!.source_version = 'c'.repeat(64);
    if (kind === 'record') updated.records[0].record_key = 'd'.repeat(32);
    vi.mocked(fetchClassification).mockImplementation(async (_session, request) => ({ ...updated, filters: request }));
    fireEvent.click(screen.getByRole('button', { name: '刷新分类记录' }));
    await waitFor(() => expect(screen.queryByRole('group', { name: '修正版本有疑问的歌' })).toBeNull());
    await edit();
    expect(editor().getByLabelText('修正理由')).toHaveProperty('value', '');
    expect(vi.mocked(postClassificationDraft)).not.toHaveBeenCalled();
  });
});
