import { act, fireEvent, render, screen } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';
import App from './App';

const idle = {
  connection: { installed: true, configured: true, authorized: true },
  data: {
    source: 'empty', account: { nickname: '暂停归属测试' }, playlists: [],
    history: null, artists_completed: false,
  },
  job: null,
};
const snapshot = (id: string, status: 'running' | 'paused') => ({
  ...idle,
  job: {
    id, action: 'preview_names', label: '读取歌单名称', status, logs: [],
    result: status === 'running' ? null : { status: 'paused', message: '旧读取任务已暂停。' },
  },
});
const response = (value: unknown) => ({ ok: true, json: async () => value });

async function pendingPause() {
  vi.useFakeTimers();
  document.head.innerHTML = '<meta name="organizer-session" content="test-session" />';
  let current = snapshot('old-job', 'running');
  let finish!: (value: ReturnType<typeof response>) => void;
  let fail!: (reason: Error) => void;
  const fetcher = vi.fn(async (url: string, options?: RequestInit) => {
    if (url === '/api/state') return response(current);
    if (url === '/api/pause') return await new Promise<ReturnType<typeof response>>((resolve, reject) => {
      finish = resolve; fail = reject;
    });
    if (url === '/api/actions' && JSON.parse(options?.body as string).action === 'preview_names') {
      current = snapshot('new-job', 'running');
      return response({ accepted: true, job_id: 'new-job' });
    }
    throw new Error(`Unexpected fake-backend route: ${url}`);
  });
  vi.stubGlobal('fetch', fetcher);
  await act(async () => { render(<App />); });
  await act(async () => { fireEvent.click(screen.getByRole('button', { name: '暂停任务' })); });
  return {
    fetcher,
    publishPaused: async () => {
      current = snapshot('old-job', 'paused');
      await act(async () => { await vi.advanceTimersByTimeAsync(1000); });
    },
    settle: async (outcome: 'success' | 'failure') => {
      await act(async () => {
        if (outcome === 'success') finish(response({ status: 'pause_requested' }));
        else fail(new Error('lost old pause reply'));
      });
    },
  };
}

describe('pause response ownership', () => {
  it.each(['success', 'failure'] as const)('does not let an old pause %s replace a newer task result', async (outcome) => {
    const backend = await pendingPause();
    await backend.publishPaused();
    const start = screen.getByRole<HTMLButtonElement>('button', { name: '更新歌单清单' });
    expect(start.disabled).toBe(false);
    await act(async () => { fireEvent.click(start); });
    const newNotice = screen.getByRole('status').textContent;
    expect(newNotice).toContain('已开始');
    await backend.settle(outcome);
    expect(screen.queryByRole('status')?.textContent).toBe(newNotice);
    expect(screen.queryByRole('alert')).toBeNull();
    expect(screen.getByRole<HTMLButtonElement>('button', { name: '暂停任务' }).disabled).toBe(false);
    expect(backend.fetcher.mock.calls.filter(([url]) => url === '/api/pause')).toHaveLength(1);
  });

  it.each(['success', 'failure'] as const)('keeps a confirmed terminal result after its late pause %s', async (outcome) => {
    const backend = await pendingPause();
    await backend.publishPaused();
    expect(screen.getByRole('status').textContent).toBe('旧读取任务已暂停。');
    await backend.settle(outcome);
    expect(screen.queryByRole('status')?.textContent).toBe('旧读取任务已暂停。');
    expect(screen.queryByRole('alert')).toBeNull();
    expect(screen.getByRole<HTMLButtonElement>('button', { name: '更新歌单清单' }).disabled).toBe(false);
  });

  it('shows acknowledgement while the paused task still owns the running state', async () => {
    const backend = await pendingPause();
    await backend.settle('success');
    expect(screen.getByRole('status').textContent).toContain('已请求暂停');
    expect(screen.getByRole<HTMLButtonElement>('button', { name: '正在暂停…' }).disabled).toBe(true);
  });

  it('keeps a current pause failure visible and permits an explicit pause attempt', async () => {
    const backend = await pendingPause();
    await backend.settle('failure');
    expect(screen.getByRole('alert').textContent).toContain('与本机服务的连接中断');
    expect(screen.getByRole<HTMLButtonElement>('button', { name: '暂停任务' }).disabled).toBe(false);
    expect(backend.fetcher.mock.calls.filter(([url]) => url === '/api/pause')).toHaveLength(1);
  });

  it('replaces a previous pause failure while an explicit retry is pending', async () => {
    const backend = await pendingPause();
    await backend.settle('failure');
    expect(screen.getByRole('alert').textContent).toContain('与本机服务的连接中断');
    await act(async () => { fireEvent.click(screen.getByRole('button', { name: '暂停任务' })); });
    expect(screen.queryByRole('alert')).toBeNull();
    expect(screen.queryByRole('status')).toBeNull();
    expect(screen.getByRole<HTMLButtonElement>('button', { name: '正在暂停…' }).disabled).toBe(true);
    expect(backend.fetcher.mock.calls.filter(([url]) => url === '/api/pause')).toHaveLength(2);
  });

  it('shows a successful pause retry instead of its previous failure', async () => {
    const backend = await pendingPause();
    await backend.settle('failure');
    await act(async () => { fireEvent.click(screen.getByRole('button', { name: '暂停任务' })); });
    await backend.settle('success');
    expect(screen.queryByRole('alert')).toBeNull();
    expect(screen.getByRole('status').textContent).toContain('已请求暂停');
    expect(screen.getByRole<HTMLButtonElement>('button', { name: '正在暂停…' }).disabled).toBe(true);
    await backend.publishPaused();
    expect(screen.getByRole('status').textContent).toBe('旧读取任务已暂停。');
  });

  it('clears the old task-start notice while its pause is awaiting acknowledgement', async () => {
    const backend = await pendingPause();
    await backend.publishPaused();
    await act(async () => { fireEvent.click(screen.getByRole('button', { name: '更新歌单清单' })); });
    expect(screen.getByRole('status').textContent).toContain('已开始');
    await act(async () => { fireEvent.click(screen.getByRole('button', { name: '暂停任务' })); });
    expect(screen.queryByRole('status')).toBeNull();
    expect(screen.queryByRole('alert')).toBeNull();
    await backend.settle('failure');
    expect(screen.getByRole('alert').textContent).toContain('与本机服务的连接中断');
    expect(screen.getByRole<HTMLButtonElement>('button', { name: '暂停任务' }).disabled).toBe(false);
  });
});
