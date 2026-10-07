import { describe, expect, it, vi } from 'vitest';
import { fetchState, LocalApiError, postAction } from './api';

const emptyState = {
  connection: { installed: true, configured: false, authorized: null },
  data: { source: 'empty', account: null, playlists: [], history: null, artists_completed: false },
  job: null,
};

describe('local session API', () => {
  it('gets only local state with the injected session header', async () => {
    const fetcher = vi.fn().mockResolvedValue({ ok: true, json: async () => emptyState });
    vi.stubGlobal('fetch', fetcher);
    await fetchState('test-session');
    expect(fetcher).toHaveBeenCalledTimes(1);
    expect(fetcher.mock.calls[0][0]).toBe('/api/state');
    expect(fetcher.mock.calls[0][1].headers['X-Organizer-Session']).toBe('test-session');
  });
  it('does not retry a write action when the connection is lost', async () => {
    const fetcher = vi.fn().mockRejectedValue(new Error('PRIVATE-KEY-RAW'));
    vi.stubGlobal('fetch', fetcher);
    await expect(postAction('test-session', 'rename')).rejects.toThrow('连接');
    expect(fetcher).toHaveBeenCalledTimes(1);
  });
  it('distinguishes unknown POST acceptance from an explicit rejection', async () => {
    const fetcher = vi.fn().mockRejectedValue(new Error('PRIVATE-KEY-RAW'));
    vi.stubGlobal('fetch', fetcher);
    await expect(postAction('test-session', 'rename')).rejects.toMatchObject({
      acceptanceUnknown: true,
    });
    fetcher.mockResolvedValue({ ok: false, status: 409, json: async () => ({ message: '已有任务正在运行。' }) });
    await expect(postAction('test-session', 'rename')).rejects.toMatchObject({
      acceptanceUnknown: false,
    });
    expect(fetcher).toHaveBeenCalledTimes(2);
  });
  it('retains unknown acceptance when a successful POST response body is unreadable', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue({
      ok: true, status: 202, json: async () => { throw new Error('PRIVATE-KEY-RAW'); },
    }));
    await expect(postAction('test-session', 'artists')).rejects.toMatchObject({
      acceptanceUnknown: true,
    });
  });
  it('does not treat a failed GET as an unknown new task submission', async () => {
    vi.stubGlobal('fetch', vi.fn().mockRejectedValue(new Error('PRIVATE-KEY-RAW')));
    await expect(fetchState('test-session')).rejects.toMatchObject({ acceptanceUnknown: false });
    expect(new LocalApiError('fixed message').acceptanceUnknown).toBe(false);
  });
  it.each([null, {}, 'accepted', { accepted: true }, { accepted: true, job_id: {} }, { accepted: 'true', job_id: 'fake' }])(
    'keeps malformed successful acceptance unknown: %j', async (reply) => {
      vi.stubGlobal('fetch', vi.fn().mockResolvedValue({ ok: true, status: 202, json: async () => reply }));
      await expect(postAction('test-session', 'rename')).rejects.toMatchObject({ acceptanceUnknown: true });
    },
  );
  it('whitelists a valid accepted action without retaining raw response fields', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue({ ok: true, status: 202, json: async () => ({
      accepted: true, job_id: 'fake-job', private_key: 'SECRET', message: 'SECRET',
    }) }));
    expect(await postAction('test-session', 'rename')).toEqual({ accepted: true, job_id: 'fake-job' });
  });
  it('sends explicit public-visibility consent only when selected', async () => {
    const fetcher = vi
      .fn()
      .mockResolvedValue({ ok: true, json: async () => ({ accepted: true, job_id: 'fake' }) });
    vi.stubGlobal('fetch', fetcher);
    await postAction('test-session', 'artists', { accept_default_visibility: true });
    expect(JSON.parse(fetcher.mock.calls[0][1].body)).toEqual({
      action: 'artists',
      payload: { accept_default_visibility: true },
    });
  });
  it('shows a fixed refresh instruction on a replaced document session without resending the action', async () => {
    const fetcher = vi.fn().mockResolvedValue({
      ok: false,
      status: 401,
      json: async () => ({ message: 'UNTRUSTED-RAW' }),
    });
    vi.stubGlobal('fetch', fetcher);
    await expect(postAction('old-session', 'rename')).rejects.toThrow('页面会话已更新，请刷新');
    expect(fetcher).toHaveBeenCalledTimes(1);
  });
  it('keeps unknown and sensitive fields out of the client state model', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn().mockResolvedValue({
        ok: true,
        json: async () => ({
          ...emptyState,
          token: 'SECRET',
          connection: { ...emptyState.connection, private_key: 'SECRET' },
          data: { ...emptyState.data, raw: 'SECRET' },
        }),
      }),
    );
    expect(JSON.stringify(await fetchState('test-session'))).not.toContain('SECRET');
  });
  it('gives a fixed refresh instruction for the backend 403 session rejection', async () => {
    const fetcher = vi.fn().mockResolvedValue({
      ok: false,
      status: 403,
      json: async () => ({ message: 'SECRET-RAW' }),
    });
    vi.stubGlobal('fetch', fetcher);
    await expect(postAction('old-session', 'rename')).rejects.toThrow(
      '页面会话已更新或请求来源无效，请刷新',
    );
    expect(fetcher).toHaveBeenCalledTimes(1);
  });
  it('rejects malformed records before React can render arbitrary object values', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn().mockResolvedValue({
        ok: true,
        json: async () => ({
          ...emptyState,
          data: {
            ...emptyState.data,
            source: 'local_record',
            playlists: [
              {
                key: 'fake',
                name: { private_key: 'SECRET' },
                track_count: 10,
                category: 'normal',
                source: 'local_record',
              },
            ],
          },
        }),
      }),
    );
    await expect(fetchState('test-session')).rejects.toThrow('本地状态');
  });
  it('times out a stalled local state request and allows a later state read to recover', async () => {
    vi.useFakeTimers();
    const fetcher = vi.fn().mockImplementation(
      async (_path: string, options: RequestInit) =>
        await new Promise((_resolve, reject) => {
          options.signal?.addEventListener(
            'abort',
            () => reject(new DOMException('Aborted', 'AbortError')),
            { once: true },
          );
        }),
    );
    vi.stubGlobal('fetch', fetcher);
    let failure: Error | undefined;
    const first = fetchState('test-session').catch((cause) => {
      failure = cause;
    });
    await vi.advanceTimersByTimeAsync(10000);
    expect(failure?.message ?? '').toContain('读取本机状态超时');
    await first;
    expect(fetcher).toHaveBeenCalledTimes(1);
    expect(vi.getTimerCount()).toBe(0);
    fetcher.mockResolvedValue({ ok: true, json: async () => emptyState });
    expect((await fetchState('test-session')).data.source).toBe('empty');
    expect(fetcher).toHaveBeenCalledTimes(2);
    expect(vi.getTimerCount()).toBe(0);
  });
  it('ends waiting for a stalled POST without retrying the accepted or unknown task', async () => {
    vi.useFakeTimers();
    const fetcher = vi.fn().mockImplementation(
      async (_path: string, options: RequestInit) =>
        await new Promise((_resolve, reject) => {
          options.signal?.addEventListener(
            'abort',
            () => reject(new DOMException('Aborted', 'AbortError')),
            { once: true },
          );
        }),
    );
    vi.stubGlobal('fetch', fetcher);
    let failure: Error | undefined;
    const pending = postAction('test-session', 'rename').catch((cause) => {
      failure = cause;
    });
    await vi.advanceTimersByTimeAsync(30000);
    expect(failure?.message ?? '').toContain('等待响应超时，后台任务可能仍在执行');
    expect(failure).toMatchObject({ acceptanceUnknown: true });
    await pending;
    expect(fetcher).toHaveBeenCalledTimes(1);
    expect(fetcher.mock.calls[0][1].signal.aborted).toBe(true);
    expect(vi.getTimerCount()).toBe(0);
  });
  it('respects an external cancellation and clears its deadline and listener', async () => {
    vi.useFakeTimers();
    const external = new AbortController();
    const remove = vi.spyOn(external.signal, 'removeEventListener');
    const fetcher = vi.fn().mockImplementation(
      async (_path: string, options: RequestInit) =>
        await new Promise((_resolve, reject) => {
          options.signal!.addEventListener(
            'abort',
            () => reject(new DOMException('Aborted', 'AbortError')),
            { once: true },
          );
        }),
    );
    vi.stubGlobal('fetch', fetcher);
    const pending = fetchState('test-session', external.signal);
    const rejection = expect(pending).rejects.toThrow('本次请求等待已取消');
    external.abort();
    await rejection;
    expect(fetcher).toHaveBeenCalledTimes(1);
    expect(vi.getTimerCount()).toBe(0);
    expect(remove).toHaveBeenCalledWith('abort', expect.any(Function));
  });
});
