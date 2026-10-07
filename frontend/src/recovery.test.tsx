import { act, fireEvent, render, screen, within } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';
import App from './App';
import { normalizeState } from './normalize';

const CLEAR = { status: 'clear', operation: 'unknown', can_reconcile: false };
const PENDING = { status: 'review_required', operation: 'renames', can_reconcile: true };
function fixture(recovery: unknown = PENDING, current: unknown = null) {
  return {
    connection: { installed: true, configured: true, authorized: null },
    data: {
      account: { nickname: '恢复测试用户' },
      source: 'empty',
      playlists: [],
      history: {
        status: 'paused',
        operation: 'renames',
        completed_count: 0,
        items: [],
        resumable: true,
      },
      artists_completed: false,
    },
    recovery,
    job: current,
  };
}
function job(
  id: string,
  action = 'reconcile_renames',
  status = 'completed',
  result: unknown = {
    status: 'completed',
    message: '上次改名已只读核对。',
    outcome_known: true,
    record_saved: true,
  },
) {
  return { id, action, status, label: '只读核对上次改名', result, logs: [] };
}
function prepare(initial: unknown) {
  vi.useFakeTimers();
  document.head.innerHTML = '<meta name="organizer-session" content="test-session" />';
  let value = initial;
  let next: unknown = initial;
  let failAction = false;
  const fetcher = vi.fn(async (url: string, options?: RequestInit) => {
    if (url === '/api/actions') {
      if (failAction) throw new Error('offline');
      value = next;
      return { ok: true, json: async () => ({ accepted: true, job_id: 'explicit-reconcile' }) };
    }
    return { ok: true, json: async () => value };
  });
  vi.stubGlobal('fetch', fetcher);
  return {
    fetcher,
    update: (updated: unknown) => {
      value = updated;
    },
    afterAction: (updated: unknown) => {
      next = updated;
    },
    failActions: (fail = true) => {
      failAction = fail;
    },
  };
}
async function mount() {
  let view: ReturnType<typeof render>;
  await act(async () => {
    view = render(<App />);
  });
  return view!;
}
async function tick() {
  await act(async () => {
    await vi.advanceTimersByTimeAsync(1100);
  });
}
async function click(name: string) {
  await act(async () => {
    fireEvent.click(screen.getByRole('button', { name }));
  });
}
const rename = () => screen.getByRole('button', { name: '整理现有歌单名称' });

describe('durable pending write recovery', () => {
  it('restores a local pending intent with no live job and no automatic account action', async () => {
    const { fetcher } = prepare(fixture());
    await mount();
    const card = within(screen.getByRole('region', { name: '上次操作待核对' }));
    expect(card.getByRole('button', { name: '只读核对上次改名' }).hasAttribute('disabled')).toBe(
      false,
    );
    expect(card.getByText(/只读取账号和歌单/)).toBeTruthy();
    expect(rename().hasAttribute('disabled')).toBe(true);
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
    expect(fetcher.mock.calls.every(([url]) => url === '/api/state')).toBe(true);
  });

  it('keeps pending recovery through a read-only check and a fresh document', async () => {
    const read = fixture(
      PENDING,
      job('read-only-check', 'check', 'completed', { status: 'credentials_saved' }),
    );
    const { fetcher, update } = prepare(fixture());
    const view = await mount();
    update(read);
    await tick();
    expect(rename().hasAttribute('disabled')).toBe(true);
    view.unmount();
    document.head.innerHTML = '<meta name="organizer-session" content="fresh-document" />';
    await mount();
    expect(rename().hasAttribute('disabled')).toBe(true);
    expect(screen.getByRole('region', { name: '上次操作待核对' })).toBeTruthy();
    expect(fetcher.mock.calls.every(([url]) => url === '/api/state')).toBe(true);
  });

  it('only submits an explicit read-only reconciliation and unlocks after a known saved result and local clear', async () => {
    const api = prepare(
      fixture(PENDING, job('old-rename', 'rename', 'uncertain', { status: 'uncertain' })),
    );
    api.afterAction(fixture(CLEAR, job('explicit-reconcile')));
    await mount();
    await click('只读核对上次改名');
    const calls = api.fetcher.mock.calls.filter(([url]) => url === '/api/actions');
    expect(calls).toHaveLength(1);
    expect(JSON.parse(calls[0][1]?.body as string)).toEqual({
      action: 'reconcile_renames',
      payload: {},
    });
    expect(rename().hasAttribute('disabled')).toBe(false);
    expect(screen.queryByRole('region', { name: '上次操作待核对' })).toBeNull();
    expect(screen.queryByRole('region', { name: '任务处理建议' })).toBeNull();
  });

  it.each([
    ['uncertain', { status: 'uncertain', outcome_known: false, record_saved: true }, CLEAR],
    ['completed', { status: 'completed', outcome_known: true, record_saved: false }, CLEAR],
    ['blocked', { status: 'blocked', outcome_known: true, record_saved: true }, CLEAR],
    ['completed', { status: 'completed', outcome_known: true, record_saved: true }, PENDING],
  ])(
    'does not unlock for incomplete or still-pending reconciliation (%s %j)',
    async (status, result, recovery) => {
      const api = prepare(
        fixture(PENDING, job('old-rename', 'rename', 'uncertain', { status: 'uncertain' })),
      );
      api.afterAction(
        fixture(recovery, job('explicit-reconcile', 'reconcile_renames', status, result)),
      );
      await mount();
      await click('只读核对上次改名');
      expect(rename().hasAttribute('disabled')).toBe(true);
    },
  );

  it('does not clear an uncertain artist task when rename reconciliation succeeds', async () => {
    const api = prepare(
      fixture(PENDING, job('old-artists', 'artists', 'uncertain', { status: 'uncertain' })),
    );
    api.afterAction(fixture(CLEAR, job('explicit-reconcile')));
    await mount();
    await click('只读核对上次改名');
    expect(rename().hasAttribute('disabled')).toBe(true);
    expect(screen.getByRole('region', { name: '任务处理建议' })).toBeTruthy();
  });

  it('retains an artist review across a failed reconciliation before a later successful reconciliation', async () => {
    const api = prepare(
      fixture(PENDING, job('old-artists', 'artists', 'uncertain', { status: 'uncertain' })),
    );
    api.afterAction(
      fixture(
        PENDING,
        job('explicit-reconcile', 'reconcile_renames', 'uncertain', { status: 'uncertain' }),
      ),
    );
    await mount();
    await click('只读核对上次改名');
    api.afterAction(fixture(CLEAR, job('explicit-reconcile')));
    await click('只读核对上次改名');
    expect(rename().hasAttribute('disabled')).toBe(true);
  });

  it('does not use an unsolicited completed reconciliation state to clear a previous unknown write', async () => {
    const api = prepare(fixture(CLEAR));
    api.failActions();
    await mount();
    await click('整理现有歌单名称');
    api.update(fixture(CLEAR, job('unsolicited-reconcile')));
    await tick();
    expect(rename().hasAttribute('disabled')).toBe(true);
    expect(screen.getByRole('region', { name: '提交结果待核对' })).toBeTruthy();
  });

  it('can resolve an unknown rename submission through explicit saved read-only reconciliation', async () => {
    const api = prepare(fixture(CLEAR));
    api.failActions();
    await mount();
    await click('整理现有歌单名称');
    api.update(fixture(PENDING));
    await tick();
    api.failActions(false);
    api.afterAction(fixture(CLEAR, job('explicit-reconcile')));
    await click('只读核对上次改名');
    expect(rename().hasAttribute('disabled')).toBe(false);
    expect(screen.queryByRole('region', { name: '提交结果待核对' })).toBeNull();
  });

  it('can resolve a confirmed rename resume intent without repeating the write', async () => {
    const api = prepare(
      fixture(PENDING, job('old-resume', 'resume', 'uncertain', { status: 'uncertain' })),
    );
    api.afterAction(fixture(CLEAR, job('explicit-reconcile')));
    await mount();
    await click('只读核对上次改名');
    expect(rename().hasAttribute('disabled')).toBe(false);
    expect(api.fetcher.mock.calls.filter(([url]) => url === '/api/actions')).toHaveLength(1);
  });

  it('keeps the submitted resume operation even if a delayed response arrives after unrelated history changes', async () => {
    const api = prepare(fixture(CLEAR));
    const original = api.fetcher.getMockImplementation()!;
    let reject!: (cause: Error) => void;
    let defer = true;
    api.fetcher.mockImplementation(async (url, options) => {
      if (url === '/api/actions' && defer) {
        defer = false;
        return new Promise((_resolve, rejectRequest) => {
          reject = rejectRequest;
        });
      }
      return original(url, options);
    });
    await mount();
    await click('继续上次任务');
    const unrelated = fixture(CLEAR);
    api.update({
      ...unrelated,
      data: { ...unrelated.data, history: { ...unrelated.data.history, operation: 'artists' } },
    });
    await tick();
    await act(async () => {
      reject(new Error('offline'));
    });
    api.update(fixture(PENDING));
    await tick();
    api.afterAction(fixture(CLEAR, job('explicit-reconcile')));
    await click('只读核对上次改名');
    expect(rename().hasAttribute('disabled')).toBe(false);
  });

  it('cannot resolve an unknown artist submission by reconciling a different rename intent', async () => {
    const api = prepare(fixture(CLEAR));
    api.failActions();
    await mount();
    fireEvent.click(screen.getByLabelText('接受歌手精选可能公开'));
    await click('创建歌手精选');
    api.update(fixture(PENDING));
    await tick();
    api.failActions(false);
    api.afterAction(fixture(CLEAR, job('explicit-reconcile')));
    await click('只读核对上次改名');
    expect(rename().hasAttribute('disabled')).toBe(true);
    expect(screen.getByRole('region', { name: '提交结果待核对' }).textContent).toContain(
      '创建歌手精选',
    );
  });

  it('does not offer a reconciliation for a malformed present recovery value', async () => {
    const { fetcher } = prepare(fixture({ ...PENDING, operation: 'artists', owner: 'SECRET' }));
    await mount();
    expect(screen.getByRole('region', { name: '上次操作待核对' })).toBeTruthy();
    expect(screen.queryByRole('button', { name: '只读核对上次改名' })).toBeNull();
    expect(rename().hasAttribute('disabled')).toBe(true);
    expect(screen.queryByText(/SECRET/)).toBeNull();
    expect(fetcher.mock.calls.every(([url]) => url === '/api/state')).toBe(true);
  });

  it('keeps only one explicit reconciliation active while the read is running', async () => {
    const api = prepare(fixture());
    api.afterAction(
      fixture(PENDING, job('explicit-reconcile', 'reconcile_renames', 'running', null)),
    );
    await mount();
    await click('只读核对上次改名');
    expect(screen.getByRole('button', { name: '只读核对上次改名' }).hasAttribute('disabled')).toBe(
      true,
    );
    await click('只读核对上次改名');
    expect(api.fetcher.mock.calls.filter(([url]) => url === '/api/actions')).toHaveLength(1);
    expect(screen.getByRole('button', { name: '暂停任务' }).hasAttribute('disabled')).toBe(false);
  });

  it.each(['artists', 'unknown'])(
    'only offers local records for %s recovery',
    async (operation) => {
      const { fetcher } = prepare(
        fixture({ status: 'review_required', operation, can_reconcile: false }),
      );
      await mount();
      const card = within(screen.getByRole('region', { name: '上次操作待核对' }));
      expect(card.queryByRole('button', { name: '只读核对上次改名' })).toBeNull();
      expect(card.getByRole('link', { name: '查看任务记录' })).toBeTruthy();
      expect(rename().hasAttribute('disabled')).toBe(true);
      expect(fetcher.mock.calls.every(([url]) => url === '/api/state')).toBe(true);
    },
  );

  it('keeps only the local recovery whitelist and preserves old states without the optional contract', () => {
    expect(
      normalizeState(fixture({ ...PENDING, owner: 'SECRET', batch_id: 'SECRET', digest: 'SECRET' }))
        .recovery,
    ).toEqual(PENDING);
    const { recovery: _removed, ...old } = fixture();
    expect(normalizeState(old).recovery).toEqual(CLEAR);
  });

  it.each(
    [
      null,
      [],
      {},
      { ...PENDING, can_reconcile: 'true' },
      { ...PENDING, operation: 'artists' },
      { ...PENDING, status: 'clear' },
      { ...PENDING, operation: 'raw' },
    ].map((recovery) => ({ recovery })),
  )('fails closed for a present malformed recovery contract (%j)', ({ recovery }) => {
    expect(normalizeState(fixture(recovery)).recovery).toEqual({
      status: 'review_required',
      operation: 'unknown',
      can_reconcile: false,
    });
  });
});
