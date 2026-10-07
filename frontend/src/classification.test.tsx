import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';
import App from './App';
import { fetchClassification } from './api';
import { RecoveryNotice } from './RecoveryNotice';
import type { ClassificationPage, WriteRecovery } from './types';

vi.mock('./api', async (original) => ({
  ...(await original<typeof import('./api')>()),
  fetchClassification: vi.fn(),
}));

const state = {
  connection: { installed: true, configured: true, authorized: true },
  data: { source: 'empty', playlists: [], account: null, history: null, artists_completed: false },
  job: null,
};
function page(offset = 0, total = 61): ClassificationPage {
  return {
    status: 'available',
    source: 'local_record',
    verification: 'verified',
    updated_at: '2026-10-05T10:00:00+08:00',
    verified_at: '2026-10-05T11:00:00+08:00',
    summary: {
      source_count: total,
      covered_count: total,
      playlist_count: 24,
      pending_count: Math.min(3, total),
      unknown_style_count: Math.min(2, total),
      unknown_language_count: total >= 3 ? 1 : 0,
    },
    options: {
      scene: ['夜晚', '学习', '通勤', '运动', '放松', '聚会', '旅行', '独处'],
      style: ['流行', '摇滚', '民谣', '爵士', '电子', '古典', '说唱', '灵魂'],
      language: ['中文', '英语', '日语', '韩语', '粤语', '法语', '其他'],
    },
    playlists: [
      ...['夜晚', '学习', '通勤', '运动', '放松', '聚会', '旅行', '独处'].map((tag, index) => ({
        name: `场景 · ${tag}`,
        dimension: 'scene' as const,
        count: index + 1,
        key: `${10001 + index}`,
      })),
      ...['流行', '摇滚', '民谣', '爵士', '电子', '古典', '说唱', '灵魂'].map((tag, index) => ({
        name: `风格 · ${tag}`,
        dimension: 'style' as const,
        count: index + 1,
        key: `${10009 + index}`,
      })),
      ...['中文', '英语', '日语', '韩语', '粤语', '法语', '其他'].map((tag, index) => ({
        name: `语言 · ${tag}`,
        dimension: 'language' as const,
        count: index + 1,
        key: `${10017 + index}`,
      })),
      { name: '分类 · 待辨识', dimension: 'review', count: 3, key: '10024' },
    ],
    filters: { dimension: 'all', tag: '', review: 'all', query: '' },
    pagination: { offset, limit: 50, total, next_offset: offset + 50 < total ? offset + 50 : null },
    records: Array.from({ length: Math.min(50, Math.max(0, total - offset)) }, (_, index) => ({
      position: offset + index + 1,
      name: `分类歌曲 ${offset + index + 1}`,
      artists: '示例歌手',
      styles: ['流行'],
      scenes: ['夜晚'],
      language: '中文',
      pending_reasons: offset + index < 3 ? ['判断证据待补充'] : [],
      evidence_note: '依据已有曲目资料与规则匹配。',
      language_evidence_note: '歌词资料支持中文判断。',
    })),
  };
}
function prepare(value: unknown = state) {
  document.head.innerHTML = '<meta name="organizer-session" content="classification-test" />';
  const fetcher = vi.fn(async (_url: string, _init?: RequestInit) => ({
    ok: true,
    json: async () => value,
  }));
  vi.stubGlobal('fetch', fetcher);
  vi.mocked(fetchClassification).mockImplementation(async (_session, selection) => ({
    ...page(selection.offset),
    filters: {
      dimension: selection.dimension,
      tag: selection.tag,
      review: selection.review,
      query: selection.query,
    },
  }));
  return fetcher;
}
async function open(value: unknown = state) {
  window.history.replaceState(null, '', '/#classification');
  const fetcher = prepare(value);
  await act(async () => {
    render(<App />);
  });
  return fetcher;
}
async function click(name: string) {
  await act(async () => {
    fireEvent.click(screen.getByRole('button', { name }));
  });
}
async function tab(name: string) {
  await act(async () => {
    fireEvent.click(screen.getByRole('tab', { name }));
  });
}
async function navigate(name: string) {
  await act(async () => {
    fireEvent.click(
      within(screen.getByRole('navigation', { name: '主导航' })).getByRole('link', { name }),
    );
  });
}

describe('local classification results', () => {
  it('opens only bound public playlist links and never uses unvalidated IDs', async () => {
    window.history.replaceState(null, '', '/#classification');
    prepare();
    const data = page();
    data.playlists[0].key = '10001';
    data.playlists[1].key = null;
    data.playlists[2].key = 'private-playlist';
    vi.mocked(fetchClassification).mockResolvedValue(data);
    await act(async () => {
      render(<App />);
    });
    await tab('分类歌单');
    const link = screen.getByRole('link', { name: '在网易云打开场景 · 夜晚' });
    expect(link.getAttribute('href')).toBe('https://music.163.com/#/playlist?id=10001');
    expect(link.getAttribute('rel')).toBe('noopener noreferrer');
    expect(screen.queryByRole('link', { name: '在网易云打开场景 · 学习' })).toBeNull();
    expect(document.querySelector('a[href*="private-playlist"]')).toBeNull();
  });
  it('opens from overview without account actions or reading classification on ordinary startup', async () => {
    const fetcher = prepare();
    await act(async () => {
      render(<App />);
    });
    expect(vi.mocked(fetchClassification)).not.toHaveBeenCalled();
    await act(async () => {
      fireEvent.click(screen.getByRole('link', { name: /查看分类成果/ }));
    });
    expect(screen.getByRole('heading', { name: '分类成果', level: 1 })).toBeTruthy();
    expect(window.location.hash).toBe('#classification');
    expect(fetcher.mock.calls.every(([url]) => url === '/api/state')).toBe(true);
  });

  it('shows all 24 recorded playlists, historical verification and 50 songs with evidence', async () => {
    await open();
    expect(screen.getByRole('tab', { name: '歌曲分类' }).getAttribute('aria-selected')).toBe(
      'true',
    );
    expect(screen.queryByRole('region', { name: '分类歌单' })).toBeNull();
    await tab('分类歌单');
    const cards = within(screen.getByRole('region', { name: '分类歌单' }));
    expect(cards.getAllByRole('heading', { level: 3 })).toHaveLength(24);
    expect(cards.getByText('分类 · 待辨识')).toBeTruthy();
    await tab('歌曲分类');
    expect(screen.getByText(/上次账号核验/)).toBeTruthy();
    expect(screen.getByText(/不能替代完整听音判断/)).toBeTruthy();
    expect(
      within(screen.getByRole('region', { name: '逐曲分类结果' })).getAllByRole('article'),
    ).toHaveLength(50);
    const firstTrack = within(
      within(screen.getByRole('region', { name: '逐曲分类结果' })).getAllByRole('article')[0],
    );
    fireEvent.click(firstTrack.getByText('查看分类依据', { selector: 'summary' }));
    expect(firstTrack.getByText('依据已有曲目资料与规则匹配。')).toBeTruthy();
    expect(document.body.textContent).not.toContain('10001');
  });

  it('loads the next 50 positions and resets offset when dimension, tag or pending filter changes', async () => {
    await open();
    vi.mocked(fetchClassification).mockResolvedValueOnce(page(50));
    await click('下一页');
    expect(screen.getByText('分类歌曲 51')).toBeTruthy();
    expect(vi.mocked(fetchClassification).mock.lastCall?.[1].offset).toBe(50);
    await act(async () => {
      fireEvent.change(screen.getByLabelText('分类维度'), { target: { value: 'style' } });
    });
    expect(vi.mocked(fetchClassification).mock.lastCall?.[1]).toMatchObject({
      offset: 0,
      dimension: 'style',
      tag: '',
    });
    await act(async () => {
      fireEvent.change(screen.getByLabelText('分类标签'), { target: { value: '流行' } });
    });
    expect(vi.mocked(fetchClassification).mock.lastCall?.[1]).toMatchObject({
      offset: 0,
      tag: '流行',
    });
    await click('只看待辨识');
    expect(vi.mocked(fetchClassification).mock.lastCall?.[1]).toMatchObject({
      offset: 0,
      review: 'pending',
    });
  });

  it('debounces song and artist search and resets a later page to the first page', async () => {
    vi.useFakeTimers();
    await open();
    vi.mocked(fetchClassification).mockResolvedValueOnce(page(50));
    await click('下一页');
    const before = vi.mocked(fetchClassification).mock.calls.length;
    fireEvent.change(screen.getByRole('textbox', { name: '搜索歌曲或歌手' }), {
      target: { value: '歌手' },
    });
    fireEvent.change(screen.getByRole('textbox', { name: '搜索歌曲或歌手' }), {
      target: { value: '示例歌手' },
    });
    await act(async () => {
      await vi.advanceTimersByTimeAsync(199);
    });
    expect(vi.mocked(fetchClassification)).toHaveBeenCalledTimes(before);
    await act(async () => {
      await vi.advanceTimersByTimeAsync(1);
    });
    expect(vi.mocked(fetchClassification)).toHaveBeenCalledTimes(before + 1);
    expect(vi.mocked(fetchClassification).mock.lastCall?.[1]).toMatchObject({
      offset: 0,
      query: '示例歌手',
    });
  });

  it('aborts old filters and rejects stale success after a newer response', async () => {
    await open();
    let resolveOld!: (value: ClassificationPage) => void;
    const older = new Promise<ClassificationPage>((resolve) => {
      resolveOld = resolve;
    });
    vi.mocked(fetchClassification).mockReturnValueOnce(older);
    await act(async () => {
      fireEvent.change(screen.getByLabelText('分类维度'), { target: { value: 'scene' } });
    });
    const oldSignal = vi.mocked(fetchClassification).mock.lastCall?.[2];
    const latest = page(0, 1);
    latest.records[0].name = '新筛选结果';
    vi.mocked(fetchClassification).mockResolvedValueOnce(latest);
    await act(async () => {
      fireEvent.change(screen.getByLabelText('分类维度'), { target: { value: 'language' } });
    });
    expect(oldSignal?.aborted).toBe(true);
    expect(screen.getByText('新筛选结果')).toBeTruthy();
    await act(async () => {
      resolveOld(page());
    });
    expect(screen.getByText('新筛选结果')).toBeTruthy();
    expect(screen.queryByText('分类歌曲 1')).toBeNull();
  });

  it('ignores an old rejection and cancels the latest local read when leaving the page', async () => {
    await open();
    let rejectOld!: (error: Error) => void;
    vi.mocked(fetchClassification).mockReturnValueOnce(
      new Promise((_resolve, reject) => {
        rejectOld = reject;
      }),
    );
    await act(async () => {
      fireEvent.change(screen.getByLabelText('分类维度'), { target: { value: 'style' } });
    });
    await act(async () => {
      fireEvent.change(screen.getByLabelText('分类维度'), { target: { value: 'scene' } });
    });
    await act(async () => {
      rejectOld(new Error('SECRET-STALE'));
    });
    expect(screen.queryByRole('alert')).toBeNull();
    expect(screen.getByText('分类歌曲 1')).toBeTruthy();
    const signal = vi.mocked(fetchClassification).mock.lastCall?.[2];
    await act(async () => {
      fireEvent.click(
        within(screen.getByRole('navigation', { name: '主导航' })).getByRole('link', {
          name: '概览',
        }),
      );
    });
    expect(signal?.aborted).toBe(true);
  });

  it('keeps an active task and pause entry visible while browsing classification records', async () => {
    const fetcher = await open({
      ...state,
      job: {
        id: 'classification-page-existing-task',
        action: 'rename',
        label: '整理名称',
        status: 'running',
        progress: { label: '正在核对账号歌单', completed_count: 1, total_count: 2 },
        logs: [],
      },
    });
    const task = within(screen.getByRole('region', { name: '当前任务' }));
    expect(task.getByText('正在核对账号歌单')).toBeTruthy();
    expect(task.getByRole('button', { name: '暂停任务' })).toBeTruthy();
    expect(fetcher.mock.calls.every(([url]) => url === '/api/state')).toBe(true);
  });

  it.each(['not_loaded', 'unavailable'] as const)(
    'offers a local retry for %s instead of an account action',
    async (status) => {
      window.history.replaceState(null, '', '/#classification');
      const fetcher = prepare();
      vi.mocked(fetchClassification).mockResolvedValueOnce({
        ...page(),
        status,
        verification: 'local_only',
        updated_at: null,
        verified_at: null,
        summary: null,
        options: { scene: [], style: [], language: [] },
        pagination: { offset: 0, limit: 50, total: 0, next_offset: null },
        playlists: [],
        records: [],
      });
      await act(async () => {
        render(<App />);
      });
      expect(
        screen.getByRole('heading', {
          name: status === 'not_loaded' ? '还没有本地分类成果' : '本地分类记录不可用',
        }),
      ).toBeTruthy();
      await click('重试读取分类成果');
      expect(screen.getByText('分类歌曲 1')).toBeTruthy();
      expect(fetcher.mock.calls.every(([url]) => url === '/api/state')).toBe(true);
      expect(screen.queryByRole('button', { name: /执行分类|继续分类/ })).toBeNull();
    },
  );

  it('keeps a safe retry after connection failure and never displays raw private errors', async () => {
    window.history.replaceState(null, '', '/#classification');
    prepare();
    vi.mocked(fetchClassification).mockRejectedValueOnce(new Error('SECRET-PRIVATE-PATH'));
    await act(async () => {
      render(<App />);
    });
    expect(screen.getByRole('alert').textContent).toContain('分类成果暂时无法读取');
    expect(document.body.textContent).not.toContain('SECRET');
    await click('重试读取分类成果');
    expect(screen.getByText('分类歌曲 1')).toBeTruthy();
  });

  it('shows local-only evidence without claiming an account verification', async () => {
    window.history.replaceState(null, '', '/#classification');
    prepare();
    vi.mocked(fetchClassification).mockResolvedValueOnce({
      ...page(),
      verification: 'local_only',
      verified_at: null,
    });
    await act(async () => {
      render(<App />);
    });
    expect(screen.getByText('仅本地分类记录')).toBeTruthy();
    expect(screen.getByText(/尚未记录账号核验/)).toBeTruthy();
  });

  it.each([
    ['场景 · 夜晚', 'scene', '夜晚'],
    ['风格 · 流行', 'style', '流行'],
    ['语言 · 中文', 'language', '中文'],
  ])('filters by clicking the recorded %s tag', async (label, dimension, tagName) => {
    await open();
    const first = within(screen.getByRole('region', { name: '逐曲分类结果' })).getAllByRole(
      'article',
    )[0];
    await act(async () => {
      fireEvent.click(within(first).getByRole('button', { name: label }));
    });
    expect(vi.mocked(fetchClassification).mock.lastCall?.[1]).toMatchObject({
      dimension,
      tag: tagName,
      offset: 0,
    });
    expect(screen.getByRole('region', { name: '已选分类筛选' }).textContent).toContain(label);
    expect(screen.getByRole('region', { name: '已选分类筛选' }).textContent).toContain(
      '匹配 61 条',
    );
    await click('清除全部筛选');
    expect(vi.mocked(fetchClassification).mock.lastCall?.[1]).toEqual({
      dimension: 'all',
      tag: '',
      review: 'all',
      query: '',
      offset: 0,
    });
  });

  it('opens playlist membership only from an exact option match and clears all intersections for pending review', async () => {
    await open();
    await act(async () => {
      fireEvent.change(screen.getByLabelText('分类维度'), { target: { value: 'style' } });
    });
    await act(async () => {
      fireEvent.change(screen.getByLabelText('分类标签'), { target: { value: '流行' } });
    });
    fireEvent.change(screen.getByRole('textbox', { name: '搜索歌曲或歌手' }), {
      target: { value: '未提交的搜索' },
    });
    await tab('分类歌单');
    const review = within(screen.getByRole('region', { name: '分类歌单' }))
      .getAllByRole('article')
      .at(-1)!;
    await act(async () => {
      fireEvent.click(within(review).getByRole('button', { name: '查看全部待辨识' }));
    });
    expect(screen.getByRole('tab', { name: '歌曲分类' }).getAttribute('aria-selected')).toBe(
      'true',
    );
    expect(screen.getByRole<HTMLInputElement>('textbox', { name: '搜索歌曲或歌手' }).value).toBe(
      '',
    );
    expect(vi.mocked(fetchClassification).mock.lastCall?.[1]).toEqual({
      dimension: 'all',
      tag: '',
      review: 'pending',
      query: '',
      offset: 0,
    });
    await tab('分类歌单');
    const style = screen.getByRole('heading', { name: '风格 · 流行' }).closest('article')!;
    await act(async () => {
      fireEvent.click(within(style).getByRole('button', { name: '查看分类曲目' }));
    });
    expect(vi.mocked(fetchClassification).mock.lastCall?.[1]).toMatchObject({
      dimension: 'style',
      tag: '流行',
      offset: 0,
    });
    await act(async () => {
      fireEvent.click(
        within(screen.getByRole('region', { name: '分类结果概览' })).getByRole('button', {
          name: '查看全部待辨识',
        }),
      );
    });
    expect(vi.mocked(fetchClassification).mock.lastCall?.[1]).toEqual({
      dimension: 'all',
      tag: '',
      review: 'pending',
      query: '',
      offset: 0,
    });
  });

  it('does not invent a filter for an unmatched playlist name or track tag', async () => {
    window.history.replaceState(null, '', '/#classification');
    prepare();
    const data = page();
    data.playlists[0].name = '夜晚 · 自定义';
    data.records[0].styles = ['未知风格'];
    vi.mocked(fetchClassification).mockResolvedValueOnce(data);
    await act(async () => {
      render(<App />);
    });
    expect(screen.queryByRole('button', { name: '风格 · 未知风格' })).toBeNull();
    await tab('分类歌单');
    const card = screen.getByRole('heading', { name: '夜晚 · 自定义' }).closest('article')!;
    expect(
      within(card).getByRole('button', { name: '查看分类曲目' }).hasAttribute('disabled'),
    ).toBe(true);
  });

  it('remembers query, filters, page and view in this window when navigating away and reads again on return', async () => {
    vi.useFakeTimers();
    await open();
    fireEvent.change(screen.getByRole('textbox', { name: '搜索歌曲或歌手' }), {
      target: { value: '示例歌手' },
    });
    await act(async () => {
      await vi.advanceTimersByTimeAsync(200);
    });
    await act(async () => {
      fireEvent.change(screen.getByLabelText('分类维度'), { target: { value: 'style' } });
    });
    await act(async () => {
      fireEvent.change(screen.getByLabelText('分类标签'), { target: { value: '流行' } });
    });
    await click('下一页');
    await tab('分类歌单');
    const before = vi.mocked(fetchClassification).mock.calls.length;
    await navigate('任务记录');
    expect(screen.getAllByRole('heading', { level: 1 })).toHaveLength(1);
    expect(document.querySelectorAll('#page-title')).toHaveLength(1);
    await act(async () => {
      await vi.advanceTimersByTimeAsync(300);
    });
    expect(vi.mocked(fetchClassification)).toHaveBeenCalledTimes(before);
    await navigate('分类成果');
    expect(screen.getByRole('tab', { name: '分类歌单' }).getAttribute('aria-selected')).toBe(
      'true',
    );
    expect(vi.mocked(fetchClassification).mock.lastCall?.[1]).toEqual({
      dimension: 'style',
      tag: '流行',
      query: '示例歌手',
      review: 'all',
      offset: 50,
    });
    await tab('歌曲分类');
    expect(screen.getByRole<HTMLInputElement>('textbox', { name: '搜索歌曲或歌手' }).value).toBe(
      '示例歌手',
    );
    expect(screen.getByText('第 2 页，共 2 页')).toBeTruthy();
    await click('刷新分类记录');
    expect(vi.mocked(fetchClassification).mock.lastCall?.[1]).toEqual({
      dimension: 'style',
      tag: '流行',
      query: '示例歌手',
      review: 'all',
      offset: 50,
    });
    expect(vi.mocked(fetchClassification).mock.lastCall?.[3]).toBe(true);
  });

  it('cancels a pending search debounce while hidden and submits it once after returning', async () => {
    vi.useFakeTimers();
    await open();
    const before = vi.mocked(fetchClassification).mock.calls.length;
    fireEvent.change(screen.getByRole('textbox', { name: '搜索歌曲或歌手' }), {
      target: { value: '待提交查询' },
    });
    await navigate('概览');
    await act(async () => {
      await vi.advanceTimersByTimeAsync(300);
    });
    expect(vi.mocked(fetchClassification)).toHaveBeenCalledTimes(before);
    await navigate('分类成果');
    await act(async () => {
      await vi.advanceTimersByTimeAsync(200);
    });
    expect(vi.mocked(fetchClassification)).toHaveBeenCalledTimes(before + 1);
    expect(vi.mocked(fetchClassification).mock.lastCall?.[1].query).toBe('待提交查询');
  });

  it('refreshes the current page and recovers a reduced file to page one only once', async () => {
    await open();
    await click('下一页');
    const before = vi.mocked(fetchClassification).mock.calls.length;
    vi.mocked(fetchClassification)
      .mockResolvedValueOnce(page(50, 7))
      .mockResolvedValueOnce(page(0, 7));
    await click('刷新分类记录');
    const calls = vi.mocked(fetchClassification).mock.calls.slice(before);
    expect(calls).toHaveLength(2);
    expect(calls[0][1].offset).toBe(50);
    expect(calls[0][3]).toBe(true);
    expect(calls[1][1].offset).toBe(0);
    expect(calls[1][3]).toBeFalsy();
    expect(screen.getByText('第 1 页，共 1 页')).toBeTruthy();
    expect(screen.getByText('分类歌曲 1')).toBeTruthy();
    expect(screen.queryByText('没有匹配的曲目')).toBeNull();
  });

  it('returns a later page to page one when a refreshed record has no matching songs', async () => {
    await open();
    await click('下一页');
    const before = vi.mocked(fetchClassification).mock.calls.length;
    vi.mocked(fetchClassification)
      .mockResolvedValueOnce(page(50, 0))
      .mockResolvedValueOnce(page(0, 0));
    await click('刷新分类记录');
    expect(screen.getByText('第 1 页，共 1 页')).toBeTruthy();
    expect(
      vi
        .mocked(fetchClassification)
        .mock.calls.slice(before)
        .map((call) => call[1].offset),
    ).toEqual([50, 0]);
    expect(screen.getByText('没有匹配的曲目')).toBeTruthy();
    expect(screen.getByRole('button', { name: '上一页' }).hasAttribute('disabled')).toBe(true);
  });

  it('labels preserved cards and historical verification as stale when a changed filter fails', async () => {
    await open();
    vi.mocked(fetchClassification).mockRejectedValueOnce(new Error('SECRET-FILTER'));
    await act(async () => {
      fireEvent.change(screen.getByLabelText('分类维度'), { target: { value: 'style' } });
    });
    await tab('分类歌单');
    expect(
      within(screen.getByRole('region', { name: '分类歌单' })).getAllByRole('article'),
    ).toHaveLength(24);
    expect(screen.getByRole('alert').textContent).toContain('旧记录');
    expect(screen.getByText('旧记录 · 等待重新读取')).toBeTruthy();
    expect(screen.queryByText('已有账号核验记录')).toBeNull();
    expect(document.body.textContent).not.toContain('SECRET');
  });

  it('preserves an old result with a safe stale warning after a refresh failure and retries without clearing filters', async () => {
    await open();
    await click('下一页');
    vi.mocked(fetchClassification).mockRejectedValueOnce(new Error('SECRET-409'));
    await click('刷新分类记录');
    expect(screen.getByText('分类歌曲 51')).toBeTruthy();
    expect(screen.getByRole('alert').textContent).toContain('旧记录');
    expect(screen.getByText('旧记录 · 等待重新读取')).toBeTruthy();
    expect(screen.queryByText('已有账号核验记录')).toBeNull();
    expect(document.body.textContent).not.toContain('SECRET');
    await click('重试读取分类成果');
    expect(vi.mocked(fetchClassification).mock.lastCall?.[1].offset).toBe(50);
    expect(vi.mocked(fetchClassification).mock.lastCall?.[3]).toBe(true);
    expect(screen.queryByRole('alert')).toBeNull();
  });

  it('keeps a removed selected tag visible without broadening the saved query', async () => {
    await open();
    await act(async () => {
      fireEvent.change(screen.getByLabelText('分类维度'), { target: { value: 'style' } });
    });
    await act(async () => {
      fireEvent.change(screen.getByLabelText('分类标签'), { target: { value: '流行' } });
    });
    const changed = page();
    changed.options.style = ['摇滚'];
    changed.filters = { dimension: 'style', tag: '流行', query: '', review: 'all' };
    vi.mocked(fetchClassification).mockResolvedValueOnce(changed);
    await click('刷新分类记录');
    expect(screen.getByLabelText<HTMLSelectElement>('分类标签').value).toBe('流行');
    expect(screen.getByText('流行（当前记录已无此标签）')).toBeTruthy();
    expect(screen.getByRole('region', { name: '已选分类筛选' }).textContent).toContain(
      '风格 · 流行',
    );
    expect(vi.mocked(fetchClassification).mock.lastCall?.[1].tag).toBe('流行');
  });

  it('moves keyboard focus to the accepted next page results and supports tab arrow keys', async () => {
    await open();
    const next = screen.getByRole('button', { name: '下一页' });
    next.focus();
    await click('下一页');
    await waitFor(() =>
      expect(document.activeElement).toBe(screen.getByRole('heading', { name: '逐曲分类结果' })),
    );
    const songs = screen.getByRole('tab', { name: '歌曲分类' });
    songs.focus();
    fireEvent.keyDown(songs, { key: 'ArrowRight' });
    expect(document.activeElement).toBe(screen.getByRole('tab', { name: '分类歌单' }));
    expect(screen.getByRole('tab', { name: '分类歌单' }).getAttribute('aria-selected')).toBe(
      'true',
    );
  });

  it('does not steal focus when the user starts a new search while a page is pending', async () => {
    await open();
    let resolve!: (value: ClassificationPage) => void;
    vi.mocked(fetchClassification).mockReturnValueOnce(
      new Promise((done) => {
        resolve = done;
      }),
    );
    screen.getByRole('button', { name: '下一页' }).focus();
    await click('下一页');
    const search = screen.getByRole('textbox', { name: '搜索歌曲或歌手' });
    search.focus();
    fireEvent.change(search, { target: { value: '新搜索' } });
    await act(async () => {
      resolve(page(50));
    });
    expect(document.activeElement).toBe(search);
    expect(screen.queryByText('分类歌曲 51')).toBeNull();
  });

  it.each(['tab', 'navigation'])(
    'cancels pending page focus and scrolling after %s changes',
    async (destination) => {
      await open();
      const heading = screen.getByRole('heading', { name: '逐曲分类结果' });
      const scroll = vi.fn();
      heading.scrollIntoView = scroll;
      let resolve!: (value: ClassificationPage) => void;
      vi.mocked(fetchClassification).mockReturnValueOnce(
        new Promise((done) => {
          resolve = done;
        }),
      );
      screen.getByRole('button', { name: '下一页' }).focus();
      await click('下一页');
      const signal = vi.mocked(fetchClassification).mock.lastCall?.[2];
      if (destination === 'tab') {
        screen.getByRole('tab', { name: '分类歌单' }).focus();
        await tab('分类歌单');
      } else {
        await navigate('任务记录');
        expect(signal?.aborted).toBe(true);
      }
      const focused = document.activeElement;
      await act(async () => {
        resolve(page(50));
      });
      expect(document.activeElement).toBe(focused);
      expect(scroll).not.toHaveBeenCalled();
    },
  );
});

describe('classification recovery boundary', () => {
  it('distinguishes confirmed membership from planned membership and an unconfirmed batch', async () => {
    window.history.replaceState(null, '', '/#history');
    prepare({
      ...state,
      data: {
        ...state.data,
        history: {
          operation: 'classification',
          status: 'uncertain',
          completed_count: 0,
          resumable: false,
          items: [
            {
              name: '待辨识 · 红心',
              count: 329,
              added_count: 300,
              expected_count: 329,
              phase: 'add_intent',
              attempted_count: 29,
              status: 'uncertain',
            },
          ],
        },
      },
    });
    await act(async () => {
      render(<App />);
    });
    expect(screen.getByText('已确认 300 / 计划 329 首')).toBeTruthy();
    expect(screen.queryByText('已确认 329 / 计划 329 首')).toBeNull();
    expect(screen.queryByText('已确认 29 首')).toBeNull();
  });

  it('labels classification preflight progress as read-only verification', async () => {
    prepare({
      ...state,
      job: {
        id: 'classification-preflight',
        action: 'resume',
        label: '分类工具预检',
        status: 'running',
        progress: {
          stage: 'classification_preflight',
          label: '核对已确认歌单',
          completed_count: 3,
          total_count: 24,
        },
        logs: [],
      },
    });
    await act(async () => {
      render(<App />);
    });
    expect(
      within(screen.getByRole('region', { name: '当前任务' })).getByText('只读核验 · 3 / 24 项'),
    ).toBeTruthy();
  });

  it('labels preflight counts in the persistent task strip as read-only while browsing classification', async () => {
    await open({
      ...state,
      job: {
        id: 'classification-preflight-strip',
        action: 'resume',
        label: '分类工具预检',
        status: 'running',
        progress: {
          stage: 'classification_preflight',
          label: '核对已确认歌单',
          completed_count: 3,
          total_count: 24,
        },
        logs: [],
      },
    });
    expect(
      within(screen.getByRole('region', { name: '当前任务' })).getByText('只读核验 · 3 / 24 项'),
    ).toBeTruthy();
  });

  it('explains dedicated verification and preserves batches without a generic resume or reconciliation button', () => {
    const recovery = {
      status: 'review_required',
      operation: 'classification',
      can_reconcile: true,
    } as WriteRecovery;
    render(
      <RecoveryNotice
        recovery={recovery}
        blocked={false}
        onReconcile={vi.fn()}
        onRecords={vi.fn()}
        canRenewAuthorization
        onRenewAuthorization={vi.fn()}
      />,
    );
    expect(screen.getByText(/分类记录与已确认批次已保留/)).toBeTruthy();
    expect(screen.getByText(/专用核验/)).toBeTruthy();
    expect(screen.queryByRole('button')).toBeNull();
  });

  it('renders all 24 classification history items and disables ordinary resume even if history says resumable', async () => {
    window.history.replaceState(null, '', '/#history');
    const fetcher = prepare({
      ...state,
      data: {
        ...state.data,
        history: {
          operation: 'classification',
          status: 'paused',
          completed_count: 24,
          resumable: true,
          items: page().playlists.map((playlist) => ({
            name: playlist.name,
            count: playlist.count,
            status: 'completed',
          })),
        },
      },
    });
    await act(async () => {
      render(<App />);
    });
    expect(screen.getByText('分类 · 待辨识')).toBeTruthy();
    for (const button of screen.getAllByRole('button', { name: '继续上次任务' })) {
      expect(button.hasAttribute('disabled')).toBe(true);
      fireEvent.click(button);
    }
    expect(screen.getByText(/分类任务需专用核验后续做/)).toBeTruthy();
    expect(fetcher.mock.calls.every(([url]) => url === '/api/state')).toBe(true);
  });
});
