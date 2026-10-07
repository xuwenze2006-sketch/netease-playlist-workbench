import { fireEvent, render, screen, within } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';
import App from './App';

const preview = {
  source: 'local_record',
  scope: 'full',
  updated_at: new Date(2026, 9, 4, 4, 0, 0).toISOString(),
  rename_count: 1,
  renames: [{ key: 'rename-1', old_name: '01 旧分类', name: '01 · 新分类' }],
  artists: [
    {
      name: '测试歌手',
      count: 2,
      tracks: [
        { name: '测试曲目一', artists: ['测试歌手', '合作歌手'] },
        { name: '测试曲目二', artists: ['测试歌手'] },
      ],
    },
  ],
  limitations: ['执行前会重新核验清单。'],
  liked: { expected: 25, observed: 23, missing: 2 },
};
function prepare(value: unknown, completed = false) {
  document.head.innerHTML = '<meta name="organizer-session" content="test-session" />';
  const fetcher = vi.fn().mockResolvedValue({
    ok: true,
    json: async () => ({
      connection: { installed: true, configured: true, authorized: null },
      data: {
        account: { nickname: '预览测试用户' },
        source: 'local_record',
        playlists: [
          {
            key: 'liked',
            name: '我喜欢的音乐',
            track_count: 25,
            category: 'liked',
            source: 'local_record',
          },
        ],
        history: null,
        artists_completed: completed,
        preview: value,
      },
      job: null,
    }),
  });
  vi.stubGlobal('fetch', fetcher);
  return fetcher;
}

describe('local inline preview', () => {
  it('expands concrete renames and selected candidate tracks without any account request', async () => {
    const fetcher = prepare(preview);
    render(<App />);
    await screen.findByText('预览测试用户');
    expect(screen.queryByText('01 旧分类')).toBeNull();
    fireEvent.click(screen.getByRole('button', { name: '查看整理内容' }));
    expect(screen.getByText('01 旧分类')).toBeTruthy();
    expect(screen.getByText('01 · 新分类')).toBeTruthy();
    expect(screen.queryByText('测试曲目一')).toBeNull();
    fireEvent.click(screen.getByRole('button', { name: '查看测试歌手的候选曲目' }));
    expect(screen.getByText('测试曲目一')).toBeTruthy();
    expect(screen.getByText('测试歌手 / 合作歌手')).toBeTruthy();
    expect(fetcher.mock.calls.every((call) => call[0] === '/api/state')).toBe(true);
  });
  it('labels the names scope as unread details and never invents zero missing tracks', async () => {
    prepare({
      ...preview,
      scope: 'names',
      artists: [],
      liked: { expected: 25, observed: null, missing: null },
    });
    render(<App />);
    await screen.findByText('预览测试用户');
    expect(screen.getByText(/未读取红心明细/)).toBeTruthy();
    expect(screen.queryByText(/缺失 0 首/)).toBeNull();
    expect(screen.queryByText(/已核对 0 首/)).toBeNull();
  });
  it('shows the exact full-scope verification counts and local timestamp', async () => {
    prepare(preview);
    render(<App />);
    await screen.findByText('预览测试用户');
    expect(screen.getByText(/目录 25 首 · 已读取 23 首 · 缺失 2 首/)).toBeTruthy();
    expect(screen.getByText(/本地预览 · 10-04 04:00/)).toBeTruthy();
    fireEvent.click(screen.getByRole('button', { name: '查看整理内容' }));
    expect(screen.getByText('执行前会重新核验清单。')).toBeTruthy();
  });
  it('keeps current candidates separate from the historical completed batch', async () => {
    prepare(preview, true);
    render(<App />);
    await screen.findByText('预览测试用户');
    fireEvent.click(screen.getByRole('button', { name: '查看整理内容' }));
    const panel = screen.getByRole('region', { name: '本地整理预览' });
    expect(within(panel).getByText('红心候选')).toBeTruthy();
    expect(within(panel).queryByText('已完成 · 候选供查看')).toBeNull();
    expect(within(panel).getByText('此前获批的五个精选已完成；下方为当前本地候选。')).toBeTruthy();
    expect(screen.getByRole('button', { name: '歌手精选已完成' }).hasAttribute('disabled')).toBe(
      true,
    );
  });
  it.each([undefined, null, { ...preview, artists: [{ name: { private_key: 'SECRET' } }] }])(
    'degrades an absent or malformed optional preview without losing the normal workbench',
    async (value) => {
      const fetcher = prepare(value);
      render(<App />);
      await screen.findByText('预览测试用户');
      expect(screen.getByText('我喜欢的音乐')).toBeTruthy();
      expect(screen.getByText('还没有可用的整理预览')).toBeTruthy();
      expect(screen.queryByText('SECRET')).toBeNull();
      expect(fetcher.mock.calls.every((call) => call[0] === '/api/state')).toBe(true);
    },
  );
});
