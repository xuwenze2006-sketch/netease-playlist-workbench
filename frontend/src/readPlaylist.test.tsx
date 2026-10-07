import { act, fireEvent, render, screen, within } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import App from './App';
import { PlaylistDetails } from './PlaylistDetails';
import { normalizeState } from './normalize';
import type { Job } from './types';

const playlist = {
  key: '9001',
  name: '英语',
  track_count: 1,
  category: 'normal',
  source: 'local_record',
};
const savedResult = {
  status: 'completed',
  message: '明细已保存。',
  playlist_key: '9001',
  record_saved: true,
  outcome_known: true,
  write_attempted: false,
  applied_to_account: false,
  expected_count: 1,
  count: 1,
  missing_count: 0,
  missing_metadata_count: 0,
};
function readJob(status = 'running'): Job {
  return {
    id: 'new-read',
    action: 'read_playlist',
    playlist_key: '9001',
    label: '读取歌单明细',
    status,
    progress: { label: '正在读取第 1 页', completed_count: 1, total_count: 2, elapsed_seconds: 3 },
    logs: [],
    result: status === 'running' ? null : { ...savedResult },
  };
}
function prepare(
  options: {
    installed?: boolean;
    configured?: boolean;
    authorized?: boolean | null;
    loaded?: boolean;
    frozen?: boolean;
    oldJob?: boolean;
    unknown?: boolean;
    deferred?: boolean;
    stateError?: boolean;
    immediatePaused?: boolean;
  } = {},
) {
  const state = {
    connection: {
      installed: options.installed ?? true,
      configured: options.configured ?? true,
      authorized: options.authorized === undefined ? null : options.authorized,
    },
    data: {
      account: { nickname: '单歌单测试用户' },
      source: 'local_record',
      playlists: [playlist],
      history: null,
      artists_completed: false,
    },
    recovery: { status: 'clear', operation: 'unknown', can_reconcile: false },
    job: (options.frozen
      ? {
          id: 'old-rename',
          action: 'rename',
          label: '整理歌单名称',
          status: 'uncertain',
          logs: [],
          result: { status: 'uncertain', write_attempted: true, outcome_known: false },
        }
      : options.oldJob
        ? { ...readJob('completed'), id: 'old-read' }
        : null) as Job | null,
  };
  let loaded = !!options.loaded;
  let partialRecord = false;
  let release!: () => void;
  const pending = new Promise<void>((done) => {
    release = done;
  });
  const fetcher = vi.fn(async (url: string, init: RequestInit) => {
    if (url === '/api/state') {
      if (options.stateError && state.job) throw new Error('SECRET');
      return { ok: true, json: async () => structuredClone(state) };
    }
    if (url === '/api/actions') {
      state.job = readJob() as typeof state.job;
      if (options.immediatePaused)
        state.job = {
          ...readJob('paused'),
          result: { ...savedResult, status: 'paused', record_saved: false },
        };
      if (options.deferred) await pending;
      if (options.unknown) throw new Error('SECRET');
      return { ok: true, json: async () => ({ accepted: true, job_id: 'new-read' }) };
    }
    if (url === '/api/pause') {
      state.job = {
        ...readJob('paused'),
        result: { ...savedResult, status: 'paused', record_saved: false },
      } as typeof state.job;
      return { ok: true, json: async () => ({ accepted: true }) };
    }
    if (url.includes('/tracks'))
      return {
        ok: true,
        json: async () => ({
          status: loaded ? 'available' : 'not_loaded',
          source: 'local_record',
          playlist: { ...playlist, key: url.split('/')[3] },
          updated_at: loaded ? '2026-10-04T02:00:00+00:00' : null,
          counts: loaded
            ? {
                expected: partialRecord ? 2 : 1,
                observed: 1,
                missing: partialRecord ? 1 : 0,
                metadata_missing: partialRecord ? 1 : 0,
              }
            : { expected: 1, observed: null, missing: null, metadata_missing: null },
          pagination: { offset: 0, limit: 50, total: loaded ? 1 : 0, next_offset: null },
          tracks: loaded
            ? [
                {
                  key: 'track-1',
                  position: 1,
                  name: '新保存的歌曲',
                  artists: ['本地歌手'],
                  artist_count: 1,
                  metadata_available: !partialRecord,
                },
              ]
            : [],
        }),
      };
    throw new Error(`unexpected ${url} ${init.method}`);
  });
  vi.stubGlobal('fetch', fetcher);
  return {
    state,
    fetcher,
    release,
    save(partial = false) {
      loaded = true;
      partialRecord = partial;
    },
    reads: () => fetcher.mock.calls.filter(([url]) => url.includes('/tracks')).length,
    posts: () => fetcher.mock.calls.filter(([url]) => url === '/api/actions'),
  };
}
async function open() {
  await act(async () => {
    render(<App />);
  });
  expect(screen.getByText('单歌单测试用户')).toBeTruthy();
  await act(async () => {
    fireEvent.click(screen.getByRole('button', { name: '打开英语的歌曲明细' }));
  });
  expect(screen.queryByText('正在读取本地歌曲资料…')).toBeNull();
}
async function startRead() {
  await act(async () => {
    fireEvent.click(screen.getByRole('button', { name: '读取此歌单明细' }));
  });
}
async function poll() {
  await act(async () => {
    await vi.advanceTimersByTimeAsync(1000);
  });
}
beforeEach(() => {
  vi.useFakeTimers();
  document.head.innerHTML = '<meta name="organizer-session" content="test-session" />';
  Object.defineProperty(HTMLDialogElement.prototype, 'showModal', {
    configurable: true,
    value() {
      this.open = true;
    },
  });
  Object.defineProperty(HTMLDialogElement.prototype, 'close', {
    configurable: true,
    value() {
      this.open = false;
    },
  });
});

describe('explicit single playlist reading', () => {
  it('starts only on the explicit drawer button and refreshes this saved new job exactly once', async () => {
    const backend = prepare();
    await open();

    expect(backend.posts()).toHaveLength(0);
    await startRead();

    expect(within(screen.getByRole('dialog')).getByText('正在读取第 1 页')).toBeTruthy();
    expect(JSON.parse(backend.posts()[0][1].body as string)).toEqual({
      action: 'read_playlist',
      payload: { key: '9001' },
    });
    expect(screen.getByRole('dialog', { name: '英语的歌曲明细' })).toBeTruthy();
    expect(backend.reads()).toBe(1);

    backend.save();
    backend.state.job = readJob('completed') as Job;
    await poll();
    expect(screen.getByText('新保存的歌曲')).toBeTruthy();
    expect(backend.reads()).toBe(2);
    await poll();
    expect(backend.reads()).toBe(2);
    expect(screen.getByRole('button', { name: '更新此歌单明细' })).toBeTruthy();
  });
  it('keeps account modifications frozen while allowing and completing an explicit readonly read', async () => {
    const backend = prepare({ frozen: true });
    await open();
    await startRead();
    expect(within(screen.getByRole('dialog')).getByText('正在读取第 1 页')).toBeTruthy();

    backend.save();
    backend.state.job = readJob('completed') as Job;
    await poll();
    fireEvent.click(screen.getByRole('button', { name: '关闭歌曲明细' }));
    expect(screen.getByRole('button', { name: '整理现有歌单名称' }).hasAttribute('disabled')).toBe(
      true,
    );
    expect(screen.getByRole('button', { name: '创建歌手精选' }).hasAttribute('disabled')).toBe(
      true,
    );
  });
  it('pauses from the drawer without publishing or refreshing an unfinished read', async () => {
    const backend = prepare();
    await open();
    await startRead();
    expect(within(screen.getByRole('dialog')).getByText('正在读取第 1 页')).toBeTruthy();
    await act(async () => {
      fireEvent.click(
        within(screen.getByRole('region', { name: '歌单明细读取任务' })).getByRole('button', {
          name: '暂停任务',
        }),
      );
    });
    expect(screen.getByText('已暂停，本地明细保留原来的记录。')).toBeTruthy();
    expect(backend.reads()).toBe(1);
    expect(backend.fetcher.mock.calls.filter(([url]) => url === '/api/pause')).toHaveLength(1);
  });
  it.each(['failed', 'paused', 'unsaved', 'other-key', 'result-key', 'other-id', 'check'])(
    'does not refresh local details for an unrelated or unsaved completion: %s',
    async (reason) => {
      const backend = prepare();
      await open();
      await startRead();
      expect(within(screen.getByRole('dialog')).getByText('正在读取第 1 页')).toBeTruthy();
      const job = readJob('completed');
      if (reason === 'failed' || reason === 'paused') {
        job.status = reason;
        job.result!.status = reason;
      }
      if (reason === 'unsaved') job.result!.record_saved = false;
      if (reason === 'other-key') job.playlist_key = '9002';
      if (reason === 'result-key') job.result!.playlist_key = '9002';
      if (reason === 'other-id') job.id = 'different-read';
      if (reason === 'check') job.action = 'check';

      backend.save();
      backend.state.job = job as Job;
      await poll();
      expect(backend.reads()).toBe(1);
      expect(screen.queryByText('新保存的歌曲')).toBeNull();
      if (reason === 'unsaved') {
        expect(
          within(screen.getByRole('region', { name: '歌单明细读取任务' })).getByText(
            '新明细保存状态未确认，请查看本地记录后主动重试读取。',
          ),
        ).toBeTruthy();
        expect(screen.queryByText('明细尚未保存，本地保留原来的记录。')).toBeNull();
      }
    },
  );
  it('does not refresh or replay a historical read when merely opening its drawer', async () => {
    const backend = prepare({ oldJob: true, loaded: true });
    await open();

    await poll();
    expect(backend.reads()).toBe(1);
    expect(backend.posts()).toHaveLength(0);
  });
  it('keeps unknown POST acceptance blocked until a matching new target job is observed and never resends', async () => {
    const backend = prepare({ unknown: true, frozen: true });
    await open();

    await startRead();
    await act(async () => {
      await Promise.resolve();
    });
    expect(screen.getByRole('button', { name: '读取此歌单明细' }).hasAttribute('disabled')).toBe(
      true,
    );
    await startRead();
    expect(backend.posts()).toHaveLength(1);
    backend.save();
    backend.state.job = readJob('completed') as Job;
    await poll();
    expect(screen.getByText('新保存的歌曲')).toBeTruthy();
    expect(backend.reads()).toBe(2);
    fireEvent.click(screen.getByRole('button', { name: '关闭歌曲明细' }));
    expect(screen.getByRole('button', { name: '整理现有歌单名称' }).hasAttribute('disabled')).toBe(
      true,
    );
  });
  it('does not adopt an old/null or different-target job to release unknown acceptance', async () => {
    const backend = prepare({ unknown: true });
    await open();

    await startRead();
    await act(async () => {
      await Promise.resolve();
    });
    backend.state.job = null;
    await poll();
    expect(screen.getByRole('button', { name: '读取此歌单明细' }).hasAttribute('disabled')).toBe(
      true,
    );
    backend.state.job = {
      ...readJob('completed'),
      playlist_key: '9002',
    } as Job;
    await poll();
    expect(screen.getByRole('button', { name: '读取此歌单明细' }).hasAttribute('disabled')).toBe(
      true,
    );
    expect(backend.posts()).toHaveLength(1);
    expect(backend.reads()).toBe(1);
    backend.state.job = { ...readJob('completed'), action: 'check' };
    await poll();
    expect(screen.getByRole('button', { name: '读取此歌单明细' }).hasAttribute('disabled')).toBe(
      true,
    );
    expect(backend.reads()).toBe(1);
    expect(screen.getAllByRole('button', { name: '重新加载并核对' }).length).toBeGreaterThan(0);
  });
  it('shows an immediately paused accepted read inside the drawer without refreshing', async () => {
    const backend = prepare({ immediatePaused: true });
    await open();
    await startRead();
    expect(screen.getByText('已暂停，本地明细保留原来的记录。')).toBeTruthy();
    expect(backend.reads()).toBe(1);
  });
  it('blocks further account reads after the state connection is lost while preserving local browsing', async () => {
    const backend = prepare({ stateError: true });
    await open();
    await startRead();
    expect(screen.getByRole('button', { name: '读取此歌单明细' }).hasAttribute('disabled')).toBe(
      true,
    );
    expect(screen.getByText('本机服务尚未连接，请稍后再试。')).toBeTruthy();
    expect(backend.posts()).toHaveLength(1);
  });
  it('does not refresh a different selected key after the earlier read finishes', async () => {
    const backend = prepare();
    const request = {
      key: '9001',
      baseline_id: null,
      job_id: 'new-read',
      sequence: 1,
      acceptance: 'accepted' as const,
    };
    const view = render(
      <PlaylistDetails
        playlist={playlist as import('./types').Playlist}
        onClose={() => {}}
        onRead={async () => true}
      />,
    );
    await act(async () => {
      await Promise.resolve();
    });
    await act(async () => {
      fireEvent.click(screen.getByRole('button', { name: '读取此歌单明细' }));
    });
    view.rerender(
      <PlaylistDetails
        playlist={{ ...playlist, key: '9002', name: '轻音乐' } as import('./types').Playlist}
        onClose={() => {}}
        readIntent={request}
        job={readJob('running')}
        onRead={async () => true}
      />,
    );
    await act(async () => {
      await Promise.resolve();
    });
    backend.save();
    view.rerender(
      <PlaylistDetails
        playlist={{ ...playlist, key: '9002', name: '轻音乐' } as import('./types').Playlist}
        onClose={() => {}}
        readIntent={request}
        job={readJob('completed')}
        onRead={async () => true}
      />,
    );
    await act(async () => {
      await Promise.resolve();
    });
    expect(backend.reads()).toBe(2);
    expect(backend.posts()).toHaveLength(0);
  });
  it('does not refresh a closed or reopened drawer after a late accepted response', async () => {
    const backend = prepare({ deferred: true });
    await open();
    await startRead();
    fireEvent.click(screen.getByRole('button', { name: '关闭歌曲明细' }));
    await act(async () => {
      fireEvent.click(screen.getByRole('button', { name: '打开英语的歌曲明细' }));
    });
    expect(screen.getByText('尚未保存歌曲明细')).toBeTruthy();
    backend.save();
    backend.state.job = readJob('completed') as Job;
    await act(async () => {
      backend.release();
    });
    expect(backend.reads()).toBe(2);
    expect(screen.queryByText('新保存的歌曲')).toBeNull();
    expect(backend.posts()).toHaveLength(1);
  });
  it('refreshes a saved partial record while showing its member and metadata gaps', async () => {
    const backend = prepare();
    await open();
    await startRead();
    backend.save(true);
    backend.state.job = {
      ...readJob('partial'),
      result: {
        ...savedResult,
        status: 'partial',
        expected_count: 2,
        count: 1,
        missing_count: 1,
        missing_metadata_count: 1,
      },
    };
    await poll();
    expect(screen.getByText('已保存 1 / 2 首')).toBeTruthy();
    expect(screen.getByText('缺失 1 首')).toBeTruthy();
    expect(screen.getByText('1 首资料不完整')).toBeTruthy();
    expect(backend.reads()).toBe(2);
    expect(backend.posts()).toHaveLength(1);
  });
  it.each([
    { installed: false, configured: false, message: '请先在接入设置安装官方接入工具。' },
    { installed: true, configured: false, message: '请先在接入设置保存凭证。' },
    {
      installed: true,
      configured: true,
      authorized: false,
      message: '账号授权已失效，请先在接入设置重新扫码。',
    },
  ])(
    'explains missing connection prerequisites without sending an account read: $message',
    async ({ message, ...connection }) => {
      const backend = prepare(connection);
      await open();
      expect(screen.getByRole('button', { name: '读取此歌单明细' }).hasAttribute('disabled')).toBe(
        true,
      );
      expect(screen.getByText(message)).toBeTruthy();
      expect(backend.posts()).toHaveLength(0);
    },
  );
});

describe('single playlist public result fields', () => {
  it('retains only validated target keys and bounded read counters', () => {
    const backend = prepare();
    backend.state.job = readJob('completed') as Job;
    Object.assign(backend.state.job!, { original_playlist_id: 'SECRET' });
    Object.assign(backend.state.job!.result!, { owner: 'SECRET' });
    const job = normalizeState(backend.state).job!;
    expect(job.playlist_key).toBe('9001');
    expect(job.result).toMatchObject({
      playlist_key: '9001',
      expected_count: 1,
      count: 1,
      missing_count: 0,
      missing_metadata_count: 0,
    });
    expect(JSON.stringify(job)).not.toContain('SECRET');
    const invalid = {
      ...backend.state,
      job: {
        ...backend.state.job,
        playlist_key: '../9001',
        result: { ...savedResult, playlist_key: 'bad', count: -1 },
      },
    };
    expect(normalizeState(invalid).job?.playlist_key).toBeUndefined();
    expect(normalizeState(invalid).job?.result?.playlist_key).toBeUndefined();
  });
});
