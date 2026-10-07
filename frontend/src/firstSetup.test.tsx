import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';
import App from './App';

function prepare(connection: {
  installed: boolean;
  configured: boolean;
  authorized: boolean | null;
}) {
  document.head.innerHTML = '<meta name="organizer-session" content="test-session" />';
  const fetcher = vi.fn().mockImplementation(async (url: string) => ({
    ok: true,
    json: async () =>
      url === '/api/state'
        ? {
            connection,
            data: {
              account: { nickname: '接入测试用户' },
              source: 'empty',
              playlists: [],
              history: null,
              artists_completed: false,
            },
            job: null,
          }
        : { accepted: true, job_id: 'fake' },
  }));
  vi.stubGlobal('fetch', fetcher);
  return fetcher;
}

describe('first connection guidance', () => {
  it('routes a missing CLI setup to the right first step without sending an account action', async () => {
    const fetcher = prepare({ installed: false, configured: false, authorized: null });
    render(<App />);
    await screen.findByText('接入测试用户');
    expect(screen.queryByRole('button', { name: '更新歌单清单' })).toBeNull();
    fireEvent.click(screen.getByRole('link', { name: '前往接入设置' }));
    await waitFor(() => expect(window.location.hash).toBe('#settings'));
    const steps = within(screen.getByRole('region', { name: '接入步骤' }));
    expect(steps.getByText('未安装')).toBeTruthy();
    expect(steps.getByText(/install-official-cli\.ps1/)).toBeTruthy();
    expect(screen.getByLabelText('App ID').hasAttribute('disabled')).toBe(true);
    expect(screen.getByRole('button', { name: '生成扫码授权' }).hasAttribute('disabled')).toBe(
      true,
    );
    expect(screen.getByRole('button', { name: '检查本地接入' }).hasAttribute('disabled')).toBe(
      false,
    );
    expect(fetcher.mock.calls.every((call) => call[0] === '/api/state')).toBe(true);
  });
  it('points an installed but unconfigured CLI to credentials and prevents premature scan requests', async () => {
    const fetcher = prepare({ installed: true, configured: false, authorized: null });
    render(<App />);
    await screen.findByText('接入测试用户');
    expect(screen.getByRole('button', { name: '整理现有歌单名称' }).hasAttribute('disabled')).toBe(
      true,
    );
    fireEvent.click(screen.getByRole('link', { name: '前往接入设置' }));
    await waitFor(() => expect(window.location.hash).toBe('#settings'));
    const steps = within(screen.getByRole('region', { name: '接入步骤' }));
    expect(steps.getByText('已安装')).toBeTruthy();
    expect(steps.getByText('下一步：保存开放平台凭证')).toBeTruthy();
    expect(screen.getByLabelText('App ID').hasAttribute('disabled')).toBe(false);
    expect(screen.getByRole('button', { name: '生成扫码授权' }).hasAttribute('disabled')).toBe(
      true,
    );
    expect(screen.getByRole('button', { name: '验证账号授权' }).hasAttribute('disabled')).toBe(
      true,
    );
    expect(fetcher.mock.calls.every((call) => call[0] === '/api/state')).toBe(true);
  });
  it('does not treat an unverified authorized=null state as a reason to block explicit reading', async () => {
    const fetcher = prepare({ installed: true, configured: true, authorized: null });
    render(<App />);
    await screen.findByText('接入测试用户');
    const read = screen.getByRole('button', { name: '更新歌单清单' });
    expect(read.hasAttribute('disabled')).toBe(false);
    fireEvent.click(read);
    await waitFor(() =>
      expect(fetcher.mock.calls.some((call) => call[0] === '/api/actions')).toBe(true),
    );
    const actions = fetcher.mock.calls
      .filter((call) => call[0] === '/api/actions')
      .map((call) => JSON.parse(call[1].body).action);
    expect(actions).toEqual(['preview_names']);
  });
  it('still exposes a local recheck when CLI is missing although saved credentials exist', async () => {
    window.history.replaceState(null, '', '/#settings');
    const fetcher = prepare({ installed: false, configured: true, authorized: null });
    render(<App />);
    await screen.findByText('接入测试用户');
    fireEvent.click(screen.getByRole('button', { name: '检查本地接入' }));
    await waitFor(() =>
      expect(fetcher.mock.calls.some((call) => call[0] === '/api/actions')).toBe(true),
    );
    expect(
      fetcher.mock.calls
        .filter((call) => call[0] === '/api/actions')
        .map((call) => JSON.parse(call[1].body).action),
    ).toEqual(['check']);
    expect(screen.getByRole('button', { name: '生成扫码授权' }).hasAttribute('disabled')).toBe(
      true,
    );
  });
  it.each([' app-id ', 'app.id', 'a'.repeat(129)])(
    'rejects invalid App IDs locally without trimming or reading the key',
    async (id) => {
      window.history.replaceState(null, '', '/#settings');
      const fetcher = prepare({ installed: true, configured: false, authorized: null });
      render(<App />);
      await screen.findByText('接入测试用户');
      const input = screen.getByLabelText('App ID') as HTMLInputElement;
      fireEvent.change(input, { target: { value: id } });
      const file = new File(['key'], 'fake.pem');
      const read = vi.fn();
      Object.defineProperty(file, 'arrayBuffer', { value: read });
      fireEvent.change(document.getElementById('private-key')!, { target: { files: [file] } });
      expect(input.maxLength).toBe(128);
      expect(input.value).toBe(id);
      expect(input.getAttribute('aria-invalid')).toBe('true');
      expect(screen.getByText(/不能包含空格/)).toBeTruthy();
      expect(screen.getByRole('button', { name: '保存接入凭证' }).hasAttribute('disabled')).toBe(
        true,
      );
      fireEvent.submit(input.closest('form')!);
      expect(read).not.toHaveBeenCalled();
      expect(fetcher.mock.calls.every((call) => call[0] === '/api/state')).toBe(true);
    },
  );
});
