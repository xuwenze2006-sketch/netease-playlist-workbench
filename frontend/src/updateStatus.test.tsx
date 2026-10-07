import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';
import App from './App';
import { normalizeState } from './normalize';

function fixture(update?: unknown) {
  return {
    connection: { installed: true, configured: true, authorized: null },
    data: {
      account: { nickname: '更新状态测试用户' },
      source: 'empty',
      playlists: [],
      history: null,
      artists_completed: false,
    },
    job: null,
    ...(update === undefined ? {} : { update }),
  };
}

function prepare(value: unknown) {
  document.head.innerHTML = '<meta name="organizer-session" content="test-session" />';
  const fetcher = vi.fn(async (url: string, _options?: RequestInit) => ({
    ok: true,
    json: async () => (url === '/api/state' ? value : { accepted: true, job_id: 'new-read-job' }),
  }));
  vi.stubGlobal('fetch', fetcher);
  return fetcher;
}

describe('local program update status', () => {
  it.each([
    ['busy', '有新版可用，当前任务完成后重新打开即可更新。'],
    ['review_required', '上次操作结果待核对，新的账号修改和程序更新暂时暂停。'],
    ['stopping', '正在更新，请从启动入口重新打开工作台。'],
  ])(
    'renders the fixed %s message without any account or update action',
    async (status, message) => {
      const fetcher = prepare(fixture({ status, revision: 'SECRET-REVISION', key: 'SECRET-KEY' }));
      render(<App />);
      await screen.findByText('更新状态测试用户');
      const banner = within(screen.getByRole('region', { name: '程序更新状态' }));
      expect(banner.getByText(message)).toBeTruthy();
      expect(banner.queryByRole('button')).toBeNull();
      expect(screen.queryByText(/SECRET/)).toBeNull();
      expect(fetcher.mock.calls.every((call) => call[0] === '/api/state')).toBe(true);
      if (status === 'review_required') {
        expect(banner.queryByText(/新版/)).toBeNull();
        expect(
          screen.getByRole('button', { name: '整理现有歌单名称' }).hasAttribute('disabled'),
        ).toBe(true);
      }
    },
  );

  it.each(
    [undefined, null, [], 'busy', {}, { status: 'invalid' }, { status: null }].map((update) => ({
      update,
    })),
  )('safely treats an absent or malformed optional update field as none (%j)', ({ update }) => {
    expect(normalizeState(fixture(update)).update).toEqual({ status: 'none' });
  });

  it('keeps only the public update status and drops internal build identity', () => {
    expect(
      normalizeState(fixture({ status: 'busy', revision: 'SECRET', key: 'SECRET' })).update,
    ).toEqual({ status: 'busy' });
  });

  it('keeps active progress and pause available while a newer local program is waiting', async () => {
    const fetcher = prepare({
      ...fixture({ status: 'busy' }),
      job: {
        id: 'current-job',
        action: 'rename',
        label: '整理歌单名称',
        status: 'running',
        progress: {
          label: '正在核对当前歌单',
          completed_count: 1,
          total_count: 3,
          elapsed_seconds: 8,
        },
        logs: [],
        result: null,
      },
    });
    render(<App />);
    await screen.findByText('更新状态测试用户');
    expect(screen.getByRole('region', { name: '程序更新状态' })).toBeTruthy();
    expect(screen.getByText('正在核对当前歌单')).toBeTruthy();
    expect(screen.getByText('1 / 3 项')).toBeTruthy();
    expect(screen.getByRole('button', { name: '暂停任务' }).hasAttribute('disabled')).toBe(false);
    expect(fetcher.mock.calls.every((call) => call[0] === '/api/state')).toBe(true);
  });

  it('does not show an update banner for a normal old-compatible local state', async () => {
    prepare(fixture());
    render(<App />);
    await screen.findByText('更新状态测试用户');
    expect(screen.queryByRole('region', { name: '程序更新状态' })).toBeNull();
  });

  it('freezes all changing actions after a completed check replaces an uncertain write', async () => {
    const base = fixture({ status: 'review_required' });
    const fetcher = prepare({
      ...base,
      data: {
        ...base.data,
        history: {
          operation: 'renames',
          status: 'paused',
          completed_count: 0,
          items: [],
          resumable: true,
        },
      },
      job: {
        id: 'completed-check',
        action: 'check',
        label: '检查本地接入',
        status: 'completed',
        logs: [],
        result: { status: 'credentials_saved' },
      },
    });
    render(<App />);
    await screen.findByText('更新状态测试用户');
    expect(screen.getByRole('button', { name: '整理现有歌单名称' }).hasAttribute('disabled')).toBe(
      true,
    );
    fireEvent.click(screen.getByLabelText('接受歌手精选可能公开'));
    expect(screen.getByRole('button', { name: '创建歌手精选' }).hasAttribute('disabled')).toBe(
      true,
    );
    expect(screen.getByRole('button', { name: '继续上次任务' }).hasAttribute('disabled')).toBe(
      true,
    );
    expect(screen.getByRole('button', { name: '更新歌单清单' }).hasAttribute('disabled')).toBe(
      false,
    );
    fireEvent.click(
      within(screen.getByRole('navigation', { name: '主导航' })).getByRole('link', {
        name: '接入设置',
      }),
    );
    await waitFor(() => expect(window.location.hash).toBe('#settings'));
    expect(screen.getByRole('button', { name: '生成扫码授权' }).hasAttribute('disabled')).toBe(
      true,
    );
    expect(screen.getByLabelText('App ID').hasAttribute('disabled')).toBe(true);
    const key = new File(['test-only'], 'test.pem');
    const readFile = vi.fn();
    Object.defineProperty(key, 'arrayBuffer', { value: readFile });
    fireEvent.change(screen.getByLabelText('App ID'), { target: { value: 'fake-app' } });
    fireEvent.change(screen.getByLabelText('Private Key 文件'), { target: { files: [key] } });
    const save = screen.getByRole('button', { name: '保存接入凭证' });
    expect(save.hasAttribute('disabled')).toBe(true);
    fireEvent.submit(save.closest('form')!);
    expect(readFile).not.toHaveBeenCalled();
    expect(screen.getByRole('button', { name: '检查本地接入' }).hasAttribute('disabled')).toBe(
      false,
    );
    expect(screen.getByRole('button', { name: '验证账号授权' }).hasAttribute('disabled')).toBe(
      false,
    );
    expect(fetcher.mock.calls.every((call) => call[0] === '/api/state')).toBe(true);
    fireEvent.click(screen.getByRole('button', { name: '检查本地接入' }));
    await waitFor(() =>
      expect(fetcher.mock.calls.filter((call) => call[0] === '/api/actions')).toHaveLength(1),
    );
    const actionCall = fetcher.mock.calls.find((call) => call[0] === '/api/actions');
    expect(JSON.parse(actionCall?.[1]?.body as string).action).toBe('check');
  });

  it('keeps an explicit local snapshot read available while write review is required', async () => {
    const fetcher = prepare({
      ...fixture({ status: 'review_required' }),
      job: {
        id: 'read-failed',
        action: 'preview_names',
        label: '读取清单',
        status: 'failed',
        logs: [],
        result: {
          status: 'failed',
          error_code: 'local_snapshot_unavailable',
          next_step: 'load_desktop_playlists',
        },
      },
    });
    render(<App />);
    await screen.findByText('更新状态测试用户');
    const read = screen.getByRole('button', { name: '重新读取本地歌单' });
    expect(read.hasAttribute('disabled')).toBe(false);
    fireEvent.click(read);
    await waitFor(() =>
      expect(fetcher.mock.calls.filter((call) => call[0] === '/api/actions')).toHaveLength(1),
    );
    const actionCall = fetcher.mock.calls.find((call) => call[0] === '/api/actions');
    expect(JSON.parse(actionCall?.[1]?.body as string).action).toBe('local_plan');
  });

  it('retains the backend write-review gate in a fresh document even when the latest job is a read', async () => {
    vi.useFakeTimers();
    const base = fixture({ status: 'review_required' });
    const value = {
      ...base,
      job: {
        id: 'read-job',
        action: 'check',
        label: '检查本地接入',
        status: 'completed',
        logs: [],
        result: { status: 'credentials_saved' },
      },
    };
    const fetcher = prepare(value);
    let first: ReturnType<typeof render>;
    await act(async () => {
      first = render(<App />);
    });
    expect(screen.getByRole('button', { name: '整理现有歌单名称' }).hasAttribute('disabled')).toBe(
      true,
    );
    first!.unmount();
    document.head.innerHTML = '<meta name="organizer-session" content="new-document-session" />';
    await act(async () => {
      render(<App />);
    });
    expect(screen.getByRole('button', { name: '整理现有歌单名称' }).hasAttribute('disabled')).toBe(
      true,
    );
    expect(fetcher.mock.calls.every((call) => call[0] === '/api/state')).toBe(true);
  });
});
