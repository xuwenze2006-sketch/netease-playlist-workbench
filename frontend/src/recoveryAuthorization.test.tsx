import { act, fireEvent, render, screen, within } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';
import App from './App';

const PENDING = { status: 'review_required', operation: 'renames', can_reconcile: true };
const CLEAR = { status: 'clear', operation: 'unknown', can_reconcile: false };
const LOGIN_LABEL = '重新扫码后只读核对';
const QR =
  'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+/lCEAAAAASUVORK5CYII=';
function fixture(recovery: unknown = PENDING, job: unknown = null) {
  return {
    connection: { installed: true, configured: true, authorized: false },
    recovery,
    update: { status: recovery === CLEAR ? 'none' : 'review_required' },
    data: {
      account: { nickname: '授权恢复测试用户' },
      source: 'empty',
      playlists: [],
      history: {
        operation: 'renames',
        status: 'paused',
        completed_count: 0,
        items: [],
        resumable: true,
      },
      artists_completed: false,
    },
    job,
  };
}
function job(id: string, action: string, status = 'completed', result: unknown = null) {
  return { id, action, status, label: '模拟核对', result, logs: [] };
}
function prepare(initial = fixture()) {
  vi.useFakeTimers();
  document.head.innerHTML = '<meta name="organizer-session" content="test-session" />';
  let value = initial;
  let sequence = 0;
  let next: (action: string, id: string) => typeof initial = (action, id) => ({
    ...value,
    job: job(id, action),
  });
  let failActions = false;
  const fetcher = vi.fn(async (url: string, options?: RequestInit) => {
    if (url === '/api/actions') {
      if (failActions) throw new Error('offline');
      const { action } = JSON.parse(options!.body as string);
      const id = `job-${++sequence}`;
      value = next(action, id);
      return { ok: true, json: async () => ({ accepted: true, job_id: id }) };
    }
    return { ok: true, json: async () => value };
  });
  vi.stubGlobal('fetch', fetcher);
  return {
    fetcher,
    update: (updated: typeof initial) => {
      value = updated;
    },
    actionResult: (callback: typeof next) => {
      next = callback;
    },
    fail: (fail = true) => {
      failActions = fail;
    },
    actions: () =>
      fetcher.mock.calls
        .filter(([url]) => url === '/api/actions')
        .map(([, options]) => JSON.parse(options!.body as string).action),
  };
}
async function mount() {
  let view: ReturnType<typeof render>;
  await act(async () => {
    view = render(<App />);
  });
  return view!;
}
async function click(name: string, container?: HTMLElement) {
  await act(async () => {
    fireEvent.click((container ? within(container) : screen).getByRole('button', { name }));
  });
}
async function tick(milliseconds = 1100) {
  await act(async () => {
    await vi.advanceTimersByTimeAsync(milliseconds);
  });
}
async function go(page: string) {
  await act(async () => {
    fireEvent.click(
      within(screen.getByRole('navigation', { name: '主导航' })).getByRole('link', { name: page }),
    );
    await vi.advanceTimersByTimeAsync(1);
  });
}
async function expectWritesFrozen() {
  await go('概览');
  expect(screen.getByRole('button', { name: '整理现有歌单名称' }).hasAttribute('disabled')).toBe(
    true,
  );
  fireEvent.click(screen.getByLabelText('接受歌手精选可能公开'));
  expect(screen.getByRole('button', { name: '创建歌手精选' }).hasAttribute('disabled')).toBe(true);
  expect(screen.getByRole('button', { name: '继续上次任务' }).hasAttribute('disabled')).toBe(true);
}
async function expectCredentialsFrozen() {
  await go('接入设置');
  expect(screen.getByLabelText('App ID').hasAttribute('disabled')).toBe(true);
  expect(screen.getByLabelText('Private Key 文件').hasAttribute('disabled')).toBe(true);
  expect(screen.getByRole('button', { name: '保存接入凭证' }).hasAttribute('disabled')).toBe(true);
}
function pendingQr(id: string) {
  return fixture(
    PENDING,
    job(id, 'login', 'completed', {
      status: 'authorization_pending',
      authorized: false,
      url: 'https://163cn.tv/fake-read-only-recovery',
      qr_png_base64: QR,
    }),
  );
}

describe('explicit authorization recovery for a local rename intent', () => {
  it('offers one explicit bounded renewal using saved credentials and performs no startup login', async () => {
    const api = prepare();
    api.actionResult((_action, id) => pendingQr(id));
    await mount();
    expect(api.actions()).toEqual([]);
    const card = screen.getByRole('region', { name: '上次操作待核对' });
    expect(within(card).getByRole('button', { name: LOGIN_LABEL }).hasAttribute('disabled')).toBe(
      false,
    );
    await click(LOGIN_LABEL, card);
    expect(api.actions()).toEqual(['login']);
    expect(screen.getByRole('heading', { name: '接入设置', level: 1 })).toBeTruthy();
    expect(screen.getByRole('img', { name: '网易云官方账号授权二维码' })).toBeTruthy();
    expect(screen.getByRole('link', { name: '打开官方授权链接' }).getAttribute('href')).toBe(
      'https://163cn.tv/fake-read-only-recovery',
    );
    await expectCredentialsFrozen();
    await expectWritesFrozen();
  });

  it('keeps every write frozen after login and authorization checks until a separate explicit reconciliation is known and saved', async () => {
    const api = prepare();
    api.actionResult((action, id) =>
      action === 'login'
        ? pendingQr(id)
        : action === 'login_status'
          ? {
              ...fixture(
                PENDING,
                job(id, action, 'completed', { status: 'authorized', authorized: true }),
              ),
              connection: { installed: true, configured: true, authorized: true },
            }
          : {
              ...fixture(
                CLEAR,
                job(id, action, 'completed', {
                  status: 'completed',
                  outcome_known: true,
                  record_saved: true,
                }),
              ),
              connection: { installed: true, configured: true, authorized: true },
            },
    );
    await mount();
    await click(LOGIN_LABEL);
    await expectWritesFrozen();
    await expectCredentialsFrozen();
    await click('验证账号授权');
    expect(screen.queryByRole('img', { name: '网易云官方账号授权二维码' })).toBeNull();
    await expectWritesFrozen();
    await expectCredentialsFrozen();
    await click('只读核对上次改名');
    await go('概览');
    expect(screen.getByRole('button', { name: '整理现有歌单名称' }).hasAttribute('disabled')).toBe(
      false,
    );
    expect(api.actions()).toEqual(['login', 'login_status', 'reconcile_renames']);
  });

  it.each([
    [
      'uncertain',
      { status: 'uncertain', outcome_known: false, error_code: 'account_mismatch' },
      PENDING,
    ],
    [
      'completed',
      { status: 'completed', outcome_known: true, applied_to_account: true, record_saved: false },
      CLEAR,
    ],
  ])(
    'continues protecting writes when reconciliation fails or cannot save (%s)',
    async (status, result, recovery) => {
      const api = prepare();
      api.actionResult((action, id) =>
        action === 'login' ? pendingQr(id) : fixture(recovery, job(id, action, status, result)),
      );
      await mount();
      await click(LOGIN_LABEL);
      await click('只读核对上次改名');
      await expectWritesFrozen();
      await expectCredentialsFrozen();
      expect(api.actions()).toEqual(['login', 'reconcile_renames']);
    },
  );

  it('does not treat an unsolicited stored login QR as a renewal intent after refreshing the document', async () => {
    const api = prepare(pendingQr('stored-login'));
    const view = await mount();
    expect(screen.queryByRole('img', { name: '网易云官方账号授权二维码' })).toBeNull();
    view.unmount();
    await mount();
    await tick(4000);
    expect(api.actions()).toEqual([]);
    expect(screen.queryByRole('img', { name: '网易云官方账号授权二维码' })).toBeNull();
  });

  it('keeps the saved credentials frozen when a successful renewal reports a QR result', async () => {
    const api = prepare();
    api.actionResult((_action, id) => pendingQr(id));
    await mount();
    await click(LOGIN_LABEL);
    const file = new File(['test-only'], 'existing.pem');
    const read = vi.fn();
    Object.defineProperty(file, 'arrayBuffer', { value: read });
    fireEvent.change(screen.getByLabelText('App ID'), { target: { value: 'replacement' } });
    fireEvent.change(screen.getByLabelText('Private Key 文件'), { target: { files: [file] } });
    fireEvent.submit(screen.getByRole('button', { name: '保存接入凭证' }).closest('form')!);
    expect(read).not.toHaveBeenCalled();
    expect(api.actions()).toEqual(['login']);
    expect(screen.getByRole('img', { name: '网易云官方账号授权二维码' })).toBeTruthy();
  });

  it('rejects a QR result pointing away from the official HTTPS authorization domains', async () => {
    const api = prepare();
    api.actionResult((_action, id) =>
      fixture(
        PENDING,
        job(id, 'login', 'completed', {
          status: 'authorization_pending',
          url: 'http://music.163.com/fake',
          qr_png_base64: QR,
        }),
      ),
    );
    await mount();
    await click(LOGIN_LABEL);
    expect(screen.queryByRole('img', { name: '网易云官方账号授权二维码' })).toBeNull();
    await tick(4000);
    expect(api.actions()).toEqual(['login']);
    await expectWritesFrozen();
  });

  it.each(['artists', 'unknown'])(
    'refuses renewal for %s or mixed local recovery',
    async (operation) => {
      const api = prepare(fixture({ status: 'review_required', operation, can_reconcile: false }));
      await mount();
      expect(screen.queryAllByRole('button', { name: LOGIN_LABEL })).toHaveLength(0);
      await go('接入设置');
      expect(screen.getByRole('button', { name: '生成扫码授权' }).hasAttribute('disabled')).toBe(
        true,
      );
      await click('生成扫码授权');
      expect(api.actions()).toEqual([]);
    },
  );

  it('refuses renewal for an unconfigured client despite a saved rename intent', async () => {
    const base = fixture();
    const api = prepare({
      ...base,
      connection: { installed: true, configured: false, authorized: false },
    });
    await mount();
    expect(screen.queryByRole('button', { name: LOGIN_LABEL })).toBeNull();
    await go('接入设置');
    expect(screen.getByRole('button', { name: '生成扫码授权' }).hasAttribute('disabled')).toBe(
      true,
    );
    expect(api.actions()).toEqual([]);
  });

  it('refuses renewal until a real unknown HTTP write has a durable rename intent', async () => {
    const api = prepare(fixture(CLEAR));
    api.fail();
    await mount();
    await click('整理现有歌单名称');
    await go('接入设置');
    expect(screen.getByRole('button', { name: '生成扫码授权' }).hasAttribute('disabled')).toBe(
      true,
    );
    expect(screen.queryByRole('button', { name: LOGIN_LABEL })).toBeNull();
    expect(api.actions()).toEqual(['rename']);
    api.update(fixture(PENDING));
    await tick();
    expect(
      screen
        .getAllByRole('button', { name: LOGIN_LABEL })
        .every((button) => !button.hasAttribute('disabled')),
    ).toBe(true);
  });

  it.each(['artists', 'preview_names'])(
    'retains an observed %s unknown reason even after the current read is replaced',
    async (otherAction) => {
      const api = prepare(
        fixture(PENDING, job('old-rename', 'rename', 'uncertain', { status: 'uncertain' })),
      );
      await mount();
      api.update(
        fixture(PENDING, job('other-unknown', otherAction, 'uncertain', { status: 'uncertain' })),
      );
      await tick();
      api.update(fixture(PENDING, job('current-read', 'check')));
      await tick();
      expect(screen.queryByRole('button', { name: LOGIN_LABEL })).toBeNull();
      await go('接入设置');
      expect(screen.getByRole('button', { name: '生成扫码授权' }).hasAttribute('disabled')).toBe(
        true,
      );
      api.actionResult((action, id) =>
        fixture(
          CLEAR,
          job(id, action, 'completed', {
            status: 'completed',
            outcome_known: true,
            record_saved: true,
          }),
        ),
      );
      await click('只读核对上次改名');
      await expectWritesFrozen();
      expect(api.actions()).toEqual(['reconcile_renames']);
    },
  );

  it('keeps a fresh-document uncertain artist history outside the rename renewal exception', async () => {
    const base = fixture(PENDING, job('completed-read', 'check'));
    const api = prepare({
      ...base,
      data: {
        ...base.data,
        history: { ...base.data.history, operation: 'artists', status: 'uncertain' },
      },
    });
    await mount();
    expect(screen.queryByRole('button', { name: LOGIN_LABEL })).toBeNull();
    await go('接入设置');
    expect(screen.getByRole('button', { name: '生成扫码授权' }).hasAttribute('disabled')).toBe(
      true,
    );
    expect(api.actions()).toEqual([]);
  });

  it('does not use an older rename intent to retry a later unknown authorization submission', async () => {
    const api = prepare(fixture(CLEAR));
    api.fail();
    await mount();
    await click('整理现有歌单名称');
    api.update(fixture(PENDING));
    await tick();
    await click(LOGIN_LABEL);
    expect(api.actions()).toEqual(['rename', 'login']);
    expect(screen.queryByRole('button', { name: LOGIN_LABEL })).toBeNull();
    expect(screen.getByRole('button', { name: '生成扫码授权' }).hasAttribute('disabled')).toBe(
      true,
    );
    await tick();
    expect(api.actions()).toEqual(['rename', 'login']);
    api.update(pendingQr('accepted-lost-login'));
    await tick();
    expect(screen.getByRole('img', { name: '网易云官方账号授权二维码' })).toBeTruthy();
    expect(screen.getByRole('region', { name: '提交结果待核对' }).textContent).toContain(
      '整理歌单名称',
    );
    await expectWritesFrozen();
  });

  it('does not restore a late unknown login QR after the user pauses that login', async () => {
    const api = prepare(fixture(CLEAR));
    api.fail();
    await mount();
    await click('整理现有歌单名称');
    api.update(fixture(PENDING));
    await tick();
    await click(LOGIN_LABEL);
    api.update(fixture(PENDING, job('lost-login', 'login', 'running')));
    await tick();
    await click('暂停任务');
    api.update(pendingQr('lost-login'));
    await tick();
    expect(screen.queryByRole('img', { name: '网易云官方账号授权二维码' })).toBeNull();
    await tick(3000);
    expect(api.actions()).toEqual(['rename', 'login']);
  });

  it('does not refresh authorization for an active write or start another job', async () => {
    const api = prepare(fixture(PENDING, job('active-write', 'rename', 'running')));
    await mount();
    await go('接入设置');
    const button =
      screen.queryByRole('button', { name: LOGIN_LABEL }) ||
      screen.getByRole('button', { name: '生成扫码授权' });
    expect(button.hasAttribute('disabled')).toBe(true);
    fireEvent.click(button);
    expect(api.actions()).toEqual([]);
  });
});
