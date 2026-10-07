import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';
import App from './App';

const fixture = {
  connection: { installed: true, configured: true, authorized: null },
  data: {
    account: { nickname: '测试用户' },
    source: 'local_record',
    updated_at: '2026-10-04T01:00:00+08:00',
    playlists: [
      {
        key: 'fake-liked',
        name: '我喜欢的音乐',
        track_count: 25,
        category: 'liked',
        source: 'local_record',
      },
      {
        key: 'fake-artist',
        name: '测试歌手 · 红心精选',
        track_count: 8,
        category: 'artist',
        source: 'local_record',
      },
    ],
    history: {
      status: 'completed',
      completed_count: 5,
      items: [],
      resumable: false,
      operation: 'artists',
    },
    artists_completed: true,
  },
  job: null,
};
function prepare(value: unknown = fixture) {
  document.head.innerHTML = '<meta name="organizer-session" content="test-session" />';
  const fetcher = vi.fn().mockImplementation(async (url: string) => ({
    ok: true,
    json: async () => (url === '/api/state' ? value : { accepted: true, job_id: 'fake-job' }),
  }));
  vi.stubGlobal('fetch', fetcher);
  return fetcher;
}
describe('workbench', () => {
  it('shows local-source records and sends no account action on startup', async () => {
    const fetcher = prepare();
    render(<App />);
    await screen.findByText('测试用户');
    expect(screen.getAllByText(/本地记录/).length).toBeGreaterThan(0);
    expect(fetcher.mock.calls.every((call: unknown[]) => call[0] === '/api/state')).toBe(true);
    expect(screen.getByRole('button', { name: '歌手精选已完成' }).hasAttribute('disabled')).toBe(
      true,
    );
  });
  it('filters actual playlist records without inserting demonstration data', async () => {
    prepare();
    render(<App />);
    await screen.findByText('测试用户');
    fireEvent.click(
      within(screen.getByRole('navigation', { name: '主导航' })).getByRole('link', {
        name: /我的歌单/,
      }),
    );
    await waitFor(() => expect(window.location.hash).toBe('#playlists'));
    fireEvent.change(screen.getByPlaceholderText('搜索歌单名称'), {
      target: { value: '测试歌手' },
    });
    expect(screen.getByText('测试歌手 · 红心精选')).toBeTruthy();
    expect(screen.queryByText('我喜欢的音乐')).toBeNull();
  });
  it('defaults to quick name preview and allows an explicit full preview', async () => {
    const fetcher = prepare();
    render(<App />);
    await screen.findByText('测试用户');
    fireEvent.click(screen.getByRole('button', { name: '更新歌单清单' }));
    await waitFor(() =>
      expect(
        fetcher.mock.calls.some(
          (call) =>
            call[0] === '/api/actions' &&
            JSON.parse(call[1].body as string).action === 'preview_names',
        ),
      ).toBe(true),
    );
  });
  it('keeps a running task visible and pause sends one local request', async () => {
    const fetcher = prepare({
      ...fixture,
      job: {
        id: 'fake-job',
        action: 'artists',
        label: '创建歌手精选',
        status: 'running',
        progress: { label: '核对歌曲', completed_count: 2, total_count: 5, elapsed_seconds: 12.4 },
        logs: [],
        result: null,
      },
    });
    render(<App />);
    await screen.findByText('核对歌曲');
    fireEvent.click(screen.getByRole('button', { name: '暂停任务' }));
    await waitFor(() =>
      expect(fetcher.mock.calls.filter((call) => call[0] === '/api/pause').length).toBe(1),
    );
  });
  it('shows real empty state when local records are missing', async () => {
    prepare({
      ...fixture,
      data: {
        source: 'empty',
        playlists: [],
        account: null,
        history: null,
        artists_completed: false,
      },
    });
    render(<App />);
    await screen.findByText('还没有歌单记录');
    expect(screen.queryByText('测试用户')).toBeNull();
  });
  it('preserves an action failure while healthy local polling resumes', async () => {
    vi.useFakeTimers();
    const fetcher = prepare();
    fetcher.mockImplementation(async (url: string) => {
      if (url === '/api/actions') throw new Error('SECRET-RAW');
      return { ok: true, json: async () => fixture };
    });
    await act(async () => {
      render(<App />);
    });
    await act(async () => {
      fireEvent.click(screen.getByRole('button', { name: '更新歌单清单' }));
    });
    expect(screen.getByRole('alert').textContent).toContain('连接中断');
    await act(async () => {
      await vi.advanceTimersByTimeAsync(1200);
    });
    expect(screen.getByRole('alert').textContent).toContain('连接中断');
    expect(screen.queryByText('SECRET-RAW')).toBeNull();
  });
  it('lets the user explicitly select the full red-heart preview', async () => {
    const fetcher = prepare();
    render(<App />);
    await screen.findByText('测试用户');
    fireEvent.click(screen.getByRole('radio', { name: /完整红心清单/ }));
    fireEvent.click(screen.getByRole('button', { name: '更新歌单清单' }));
    await waitFor(() =>
      expect(
        fetcher.mock.calls.some(
          (call) =>
            call[0] === '/api/actions' &&
            JSON.parse(call[1].body as string).action === 'preview_full',
        ),
      ).toBe(true),
    );
  });
  it('does not submit another account task after a rapid double click', async () => {
    const fetcher = prepare();
    let release: (value: unknown) => void = () => {};
    fetcher.mockImplementation(async (url: string) =>
      url === '/api/actions'
        ? await new Promise((resolve) => {
            release = resolve;
          })
        : { ok: true, json: async () => fixture },
    );
    render(<App />);
    await screen.findByText('测试用户');
    const button = screen.getByRole('button', { name: '更新歌单清单' });
    fireEvent.click(button);
    fireEvent.click(button);
    expect(fetcher.mock.calls.filter((call) => call[0] === '/api/actions').length).toBe(1);
    await act(async () => {
      release({ ok: true, json: async () => ({ accepted: true }) });
    });
  });
  it('renders actionable local-connection error instead of permanent loading placeholders', async () => {
    prepare();
    vi.stubGlobal('fetch', vi.fn().mockRejectedValue(new Error('SECRET')));
    render(<App />);
    await screen.findByRole('alert');
    expect(screen.queryByLabelText('正在加载')).toBeNull();
    expect(screen.getByRole('button', { name: '重新连接本机服务' })).toBeTruthy();
  });
  it('does not probe an authorization result left over before this page was opened', async () => {
    vi.useFakeTimers();
    const fetcher = prepare({
      ...fixture,
      job: {
        id: 'old-login',
        action: 'login',
        status: 'completed',
        label: '账号授权',
        result: { status: 'authorization_pending', url: 'https://163cn.tv/old-local-example' },
        logs: [],
      },
    });
    await act(async () => {
      render(<App />);
    });
    await act(async () => {
      await vi.advanceTimersByTimeAsync(4200);
    });
    expect(fetcher.mock.calls.every((call) => call[0] === '/api/state')).toBe(true);
  });
  it('sends one nonce-protected close signal when an active task window is closed', async () => {
    const fetcher = prepare({
      ...fixture,
      job: {
        id: 'active-job',
        action: 'rename',
        label: '整理名称',
        status: 'running',
        progress: { label: '核对歌单' },
        logs: [],
      },
    });
    render(<App />);
    await screen.findByText('核对歌单');
    window.dispatchEvent(new Event('pagehide'));
    window.dispatchEvent(new Event('beforeunload'));
    await waitFor(() =>
      expect(fetcher.mock.calls.filter((call) => call[0] === '/api/close').length).toBe(1),
    );
    const call = fetcher.mock.calls.find((entry) => entry[0] === '/api/close')!;
    expect(call[1].headers['X-Organizer-Session']).toBe('test-session');
    expect(call[1].keepalive).toBe(true);
    expect(JSON.parse(call[1].body)).toEqual({});
  });
  it('does not pause when hidden and detaches an idle document only when closed', async () => {
    const fetcher = prepare();
    render(<App />);
    await screen.findByText('测试用户');
    window.dispatchEvent(new Event('visibilitychange'));
    expect(fetcher.mock.calls.every((call) => call[0] === '/api/state')).toBe(true);
    window.dispatchEvent(new Event('pagehide'));
    expect(fetcher.mock.calls.filter((call) => call[0] === '/api/close').length).toBe(1);
    expect(fetcher.mock.calls.filter((call) => call[0] === '/api/pause').length).toBe(0);
  });
  it('starts bounded metadata probes after this page explicitly requests login and clears on success', async () => {
    vi.useFakeTimers();
    window.history.replaceState(null, '', '/#settings');
    let current: unknown = fixture;
    const fetcher = prepare();
    fetcher.mockImplementation(async (url: string, options?: RequestInit) => {
      if (url === '/api/actions') {
        const action = JSON.parse(options!.body as string).action;
        if (action === 'login')
          current = {
            ...fixture,
            job: {
              id: 'new-login',
              action: 'login',
              status: 'completed',
              label: '扫码授权',
              result: {
                status: 'authorization_pending',
                url: 'https://163cn.tv/current-local-example',
              },
              logs: [],
            },
          };
        if (action === 'authorization_probe')
          current = {
            ...fixture,
            job: {
              id: 'probe',
              action: 'authorization_probe',
              status: 'completed',
              label: '授权检查',
              result: { status: 'authorized', authorized: true },
              logs: [],
            },
          };
        return {
          ok: true,
          json: async () => ({
            accepted: true,
            job_id: action === 'login' ? 'new-login' : 'probe',
          }),
        };
      }
      return { ok: true, json: async () => current };
    });
    await act(async () => {
      render(<App />);
    });
    await act(async () => {
      fireEvent.click(screen.getByRole('button', { name: '生成扫码授权' }));
    });
    expect(screen.getByRole('link', { name: /打开官方授权链接/ }).getAttribute('href')).toContain(
      'current-local-example',
    );
    await act(async () => {
      await vi.advanceTimersByTimeAsync(2400);
    });
    expect(screen.queryByRole('link', { name: /打开官方授权链接/ })).toBeNull();
    await act(async () => {
      await vi.advanceTimersByTimeAsync(5000);
    });
    expect(
      fetcher.mock.calls.filter(
        (call) =>
          call[0] === '/api/actions' && JSON.parse(call[1].body).action === 'authorization_probe',
      ).length,
    ).toBe(1);
    expect(
      fetcher.mock.calls.filter(
        (call) => call[0] === '/api/actions' && JSON.parse(call[1].body).action === 'login',
      ).length,
    ).toBe(1);
  });
  it('clears and stops probing after five minutes without automatically issuing a new login', async () => {
    vi.useFakeTimers();
    window.history.replaceState(null, '', '/#settings');
    let current: unknown = fixture;
    const fetcher = prepare();
    fetcher.mockImplementation(async (url: string, options?: RequestInit) => {
      if (url === '/api/actions') {
        const action = JSON.parse(options!.body as string).action;
        if (action === 'login')
          current = {
            ...fixture,
            job: {
              id: 'pending-login',
              action: 'login',
              status: 'completed',
              label: '扫码授权',
              result: {
                status: 'authorization_pending',
                url: 'https://163cn.tv/pending-local-example',
              },
              logs: [],
            },
          };
        return { ok: true, json: async () => ({ accepted: true, job_id: 'pending-login' }) };
      }
      return { ok: true, json: async () => current };
    });
    await act(async () => {
      render(<App />);
    });
    await act(async () => {
      fireEvent.click(screen.getByRole('button', { name: '生成扫码授权' }));
    });
    await act(async () => {
      await vi.advanceTimersByTimeAsync(301000);
    });
    expect(screen.queryByRole('link', { name: /打开官方授权链接/ })).toBeNull();
    const probes = fetcher.mock.calls.filter(
      (call) =>
        call[0] === '/api/actions' && JSON.parse(call[1].body).action === 'authorization_probe',
    ).length;
    await act(async () => {
      await vi.advanceTimersByTimeAsync(6000);
    });
    expect(
      fetcher.mock.calls.filter(
        (call) =>
          call[0] === '/api/actions' && JSON.parse(call[1].body).action === 'authorization_probe',
      ).length,
    ).toBe(probes);
    expect(
      fetcher.mock.calls.filter(
        (call) => call[0] === '/api/actions' && JSON.parse(call[1].body).action === 'login',
      ).length,
    ).toBe(1);
  });
  it('rejects oversized keys before reading or sending their content', async () => {
    window.history.replaceState(null, '', '/#settings');
    const fetcher = prepare();
    render(<App />);
    await screen.findByText('测试用户');
    fireEvent.change(screen.getByLabelText('App ID'), { target: { value: 'fake-app-id' } });
    const file = new File(['x'.repeat(61 * 1024)], 'large-private-key.pem', { type: 'text/plain' });
    const read = vi.fn();
    Object.defineProperty(file, 'arrayBuffer', { value: read });
    fireEvent.change(document.getElementById('private-key')!, { target: { files: [file] } });
    fireEvent.click(screen.getByRole('button', { name: '保存接入凭证' }));
    await screen.findByText(/私钥文件不能超过 60 KiB/);
    expect(read).not.toHaveBeenCalled();
    expect(fetcher.mock.calls.every((call) => call[0] === '/api/state')).toBe(true);
  });
  it('submits a UTF-8 key once then clears all sensitive inputs without browser storage', async () => {
    window.history.replaceState(null, '', '/#settings');
    const fetcher = prepare();
    const localStorageSpy = vi.spyOn(Storage.prototype, 'setItem');
    render(<App />);
    await screen.findByText('测试用户');
    fireEvent.change(screen.getByLabelText('App ID'), { target: { value: 'fake-app-id' } });
    const key = '-----BEGIN PRIVATE KEY-----\nTEST-PRIVATE-KEY\n-----END PRIVATE KEY-----';
    const file = new File([key], 'private-key.pem', { type: 'text/plain' });
    Object.defineProperty(file, 'arrayBuffer', {
      value: async () => new TextEncoder().encode(key).buffer,
    });
    fireEvent.change(document.getElementById('private-key')!, { target: { files: [file] } });
    fireEvent.click(screen.getByRole('button', { name: '保存接入凭证' }));
    await waitFor(() =>
      expect(fetcher.mock.calls.some((call) => call[0] === '/api/actions')).toBe(true),
    );
    const call = fetcher.mock.calls.find((entry) => entry[0] === '/api/actions')!;
    expect(JSON.parse(call[1].body)).toEqual({
      action: 'save_credentials',
      payload: { app_id: 'fake-app-id', private_key: key },
    });
    expect((screen.getByLabelText('App ID') as HTMLInputElement).value).toBe('');
    expect(screen.queryByText('private-key.pem')).toBeNull();
    expect(screen.queryByText('TEST-PRIVATE-KEY')).toBeNull();
    expect(localStorageSpy).not.toHaveBeenCalled();
  });
  it('requests pause on close even while the action response and first job snapshot are pending', async () => {
    const fetcher = prepare();
    let release: (value: unknown) => void = () => {};
    fetcher.mockImplementation(async (url: string) =>
      url === '/api/actions'
        ? await new Promise((resolve) => {
            release = resolve;
          })
        : { ok: true, json: async () => (url === '/api/state' ? fixture : { accepted: true }) },
    );
    render(<App />);
    await screen.findByText('测试用户');
    fireEvent.click(screen.getByRole('button', { name: '更新歌单清单' }));
    window.dispatchEvent(new Event('pagehide'));
    window.dispatchEvent(new Event('beforeunload'));
    expect(fetcher.mock.calls.filter((call) => call[0] === '/api/close').length).toBe(1);
    await act(async () => {
      release({ ok: true, json: async () => ({ accepted: true, job_id: 'pending' }) });
    });
  });
  it('keeps current progress and pause accessible while browsing playlists', async () => {
    window.history.replaceState(null, '', '/#playlists');
    const fetcher = prepare({
      ...fixture,
      job: {
        id: 'active-preview',
        action: 'preview_names',
        label: '读取名称清单',
        status: 'running',
        progress: { label: '核对歌单目录', completed_count: 2, total_count: 5, elapsed_seconds: 8 },
        logs: [],
      },
    });
    render(<App />);
    await screen.findByText('测试用户');
    expect(screen.getByText('核对歌单目录')).toBeTruthy();
    fireEvent.click(screen.getByRole('button', { name: '暂停任务' }));
    await waitFor(() =>
      expect(fetcher.mock.calls.filter((call) => call[0] === '/api/pause').length).toBe(1),
    );
  });
  it('shows the final paused result instead of a stale in-flight pause label', async () => {
    prepare({
      ...fixture,
      job: {
        id: 'paused-job',
        action: 'artists',
        label: '创建歌手精选',
        status: 'paused',
        progress: { label: '正在暂停，等待当前请求核对完成', completed_count: 2, total_count: 5 },
        result: { status: 'paused', message: '已暂停，已确认的进度已保留。' },
        logs: [],
      },
    });
    render(<App />);
    await screen.findByText('测试用户');
    expect(screen.getByText('已暂停，已确认的进度已保留。')).toBeTruthy();
    expect(screen.queryByText('正在暂停，等待当前请求核对完成')).toBeNull();
  });
  it('protects an artist checkpoint while leaving preview and rename available', async () => {
    prepare({
      ...fixture,
      data: {
        ...fixture.data,
        artists_completed: false,
        history: { ...fixture.data.history, status: 'paused', completed_count: 2, resumable: true },
      },
    });
    render(<App />);
    await screen.findByText('测试用户');
    expect(screen.getByRole('button', { name: '已有歌手精选任务' }).hasAttribute('disabled')).toBe(
      true,
    );
    expect(screen.getByText(/请从任务面板继续/)).toBeTruthy();
    expect(screen.getByRole('button', { name: '继续上次任务' }).hasAttribute('disabled')).toBe(
      false,
    );
    expect(screen.getByRole('button', { name: '整理现有歌单名称' }).hasAttribute('disabled')).toBe(
      false,
    );
  });
  it('clears an earlier QR and probe intent immediately when replacement credentials cannot be read', async () => {
    vi.useFakeTimers();
    window.history.replaceState(null, '', '/#settings');
    const fetcher = prepare();
    let current: unknown = fixture;
    fetcher.mockImplementation(async (url: string, options: { body?: string } = {}) => {
      if (url === '/api/state') return { ok: true, json: async () => current };
      if (JSON.parse(options.body || '{}').action === 'login') {
        current = {
          ...fixture,
          job: {
            id: 'old-qr',
            action: 'login',
            label: '生成扫码授权',
            status: 'completed',
            result: { status: 'pending', url: 'https://music.163.com/login?fake=old' },
            logs: [],
          },
        };
      }
      return { ok: true, json: async () => ({ accepted: true, job_id: 'old-qr' }) };
    });
    await act(async () => {
      render(<App />);
    });
    await act(async () => {
      fireEvent.click(screen.getByRole('button', { name: '生成扫码授权' }));
    });
    expect(screen.getByRole('link', { name: /打开官方授权链接/ })).toBeTruthy();
    fireEvent.change(screen.getByLabelText('App ID'), { target: { value: 'replacement' } });
    const file = new File(['key'], 'replacement.pem');
    Object.defineProperty(file, 'arrayBuffer', {
      value: async () => {
        throw new Error('SECRET');
      },
    });
    fireEvent.change(document.getElementById('private-key')!, { target: { files: [file] } });
    await act(async () => {
      fireEvent.click(screen.getByRole('button', { name: '保存接入凭证' }));
    });
    expect(screen.queryByRole('link', { name: /打开官方授权链接/ })).toBeNull();
    await act(async () => {
      await vi.advanceTimersByTimeAsync(4200);
    });
    expect(
      fetcher.mock.calls.some(
        (call) =>
          call[0] === '/api/actions' && JSON.parse(call[1].body).action === 'authorization_probe',
      ),
    ).toBe(false);
  });
  it('recognizes the explicit one-time launcher login job without sending a new login', async () => {
    window.history.replaceState(null, '', '/#settings');
    const id = 'abcdef123456abcdef123456';
    const fetcher = prepare({
      ...fixture,
      job: {
        id,
        action: 'login',
        label: '生成扫码授权',
        status: 'completed',
        result: { status: 'pending', url: 'https://music.163.com/login?fake=startup' },
        logs: [],
      },
    });
    const meta = document.createElement('meta');
    meta.name = 'organizer-login-job';
    meta.content = id;
    document.head.append(meta);
    render(<App />);
    await screen.findByText('测试用户');
    const authorizationLink = await screen.findByRole('link', { name: /打开官方授权链接/ });
    expect(authorizationLink.getAttribute('href')).toBe('https://music.163.com/login?fake=startup');
    expect(fetcher.mock.calls.every((call) => call[0] === '/api/state')).toBe(true);
  });
  it('ignores a delayed old authorized snapshot after a newer explicit login result', async () => {
    vi.useFakeTimers();
    window.history.replaceState(null, '', '/#settings');
    const fetcher = prepare();
    let stateCalls = 0;
    let releaseOld: (value: unknown) => void = () => {};
    const oldState = {
      ...fixture,
      job: {
        id: 'old-auth',
        action: 'login_status',
        label: '验证账号授权',
        status: 'completed',
        result: { authorized: true },
        logs: [],
      },
    };
    const newState = {
      ...fixture,
      job: {
        id: 'new-login',
        action: 'login',
        label: '生成扫码授权',
        status: 'completed',
        result: { status: 'pending', url: 'https://music.163.com/login?fake=new' },
        logs: [],
      },
    };
    fetcher.mockImplementation(async (url: string) => {
      if (url === '/api/state') {
        stateCalls += 1;
        if (stateCalls === 2)
          return await new Promise((resolve) => {
            releaseOld = resolve;
          });
        return { ok: true, json: async () => (stateCalls === 1 ? fixture : newState) };
      }
      return { ok: true, json: async () => ({ accepted: true, job_id: 'new-login' }) };
    });
    await act(async () => {
      render(<App />);
    });
    await act(async () => {
      await vi.advanceTimersByTimeAsync(1000);
    });
    await act(async () => {
      fireEvent.click(screen.getByRole('button', { name: '生成扫码授权' }));
    });
    expect(screen.getByRole('link', { name: /打开官方授权链接/ })).toBeTruthy();
    await act(async () => {
      releaseOld({ ok: true, json: async () => oldState });
    });
    expect(screen.getByRole('link', { name: /打开官方授权链接/ })).toBeTruthy();
  });
  it('shows an explicit settings action failure after the server initially accepted it', async () => {
    window.history.replaceState(null, '', '/#settings');
    const fetcher = prepare();
    let current: unknown = fixture;
    fetcher.mockImplementation(async (url: string) => {
      if (url === '/api/state') return { ok: true, json: async () => current };
      current = {
        ...fixture,
        job: {
          id: 'failed-check',
          action: 'check',
          label: '检查本地接入',
          status: 'failed',
          result: { status: 'blocked', message: '接入配置尚不完整，请检查凭证。' },
          logs: [],
        },
      };
      return { ok: true, json: async () => ({ accepted: true, job_id: 'failed-check' }) };
    });
    render(<App />);
    await screen.findByText('测试用户');
    fireEvent.click(screen.getByRole('button', { name: '检查本地接入' }));
    await screen.findByRole('alert');
    expect(screen.getByRole('alert').textContent).toContain('接入配置尚不完整，请检查凭证。');
    expect(screen.queryByText('已开始检查本地接入。')).toBeNull();
  });
  it('replaces an explicit resume start notice with its safe final result', async () => {
    const resumable = {
      ...fixture,
      data: {
        ...fixture.data,
        history: {
          ...fixture.data.history,
          status: 'paused',
          resumable: true,
        },
      },
    };
    const fetcher = prepare(resumable);
    let current: unknown = resumable;
    fetcher.mockImplementation(async (url: string) => {
      if (url === '/api/state') return { ok: true, json: async () => current };
      current = {
        ...fixture,
        job: {
          id: 'finished-resume',
          action: 'resume',
          label: '继续上次任务',
          status: 'completed',
          result: { status: 'completed', message: '这次整理已完成。' },
          logs: [],
        },
      };
      return { ok: true, json: async () => ({ accepted: true, job_id: 'finished-resume' }) };
    });
    render(<App />);
    await screen.findByText('测试用户');
    fireEvent.click(screen.getByRole('button', { name: '继续上次任务' }));
    await waitFor(() =>
      expect(screen.getByRole('status').textContent).toContain('这次整理已完成。'),
    );
    expect(screen.queryByText('已开始继续上次任务。')).toBeNull();
  });
  it('offers a full page refresh for a rejected document session', async () => {
    const fetcher = prepare();
    fetcher.mockResolvedValue({
      ok: false,
      status: 403,
      json: async () => ({ message: 'SECRET' }),
    });
    render(<App />);
    await screen.findByRole('alert');
    expect(screen.getByRole('button', { name: '刷新工作台' })).toBeTruthy();
    expect(screen.queryByRole('button', { name: '重新连接本机服务' })).toBeNull();
    expect(screen.queryByText('SECRET')).toBeNull();
  });
  it('stops idle QR waiting on explicit pause and permits a fresh explicit login later', async () => {
    vi.useFakeTimers();
    window.history.replaceState(null, '', '/#settings');
    const fetcher = prepare();
    let current: unknown = fixture;
    let logins = 0;
    fetcher.mockImplementation(async (url: string, options?: RequestInit) => {
      if (url === '/api/state') return { ok: true, json: async () => current };
      const action = JSON.parse((options?.body as string) || '{}').action;
      if (action === 'login') {
        logins += 1;
        current = {
          ...fixture,
          job: {
            id: `login-${logins}`,
            action: 'login',
            label: '扫码授权',
            status: 'completed',
            result: {
              status: 'authorization_pending',
              url: `https://music.163.com/login?fake=${logins}`,
            },
            logs: [],
          },
        };
      }
      return { ok: true, json: async () => ({ accepted: true, job_id: `login-${logins}` }) };
    });
    await act(async () => {
      render(<App />);
    });
    await act(async () => {
      fireEvent.click(screen.getByRole('button', { name: '生成扫码授权' }));
    });
    expect(screen.getByRole('link', { name: /打开官方授权链接/ })).toBeTruthy();
    await act(async () => {
      fireEvent.click(screen.getByRole('button', { name: '停止等待扫码' }));
    });
    expect(screen.queryByRole('link', { name: /打开官方授权链接/ })).toBeNull();
    await act(async () => {
      await vi.advanceTimersByTimeAsync(6200);
    });
    expect(fetcher.mock.calls.filter((call) => call[0] === '/api/pause')).toHaveLength(1);
    expect(
      fetcher.mock.calls.filter(
        (call) =>
          call[0] === '/api/actions' && JSON.parse(call[1].body).action === 'authorization_probe',
      ),
    ).toHaveLength(0);
    await act(async () => {
      fireEvent.click(screen.getByRole('button', { name: '生成扫码授权' }));
    });
    expect(screen.getByRole('link', { name: /打开官方授权链接/ }).getAttribute('href')).toContain(
      'fake=2',
    );
    await act(async () => {
      await vi.advanceTimersByTimeAsync(2200);
    });
    expect(
      fetcher.mock.calls.filter(
        (call) =>
          call[0] === '/api/actions' && JSON.parse(call[1].body).action === 'authorization_probe',
      ),
    ).toHaveLength(1);
  });
  it('does not restore QR waiting when an in-flight probe returns after pause', async () => {
    vi.useFakeTimers();
    window.history.replaceState(null, '', '/#settings');
    const fetcher = prepare();
    let current: unknown = fixture;
    let releaseProbe: (value: unknown) => void = () => {};
    fetcher.mockImplementation(async (url: string, options?: RequestInit) => {
      if (url === '/api/state') return { ok: true, json: async () => current };
      if (url === '/api/pause') {
        current = {
          ...fixture,
          job: {
            id: 'probe',
            action: 'authorization_probe',
            label: '等待扫码授权',
            status: 'paused',
            logs: [],
          },
        };
        return { ok: true, json: async () => ({ accepted: true }) };
      }
      const action = JSON.parse((options?.body as string) || '{}').action;
      if (action === 'login') {
        current = {
          ...fixture,
          job: {
            id: 'login',
            action: 'login',
            label: '扫码授权',
            status: 'completed',
            result: {
              status: 'authorization_pending',
              url: 'https://music.163.com/login?fake=waiting',
            },
            logs: [],
          },
        };
        return { ok: true, json: async () => ({ accepted: true, job_id: 'login' }) };
      }
      current = {
        ...fixture,
        job: {
          id: 'probe',
          action: 'authorization_probe',
          label: '等待扫码授权',
          status: 'running',
          logs: [],
        },
      };
      return await new Promise((resolve) => {
        releaseProbe = resolve;
      });
    });
    await act(async () => {
      render(<App />);
    });
    await act(async () => {
      fireEvent.click(screen.getByRole('button', { name: '生成扫码授权' }));
    });
    await act(async () => {
      await vi.advanceTimersByTimeAsync(3200);
    });
    await act(async () => {
      fireEvent.click(screen.getByRole('button', { name: '暂停任务' }));
    });
    expect(screen.queryByRole('link', { name: /打开官方授权链接/ })).toBeNull();
    await act(async () => {
      releaseProbe({ ok: true, json: async () => ({ accepted: true, job_id: 'probe' }) });
    });
    await act(async () => {
      await vi.advanceTimersByTimeAsync(6200);
    });
    expect(screen.queryByRole('link', { name: /打开官方授权链接/ })).toBeNull();
    expect(
      fetcher.mock.calls.filter(
        (call) =>
          call[0] === '/api/actions' && JSON.parse(call[1].body).action === 'authorization_probe',
      ),
    ).toHaveLength(1);
  });
  it('does not rebuild a login intent when its acceptance response arrives after pause', async () => {
    vi.useFakeTimers();
    window.history.replaceState(null, '', '/#settings');
    const fetcher = prepare();
    let current: unknown = fixture;
    let releaseLogin: (value: unknown) => void = () => {};
    fetcher.mockImplementation(async (url: string) => {
      if (url === '/api/state') return { ok: true, json: async () => current };
      if (url === '/api/pause') {
        current = {
          ...fixture,
          job: {
            id: 'late-login',
            action: 'login',
            label: '扫码授权',
            status: 'paused',
            result: {
              status: 'authorization_pending',
              url: 'https://music.163.com/login?fake=late',
            },
            logs: [],
          },
        };
        return { ok: true, json: async () => ({ accepted: true }) };
      }
      current = {
        ...fixture,
        job: { id: 'late-login', action: 'login', label: '扫码授权', status: 'running', logs: [] },
      };
      return await new Promise((resolve) => {
        releaseLogin = resolve;
      });
    });
    await act(async () => {
      render(<App />);
    });
    await act(async () => {
      fireEvent.click(screen.getByRole('button', { name: '生成扫码授权' }));
    });
    await act(async () => {
      await vi.advanceTimersByTimeAsync(1200);
    });
    await act(async () => {
      fireEvent.click(screen.getByRole('button', { name: '暂停任务' }));
    });
    await act(async () => {
      releaseLogin({ ok: true, json: async () => ({ accepted: true, job_id: 'late-login' }) });
    });
    await act(async () => {
      await vi.advanceTimersByTimeAsync(5200);
    });
    expect(screen.queryByRole('link', { name: /打开官方授权链接/ })).toBeNull();
    expect(fetcher.mock.calls.filter((call) => call[0] === '/api/actions')).toHaveLength(1);
  });
  it('keeps a credentials failure visible and offers refresh after a delayed 403 state response', async () => {
    vi.useFakeTimers();
    window.history.replaceState(null, '', '/#settings');
    const fetcher = prepare();
    let reads = 0;
    let releaseState: (value: unknown) => void = () => {};
    const failedState = {
      ...fixture,
      job: {
        id: 'failed-credentials',
        action: 'save_credentials',
        label: '保存接入凭证',
        status: 'failed',
        result: { status: 'failed', message: '凭证未能保存，请检查文件格式。' },
        logs: [],
      },
    };
    fetcher.mockImplementation(async (url: string) => {
      if (url !== '/api/state')
        return { ok: true, json: async () => ({ accepted: true, job_id: 'failed-credentials' }) };
      reads += 1;
      if (reads >= 3)
        return await new Promise((resolve) => {
          releaseState = resolve;
        });
      return { ok: true, json: async () => (reads === 1 ? fixture : failedState) };
    });
    await act(async () => {
      render(<App />);
    });
    fireEvent.change(screen.getByLabelText('App ID'), { target: { value: 'test-app' } });
    const file = new File(['test-key'], 'fake.pem');
    Object.defineProperty(file, 'arrayBuffer', {
      value: async () => new TextEncoder().encode('test-key').buffer,
    });
    fireEvent.change(document.getElementById('private-key')!, { target: { files: [file] } });
    await act(async () => {
      fireEvent.click(screen.getByRole('button', { name: '保存接入凭证' }));
    });
    expect(screen.getByRole('alert').textContent).toContain('凭证未能保存，请检查文件格式。');
    await act(async () => {
      await vi.advanceTimersByTimeAsync(1000);
    });
    await act(async () => {
      releaseState({ ok: false, status: 403, json: async () => ({ message: 'SECRET' }) });
    });
    expect(screen.getByRole('alert').textContent).toContain('凭证未能保存，请检查文件格式。');
    expect(screen.getByRole('button', { name: '刷新工作台' })).toBeTruthy();
    expect(screen.queryByRole('button', { name: '重新连接本机服务' })).toBeNull();
  });
});
