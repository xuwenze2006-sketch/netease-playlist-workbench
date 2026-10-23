import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { ClassificationPanel } from './ClassificationPanel';
import { ClassificationReadError, fetchClassification, postClassificationDraft } from './api';
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
function twoSongPage(revision = 0, draft: unknown = null): ClassificationPage {
  const result = page(revision, draft);
  result.records.push({ ...result.records[0], position: 2, record_key: 'c'.repeat(32), name: '另一首待核对的歌', draft: null });
  result.pagination.total = 2;
  result.summary!.source_count = result.summary!.covered_count = 2;
  return result;
}

function unavailablePage(request: ClassificationQuery, pending = false): ClassificationPage {
  return { status: 'unavailable', source: 'local_record', verification: 'local_only',
    updated_at: null, verified_at: null, summary: null,
    options: { scene: [], style: [], language: [] }, playlists: [], records: [],
    filters: request, pagination: { offset: request.offset, limit: 50, total: 0, next_offset: null },
    ...(pending ? { error_code: 'classification_generation_incomplete' } : {}) } as ClassificationPage;
}
async function open(initial = page()) {
  vi.mocked(fetchClassification).mockImplementation(async (_session, request) => ({ ...initial, filters: request }));
  let view!: ReturnType<typeof render>;
  await act(async () => { view = render(<ClassificationPanel />); });
  return view;
}
function editor() { return within(screen.getByRole('group', { name: '修正版本有疑问的歌' })); }
function nativeDisabledBlur(source: HTMLElement) {
  // jsdom also ignores blur() on disabled controls, unlike Edge's native blur.
  const disabled = [source, source.closest('fieldset[disabled]')]
    .filter((element): element is HTMLElement => !!element && element.hasAttribute('disabled'));
  disabled.forEach((element) => element.removeAttribute('disabled'));
  source.blur();
  disabled.forEach((element) => element.setAttribute('disabled', ''));
}
async function edit() {
  fireEvent.click(screen.getByRole('button', { name: '修正分类' }));
  await screen.findByRole('group', { name: '修正版本有疑问的歌' });
}
async function editBothSongs() {
  await open(twoSongPage());
  const rows = within(screen.getByRole('region', { name: '逐曲分类结果' })).getAllByRole('article');
  fireEvent.click(within(rows[0]).getByRole('button', { name: '修正分类' }));
  fireEvent.click(within(rows[1]).getByRole('button', { name: '修正分类' }));
  fireEvent.change(editor().getByLabelText('修正理由'), { target: { value: '第一首核对依据' } });
  fireEvent.change(within(screen.getByRole('group', { name: '修正另一首待核对的歌' })).getByLabelText('修正理由'),
    { target: { value: '第二首未保存依据' } });
}
beforeEach(() => {
  document.head.innerHTML = '<meta name="organizer-session" content="quality-test" />';
  vi.mocked(fetchClassification).mockReset();
  vi.mocked(postClassificationDraft).mockReset();
});

describe('classification review and local corrections', () => {
  it.each([true, false])('keeps unsaved text through an unavailable ordinary query and same-source recovery (pending=%s)', async (pending) => {
    await open(); await edit();
    fireEvent.change(editor().getByLabelText('修正理由'), { target: { value: '暂时不可读时也应保留的理由' } });
    fireEvent.change(editor().getByLabelText('录音版本依据'), { target: { value: '尚未保存的专辑版本' } });
    vi.mocked(fetchClassification).mockImplementationOnce(async (_session, request) => unavailablePage(request, pending));
    fireEvent.change(screen.getByLabelText('复核筛选'), { target: { value: 'pilot' } });
    const warning = await screen.findByRole('alert');
    expect(warning.textContent).toContain('保留');
    expect(screen.queryByText('资料版本或草稿状态已变化，旧的未保存编辑已清除，请重新核对。')).toBeNull();
    expect(screen.getByText('来源曲目')).toBeTruthy();
    expect(screen.queryByRole('link', { name: '查看恢复说明' }) !== null).toBe(pending);
    fireEvent.click(screen.getByRole('button', { name: '重试读取分类成果' }));
    await screen.findByRole('group', { name: '修正版本有疑问的歌' });
    expect(editor().getByLabelText('修正理由')).toHaveProperty('value', '暂时不可读时也应保留的理由');
    expect(editor().getByLabelText('录音版本依据')).toHaveProperty('value', '尚未保存的专辑版本');
    expect(editor().getByLabelText('修正理由')).toHaveProperty('disabled', false);
    expect(screen.queryByRole('alert')).toBeNull();
    expect(vi.mocked(postClassificationDraft)).not.toHaveBeenCalled();
  });

  it('explains a cold pending generation with a separate help link and an explicit read retry', async () => {
    vi.mocked(fetchClassification).mockResolvedValueOnce(unavailablePage(query, true));
    await open();
    expect(screen.getByRole('heading', { name: '分类文件尚未生成完整' })).toBeTruthy();
    const help = screen.getByRole('link', { name: '查看恢复说明' });
    expect(help.getAttribute('href')).toContain('docs/development.md');
    expect(help.getAttribute('target')).toBe('_blank');
    expect(help.getAttribute('rel')).toContain('noopener');
    fireEvent.click(screen.getByRole('button', { name: '重试读取分类成果' }));
    await screen.findByText('版本有疑问的歌');
    expect(screen.queryByRole('link', { name: '查看恢复说明' })).toBeNull();
    expect(vi.mocked(postClassificationDraft)).not.toHaveBeenCalled();
  });

  async function navigateOriginalCard(kind: 'quality' | 'legacy' | 'review' = 'quality', detail = 0) {
    const source = page();
    if (kind === 'legacy') delete source.quality;
    if (kind === 'review') source.playlists = [{ name: '分类 · 待辨识', dimension: 'review', count: 1, key: null }];
    const view = await open(source);
    fireEvent.change(screen.getByRole('textbox', { name: '搜索歌曲或歌手' }), { target: { value: '旧搜索' } });
    fireEvent.click(screen.getByRole('tab', { name: '分类歌单' }));
    const opener = within(screen.getByRole('region', { name: '分类歌单' })).getByRole('button', {
      name: kind === 'review' ? '查看全部待辨识' : '查看分类曲目',
    });
    const heading = screen.getByRole('heading', { name: '逐曲分类结果', hidden: true });
    const focus = vi.spyOn(heading, 'focus'), scroll = vi.fn(); heading.scrollIntoView = scroll;
    let resolve!: () => void, reject!: () => void;
    vi.mocked(fetchClassification).mockImplementationOnce((_session, request) => new Promise((done, fail) => {
      resolve = () => done({ ...source, filters: request });
      reject = () => fail(new Error('读取失败'));
    }));
    opener.focus();
    if (detail === 0) fireEvent.keyDown(opener, { key: 'Enter' });
    fireEvent.click(opener, { detail });
    return { view, heading, focus, scroll, resolve, reject };
  }

  it.each(['quality', 'legacy', 'review'] as const)('lands on results immediately after keyboard navigation from a %s original playlist card', async (kind) => {
    const { heading, focus, scroll, resolve } = await navigateOriginalCard(kind);
    expect(document.activeElement).toBe(heading);
    expect(focus).toHaveBeenCalledOnce();
    expect(scroll).toHaveBeenCalledExactlyOnceWith({ block: 'start' });
    expect(screen.getByRole('tab', { name: '歌曲分类' }).getAttribute('aria-selected')).toBe('true');
    expect(screen.getByRole('textbox', { name: '搜索歌曲或歌手' })).toHaveProperty('value', '');
    expect(vi.mocked(fetchClassification).mock.lastCall?.[1]).toEqual(kind === 'review'
      ? { offset: 0, dimension: 'all', tag: '', review: 'pending', query: '', basis: 'original' }
      : { offset: 0, dimension: 'scene', tag: '通勤散步', review: 'all', query: '', ...(kind === 'quality' ? { basis: 'original' } : {}) });
    await act(async () => { resolve(); });
    expect(focus).toHaveBeenCalledOnce(); expect(scroll).toHaveBeenCalledOnce();
    expect(vi.mocked(postClassificationDraft)).not.toHaveBeenCalled();
  });

  it('scrolls to results without focusing the heading after pointer navigation from an original playlist card', async () => {
    const { heading, focus, scroll, resolve } = await navigateOriginalCard('quality', 1);
    expect(document.activeElement).not.toBe(heading); expect(focus).not.toHaveBeenCalled();
    expect(scroll).toHaveBeenCalledExactlyOnceWith({ block: 'start' });
    await act(async () => { resolve(); });
    expect(focus).not.toHaveBeenCalled(); expect(scroll).toHaveBeenCalledOnce();
    expect(vi.mocked(postClassificationDraft)).not.toHaveBeenCalled();
  });

  it('keeps the keyboard card destination when its read fails without focusing or scrolling again', async () => {
    const { heading, focus, scroll, reject } = await navigateOriginalCard();
    expect(document.activeElement).toBe(heading);
    await act(async () => { reject(); });
    expect(screen.getByRole('alert').textContent).toContain('重新读取未完成');
    expect(document.activeElement).toBe(heading);
    expect(focus).toHaveBeenCalledOnce(); expect(scroll).toHaveBeenCalledOnce();
    expect(vi.mocked(postClassificationDraft)).not.toHaveBeenCalled();
  });

  it.each(['search', 'tab', 'inactive'] as const)('does not move back to card results after later user %s while the read is pending', async (movement) => {
    const { view, heading, focus, scroll, resolve } = await navigateOriginalCard();
    expect(document.activeElement).toBe(heading);
    if (movement === 'search') {
      const search = screen.getByRole('textbox', { name: '搜索歌曲或歌手' }); search.focus();
      fireEvent.change(search, { target: { value: '继续输入' } });
    } else if (movement === 'tab') {
      const tab = screen.getByRole('tab', { name: '分类歌单' }); tab.focus(); fireEvent.click(tab);
    } else {
      view.rerender(<ClassificationPanel active={false} />);
    }
    const afterMovement = document.activeElement;
    await act(async () => { resolve(); });
    expect(document.activeElement).toBe(afterMovement);
    expect(focus).toHaveBeenCalledOnce(); expect(scroll).toHaveBeenCalledOnce();
    expect(vi.mocked(postClassificationDraft)).not.toHaveBeenCalled();
  });

  it.each([true, false])('opens the complete original playlist after draft moves (allMoved=%s)', async (allMoved) => {
    const draft = { styles: ['流行抒情'], scenes: ['放松睡前'], language: '国语', reason: '已核对的草稿', recording_note: '' };
    const source = allMoved ? page(1, draft) : twoSongPage(1, draft);
    source.playlists[0].count = source.records.length;
    source.quality!.draft_summary = { pending_count: 0, unknown_style_count: 0, unknown_language_count: 0 };
    vi.mocked(fetchClassification).mockImplementation(async (_session, request) => {
      const records = source.records.filter((row) => !request.tag ||
        (request.basis === 'draft' && row.draft ? row.draft.scenes : row.scenes).includes(request.tag));
      return { ...source, filters: request,
        options: { ...source.options, scene: request.basis === 'draft'
          ? allMoved ? ['放松睡前'] : ['通勤散步', '放松睡前'] : ['通勤散步'] },
        records, pagination: { offset: 0, limit: 50, total: records.length, next_offset: null } };
    });
    await act(async () => { render(<ClassificationPanel />); });
    fireEvent.change(screen.getByLabelText('分类依据'), { target: { value: 'draft' } });
    await screen.findByRole('button', { name: '场景 · 放松睡前' });
    fireEvent.click(screen.getByRole('button', { name: '编辑本地修正' }));
    fireEvent.change(editor().getByLabelText('修正理由'), { target: { value: '导航前的未保存依据' } });
    fireEvent.click(screen.getByRole('tab', { name: '分类歌单' }));
    const card = screen.getByRole('heading', { name: '场景 · 通勤散步' }).closest('article')!;
    const opener = within(card).getByRole('button', { name: '查看分类曲目' });
    expect(opener).toHaveProperty('disabled', false);
    fireEvent.click(opener);
    await waitFor(() => expect(screen.getByLabelText('分类依据')).toHaveProperty('value', 'original'));
    expect(vi.mocked(fetchClassification).mock.lastCall?.[1]).toEqual({
      dimension: 'scene', tag: '通勤散步', review: 'all', query: '', offset: 0, basis: 'original',
    });
    const tracks = within(screen.getByRole('region', { name: '逐曲分类结果' }));
    expect(await tracks.findByRole('heading', { name: '版本有疑问的歌' })).toBeTruthy();
    expect(tracks.getAllByRole('article')).toHaveLength(allMoved ? 1 : 2);
    expect(editor().getByLabelText('修正理由')).toHaveProperty('value', '导航前的未保存依据');
    expect(vi.mocked(postClassificationDraft)).not.toHaveBeenCalled();
  });

  it('keeps track label shortcuts on the draft basis with search and review intersections', async () => {
    await open();
    fireEvent.change(screen.getByLabelText('分类依据'), { target: { value: 'draft' } });
    await screen.findByText('修正后待辨识');
    fireEvent.change(screen.getByLabelText('复核筛选'), { target: { value: 'version' } });
    fireEvent.change(screen.getByLabelText('搜索歌曲或歌手'), { target: { value: '版本' } });
    await waitFor(() => expect(vi.mocked(fetchClassification).mock.lastCall?.[1].query).toBe('版本'));
    fireEvent.click(await screen.findByRole('button', { name: '场景 · 通勤散步' }));
    await waitFor(() => expect(vi.mocked(fetchClassification).mock.lastCall?.[1]).toMatchObject({
      basis: 'draft', dimension: 'scene', tag: '通勤散步', review: 'version', query: '版本', offset: 0,
    }));
  });

  it('clears search composition, review and later pagination when opening an original playlist', async () => {
    const source = page();
    source.records = Array.from({ length: 51 }, (_, index) => ({ ...source.records[0],
      position: index + 1, record_key: (index + 1).toString(16).padStart(32, '0'), name: `Version Live ${index + 1}` }));
    source.summary!.source_count = source.summary!.covered_count = source.playlists[0].count = 51;
    vi.mocked(fetchClassification).mockImplementation(async (_session, request) => {
      const records = source.records.filter((row) => row.name.includes(request.query));
      return { ...source, filters: request, records: records.slice(request.offset, request.offset + 50),
        pagination: { offset: request.offset, limit: 50, total: records.length,
          next_offset: request.offset + 50 < records.length ? request.offset + 50 : null } };
    });
    await act(async () => { render(<ClassificationPanel />); });
    fireEvent.change(screen.getByLabelText('分类依据'), { target: { value: 'draft' } });
    await screen.findByText('修正后待辨识');
    fireEvent.change(screen.getByLabelText('复核筛选'), { target: { value: 'version' } });
    const search = screen.getByLabelText('搜索歌曲或歌手');
    fireEvent.change(search, { target: { value: 'Version' } });
    await waitFor(() => expect(vi.mocked(fetchClassification).mock.lastCall?.[1].query).toBe('Version'));
    fireEvent.click(screen.getByRole('button', { name: '下一页' }));
    await screen.findByRole('heading', { name: 'Version Live 51' });
    fireEvent.compositionStart(search);
    fireEvent.change(search, { target: { value: '未确认的候选词' } });
    fireEvent.click(screen.getByRole('tab', { name: '分类歌单' }));
    fireEvent.click(screen.getByRole('button', { name: '查看分类曲目' }));
    await screen.findByRole('heading', { name: 'Version Live 1' });
    expect(screen.getByLabelText('搜索歌曲或歌手')).toHaveProperty('value', '');
    expect(vi.mocked(fetchClassification).mock.lastCall?.[1]).toEqual({
      dimension: 'scene', tag: '通勤散步', review: 'all', query: '', offset: 0, basis: 'original',
    });
    expect(screen.getByRole('region', { name: '逐曲分类结果' }).querySelectorAll('article')).toHaveLength(50);
  });

  it('does not open custom or mismatched canonical playlist names on quality pages', async () => {
    const data = page();
    data.playlists = [
      { name: '场景 · 通勤散步（自定义）', dimension: 'scene', count: 1, key: null },
      { name: '风格 · 通勤散步', dimension: 'style', count: 1, key: null },
      { name: '通勤散步', dimension: 'scene', count: 1, key: null },
    ];
    await open(data);
    fireEvent.click(screen.getByRole('tab', { name: '分类歌单' }));
    for (const button of screen.getAllByRole('button', { name: '查看分类曲目' }))
      expect(button).toHaveProperty('disabled', true);
  });

  it.each([
    ['save', true], ['save', false], ['remove', true], ['remove', false],
  ] as const)('preserves another editor and evidence disclosure after own %s (open=%s)', async (action, evidenceOpen) => {
    const draft = { styles: ['流行抒情'], scenes: ['通勤散步'], language: '国语', reason: '第一首已核对', recording_note: '' };
    await open(twoSongPage(action === 'remove' ? 1 : 0, action === 'remove' ? draft : null));
    const rows = within(screen.getByRole('region', { name: '逐曲分类结果' })).getAllByRole('article');
    const firstEvidence = rows[0].querySelector('details')!;
    const secondEvidence = rows[1].querySelector('details')!;
    firstEvidence.open = true;
    secondEvidence.open = true;
    if (!evidenceOpen) secondEvidence.open = false;
    fireEvent.click(within(rows[0]).getByRole('button', { name: action === 'remove' ? '编辑本地修正' : '修正分类' }));
    fireEvent.click(within(rows[1]).getByRole('button', { name: '修正分类' }));
    const second = within(screen.getByRole('group', { name: '修正另一首待核对的歌' }));
    fireEvent.change(second.getByLabelText('修正理由'), { target: { value: '第二首尚未保存\n继续核对' } });
    fireEvent.change(second.getByLabelText('录音版本依据'), { target: { value: '专辑版\n独立保留' } });
    fireEvent.click(second.getByLabelText('学习专注'));
    if (action === 'save') fireEvent.change(editor().getByLabelText('修正理由'), { target: { value: draft.reason } });
    const revision = action === 'remove' ? 2 : 1;
    vi.mocked(postClassificationDraft).mockResolvedValue({ accepted: true, message: '已保存', revision, changed_count: action === 'remove' ? 0 : 1 });
    vi.mocked(fetchClassification).mockImplementation(async (_session, request) => ({
      ...twoSongPage(revision, action === 'remove' ? null : draft), filters: request,
    }));
    fireEvent.click(within(rows[0]).getByRole('button', { name: action === 'remove' ? '撤销本地修正' : '保存本地修正' }));
    await waitFor(() => expect(vi.mocked(fetchClassification)).toHaveBeenCalledTimes(2));
    await waitFor(() => expect(screen.getByRole('region', { name: '逐曲分类结果' }).getAttribute('aria-busy')).toBe('false'));
    const restored = within(screen.getByRole('group', { name: '修正另一首待核对的歌' }));
    expect(firstEvidence.isConnected).toBe(true);
    expect(firstEvidence.open).toBe(true);
    expect(secondEvidence.isConnected).toBe(true);
    expect(secondEvidence.open).toBe(evidenceOpen);
    expect(restored.getByLabelText('修正理由')).toHaveProperty('value', '第二首尚未保存\n继续核对');
    expect(restored.getByLabelText('录音版本依据')).toHaveProperty('value', '专辑版\n独立保留');
    expect(restored.getByLabelText('学习专注')).toHaveProperty('checked', true);
    vi.mocked(postClassificationDraft).mockRejectedValueOnce(new Error('第二首保存探针'));
    fireEvent.click(restored.getByRole('button', { name: '保存本地修正' }));
    await screen.findByRole('alert');
    expect(vi.mocked(postClassificationDraft)).toHaveBeenCalledTimes(2);
    expect(vi.mocked(postClassificationDraft).mock.lastCall?.[1]).toMatchObject({ revision, record_key: 'c'.repeat(32), reason: '第二首尚未保存\n继续核对' });
  });

  it.each([true, false])('restores a hidden editor when a filtered GET finishes before the POST settles (accepted=%s)', async (accepted) => {
    await editBothSongs();
    let settle!: (value: Awaited<ReturnType<typeof postClassificationDraft>>) => void;
    let reject!: (error: Error) => void;
    vi.mocked(postClassificationDraft).mockImplementation(() => new Promise((resolve, fail) => { settle = resolve; reject = fail; }));
    const draft = { styles: ['流行抒情'], scenes: ['通勤散步'], language: '国语', reason: '第一首核对依据', recording_note: '' };
    vi.mocked(fetchClassification).mockImplementation(async (_session, request) => {
      const result = twoSongPage(accepted ? 1 : 0, accepted ? draft : null);
      if (request.review === 'conflict') { result.records = [result.records[0]]; result.pagination.total = 1; }
      return { ...result, filters: request };
    });
    fireEvent.click(editor().getByRole('button', { name: '保存本地修正' }));
    fireEvent.change(screen.getByLabelText('复核筛选'), { target: { value: 'conflict' } });
    await waitFor(() => expect(vi.mocked(fetchClassification)).toHaveBeenCalledTimes(2));
    await act(async () => {
      if (accepted) settle({ accepted: true, message: '已保存', revision: 1, changed_count: 1 });
      else reject(new Error('模拟保存失败'));
    });
    await waitFor(() => expect(vi.mocked(fetchClassification)).toHaveBeenCalledTimes(3));
    await waitFor(() => expect(screen.getByRole('region', { name: '逐曲分类结果' }).getAttribute('aria-busy')).toBe('false'));
    fireEvent.click(screen.getByRole('button', { name: '清除全部筛选' }));
    const second = within(await screen.findByRole('group', { name: '修正另一首待核对的歌' }));
    expect(second.getByLabelText('修正理由')).toHaveProperty('value', '第二首未保存依据');
    if (!accepted) expect(editor().getByLabelText('修正理由')).toHaveProperty('value', '第一首核对依据');
    expect(vi.mocked(postClassificationDraft)).toHaveBeenCalledTimes(1);
  });

  it.each([true, false])('keeps other edits when an unavailable GET precedes a save acknowledgement (accepted=%s)', async (accepted) => {
    await editBothSongs();
    let accept!: (value: Awaited<ReturnType<typeof postClassificationDraft>>) => void;
    let reject!: (error: Error) => void;
    vi.mocked(postClassificationDraft).mockImplementation(() => new Promise((done, fail) => { accept = done; reject = fail; }));
    vi.mocked(fetchClassification).mockRejectedValueOnce(new ClassificationReadError('generation_incomplete'));
    const draft = { styles: ['流行抒情'], scenes: ['通勤散步'], language: '国语', reason: '第一首核对依据', recording_note: '' };
    vi.mocked(fetchClassification).mockImplementation(async (_session, request) => ({
      ...twoSongPage(accepted ? 1 : 0, accepted ? draft : null), filters: request,
    }));
    fireEvent.click(editor().getByRole('button', { name: '保存本地修正' }));
    fireEvent.change(screen.getByLabelText('复核筛选'), { target: { value: 'conflict' } });
    await screen.findByRole('link', { name: '查看恢复说明' });
    expect(screen.queryByText('资料版本或草稿状态已变化，旧的未保存编辑已清除，请重新核对。')).toBeNull();
    await act(async () => {
      if (accepted) accept({ accepted: true, message: '已保存', revision: 1, changed_count: 1 });
      else reject(new Error('模拟保存失败'));
    });
    if (!accepted) {
      expect(vi.mocked(fetchClassification)).toHaveBeenCalledTimes(2);
      fireEvent.click(screen.getByRole('button', { name: '重试读取分类成果' }));
    }
    await waitFor(() => expect(vi.mocked(fetchClassification)).toHaveBeenCalledTimes(3));
    await waitFor(() => expect(screen.getByRole('region', { name: '逐曲分类结果' }).getAttribute('aria-busy')).toBe('false'));
    fireEvent.click(screen.getByRole('button', { name: '清除全部筛选' }));
    const second = within(await screen.findByRole('group', { name: '修正另一首待核对的歌' }));
    expect(second.getByLabelText('修正理由')).toHaveProperty('value', '第二首未保存依据');
    if (!accepted) expect(editor().getByLabelText('修正理由')).toHaveProperty('value', '第一首核对依据');
    expect(vi.mocked(postClassificationDraft)).toHaveBeenCalledTimes(1);
  });

  it.each(['source', 'revision', 'unavailable'] as const)('does not carry other edits across %s changes after an own save', async (change) => {
    await editBothSongs();
    const evidence = screen.getAllByText('查看分类依据').map((summary) => summary.closest('details')!);
    evidence.forEach((detail) => { detail.open = true; });
    const updated = twoSongPage(1);
    if (change === 'source') updated.quality!.source_version = 'd'.repeat(64);
    if (change === 'revision') updated.quality!.revision = 2;
    if (change === 'unavailable') updated.quality!.draft_status = 'unavailable';
    vi.mocked(postClassificationDraft).mockResolvedValue({ accepted: true, message: '已保存', revision: 1, changed_count: 1 });
    vi.mocked(fetchClassification).mockImplementation(async (_session, request) => ({ ...updated, filters: request }));
    fireEvent.click(editor().getByRole('button', { name: '保存本地修正' }));
    await waitFor(() => expect(vi.mocked(fetchClassification)).toHaveBeenCalledTimes(2));
    await waitFor(() => expect(screen.queryByRole('group', { name: '修正另一首待核对的歌' })).toBeNull());
    expect(evidence.every((detail) => !detail.isConnected)).toBe(true);
    expect(screen.getAllByText('查看分类依据').every((summary) => !summary.closest('details')!.open)).toBe(true);
    vi.mocked(fetchClassification).mockImplementation(async (_session, request) => ({ ...twoSongPage(1), filters: request }));
    fireEvent.click(screen.getByRole('button', { name: '刷新分类记录' }));
    await waitFor(() => expect(screen.getByRole('region', { name: '逐曲分类结果' }).getAttribute('aria-busy')).toBe('false'));
    const rows = within(screen.getByRole('region', { name: '逐曲分类结果' })).getAllByRole('article');
    fireEvent.click(within(rows[1]).getByRole('button', { name: '修正分类' }));
    expect(within(screen.getByRole('group', { name: '修正另一首待核对的歌' })).getByLabelText('修正理由')).toHaveProperty('value', '');
  });

  it('keeps a failed save visible after filtering unmounts its editor', async () => {
    await editBothSongs();
    let reject!: (error: Error) => void;
    vi.mocked(postClassificationDraft).mockImplementation(() => new Promise((_resolve, fail) => { reject = fail; }));
    vi.mocked(fetchClassification).mockImplementation(async (_session, request) => ({ ...twoSongPage(), filters: request,
      records: [], pagination: { offset: 0, limit: 50, total: 0, next_offset: null } }));
    fireEvent.click(editor().getByRole('button', { name: '保存本地修正' }));
    fireEvent.change(screen.getByLabelText('复核筛选'), { target: { value: 'pending' } });
    await waitFor(() => expect(vi.mocked(fetchClassification)).toHaveBeenCalledTimes(2));
    await act(async () => { reject(new Error('模拟响应丢失，请重新读取核对。')); });
    expect(await screen.findByText('模拟响应丢失，请重新读取核对。')).toBeTruthy();
    expect(vi.mocked(postClassificationDraft)).toHaveBeenCalledTimes(1);
  });

  it('clears visible edits when an accepted save is followed by its old revision', async () => {
    await editBothSongs();
    vi.mocked(postClassificationDraft).mockResolvedValue({ accepted: true, message: '已保存', revision: 1, changed_count: 1 });
    vi.mocked(fetchClassification).mockImplementation(async (_session, request) => ({ ...twoSongPage(), filters: request }));
    fireEvent.click(editor().getByRole('button', { name: '保存本地修正' }));
    await waitFor(() => expect(vi.mocked(fetchClassification)).toHaveBeenCalledTimes(2));
    await waitFor(() => expect(screen.getByRole('region', { name: '逐曲分类结果' }).getAttribute('aria-busy')).toBe('false'));
    expect(screen.queryByRole('group', { name: '修正另一首待核对的歌' })).toBeNull();
    const rows = within(screen.getByRole('region', { name: '逐曲分类结果' })).getAllByRole('article');
    fireEvent.click(within(rows[1]).getByRole('button', { name: '修正分类' }));
    expect(within(screen.getByRole('group', { name: '修正另一首待核对的歌' })).getByLabelText('修正理由')).toHaveProperty('value', '');
    expect(vi.mocked(postClassificationDraft)).toHaveBeenCalledTimes(1);
  });

  it('retains an unknown save failure while the recovery read clears old edits', async () => {
    await editBothSongs();
    let reject!: (error: Error) => void;
    vi.mocked(postClassificationDraft).mockImplementation(() => new Promise((_resolve, fail) => { reject = fail; }));
    vi.mocked(fetchClassification).mockImplementation(async (_session, request) => ({ ...twoSongPage(), filters: request,
      records: [], pagination: { offset: 0, limit: 50, total: 0, next_offset: null } }));
    fireEvent.click(editor().getByRole('button', { name: '保存本地修正' }));
    fireEvent.change(screen.getByLabelText('复核筛选'), { target: { value: 'pending' } });
    await waitFor(() => expect(vi.mocked(fetchClassification)).toHaveBeenCalledTimes(2));
    await waitFor(() => expect(screen.getByRole('region', { name: '逐曲分类结果' }).getAttribute('aria-busy')).toBe('false'));
    vi.mocked(fetchClassification).mockImplementation(async (_session, request) => ({ ...twoSongPage(1), filters: request,
      records: [], pagination: { offset: 0, limit: 50, total: 0, next_offset: null } }));
    const failure = Object.assign(new Error('保存状态无法确认，请勿再次提交。'), { acceptanceUnknown: true });
    await act(async () => { reject(failure); });
    await waitFor(() => expect(vi.mocked(fetchClassification)).toHaveBeenCalledTimes(3));
    await waitFor(() => expect(screen.getByRole('region', { name: '逐曲分类结果' }).getAttribute('aria-busy')).toBe('false'));
    expect(screen.getByRole('alert')).toHaveProperty('textContent', failure.message);
    expect(vi.mocked(postClassificationDraft)).toHaveBeenCalledTimes(1);
    fireEvent.change(screen.getByLabelText('复核筛选'), { target: { value: 'all' } });
    expect(screen.getByRole('alert')).toHaveProperty('textContent', failure.message);
  });

  it('retains unsaved labels and multiline evidence when the editor is collapsed and reopened', async () => {
    await open(); await edit();
    fireEvent.click(editor().getByLabelText('学习专注'));
    fireEvent.change(editor().getByLabelText('修正理由'), { target: { value: '第一段依据\n第二段依据' } });
    fireEvent.change(editor().getByLabelText('录音版本依据'), { target: { value: '专辑版\n核对了人声' } });
    fireEvent.click(screen.getByRole('button', { name: '收起修正' }));
    await edit();
    expect(editor().getByLabelText('修正理由')).toHaveProperty('value', '第一段依据\n第二段依据');
    expect(editor().getByLabelText('录音版本依据')).toHaveProperty('value', '专辑版\n核对了人声');
    expect(editor().getByLabelText('学习专注')).toHaveProperty('checked', true);
    expect(vi.mocked(postClassificationDraft)).not.toHaveBeenCalled();
  });

  it('restores only the matching unsaved editor after filtering the song out and returning', async () => {
    await open(); await edit();
    fireEvent.change(editor().getByLabelText('修正理由'), { target: { value: '筛选返回后仍保留' } });
    vi.mocked(fetchClassification).mockImplementation(async (_session, request) => ({ ...page(), filters: request,
      ...(request.review === 'pending' ? { records: [], pagination: { offset: 0, limit: 50, total: 0, next_offset: null } } : {}) }));
    fireEvent.change(screen.getByLabelText('复核筛选'), { target: { value: 'pending' } });
    await screen.findByText('没有匹配的曲目');
    fireEvent.click(screen.getByRole('button', { name: '清除全部筛选' }));
    await screen.findByRole('group', { name: '修正版本有疑问的歌' });
    expect(editor().getByLabelText('修正理由')).toHaveProperty('value', '筛选返回后仍保留');
    expect(vi.mocked(postClassificationDraft)).not.toHaveBeenCalled();
  });

  it('discards only unsaved changes back to the saved correction without posting', async () => {
    const draft = { styles: ['摇滚与独立'], scenes: [], language: '英语', reason: '已保存的依据', recording_note: '专辑版' };
    await open(page(1, draft));
    fireEvent.click(screen.getByRole('button', { name: '编辑本地修正' }));
    fireEvent.change(editor().getByLabelText('修正理由'), { target: { value: '尚未保存的更改' } });
    fireEvent.click(editor().getByLabelText('学习专注'));
    fireEvent.click(editor().getByRole('button', { name: '放弃未保存修改' }));
    expect(editor().getByLabelText('修正理由')).toHaveProperty('value', '已保存的依据');
    expect(editor().getByLabelText('学习专注')).toHaveProperty('checked', false);
    expect(screen.getByText('本地修正 · 1 首')).toBeTruthy();
    expect(vi.mocked(postClassificationDraft)).not.toHaveBeenCalled();
  });

  it('does not revive an old cached editor after a revision changes and an older revision is seen again', async () => {
    await open(); await edit();
    fireEvent.change(editor().getByLabelText('修正理由'), { target: { value: '旧修订的未保存依据' } });
    vi.mocked(fetchClassification).mockImplementation(async (_session, request) => ({ ...page(1), filters: request }));
    fireEvent.click(screen.getByRole('button', { name: '刷新分类记录' }));
    await waitFor(() => expect(screen.queryByRole('group', { name: '修正版本有疑问的歌' })).toBeNull());
    vi.mocked(fetchClassification).mockImplementation(async (_session, request) => ({ ...page(), filters: request }));
    fireEvent.click(screen.getByRole('button', { name: '刷新分类记录' }));
    await waitFor(() => expect(screen.getByRole('region', { name: '逐曲分类结果' }).getAttribute('aria-busy')).toBe('false'));
    await edit();
    expect(editor().getByLabelText('修正理由')).toHaveProperty('value', '');
    expect(vi.mocked(postClassificationDraft)).not.toHaveBeenCalled();
  });

  it('clears both the visible and cached editor if draft availability changes without a new revision', async () => {
    await open(); await edit();
    fireEvent.change(editor().getByLabelText('修正理由'), { target: { value: '损坏草稿之前的输入' } });
    vi.mocked(fetchClassification).mockImplementation(async (_session, request) => ({ ...page(0, null, 'unavailable'), filters: request }));
    fireEvent.click(screen.getByRole('button', { name: '刷新分类记录' }));
    await screen.findByText(/本地修正草稿暂不可用/);
    expect(screen.queryByRole('group', { name: '修正版本有疑问的歌' })).toBeNull();
    vi.mocked(fetchClassification).mockImplementation(async (_session, request) => ({ ...page(), filters: request }));
    fireEvent.click(screen.getByRole('button', { name: '刷新分类记录' }));
    await waitFor(() => expect(screen.getByRole('region', { name: '逐曲分类结果' }).getAttribute('aria-busy')).toBe('false'));
    await edit();
    expect(editor().getByLabelText('修正理由')).toHaveProperty('value', '');
    expect(vi.mocked(postClassificationDraft)).not.toHaveBeenCalled();
  });

  it('preserves the selected classification basis when clearing song filters', async () => {
    await open();
    fireEvent.change(screen.getByLabelText('分类依据'), { target: { value: 'draft' } });
    await screen.findByText('修正后待辨识');
    fireEvent.change(screen.getByLabelText('复核筛选'), { target: { value: 'conflict' } });
    await waitFor(() => expect(screen.getByRole('region', { name: '逐曲分类结果' }).getAttribute('aria-busy')).toBe('false'));
    fireEvent.click(screen.getByRole('button', { name: '清除全部筛选' }));
    await waitFor(() => expect(vi.mocked(fetchClassification).mock.lastCall?.[1]).toMatchObject({ basis: 'draft', review: 'all', query: '', tag: '' }));
    expect(screen.getByLabelText('分类依据')).toHaveProperty('value', 'draft');
  });

  it('focuses the multiline reason when opening and saves it once with Control Enter', async () => {
    await open(); await edit();
    const reason = editor().getByLabelText('修正理由');
    expect(reason.tagName).toBe('TEXTAREA');
    expect(document.activeElement).toBe(reason);
    fireEvent.change(reason, { target: { value: '录音依据第一行\n第二行' } });
    vi.mocked(postClassificationDraft).mockRejectedValue(new Error('模拟保存失败'));
    fireEvent.keyDown(reason, { key: 'Enter', ctrlKey: true });
    await screen.findByRole('alert');
    expect(vi.mocked(postClassificationDraft)).toHaveBeenCalledTimes(1);
    expect(vi.mocked(postClassificationDraft).mock.lastCall?.[1]).toMatchObject({ reason: '录音依据第一行\n第二行' });
    expect(reason).toHaveProperty('value', '录音依据第一行\n第二行');
  });

  it('does not submit Control Enter while the input method is composing', async () => {
    await open(); await edit();
    const reason = editor().getByLabelText('修正理由');
    fireEvent.change(reason, { target: { value: '输入法尚未结束的依据' } });
    fireEvent.keyDown(reason, { key: 'Enter', ctrlKey: true, isComposing: true });
    expect(vi.mocked(postClassificationDraft)).not.toHaveBeenCalled();
    expect(reason).toHaveProperty('value', '输入法尚未结束的依据');
  });

  it.each(['reason', 'recording', 'save'] as const)('restores the %s control after native disabling blurs a failed save', async (control) => {
    await open(); await edit();
    const reason = editor().getByLabelText('修正理由');
    const recording = editor().getByLabelText('录音版本依据');
    fireEvent.change(reason, { target: { value: '保存失败后继续核对\n保留原输入' } });
    fireEvent.change(recording, { target: { value: '专辑录音\n尚待复听' } });
    let reject!: (error: Error) => void;
    vi.mocked(postClassificationDraft).mockImplementation(() => new Promise((_resolve, fail) => { reject = fail; }));
    const source = control === 'reason' ? reason : control === 'recording' ? recording
      : editor().getByRole('button', { name: '保存本地修正' });
    source.focus();
    if (control === 'save') fireEvent.click(source);
    else fireEvent.keyDown(source, { key: 'Enter', ctrlKey: true });
    // Edge blurs disabled controls; jsdom needs that native transition supplied.
    nativeDisabledBlur(source);
    expect(document.activeElement).toBe(document.body);
    await act(async () => { reject(new Error('本地草稿暂时不可写')); });
    await waitFor(() => expect(document.activeElement).toBe(source));
    expect(reason).toHaveProperty('value', '保存失败后继续核对\n保留原输入');
    expect(recording).toHaveProperty('value', '专辑录音\n尚待复听');
    expect(vi.mocked(postClassificationDraft)).toHaveBeenCalledTimes(1);
  });

  it.each(['focus', 'wheel', 'PageDown'] as const)('does not restore failed-save focus after the user continues with %s', async (move) => {
    await open(); await edit();
    const reason = editor().getByLabelText('修正理由');
    fireEvent.change(reason, { target: { value: '保存时继续浏览其他内容' } });
    let reject!: (error: Error) => void;
    vi.mocked(postClassificationDraft).mockImplementation(() => new Promise((_resolve, fail) => { reject = fail; }));
    reason.focus(); fireEvent.keyDown(reason, { key: 'Enter', ctrlKey: true }); nativeDisabledBlur(reason);
    const search = screen.getByLabelText('搜索歌曲或歌手');
    if (move === 'focus') search.focus();
    else if (move === 'wheel') fireEvent.wheel(document);
    else fireEvent.keyDown(document, { key: 'PageDown' });
    await act(async () => { reject(new Error('本地草稿暂时不可写')); });
    expect(await screen.findByRole('alert')).toHaveProperty('textContent', '本地草稿暂时不可写');
    expect(document.activeElement).toBe(move === 'focus' ? search : document.body);
    expect(reason).toHaveProperty('value', '保存时继续浏览其他内容');
    expect(vi.mocked(postClassificationDraft)).toHaveBeenCalledTimes(1);
  });

  it('returns keyboard focus to the same song after saving and accepting a refreshed revision', async () => {
    await open(); await edit();
    fireEvent.change(editor().getByLabelText('修正理由'), { target: { value: '已完成版本核对' } });
    const draft = { styles: ['流行抒情'], scenes: ['通勤散步'], language: '国语', reason: '已完成版本核对', recording_note: '' };
    vi.mocked(postClassificationDraft).mockResolvedValue({ accepted: true, message: '已保存', revision: 1, changed_count: 1 });
    vi.mocked(fetchClassification).mockImplementation(async (_session, request) => ({ ...page(1, draft), filters: request }));
    const save = editor().getByRole('button', { name: '保存本地修正' }); save.focus(); fireEvent.click(save);
    const reopen = await screen.findByRole('button', { name: '编辑本地修正' });
    await waitFor(() => expect(document.activeElement).toBe(reopen));
  });

  it.each(['stay', 'focus', 'wheel'] as const)('recovers a shrinking review queue after a last-page save without losing user intent (%s)', async (movement) => {
    let accepted = false;
    let finishFilter!: () => void;
    let filterHeld = false;
    vi.mocked(fetchClassification).mockImplementation(async (_session, request) => {
      const result = page(accepted ? 1 : 0);
      result.filters = request;
      if (request.offset === 0) result.records[0] = { ...result.records[0],
        record_key: 'c'.repeat(32), name: '队列首页的另一首歌' };
      result.pagination = { offset: request.offset, limit: 50, total: accepted ? 50 : 51,
        next_offset: !accepted && request.offset === 0 ? 50 : null };
      // Only the last page's one row disappears after saving its draft.
      if (accepted && request.offset === 50) result.records = [];
      if (!filterHeld && request.review === 'undrafted_review' && request.offset === 0) {
        filterHeld = true;
        return await new Promise<ClassificationPage>((resolve) => { finishFilter = () => resolve(result); });
      }
      return result;
    });
    await act(async () => { render(<ClassificationPanel />); });
    fireEvent.click(screen.getByRole('button', { name: '查看需复核且无草稿' }));
    await waitFor(() => expect(vi.mocked(fetchClassification).mock.lastCall?.[1].review).toBe('undrafted_review'));
    // A started request does not mean the selected queue has committed to the DOM.
    expect(screen.getByRole('region', { name: '逐曲分类结果' }).getAttribute('aria-busy')).toBe('true');
    expect(screen.queryByRole('button', { name: '下一页' })).toBeNull();
    await act(async () => { finishFilter(); });
    await waitFor(() => {
      expect(screen.getByRole('region', { name: '逐曲分类结果' }).getAttribute('aria-busy')).toBe('false');
      expect(screen.getByRole('button', { name: '下一页' })).toHaveProperty('disabled', false);
    });
    fireEvent.click(screen.getByRole('button', { name: '下一页' }));
    await screen.findByText('第 2 页，共 2 页');
    await edit();
    const reason = editor().getByLabelText('修正理由');
    fireEvent.change(reason, { target: { value: '最后一首的复核依据' } });
    let accept!: (value: Awaited<ReturnType<typeof postClassificationDraft>>) => void;
    vi.mocked(postClassificationDraft).mockImplementation(() => new Promise((resolve) => { accept = resolve; }));
    reason.focus();
    fireEvent.keyDown(reason, { key: 'Enter', ctrlKey: true });
    nativeDisabledBlur(reason);
    const search = screen.getByLabelText('搜索歌曲或歌手');
    if (movement === 'focus') search.focus();
    else if (movement === 'wheel') fireEvent.wheel(document.body);
    await act(async () => {
      accepted = true;
      accept({ accepted: true, message: '已保存', revision: 1, changed_count: 1 });
    });
    await screen.findByText('记录数量已变化，已返回第 1 页。');
    await screen.findByText('第 1 页，共 1 页');
    await waitFor(() => expect(screen.getByRole('region', { name: '逐曲分类结果' }).getAttribute('aria-busy')).toBe('false'));
    expect(document.activeElement).toBe(movement === 'stay'
      ? screen.getByRole('heading', { name: '逐曲分类结果' }) : movement === 'focus' ? search : document.body);
    expect(vi.mocked(fetchClassification).mock.calls.slice(-2).map((call) => call[1].offset)).toEqual([50, 0]);
    expect(vi.mocked(postClassificationDraft)).toHaveBeenCalledTimes(1);
  });

  it('does not reclaim focus if the user moves away while a correction is saving', async () => {
    await open(); await edit();
    fireEvent.change(editor().getByLabelText('修正理由'), { target: { value: '较晚返回的保存' } });
    let accept!: (value: Awaited<ReturnType<typeof postClassificationDraft>>) => void;
    vi.mocked(postClassificationDraft).mockImplementation(() => new Promise((resolve) => { accept = resolve; }));
    const draft = { styles: ['流行抒情'], scenes: ['通勤散步'], language: '国语', reason: '较晚返回的保存', recording_note: '' };
    vi.mocked(fetchClassification).mockImplementation(async (_session, request) => ({ ...page(1, draft), filters: request }));
    const save = editor().getByRole('button', { name: '保存本地修正' }); save.focus(); fireEvent.click(save);
    const search = screen.getByLabelText('搜索歌曲或歌手'); search.focus();
    await act(async () => { accept({ accepted: true, message: '已保存', revision: 1, changed_count: 1 }); });
    await screen.findByRole('button', { name: '编辑本地修正' });
    expect(document.activeElement).toBe(search);
  });

  it('does not reclaim focus after keyboard scrolling while a save is pending', async () => {
    await open(); await edit();
    fireEvent.change(editor().getByLabelText('修正理由'), { target: { value: '保存时继续浏览其他内容' } });
    let accept!: (value: Awaited<ReturnType<typeof postClassificationDraft>>) => void;
    vi.mocked(postClassificationDraft).mockImplementation(() => new Promise((resolve) => { accept = resolve; }));
    const draft = { styles: ['流行抒情'], scenes: ['通勤散步'], language: '国语', reason: '保存时继续浏览其他内容', recording_note: '' };
    vi.mocked(fetchClassification).mockImplementation(async (_session, request) => ({ ...page(1, draft), filters: request }));
    const save = editor().getByRole('button', { name: '保存本地修正' }); save.focus(); fireEvent.click(save);
    fireEvent.keyDown(document, { key: 'PageDown' });
    await act(async () => { accept({ accepted: true, message: '已保存', revision: 1, changed_count: 1 }); });
    const reopen = await screen.findByRole('button', { name: '编辑本地修正' });
    expect(document.activeElement).not.toBe(reopen);
  });

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
    expect(screen.getByText(/未保存的输入仅保留在当前窗口/)).toBeTruthy();
    expect(data.records[0].language).toBe('待辨识');
  });

  it('falls back to the original labels when corrections are stale and offers returning to the original basis', async () => {
    const data = page(1, { styles: ['流行抒情'], scenes: [], language: '英语', reason: '旧判断', recording_note: '' }, 'stale');
    await open(data);
    fireEvent.change(screen.getByLabelText('分类依据'), { target: { value: 'draft' } });
    await waitFor(() => expect(vi.mocked(fetchClassification).mock.lastCall?.[1].basis).toBe('draft'));
    expect(screen.getByText(/当前回退原分类/)).toBeTruthy();
    expect(await screen.findByRole('button', { name: '语言 · 国语' })).toBeTruthy();
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
    ['needs_review', '查看全部需复核'], ['undrafted_review', '查看需复核且无草稿'],
    ['pilot', '查看 1 首试点样本'], ['draft', '查看本地修正'],
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

  it('keeps an unknown save failure when a same-filter refresh replaces its still-connected editor', async () => {
    await editBothSongs();
    let reject!: (error: Error) => void;
    vi.mocked(postClassificationDraft).mockImplementation(() => new Promise((_resolve, fail) => { reject = fail; }));
    vi.mocked(fetchClassification).mockImplementation(async (_session, request) => ({ ...twoSongPage(1), filters: request }));
    fireEvent.click(editor().getByRole('button', { name: '保存本地修正' }));
    fireEvent.click(screen.getByRole('button', { name: '刷新分类记录' }));
    await waitFor(() => expect(vi.mocked(fetchClassification)).toHaveBeenCalledTimes(2));
    await waitFor(() => expect(screen.getByRole('region', { name: '逐曲分类结果' }).getAttribute('aria-busy')).toBe('false'));
    expect(screen.getByRole('group', { name: '修正版本有疑问的歌' }).isConnected).toBe(true);
    await act(async () => { reject(new Error('保存状态无法确认，请勿再次提交。')); });
    await waitFor(() => expect(vi.mocked(fetchClassification)).toHaveBeenCalledTimes(3));
    await waitFor(() => expect(screen.queryByRole('group', { name: '修正版本有疑问的歌' })).toBeNull());
    expect(screen.getByRole('alert')).toHaveProperty('textContent', '保存状态无法确认，请勿再次提交。');
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
