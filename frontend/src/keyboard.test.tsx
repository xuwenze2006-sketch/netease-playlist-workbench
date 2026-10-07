import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, expect, it, vi } from 'vitest';
import App from './App';

function prepare(job: unknown = null) {
  document.head.innerHTML = '<meta name="organizer-session" content="test-session" />';
  const fetcher = vi.fn().mockResolvedValue({
    ok: true,
    json: async () => ({
      connection: { installed: true, configured: true, authorized: null },
      data: {
        account: { nickname: '键盘测试用户' },
        source: 'local_record',
        history: null,
        artists_completed: false,
        playlists: [
          {
            key: 'liked',
            name: '我喜欢的音乐',
            track_count: 20,
            category: 'liked',
            source: 'local_record',
          },
          {
            key: 'normal',
            name: '测试分类',
            track_count: 10,
            category: 'normal',
            source: 'local_record',
          },
        ],
      },
      job,
    }),
  });
  vi.stubGlobal('fetch', fetcher);
  return fetcher;
}

describe('keyboard and long-task experience', () => {
  it('skips to the current main content without changing the active playlist route', async () => {
    window.history.replaceState(null, '', '/#playlists');
    const fetcher = prepare();
    const user = userEvent.setup();
    render(<App />);
    await screen.findByText('键盘测试用户');
    await user.tab();
    expect(document.activeElement).toBe(screen.getByRole('link', { name: '跳到主要内容' }));
    await user.keyboard('{Enter}');
    expect(window.location.hash).toBe('#playlists');
    expect(document.activeElement).toBe(screen.getByRole('main'));
    expect(screen.getByRole('heading', { level: 1, name: '我的歌单' })).toBeTruthy();
    expect(fetcher.mock.calls.every((call) => call[0] === '/api/state')).toBe(true);
  });
  it('moves focus to the newly selected page after keyboard navigation', async () => {
    prepare();
    const user = userEvent.setup();
    render(<App />);
    await screen.findByText('键盘测试用户');
    const link = within(screen.getByRole('navigation', { name: '主导航' })).getByRole('link', {
      name: /我的歌单/,
    });
    link.focus();
    await user.keyboard('{Enter}');
    await waitFor(() => expect(window.location.hash).toBe('#playlists'));
    expect(document.activeElement).toBe(screen.getByRole('main'));
    expect(screen.getByRole('main').getAttribute('aria-labelledby')).toBe('page-title');
  });
  it('keeps search focus while periodic local snapshots update', async () => {
    vi.useFakeTimers();
    window.history.replaceState(null, '', '/#playlists');
    prepare();
    await act(async () => {
      render(<App />);
    });
    const input = screen.getByRole('textbox', { name: '搜索歌单名称' });
    input.focus();
    await act(async () => {
      await vi.advanceTimersByTimeAsync(2200);
    });
    expect(document.activeElement).toBe(input);
  });
  it('restores search focus after clearing the query and announces the result count', async () => {
    window.history.replaceState(null, '', '/#playlists');
    prepare();
    const user = userEvent.setup();
    render(<App />);
    await screen.findByText('键盘测试用户');
    const input = screen.getByRole('textbox', { name: '搜索歌单名称' });
    fireEvent.change(input, { target: { value: '不存在的名称' } });
    const clear = screen.getByRole('button', { name: '清除搜索' });
    clear.focus();
    await user.keyboard('{Enter}');
    expect(document.activeElement).toBe(input);
    expect((input as HTMLInputElement).value).toBe('');
    expect(screen.getByText('显示 2 / 2 个歌单').getAttribute('aria-live')).toBe('polite');
  });
  it('offers a keyboard reset from an empty category and exposes the selected filter', async () => {
    window.history.replaceState(null, '', '/#playlists');
    prepare();
    const user = userEvent.setup();
    render(<App />);
    await screen.findByText('键盘测试用户');
    fireEvent.click(screen.getByRole('button', { name: '歌手精选' }));
    expect(screen.getByRole('button', { name: '歌手精选' }).getAttribute('aria-pressed')).toBe(
      'true',
    );
    expect(screen.getByRole('button', { name: '全部歌单' }).getAttribute('aria-pressed')).toBe(
      'false',
    );
    screen.getByRole('button', { name: '重置筛选' }).focus();
    await user.keyboard('{Enter}');
    expect(screen.getByRole('button', { name: '全部歌单' }).getAttribute('aria-pressed')).toBe(
      'true',
    );
    expect(document.activeElement).toBe(screen.getByRole('textbox', { name: '搜索歌单名称' }));
    expect(screen.getByText('显示 2 / 2 个歌单')).toBeTruthy();
  });
  it('keeps counts, elapsed time, records and pause available on the persistent task strip', async () => {
    window.history.replaceState(null, '', '/#playlists');
    prepare({
      id: 'active',
      action: 'preview_names',
      label: '读取名称清单',
      status: 'running',
      progress: {
        label: '正在核对当前歌单目录',
        completed_count: 2,
        total_count: 5,
        elapsed_seconds: 12.8,
      },
      logs: [],
    });
    render(<App />);
    await screen.findByText('键盘测试用户');
    const strip = within(screen.getByRole('region', { name: '当前任务' }));
    expect(strip.getByText('2 / 5 项')).toBeTruthy();
    expect(strip.getByText('12 秒')).toBeTruthy();
    expect(strip.getByText('正在核对当前歌单目录')).toBeTruthy();
    expect(strip.getByRole('link', { name: '查看任务记录' })).toBeTruthy();
    expect(strip.getByRole('button', { name: '暂停任务' })).toBeTruthy();
  });
});
