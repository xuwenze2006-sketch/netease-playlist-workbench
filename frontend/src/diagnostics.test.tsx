import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';
import App from './App';
import { normalizeState } from './normalize';

function fixture(job: unknown = null) {
  return {
    connection: { installed: true, configured: true, authorized: null },
    data: {
      account: { nickname: '核对测试用户' },
      source: 'local_record',
      playlists: [],
      history: {
        status: 'paused',
        completed_count: 0,
        items: [],
        resumable: true,
        operation: 'renames',
      },
      artists_completed: false,
    },
    job,
  };
}
function job(
  id: string,
  action = 'rename',
  status = 'completed',
  result: unknown = { status: 'completed', message: '核对完成。', outcome_known: true },
) {
  return { id, action, status, label: '整理现有歌单名称', result, logs: [] };
}
function prepare(initial: unknown) {
  document.head.innerHTML = '<meta name="organizer-session" content="test-session" />';
  let value = initial;
  const fetcher = vi.fn(async (url: string) => {
    if (url === '/api/actions') throw new Error('offline');
    return { ok: true, json: async () => value };
  });
  vi.stubGlobal('fetch', fetcher);
  return {
    fetcher,
    update: (next: unknown) => {
      value = next;
    },
  };
}
async function tick() {
  await act(async () => {
    await vi.advanceTimersByTimeAsync(1100);
  });
}

describe('explicit outcome diagnostics', () => {
  it('keeps an unconfirmed submission frozen across stale, empty, and unrelated local states', async () => {
    vi.useFakeTimers();
    const { fetcher, update } = prepare(fixture(job('old-job')));
    await act(async () => {
      render(<App />);
    });
    await act(async () => {
      fireEvent.click(screen.getByRole('button', { name: '整理现有歌单名称' }));
    });
    const pending = within(screen.getByRole('region', { name: '提交结果待核对' }));
    expect(pending.getByRole('button', { name: '刷新任务状态' })).toBeTruthy();
    expect(pending.getByRole('button', { name: '重新加载并核对' })).toBeTruthy();
    expect(screen.getByRole('button', { name: '整理现有歌单名称' }).hasAttribute('disabled')).toBe(
      true,
    );
    expect(screen.getByRole('button', { name: '更新歌单清单' }).hasAttribute('disabled')).toBe(
      false,
    );
    await tick();
    update(fixture());
    await tick();
    update(fixture(job('unrelated-job', 'check')));
    await tick();
    expect(screen.getByRole('region', { name: '提交结果待核对' })).toBeTruthy();
    expect(screen.getByRole('button', { name: '整理现有歌单名称' }).hasAttribute('disabled')).toBe(
      true,
    );
    expect(fetcher.mock.calls.filter((call) => call[0] === '/api/actions')).toHaveLength(1);
    expect(fetcher.mock.calls.some((call) => call[0] === '/api/pause')).toBe(false);
    update(fixture(job('accepted-new-job', 'rename', 'running', null)));
    await tick();
    expect(screen.getByRole('region', { name: '提交结果待核对' })).toBeTruthy();
    update(fixture(job('accepted-new-job')));
    await tick();
    expect(screen.queryByRole('region', { name: '提交结果待核对' })).toBeNull();
    expect(screen.getByRole('button', { name: '整理现有歌单名称' }).hasAttribute('disabled')).toBe(
      false,
    );
    expect(screen.getByRole('status').textContent).toContain('核对完成。');
  });

  it('freezes new writes for a locally recorded uncertain job without blocking explicit reading', async () => {
    const { fetcher } = prepare(
      fixture(
        job('uncertain-job', 'rename', 'uncertain', {
          status: 'uncertain',
          message: '结果尚未核对。',
          outcome_known: false,
          write_attempted: true,
          applied_to_account: null,
          error_code: 'operation_failed',
          next_step: 'inspect_records',
        }),
      ),
    );
    render(<App />);
    await screen.findByText('核对测试用户');
    expect(screen.getByRole('button', { name: '整理现有歌单名称' }).hasAttribute('disabled')).toBe(
      true,
    );
    expect(screen.getByRole('button', { name: '继续上次任务' }).hasAttribute('disabled')).toBe(
      true,
    );
    expect(screen.getByRole('button', { name: '更新歌单清单' }).hasAttribute('disabled')).toBe(
      false,
    );
    const diagnostic = within(screen.getByRole('region', { name: '任务处理建议' }));
    fireEvent.click(diagnostic.getByRole('link', { name: '查看任务记录' }));
    await waitFor(() => expect(window.location.hash).toBe('#history'));
    expect(screen.getByRole('heading', { name: '任务记录', level: 1 })).toBeTruthy();
    expect(fetcher.mock.calls.every((call) => call[0] === '/api/state')).toBe(true);
  });

  it('calls out confirmed account changes whose local record failed to save', async () => {
    vi.useFakeTimers();
    const { fetcher, update } = prepare(fixture());
    fetcher.mockImplementation(async (url: string) => ({
      ok: true,
      json: async () =>
        url === '/api/state' ? fixture() : { accepted: true, job_id: 'write-job' },
    }));
    await act(async () => {
      render(<App />);
    });
    await act(async () => {
      fireEvent.click(screen.getByRole('button', { name: '整理现有歌单名称' }));
    });
    const result = fixture(
      job('write-job', 'rename', 'completed', {
        status: 'completed',
        message: '账号名称已修改。',
        outcome_known: true,
        write_attempted: true,
        applied_to_account: true,
        record_saved: false,
        next_step: 'inspect_records',
      }),
    );
    update(result);
    fetcher.mockImplementation(async () => ({ ok: true, json: async () => result }));
    await tick();
    const diagnostic = within(screen.getByRole('region', { name: '任务处理建议' }));
    expect(diagnostic.getByText('本地记录未保存，请勿重复提交。')).toBeTruthy();
    expect(diagnostic.getByRole('link', { name: '查看任务记录' })).toBeTruthy();
    expect(screen.queryByRole('status')).toBeNull();
    expect(screen.getByRole('button', { name: '整理现有歌单名称' }).hasAttribute('disabled')).toBe(
      true,
    );
  });

  it('offers a settings next step for a typed credentials failure without running it', async () => {
    const { fetcher } = prepare(
      fixture(
        job('read-failed', 'preview_names', 'failed', {
          status: 'failed',
          message: '尚未保存开放平台凭证。',
          error_code: 'credentials_required',
          next_step: 'configure_credentials',
        }),
      ),
    );
    render(<App />);
    await screen.findByText('核对测试用户');
    const diagnostic = within(screen.getByRole('region', { name: '任务处理建议' }));
    fireEvent.click(diagnostic.getByRole('link', { name: '前往接入设置' }));
    await waitFor(() => expect(window.location.hash).toBe('#settings'));
    expect(screen.getByRole('heading', { name: '接入设置' })).toBeTruthy();
    expect(fetcher.mock.calls.every((call) => call[0] === '/api/state')).toBe(true);
  });

  it('retains only typed diagnostic fields and explicit outcome flags from backend results', () => {
    const value = normalizeState(
      fixture(
        job('typed-result', 'rename', 'uncertain', {
          error_code: 'cli_timeout',
          next_step: 'inspect_records',
          outcome_known: false,
          write_attempted: true,
          applied_to_account: null,
          record_saved: false,
          resumable: false,
          private_key: 'SECRET',
          commands: ['SECRET'],
        }),
      ),
    );
    expect(value.job?.result).toEqual({
      error_code: 'cli_timeout',
      next_step: 'inspect_records',
      outcome_known: false,
      write_attempted: true,
      applied_to_account: null,
      record_saved: false,
      resumable: false,
    });
    const malformed = normalizeState(
      fixture(
        job('bad-result', 'rename', 'completed', {
          error_code: 'raw_unknown',
          next_step: 'repeat_write',
          outcome_known: 'true',
          write_attempted: 1,
          applied_to_account: 'yes',
          record_saved: 'false',
          resumable: 'yes',
        }),
      ),
    );
    expect(malformed.job?.result).toEqual({});
  });

  it('does not replace an unconfirmed write with a later unconfirmed read', async () => {
    vi.useFakeTimers();
    const { fetcher, update } = prepare(fixture());
    await act(async () => {
      render(<App />);
    });
    await act(async () => {
      fireEvent.click(screen.getByRole('button', { name: '整理现有歌单名称' }));
    });
    await act(async () => {
      fireEvent.click(screen.getByRole('button', { name: '更新歌单清单' }));
    });
    update(fixture(job('new-read', 'preview_names')));
    await tick();
    expect(screen.getByRole('region', { name: '提交结果待核对' }).textContent).toContain(
      '整理歌单名称',
    );
    expect(screen.getByRole('button', { name: '整理现有歌单名称' }).hasAttribute('disabled')).toBe(
      true,
    );
    expect(fetcher.mock.calls.filter((call) => call[0] === '/api/actions')).toHaveLength(2);
  });

  it('keeps an uncertain write warning while an unrelated read replaces the current task', async () => {
    vi.useFakeTimers();
    const { fetcher, update } = prepare(
      fixture(
        job('uncertain-write', 'rename', 'uncertain', {
          status: 'uncertain',
          message: '上次修改尚未确认。',
          write_attempted: true,
          outcome_known: false,
          next_step: 'inspect_records',
        }),
      ),
    );
    await act(async () => {
      render(<App />);
    });
    update(fixture(job('read-only-check', 'check')));
    await tick();
    expect(screen.getByRole('region', { name: '任务处理建议' }).textContent).toContain(
      '上次修改尚未确认。',
    );
    expect(screen.getByRole('button', { name: '整理现有歌单名称' }).hasAttribute('disabled')).toBe(
      true,
    );
    expect(screen.getByRole('button', { name: '更新歌单清单' }).hasAttribute('disabled')).toBe(
      false,
    );
    expect(fetcher.mock.calls.every((call) => call[0] === '/api/state')).toBe(true);
  });

  it('recovers only the explicitly submitted login QR after its response was lost', async () => {
    vi.useFakeTimers();
    const { fetcher, update } = prepare(fixture(job('old-login', 'login')));
    window.history.replaceState(null, '', '/#settings');
    await act(async () => {
      render(<App />);
    });
    await act(async () => {
      fireEvent.click(screen.getByRole('button', { name: '生成扫码授权' }));
    });
    expect(screen.getByRole('button', { name: '生成扫码授权' }).hasAttribute('disabled')).toBe(
      true,
    );
    expect(screen.getByLabelText('App ID').hasAttribute('disabled')).toBe(true);
    update(
      fixture(
        job('new-login', 'login', 'completed', {
          status: 'authorization_pending',
          url: 'https://163cn.tv/test-login',
        }),
      ),
    );
    await tick();
    expect(screen.getByRole('link', { name: '打开官方授权链接' }).getAttribute('href')).toBe(
      'https://163cn.tv/test-login',
    );
    expect(screen.queryByRole('region', { name: '提交结果待核对' })).toBeNull();
    expect(fetcher.mock.calls.filter((call) => call[0] === '/api/actions')).toHaveLength(1);
  });
});
