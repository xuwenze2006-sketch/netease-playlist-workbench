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
  it.each([21, 100])('shows all %i credited artists from a valid local candidate', async (count) => {
    const credits = Array.from({ length: count }, (_, index) => `合作歌手${index + 1}`);
    const fetcher = prepare({
      ...preview,
      artists: [{ name: '测试歌手', count: 1, tracks: [{ name: '合唱曲目', artists: credits }] }],
    });
    render(<App />);
    await screen.findByText('预览测试用户');
    fireEvent.click(screen.getByRole('button', { name: '查看整理内容' }));
    fireEvent.click(screen.getByRole('button', { name: '查看测试歌手的候选曲目' }));
    expect(screen.getByText('合唱曲目')).toBeTruthy();
    expect(screen.getByText(credits.join(' / '))).toBeTruthy();
    expect(fetcher.mock.calls.every((call) => call[0] === '/api/state')).toBe(true);
  });
  it('rejects a candidate exceeding the backend artist limit while keeping the directory', async () => {
    prepare({
      ...preview,
      artists: [{ name: '测试歌手', count: 1, tracks: [{
        name: '超限曲目', artists: Array.from({ length: 101 }, (_, index) => `歌手${index + 1}`),
      }] }],
    });
    render(<App />);
    await screen.findByText('预览测试用户');
    expect(screen.getByText('我喜欢的音乐')).toBeTruthy();
    expect(screen.getByText('还没有可用的整理预览')).toBeTruthy();
  });
  it.each([298, 299])('keeps a valid emoji intact when shortening a %i-character title', async (prefixLength) => {
    const prefix = '长'.repeat(prefixLength);
    const original = `${prefix}🎵`;
    prepare({
      ...preview,
      artists: [{ name: '测试歌手', count: 1, tracks: [{ name: original, artists: ['测试歌手'] }] }],
    });
    render(<App />);
    await screen.findByText('预览测试用户');
    fireEvent.click(screen.getByRole('button', { name: '查看整理内容' }));
    fireEvent.click(screen.getByRole('button', { name: '查看测试歌手的候选曲目' }));
    // A 300-unit display budget can include both halves of the emoji, or neither.
    expect(screen.getByText(prefixLength === 298 ? original : prefix)).toBeTruthy();
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
