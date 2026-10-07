import { act, fireEvent, render, screen } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';
import { Profiler } from 'react';
import App from './App';

const idle = {
  connection: { installed: true, configured: true, authorized: true },
  data: {
    source: 'local_record',
    account: { nickname: '本地测试' },
    playlists: [],
    history: null,
    artists_completed: false,
  },
  job: null,
};
const completed = {
  ...idle,
  job: {
    id: 'accepted-job',
    action: 'preview_names',
    label: '读取名称清单',
    status: 'completed',
    logs: [],
    result: { status: 'completed' },
  },
};
const response = (value: unknown) => ({ ok: true, json: async () => value });
const updateButton = () => screen.getByRole<HTMLButtonElement>('button', { name: '更新歌单清单' });

function setup() {
  vi.useFakeTimers();
  document.head.innerHTML = '<meta name="organizer-session" content="test-session" />';
  const fetcher = vi.fn(async (url: string, _options?: RequestInit) =>
    response(url === '/api/state' ? idle : { accepted: true, job_id: 'accepted-job' }),
  );
  vi.stubGlobal('fetch', fetcher);
  return fetcher;
}

describe('state polling and accepted task ownership', () => {
  it.each(['accepted', 'unknown'])('consumes a login result observed before the delayed %s POST response', async (outcome) => {
    const fetcher = setup();
    window.history.replaceState(null, '', '/#settings');
    await act(async () => { render(<App />); });
    let finish!: (value: ReturnType<typeof response>) => void;
    let fail!: (reason: Error) => void;
    const login = {
      ...completed,
      job: {
        ...completed.job, action: 'login', label: '扫码授权',
        result: {
          status: 'authorization_pending', message: '请使用官方页面完成扫码。',
          url: 'https://163cn.tv/local-test-only',
        },
      },
    };
    fetcher.mockImplementation(async (url) => url === '/api/state'
      ? response(login)
      : await new Promise((resolve, reject) => { finish = resolve; fail = reject; }));
    await act(async () => {
      fireEvent.click(screen.getByRole('button', { name: '生成扫码授权' }));
      await vi.advanceTimersByTimeAsync(1000);
    });
    expect(screen.queryByRole('link', { name: '打开官方授权链接' })).toBeNull();
    await act(async () => {
      if (outcome === 'accepted') finish(response({ accepted: true, job_id: 'accepted-job' }));
      else fail(new Error('lost acceptance response'));
    });
    await act(async () => { await vi.advanceTimersByTimeAsync(1000); });
    expect(screen.getByRole('link', { name: '打开官方授权链接' }).getAttribute('href'))
      .toBe('https://163cn.tv/local-test-only');
    expect(screen.getByText('请使用官方页面完成扫码。')).toBeTruthy();
    expect(fetcher.mock.calls.filter(([url]) => url === '/api/actions')).toHaveLength(1);
  });

  it('does not commit another page render for unchanged polls but still shows changed state', async () => {
    const fetcher = setup();
    const commits = vi.fn();
    await act(async () => {
      render(<Profiler id="workbench" onRender={commits}><App /></Profiler>);
    });
    // Settle React's first same-value state bailout before measuring steady polls.
    await act(async () => {
      await vi.advanceTimersByTimeAsync(1000);
    });
    commits.mockClear();
    await act(async () => {
      await vi.advanceTimersByTimeAsync(3100);
    });
    expect(fetcher.mock.calls.filter(([url]) => url === '/api/state')).toHaveLength(5);
    expect(commits).not.toHaveBeenCalled();
    fetcher.mockImplementation(async () => response({
      ...idle, data: { ...idle.data, account: { nickname: '更新后的用户' } },
    }));
    await act(async () => {
      await vi.advanceTimersByTimeAsync(1000);
    });
    expect(screen.getByText('更新后的用户')).toBeTruthy();
    expect(commits).toHaveBeenCalled();
  });

  it('keeps a confirmed submission locked through old idle or unrelated snapshots until its job is observed', async () => {
    const fetcher = setup();
    await act(async () => {
      render(<App />);
    });
    await act(async () => {
      fireEvent.click(updateButton());
    });
    expect(updateButton().disabled).toBe(true);
    fireEvent.click(updateButton());
    expect(fetcher.mock.calls.filter(([url]) => url === '/api/actions')).toHaveLength(1);

    fetcher.mockImplementation(async () =>
      response({ ...completed, job: { ...completed.job, id: 'older-job' } }),
    );
    await act(async () => {
      await vi.advanceTimersByTimeAsync(1000);
    });
    expect(updateButton().disabled).toBe(true);

    fetcher.mockImplementation(async () => response(completed));
    await act(async () => {
      await vi.advanceTimersByTimeAsync(1000);
    });
    expect(updateButton().disabled).toBe(false);
  });

  it('does not release a known accepted task after a read failure and a later stale idle recovery', async () => {
    const fetcher = setup();
    await act(async () => {
      render(<App />);
    });
    fetcher.mockImplementation(async (url) => {
      if (url === '/api/state') throw new Error('offline');
      return response({ accepted: true, job_id: 'accepted-job' });
    });
    await act(async () => {
      fireEvent.click(updateButton());
    });
    expect(updateButton().disabled).toBe(true);
    fetcher.mockImplementation(async () => response(idle));
    await act(async () => {
      await vi.advanceTimersByTimeAsync(1000);
    });
    expect(updateButton().disabled).toBe(true);
  });

  it('shares an action follow-up read with periodic polling instead of invalidating its response', async () => {
    const fetcher = setup();
    await act(async () => {
      render(<App />);
    });
    let finish!: (value: ReturnType<typeof response>) => void;
    fetcher.mockImplementation(async (url) =>
      url === '/api/state'
        ? await new Promise((resolve) => {
            finish = resolve;
          })
        : response({ accepted: true, job_id: 'accepted-job' }),
    );
    await act(async () => {
      fireEvent.click(updateButton());
    });
    await act(async () => {
      const refresh = screen.getByRole('button', { name: '刷新任务状态' });
      fireEvent.click(refresh);
      fireEvent.click(refresh);
    });
    await act(async () => {
      await vi.advanceTimersByTimeAsync(2200);
    });
    expect(fetcher.mock.calls.filter(([url]) => url === '/api/state')).toHaveLength(2);
    await act(async () => {
      finish(response(completed));
    });
    expect(updateButton().disabled).toBe(false);
  });

  it('aborts the current state read when the component is unmounted', async () => {
    const fetcher = setup();
    let signal: AbortSignal | undefined;
    fetcher.mockImplementation(async (_url, options) => {
      signal = options?.signal ?? undefined;
      return await new Promise((_, reject) => {
        signal?.addEventListener('abort', () => reject(new DOMException('Aborted', 'AbortError')));
      });
    });
    const view = render(<App />);
    expect(signal?.aborted).toBe(false);
    await act(async () => {
      view.unmount();
    });
    expect(signal?.aborted).toBe(true);
    expect(vi.getTimerCount()).toBe(0);
  });

  it('cancels a pre-action state read and ignores its late idle result', async () => {
    const fetcher = setup();
    await act(async () => {
      render(<App />);
    });
    let oldSignal: AbortSignal | undefined;
    let finishOld!: (value: ReturnType<typeof response>) => void;
    fetcher.mockImplementationOnce(async (_url, options) => {
      oldSignal = options?.signal ?? undefined;
      return await new Promise((resolve) => {
        finishOld = resolve;
      });
    });
    await act(async () => {
      await vi.advanceTimersByTimeAsync(1000);
    });
    fetcher.mockImplementation(async (url) =>
      response(
        url === '/api/state'
          ? { ...completed, job: { ...completed.job, status: 'running' } }
          : { accepted: true, job_id: 'accepted-job' },
      ),
    );
    await act(async () => {
      fireEvent.click(updateButton());
    });
    expect(oldSignal?.aborted).toBe(true);
    await act(async () => {
      finishOld(response(idle));
    });
    expect(updateButton().disabled).toBe(true);
    expect(screen.getByRole('button', { name: '暂停任务' })).toBeTruthy();
  });

  it('does not start a follow-up state read if a POST finishes after unmount', async () => {
    const fetcher = setup();
    let view!: ReturnType<typeof render>;
    await act(async () => {
      view = render(<App />);
    });
    let finish!: (value: ReturnType<typeof response>) => void;
    fetcher.mockImplementationOnce(
      async () =>
        await new Promise((resolve) => {
          finish = resolve;
        }),
    );
    await act(async () => {
      fireEvent.click(updateButton());
    });
    view.unmount();
    await act(async () => {
      finish(response({ accepted: true, job_id: 'accepted-job' }));
    });
    expect(fetcher.mock.calls.filter(([url]) => url === '/api/state')).toHaveLength(1);
    expect(vi.getTimerCount()).toBe(0);
  });
});
