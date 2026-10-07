import { useCallback, useEffect, useRef, useState } from 'react';
import type {
  Action,
  Category,
  Job,
  Playlist,
  PlaylistReadIntent,
  WorkbenchState,
  WriteRecovery,
} from './types';
import {
  ACTION_LABELS,
  closeWorkbench,
  fetchState,
  LocalApiError,
  pauseJob,
  postAction,
  readSession,
  readStartupLoginJob,
} from './api';
import { Icon } from './icons';
import { PreviewPanel } from './PreviewPanel';
import { ConnectionSteps } from './ConnectionSteps';
import { DiagnosticNotice, needsWriteInspection, recordNotSaved } from './DiagnosticNotice';
import { RecoveryNotice } from './RecoveryNotice';
import { PlaylistDetails } from './PlaylistDetails';
import { ClassificationPanel } from './ClassificationPanel';
import type { IconName } from './icons';

type Page = 'overview' | 'playlists' | 'classification' | 'history' | 'settings';
const NAV: { page: Page; label: string; icon: IconName }[] = [
  { page: 'overview', label: '概览', icon: 'home' },
  { page: 'playlists', label: '我的歌单', icon: 'music' },
  { page: 'classification', label: '分类成果', icon: 'file' },
  { page: 'history', label: '任务记录', icon: 'history' },
  { page: 'settings', label: '接入设置', icon: 'settings' },
];
const ACTIVE = new Set(['queued', 'running', 'pause_requested']);
const CHANGING_ACTIONS = new Set<Action>([
  'rename',
  'artists',
  'resume',
  'login',
  'save_credentials',
]);
type UnknownSubmission = {
  action: Action;
  baselineId: string | null;
  operation: WriteRecovery['operation'];
  generation: number;
};
type UnknownAuthorization = { baselineId: string | null; generation: number };
type AcceptedJob = { id: string; action: Action };
type Inspection = { job: Job; operation: WriteRecovery['operation'] };
function reviewOperation(action: Action, state: WorkbenchState | null): WriteRecovery['operation'] {
  if (action === 'rename' || action === 'reconcile_renames') return 'renames';
  if (action === 'artists') return 'artists';
  if (action === 'resume') {
    return state?.recovery?.status === 'review_required'
      ? state.recovery.operation
      : (state?.data.history?.operation ?? 'unknown');
  }
  return 'unknown';
}
function canRenewRenameAuthorization(
  state: WorkbenchState | null,
  pending: UnknownSubmission | null,
  inspections: Iterable<Inspection>,
  unknownAuthorization = false,
): boolean {
  if (
    unknownAuthorization ||
    !state?.connection.installed ||
    !state.connection.configured ||
    state.connection.authorized !== false ||
    state.update?.status === 'stopping' ||
    state.recovery?.status !== 'review_required' ||
    state.recovery.operation !== 'renames' ||
    state.recovery.can_reconcile !== true ||
    (state.job && ACTIVE.has(state.job.status))
  )
    return false;
  if (pending && pending.operation !== 'renames') return false;
  if (state.data.history?.status === 'uncertain' && state.data.history.operation !== 'renames')
    return false;
  if (
    state.job &&
    needsWriteInspection(state.job) &&
    reviewOperation(state.job.action, state) !== 'renames'
  )
    return false;
  return Array.from(inspections).every((inspection) => inspection.operation === 'renames');
}
const STATUS: Record<string, string> = {
  queued: '等待开始',
  running: '正在进行',
  pause_requested: '正在暂停',
  completed: '已完成',
  applied: '已完成',
  paused: '已暂停',
  partial: '部分完成',
  blocked: '已阻止',
  uncertain: '需要核对',
  failed: '未完成',
  error: '未完成',
  prepared: '清单已生成',
  authorized: '授权成功',
};
const CATEGORY: Record<Category, string> = {
  liked: '红心歌单',
  artist: '歌手精选',
  normal: '我的分类',
};
const UPDATE_MESSAGE = {
  busy: '有新版可用，当前任务完成后重新打开即可更新。',
  review_required: '上次操作结果待核对，新的账号修改和程序更新暂时暂停。',
  stopping: '正在更新，请从启动入口重新打开工作台。',
};
function currentPage(): Page {
  return NAV.find((item) => `#${item.page}` === window.location.hash)?.page ?? 'overview';
}
function dateText(value?: string | null): string {
  if (!value) return '更新时间未记录';
  const date = new Date(value);
  return Number.isFinite(date.valueOf())
    ? date.toLocaleString('zh-CN', {
        month: '2-digit',
        day: '2-digit',
        hour: '2-digit',
        minute: '2-digit',
        hour12: false,
      })
    : '更新时间未记录';
}
function numberText(value?: number | null): string {
  return typeof value === 'number' && Number.isFinite(value) ? value.toLocaleString('zh-CN') : '—';
}
function safeAuthorizationUrl(value: unknown): string | null {
  if (typeof value !== 'string' || value.length > 4096 || /[\u0000-\u0020\u007f\\]/.test(value))
    return null;
  try {
    const url = new URL(value);
    return url.protocol === 'https:' &&
      !url.username &&
      !url.password &&
      (!url.port || url.port === '443') &&
      (url.hostname === '163cn.tv' ||
        url.hostname === 'music.163.com' ||
        url.hostname.endsWith('.music.163.com'))
      ? value
      : null;
  } catch {
    return null;
  }
}
function safeQr(value: unknown): string | null {
  return typeof value === 'string' &&
    value.length <= 256 * 1024 &&
    /^iVBORw0KGgo[A-Za-z0-9+/=]*$/.test(value)
    ? value
    : null;
}
function validState(value: WorkbenchState): boolean {
  return (
    !!value &&
    typeof value === 'object' &&
    !!value.connection &&
    !!value.data &&
    Array.isArray(value.data.playlists)
  );
}

function Cover({ playlist, compact = false }: { playlist: Playlist; compact?: boolean }) {
  const tone =
    playlist.category === 'liked'
      ? 'liked'
      : `tone-${Array.from(playlist.key).reduce((sum, char) => sum + char.charCodeAt(0), 0) % 5}`;
  return (
    <div className={`playlist-cover ${tone} ${compact ? 'compact' : ''}`} aria-hidden="true">
      {playlist.category === 'liked' ? (
        <Icon name="heart" size={compact ? 23 : 52} />
      ) : (
        <>
          <div className="vinyl" />
          <span className="cover-letter">{playlist.name.trim().slice(0, 1)}</span>
        </>
      )}
      {!compact && (
        <span className="cover-label">
          {playlist.category === 'liked' ? 'MY FAVORITES' : 'PERSONAL COLLECTION'}
        </span>
      )}
    </div>
  );
}
function PlaylistCard({
  playlist,
  onOpen,
}: {
  playlist: Playlist;
  onOpen: (playlist: Playlist, trigger: HTMLButtonElement) => void;
}) {
  return (
    <article className="playlist-card">
      <Cover playlist={playlist} />
      <div className="playlist-info">
        <span className={`eyebrow category-${playlist.category}`}>
          {CATEGORY[playlist.category]}
        </span>
        <h3 title={playlist.name}>{playlist.name}</h3>
        <p>
          {numberText(playlist.track_count)} 首歌曲 <span>· 本地记录</span>
        </p>
        <button
          type="button"
          className="playlist-details-link"
          aria-label={`打开${playlist.name}的歌曲明细`}
          onClick={(event) => onOpen(playlist, event.currentTarget)}
        >
          查看歌曲 <Icon name="arrow" size={15} />
        </button>
      </div>
    </article>
  );
}
function EmptyState({
  title = '还没有歌单记录',
  text = '读取一次歌单清单，之后打开工作台即可浏览本地记录。',
  onReset,
}: {
  title?: string;
  text?: string;
  onReset?: () => void;
}) {
  return (
    <div className="empty-state">
      <div className="empty-icon">
        <Icon name="music" size={32} />
      </div>
      <h3>{title}</h3>
      <p>{text}</p>
      {onReset && (
        <button className="button secondary" onClick={onReset}>
          重置筛选
        </button>
      )}
    </div>
  );
}
function JobPanel({
  job,
  history,
  blocked,
  pauseRequested,
  onPause,
  onResume,
}: {
  job: Job | null;
  history: WorkbenchState['data']['history'];
  blocked: boolean;
  pauseRequested: boolean;
  onPause: () => void;
  onResume: () => void;
}) {
  const active = !!job && ACTIVE.has(job.status);
  const progress = job?.progress;
  const total =
    typeof progress?.total_count === 'number' && progress.total_count > 0
      ? progress.total_count
      : null;
  const completed = Math.max(0, progress?.completed_count ?? 0);
  const ratio = total ? Math.min(100, (completed / total) * 100) : 0;
  return (
    <section className={`task-panel ${active ? 'task-active' : ''}`} aria-label="当前任务">
      <div className="section-heading">
        <div>
          <span className="eyebrow">TASK CENTER</span>
          <h2>{active ? '任务正在进行' : job ? '本次任务' : '当前任务'}</h2>
        </div>
        <span className={`status-pill ${active ? 'working' : ''}`}>
          {job ? (recordNotSaved(job) ? '记录未保存' : (STATUS[job.status] ?? '已记录')) : '空闲'}
        </span>
      </div>
      {job ? (
        <>
          <h3 className="task-title">{job.label || ACTION_LABELS[job.action]}</h3>
          <p className="task-stage" aria-live="polite">
            {active
              ? progress?.label || '等待任务状态'
              : job.result?.message ||
                (job.status === 'paused'
                  ? '已暂停，已确认的进度已保留。'
                  : `${STATUS[job.status] ?? '任务已结束'}，可查看任务记录。`)}
          </p>
          <div className={`task-meter ${active && !total ? 'indeterminate' : ''}`}>
            <span style={{ width: total ? `${ratio}%` : active ? '32%' : '100%' }} />
          </div>
          <div className="task-numbers">
            <span>{`${progress?.stage === 'classification_preflight' ? '只读核验 · ' : ''}${total ? `${completed} / ${total} 项` : '进度已记录'}`}</span>
            <span>{Math.floor(progress?.elapsed_seconds ?? 0)} 秒</span>
          </div>
          {job.result?.performance && (
            <p className="subtle small">
              接口调用 {numberText(job.result.performance.call_count)} 次 ·{' '}
              {numberText(job.result.performance.elapsed_seconds)} 秒
            </p>
          )}
        </>
      ) : (
        <div className="idle-task">
          <Icon name="check" size={25} />
          <p>
            由你决定何时开始。
            <br />
            <span>浏览本地歌单不会触发账号整理。</span>
          </p>
        </div>
      )}
      <div className="task-panel-actions">
        {active ? (
          <button
            className="button secondary"
            onClick={onPause}
            disabled={pauseRequested || job?.status === 'pause_requested'}
          >
            <Icon name="pause" />
            {pauseRequested || job?.status === 'pause_requested' ? '正在暂停…' : '暂停任务'}
          </button>
        ) : (
          <button
            className="button secondary"
            onClick={onResume}
            disabled={blocked || !history?.resumable || history.operation === 'classification'}
          >
            <Icon name="play" />
            继续上次任务
          </button>
        )}
        <span>
          {active
            ? '当前请求完成并核对后停止'
            : history?.operation === 'classification'
              ? '分类任务需专用核验后续做，已确认批次会保留'
              : history?.resumable
                ? '接着已确认的进度继续'
                : '没有可继续的任务'}
        </span>
      </div>
    </section>
  );
}

export default function App() {
  const [page, setPage] = useState<Page>(currentPage);
  const [classificationVisited, setClassificationVisited] = useState(() => currentPage() === 'classification');
  const [state, setState] = useState<WorkbenchState | null>(null);
  const [connected, setConnected] = useState(false);
  const [error, setError] = useState('');
  const [connectionError, setConnectionError] = useState('');
  const [notice, setNotice] = useState('');
  const [submitting, setSubmitting] = useState(false);
  const [acceptedJob, setAcceptedJob] = useState<AcceptedJob | null>(null);
  const [pauseRequested, setPauseRequested] = useState(false);
  const [unknownSubmission, setUnknownSubmission] = useState<UnknownSubmission | null>(null);
  const [unknownAuthorization, setUnknownAuthorization] = useState<UnknownAuthorization | null>(
    null,
  );
  const [inspectionJob, setInspectionJob] = useState<Job | null>(null);
  const [previewMode, setPreviewMode] = useState<'names' | 'full'>('names');
  const [query, setQuery] = useState('');
  const [category, setCategory] = useState<'all' | Category>('all');
  const [selectedPlaylist, setSelectedPlaylist] = useState<Playlist | null>(null);
  const [playlistReadIntent, setPlaylistReadIntent] = useState<PlaylistReadIntent | null>(null);
  const playlistReadIntentRef = useRef<PlaylistReadIntent | null>(null);
  const detailTrigger = useRef<HTMLButtonElement | null>(null);
  const [appId, setAppId] = useState('');
  const [keyFile, setKeyFile] = useState<File | null>(null);
  const [saving, setSaving] = useState(false);
  const [acceptPublic, setAcceptPublic] = useState(false);
  const [authorization, setAuthorization] = useState<{
    url: string;
    qr: string | null;
    startedAt: number;
  } | null>(null);
  const session = useRef('');
  const requestInFlight = useRef(false);
  const acceptedJobRef = useRef<AcceptedJob | null>(null);
  const unknownSubmissionRef = useRef<UnknownSubmission | null>(null);
  const unknownAuthorizationRef = useRef<UnknownAuthorization | null>(null);
  const inspectionJobRef = useRef<Job | null>(null);
  const inspections = useRef(new Map<string, Inspection>());
  const keyInput = useRef<HTMLInputElement>(null);
  const searchInput = useRef<HTMLInputElement>(null);
  const mainContent = useRef<HTMLElement>(null);
  const previousPage = useRef(page);
  const lastAuthorizationJob = useRef('');
  const loginIntentJob = useRef('');
  const closePauseRequested = useRef(false);
  const stateRef = useRef<WorkbenchState | null>(null);
  const stateFingerprint = useRef('');
  const stateRequestSequence = useRef(0);
  const stateRead = useRef<{
    sequence: number;
    controller: AbortController;
    promise: Promise<void>;
  } | null>(null);
  const actionGeneration = useRef(0);
  const actionIntentJob = useRef<{ id: string; action: Action; generation: number } | null>(null);
  const probeInFlight = useRef(false);
  const active = !!state?.job && ACTIVE.has(state.job.status);
  const readAcceptanceUnknown = playlistReadIntent?.acceptance === 'unknown';
  const blocked = active || submitting || !!acceptedJob || saving || !connected || readAcceptanceUnknown;
  const updateReadIntent = useCallback((intent: PlaylistReadIntent | null) => {
    playlistReadIntentRef.current = intent;
    setPlaylistReadIntent(intent);
  }, []);
  const writesFrozen =
    !!unknownSubmission ||
    !!unknownAuthorization ||
    !!inspectionJob ||
    needsWriteInspection(state?.job) ||
    state?.update?.status === 'review_required' ||
    state?.recovery?.status === 'review_required';
  const writeBlocked = blocked || writesFrozen;
  const cliInstalled = state?.connection.installed === true;
  const connectionReady = cliInstalled && state?.connection.configured === true;
  const playlistReadBlockedReason = !connected
    ? '本机服务尚未连接，请稍后再试。'
    : !cliInstalled
      ? '请先在接入设置安装官方接入工具。'
      : !connectionReady
        ? '请先在接入设置保存凭证。'
        : state?.connection.authorized === false
          ? '账号授权已失效，请先在接入设置重新扫码。'
          : readAcceptanceUnknown
            ? '读取任务是否已接受尚未确认，请等待状态核对，勿重复提交。'
            : blocked
              ? '请等待当前任务结束，或先暂停任务。'
              : undefined;
  const canRenewForReconcile = canRenewRenameAuthorization(
    state,
    unknownSubmission,
    inspections.current.values(),
    !!unknownAuthorization,
  );
  const loginBlocked = blocked || !connectionReady || (writesFrozen && !canRenewForReconcile);
  const setupNeeded = !!state && !connectionReady;
  const appIdValid = /^[A-Za-z0-9_-]{1,128}$/.test(appId);
  const appIdHelp = 'App ID 仅允许 1–128 位字母、数字、下划线或短横线，不能包含空格。';
  const playlists = state?.data.playlists ?? [];
  const openDetails = (playlist: Playlist, trigger: HTMLButtonElement) => {
    detailTrigger.current = trigger;
    setSelectedPlaylist(playlist);
  };
  const history = state?.data.history ?? null;
  const artistCheckpoint =
    history?.operation === 'artists' &&
    (history.resumable || ['paused', 'partial', 'uncertain'].includes(history.status));
  const authorized = state?.connection.authorized;
  const updateStatus = state?.update?.status ?? 'none';
  const visibleError = error || connectionError;
  const sessionError = [connectionError, error].find((message) =>
    message.startsWith('页面会话已更新'),
  );
  const queryKey = query.normalize('NFKC').trim().toLocaleLowerCase();
  const filtered = playlists.filter(
    (playlist) =>
      (category === 'all' || playlist.category === category) &&
      playlist.name.normalize('NFKC').toLocaleLowerCase().includes(queryKey),
  );

  useEffect(() => {
    const handler = () => setPage(currentPage());
    window.addEventListener('hashchange', handler);
    return () => window.removeEventListener('hashchange', handler);
  }, []);
  useEffect(() => {
    if (page === 'classification') setClassificationVisited(true);
  }, [page]);
  useEffect(() => {
    if (previousPage.current === page) return;
    previousPage.current = page;
    mainContent.current?.focus({ preventScroll: true });
    mainContent.current?.scrollIntoView?.({ block: 'start' });
  }, [page]);
  const cancelStateRead = useCallback(() => {
    stateRequestSequence.current += 1;
    stateRead.current?.controller.abort();
    stateRead.current = null;
  }, []);
  const refreshState = useCallback((): Promise<void> => {
    if (!session.current) return Promise.resolve();
    // Polling, manual refresh, and an accepted action share one current read.
    // An action boundary explicitly cancels reads started before that action.
    if (stateRead.current) return stateRead.current.promise;
    const sequence = ++stateRequestSequence.current;
    const controller = new AbortController();
    const promise = (async () => {
      try {
        const value = await fetchState(session.current, controller.signal);
        if (sequence !== stateRequestSequence.current) return;
        if (!validState(value)) throw new LocalApiError('本地状态暂不可用，请检查服务后刷新页面。');
        stateRef.current = value;
        // Idle polls usually repeat the same normalized JSON snapshot. Avoid
        // repainting every card and song row when nothing observable changed.
        const fingerprint = JSON.stringify(value);
        if (fingerprint !== stateFingerprint.current) {
          stateFingerprint.current = fingerprint;
          setState(value);
        }
        setConnected(true);
        setConnectionError('');
        const accepted = acceptedJobRef.current;
        if (accepted && value.job?.id === accepted.id && value.job.action === accepted.action) {
          acceptedJobRef.current = null;
          setAcceptedJob(null);
        }
        if (!value.job || !ACTIVE.has(value.job.status)) setPauseRequested(false);
      } catch (cause) {
        if (sequence !== stateRequestSequence.current) return;
        setConnected(false);
        setConnectionError(
          cause instanceof LocalApiError ? cause.message : '本地状态暂不可用，请重新打开工作台。',
        );
      } finally {
        if (stateRead.current?.sequence === sequence) stateRead.current = null;
      }
    })();
    stateRead.current = { sequence, controller, promise };
    return promise;
  }, []);
  useEffect(() => {
    let disposed = false;
    let polling = false;
    try {
      session.current = readSession();
      loginIntentJob.current = readStartupLoginJob();
    } catch (cause) {
      setConnectionError((cause as Error).message);
      return;
    }
    const poll = async () => {
      if (disposed || polling) return;
      polling = true;
      await refreshState();
      polling = false;
    };
    void poll();
    const timer = window.setInterval(() => {
      void poll();
    }, 1000);
    return () => {
      disposed = true;
      session.current = '';
      actionGeneration.current += 1;
      cancelStateRead();
      window.clearInterval(timer);
    };
  }, [refreshState, cancelStateRead]);
  useEffect(() => {
    const close = () => {
      if (!session.current || closePauseRequested.current) return;
      closePauseRequested.current = true;
      void closeWorkbench(session.current).catch(() => undefined);
    };
    window.addEventListener('pagehide', close);
    window.addEventListener('beforeunload', close);
    return () => {
      window.removeEventListener('pagehide', close);
      window.removeEventListener('beforeunload', close);
    };
  }, []);
  const runAction = useCallback(
    async (action: Action, payload: unknown = {}) => {
      if (requestInFlight.current || acceptedJobRef.current || !session.current || !connected || active) return false;
      if (playlistReadIntentRef.current?.acceptance === 'unknown') return false;
      if (action === 'resume' && stateRef.current?.data.history?.operation === 'classification')
        return false;
      const readKey =
        action === 'read_playlist' &&
        payload &&
        typeof payload === 'object' &&
        'key' in payload &&
        typeof payload.key === 'string'
          ? payload.key
          : null;
      if (
        action === 'read_playlist' &&
        (!readKey ||
          !/^[1-9][0-9]{0,19}$/.test(readKey) ||
          stateRef.current?.connection.authorized === false)
      )
        return false;
      const renewingAuthorization =
        action === 'login' &&
        canRenewRenameAuthorization(
          stateRef.current,
          unknownSubmissionRef.current,
          inspections.current.values(),
          !!unknownAuthorizationRef.current,
        );
      if (
        CHANGING_ACTIONS.has(action) &&
        !renewingAuthorization &&
        (unknownSubmissionRef.current ||
          unknownAuthorizationRef.current ||
          inspectionJobRef.current ||
          stateRef.current?.update?.status === 'review_required' ||
          stateRef.current?.recovery?.status === 'review_required' ||
          needsWriteInspection(stateRef.current?.job))
      )
        return false;
      if (!cliInstalled && action !== 'check') return false;
      if (!connectionReady && action !== 'check' && action !== 'save_credentials') return false;
      if (
        action === 'reconcile_renames' &&
        !(
          stateRef.current?.recovery?.status === 'review_required' &&
          stateRef.current.recovery.operation === 'renames' &&
          stateRef.current.recovery.can_reconcile
        )
      )
        return false;
      requestInFlight.current = true;
      actionGeneration.current += 1;
      cancelStateRead();
      const generation = actionGeneration.current;
      const baselineId = stateRef.current?.job?.id ?? null;
      const operation = reviewOperation(action, stateRef.current);
      if (readKey)
        updateReadIntent({
          key: readKey,
          baseline_id: baselineId,
          job_id: null,
          sequence: generation,
          acceptance: 'sending',
        });
      actionIntentJob.current = null;
      if (action === 'login') {
        setAuthorization(null);
        loginIntentJob.current = '';
      }
      setSubmitting(true);
      setNotice('');
      setError('');
      try {
        const reply = await postAction(session.current, action, payload);
        if (generation !== actionGeneration.current) return reply.accepted === true;
        if (!reply.accepted)
          throw new LocalApiError(reply.message || '本次任务未被接受，请查看任务记录。');
        const observedJob = stateRef.current?.job;
        if (reply.job_id && (observedJob?.id !== reply.job_id || observedJob?.action !== action)) {
          const accepted = { id: reply.job_id, action };
          acceptedJobRef.current = accepted;
          setAcceptedJob(accepted);
        }
        if (action === 'login') loginIntentJob.current = reply.job_id || '';
        if (readKey)
          updateReadIntent({
            key: readKey,
            baseline_id: baselineId,
            job_id: reply.job_id || null,
            sequence: generation,
            acceptance: 'accepted',
          });
        if (reply.job_id) {
          actionIntentJob.current = {
            id: reply.job_id,
            action,
            generation: actionGeneration.current,
          };
        }
        // A poll may have observed completion before the POST response bound
        // this intent. Publish the next snapshot once so its result is consumed.
        stateFingerprint.current = '';
        setNotice(`已开始${ACTION_LABELS[action]}。`);
        await refreshState();
        return true;
      } catch (cause) {
        if (generation !== actionGeneration.current) return false;
        // Lost acceptance can also arrive after the completed job was polled.
        if (cause instanceof LocalApiError && cause.acceptanceUnknown)
          stateFingerprint.current = '';
        if (readKey) {
          const known = playlistReadIntentRef.current?.job_id;
          updateReadIntent(
            cause instanceof LocalApiError && cause.acceptanceUnknown
              ? {
                  key: readKey,
                  baseline_id: baselineId,
                  job_id: known || null,
                  sequence: generation,
                  acceptance: known ? 'accepted' : 'unknown',
                }
              : null,
          );
        }
        if (renewingAuthorization && cause instanceof LocalApiError && cause.acceptanceUnknown) {
          unknownAuthorizationRef.current = { baselineId, generation };
          setUnknownAuthorization({ baselineId, generation });
        }
        if (
          cause instanceof LocalApiError &&
          cause.acceptanceUnknown &&
          action !== 'read_playlist' &&
          !unknownSubmissionRef.current
        ) {
          const pending = { action, baselineId, operation, generation };
          unknownSubmissionRef.current = pending;
          setUnknownSubmission(pending);
        }
        setError(
          cause instanceof LocalApiError ? cause.message : '任务未能开始，请查看当前任务后再操作。',
        );
        return false;
      } finally {
        requestInFlight.current = false;
        setSubmitting(false);
      }
    },
    [connected, active, cliInstalled, connectionReady, refreshState, cancelStateRead, updateReadIntent],
  );
  const requestPause = async () => {
    if (pauseRequested || (!active && !authorization)) return;
    const waitingOnly = !!authorization && !active;
    setAuthorization(null);
    loginIntentJob.current = '';
    actionGeneration.current += 1;
    cancelStateRead();
    const job = stateRef.current?.job;
    actionIntentJob.current =
      job && ACTIVE.has(job.status)
        ? { id: job.id, action: job.action, generation: actionGeneration.current }
        : null;
    setPauseRequested(true);
    try {
      await pauseJob(session.current);
      setNotice(waitingOnly ? '已停止等待扫码。' : '已请求暂停，当前请求完成并核对后停止。');
      await refreshState();
    } catch (cause) {
      setPauseRequested(false);
      setError(
        cause instanceof LocalApiError ? cause.message : '暂停请求未确认，请查看当前任务状态。',
      );
    }
  };
  const saveCredentials = async (event: React.FormEvent) => {
    event.preventDefault();
    if (writeBlocked || !cliInstalled || !keyFile) return;
    if (!appIdValid) {
      setError(appIdHelp);
      return;
    }
    setAuthorization(null);
    loginIntentJob.current = '';
    actionGeneration.current += 1;
    cancelStateRead();
    actionIntentJob.current = null;
    if (keyFile.size > 60 * 1024) {
      setError('私钥文件不能超过 60 KiB。');
      return;
    }
    setSaving(true);
    try {
      const raw = new Uint8Array(await keyFile.arrayBuffer());
      const key = new TextDecoder('utf-8', { fatal: true })
        .decode(raw)
        .replace(/^\uFEFF/, '')
        .trim();
      if (!key) throw new LocalApiError('私钥文件为空，请重新选择。');
      const id = appId;
      setAppId('');
      setKeyFile(null);
      if (keyInput.current) keyInput.current.value = '';
      await runAction('save_credentials', { app_id: id, private_key: key });
    } catch (cause) {
      setError(cause instanceof LocalApiError ? cause.message : '请使用 UTF-8 编码的私钥文件。');
    } finally {
      setSaving(false);
    }
  };

  const showInspection = useCallback(() => {
    const first = inspections.current.values().next().value?.job ?? null;
    inspectionJobRef.current = first;
    setInspectionJob(first);
  }, []);

  useEffect(() => {
    const intent = playlistReadIntentRef.current;
    const job = state?.job;
    if (
      !intent ||
      !job ||
      job.action !== 'read_playlist' ||
      job.playlist_key !== intent.key ||
      job.id === intent.baseline_id ||
      (intent.job_id && job.id !== intent.job_id)
    )
      return;
    if (intent.acceptance !== 'accepted' || !intent.job_id) {
      updateReadIntent({ ...intent, job_id: job.id, acceptance: 'accepted' });
      actionIntentJob.current = {
        id: job.id,
        action: 'read_playlist',
        generation: actionGeneration.current,
      };
    }
  }, [state?.job, playlistReadIntent, updateReadIntent]);

  useEffect(() => {
    const job = state?.job;
    if (!job) return;
    if (needsWriteInspection(job)) {
      // Keep every observed unresolved operation; a later read cannot hide a mixed reason.
      const operation =
        inspections.current.get(job.id)?.operation ?? reviewOperation(job.action, state);
      inspections.current.set(job.id, { job, operation });
    } else inspections.current.delete(job.id);
    showInspection();
    const pendingAuthorization = unknownAuthorizationRef.current;
    if (
      pendingAuthorization &&
      job.id !== pendingAuthorization.baselineId &&
      job.action === 'login'
    ) {
      if (pendingAuthorization.generation === actionGeneration.current)
        loginIntentJob.current = job.id;
      actionIntentJob.current = {
        id: job.id,
        action: 'login',
        generation: actionGeneration.current,
      };
      if (!ACTIVE.has(job.status)) {
        unknownAuthorizationRef.current = null;
        setUnknownAuthorization(null);
      }
    }
    const pending = unknownSubmissionRef.current;
    if (!pending || job.id === pending.baselineId || job.action !== pending.action) return;
    if (pending.action === 'login' && pending.generation === actionGeneration.current)
      loginIntentJob.current = job.id;
    actionIntentJob.current = {
      id: job.id,
      action: pending.action,
      generation: actionGeneration.current,
    };
    if (!ACTIVE.has(job.status)) {
      unknownSubmissionRef.current = null;
      setUnknownSubmission(null);
    }
  }, [state?.job, unknownSubmission, unknownAuthorization, showInspection]);

  useEffect(() => {
    const job = state?.job;
    const intent = actionIntentJob.current;
    if (
      !job ||
      ACTIVE.has(job.status) ||
      !intent ||
      intent.id !== job.id ||
      intent.generation !== actionGeneration.current
    )
      return;
    actionIntentJob.current = null;
    const status = job.result?.status || job.status;
    if (
      intent.action === 'reconcile_renames' &&
      job.action === 'reconcile_renames' &&
      job.status === 'completed' &&
      status === 'completed' &&
      job.result?.outcome_known === true &&
      job.result.record_saved === true &&
      state?.recovery?.status === 'clear'
    ) {
      for (const [id, inspection] of inspections.current) {
        if (inspection.operation === 'renames') inspections.current.delete(id);
      }
      showInspection();
      if (unknownSubmissionRef.current?.operation === 'renames') {
        unknownSubmissionRef.current = null;
        setUnknownSubmission(null);
      }
    }
    if (recordNotSaved(job)) {
      setNotice('');
      setError('本地记录未保存，请勿重复提交。');
    } else if (['failed', 'error', 'blocked', 'uncertain'].includes(status)) {
      setNotice('');
      setError(job.result?.message || `${ACTION_LABELS[intent.action]}未完成，请查看任务记录。`);
    } else {
      setError('');
      setNotice(
        job.result?.message ||
          (status === 'paused'
            ? '已暂停，已确认的进度已保留。'
            : `${ACTION_LABELS[intent.action]}${STATUS[status] ?? '已结束'}。`),
      );
    }
  }, [state?.job, showInspection]);

  useEffect(() => {
    const job = state?.job;
    if (!job?.result || ACTIVE.has(job.status)) return;
    if (
      job.result.authorized === true ||
      ['authorized', 'authorization_expired'].includes(job.result.status ?? '')
    ) {
      setAuthorization(null);
      loginIntentJob.current = '';
      return;
    }
    if (
      job.action !== 'login' ||
      loginIntentJob.current !== job.id ||
      lastAuthorizationJob.current === job.id
    )
      return;
    lastAuthorizationJob.current = job.id;
    const url = safeAuthorizationUrl(job.result.url);
    if (url) setAuthorization({ url, qr: safeQr(job.result.qr_png_base64), startedAt: Date.now() });
  }, [state?.job]);
  useEffect(() => {
    if (!authorization) return;
    const timer = window.setInterval(async () => {
      if (Date.now() - authorization.startedAt >= 300000) {
        setAuthorization(null);
        setNotice('二维码等待已结束，请手动重新获取。');
        return;
      }
      if (
        !connected ||
        probeInFlight.current ||
        requestInFlight.current ||
        acceptedJobRef.current ||
        (stateRef.current?.job && ACTIVE.has(stateRef.current.job.status))
      )
        return;
      probeInFlight.current = true;
      const generation = actionGeneration.current;
      try {
        await postAction(session.current, 'authorization_probe');
        if (generation !== actionGeneration.current) return;
        await refreshState();
      } catch {
        /* A probe never restarts login or retries any account mutation. */
      } finally {
        probeInFlight.current = false;
      }
    }, 2000);
    return () => window.clearInterval(timer);
  }, [authorization, connected, refreshState]);

  const preview = () => {
    void runAction(previewMode === 'names' ? 'preview_names' : 'preview_full');
  };
  const taskPanel = (
    <JobPanel
      job={state?.job ?? null}
      history={history}
      blocked={writeBlocked || !connectionReady}
      pauseRequested={pauseRequested}
      onPause={() => {
        void requestPause();
      }}
      onResume={() => {
        void runAction('resume');
      }}
    />
  );
  return (
    <div className="app-shell">
      <a
        className="skip-link"
        href="#main"
        onClick={(event) => {
          event.preventDefault();
          mainContent.current?.focus();
        }}
      >
        跳到主要内容
      </a>
      <aside className="sidebar">
        <a className="brand" href="#overview">
          <span className="brand-symbol">
            <Icon name="headphones" size={25} />
          </span>
          <span>
            网易云<span className="brand-subtitle">歌单工作台</span>
          </span>
        </a>
        <div className="nav-label">音乐资料库</div>
        <nav aria-label="主导航">
          {NAV.map((item) => (
            <a
              key={item.page}
              href={`#${item.page}`}
              onClick={() => setPage(item.page)}
              className={`nav-item ${page === item.page ? 'active' : ''}`}
              aria-current={page === item.page ? 'page' : undefined}
            >
              <Icon name={item.icon} />
              <span>{item.label}</span>
              {item.page === 'playlists' && playlists.length > 0 && (
                <span className="nav-count">{playlists.length}</span>
              )}
            </a>
          ))}
        </nav>
        <div className="sidebar-bottom">
          <div className="local-badge">
            <Icon name="shield" size={17} />
            <span>在你的电脑上运行</span>
          </div>
          <p>本地浏览 · 手动开始任务</p>
        </div>
      </aside>
      <div
        className={`workspace ${active && (page === 'playlists' || page === 'settings' || page === 'classification') ? 'with-task-strip' : ''}`}
      >
        <header className="topbar">
          <div className="breadcrumb">
            音乐资料库 <span>/</span> {NAV.find((item) => item.page === page)?.label}
          </div>
          <div className="topbar-account">
            <span className={`connection-dot ${connected ? 'online' : ''}`} />
            <span>{connected ? '本机服务已连接' : state ? '连接已中断' : '连接中'}</span>
            <div className="avatar" aria-hidden="true">
              {state?.data.account?.nickname?.slice(0, 1) || <Icon name="music" size={16} />}
            </div>
            <span className="account-name">{state?.data.account?.nickname || '本地工作台'}</span>
          </div>
        </header>
        <main
          ref={mainContent}
          id="main"
          tabIndex={-1}
          aria-labelledby="page-title"
          className={`main-content page-${page}`}
        >
          {updateStatus !== 'none' && (
            <section className={`update-banner update-${updateStatus}`} aria-label="程序更新状态">
              <Icon name={updateStatus === 'review_required' ? 'shield' : 'refresh'} size={19} />
              <p aria-live="polite" aria-atomic="true">
                {UPDATE_MESSAGE[updateStatus]}
              </p>
            </section>
          )}
          {visibleError && (
            <div className="message-banner error" role="alert">
              <span>
                {visibleError}
                {sessionError && sessionError !== visibleError && (
                  <>
                    <br />
                    {sessionError}
                  </>
                )}
              </span>
              {sessionError ? (
                <button className="button secondary" onClick={() => window.location.reload()}>
                  刷新工作台
                </button>
              ) : (
                !connected &&
                !!session.current && (
                  <button
                    className="button secondary"
                    onClick={() => {
                      void refreshState();
                    }}
                  >
                    重新连接本机服务
                  </button>
                )
              )}
              <button
                className="icon-button"
                aria-label="关闭提示"
                onClick={() => {
                  setError('');
                  setConnectionError('');
                }}
              >
                <Icon name="close" size={17} />
              </button>
            </div>
          )}
          {(unknownSubmission || readAcceptanceUnknown || acceptedJob) && (
            <section className="diagnostic-notice" aria-label="提交结果待核对">
              <div>
                <h2>{acceptedJob ? '任务已接受，正在核对状态' : '提交结果待核对'}</h2>
                {acceptedJob ? (
                  <p>“{ACTION_LABELS[acceptedJob.action]}”已接受，新任务状态尚未到达，请勿重复提交。</p>
                ) : (
                  <p>
                    后台可能已接受“{ACTION_LABELS[unknownSubmission?.action ?? 'read_playlist']}
                    ”。新的账号修改已暂停，请先核对任务状态。
                  </p>
                )}
                <p className="subtle">旧记录或空记录无法确认这次提交是否执行。这里不会自动重试。</p>
              </div>
              <div className="diagnostic-actions">
                <button
                  className="button secondary"
                  onClick={() => {
                    void refreshState();
                  }}
                >
                  刷新任务状态
                </button>
                <button className="button quiet" onClick={() => window.location.reload()}>
                  重新加载并核对
                </button>
              </div>
            </section>
          )}
          <RecoveryNotice
            recovery={state?.recovery}
            blocked={blocked || !connectionReady}
            onReconcile={() => {
              void runAction('reconcile_renames');
            }}
            onRecords={() => setPage('history')}
            canRenewAuthorization={canRenewForReconcile}
            onRenewAuthorization={() => {
              setPage('settings');
              window.location.hash = 'settings';
              void runAction('login');
            }}
          />
          <DiagnosticNotice
            job={inspectionJob || state?.job || null}
            onNavigate={setPage}
            readBlocked={blocked || !connectionReady}
            onReadLocal={() => {
              void runAction('local_plan');
            }}
          />
          {notice && !visibleError && !writesFrozen && (
            <div className="message-banner notice" role="status">
              <Icon name="check" size={17} />
              <span>{notice}</span>
              <button className="icon-button" aria-label="关闭提示" onClick={() => setNotice('')}>
                <Icon name="close" size={17} />
              </button>
            </div>
          )}
          {page === 'overview' && (
            <>
              <section className="hero">
                <div className="hero-copy">
                  <span className="eyebrow">YOUR MUSIC, IN ORDER</span>
                  <h1 id="page-title">
                    让每一份喜欢，
                    <br />
                    都有自己的位置。
                  </h1>
                  <p>
                    {setupNeeded
                      ? cliInstalled
                        ? '先保存开放平台凭证，再按需读取与整理歌单。'
                        : '先完成本机 CLI 接入，就可以按自己的节奏读取与整理。'
                      : '沿用熟悉的分类，把歌单整理成你喜欢的样子。'}
                  </p>
                  <div className="hero-actions">
                    {setupNeeded ? (
                      <a
                        className="button primary"
                        href="#settings"
                        onClick={() => setPage('settings')}
                      >
                        <Icon name="settings" size={17} />
                        前往接入设置
                      </a>
                    ) : (
                      <button
                        className="button primary"
                        disabled={blocked || !connectionReady}
                        onClick={preview}
                      >
                        <Icon name="refresh" size={17} />
                        更新歌单清单
                      </button>
                    )}
                    <a className="text-link" href="#playlists" onClick={() => setPage('playlists')}>
                      查看我的歌单 <Icon name="arrow" size={17} />
                    </a>
                    <a
                      className="text-link"
                      href="#classification"
                      onClick={() => setPage('classification')}
                    >
                      查看分类成果 <Icon name="arrow" size={17} />
                    </a>
                  </div>
                </div>
                <div className="hero-art" aria-hidden="true">
                  <div className="hero-disc">
                    <i />
                  </div>
                  <div className="hero-note">
                    <Icon name="music" size={52} />
                    <span>
                      FOR THE LOVE
                      <br />
                      OF MUSIC
                    </span>
                  </div>
                  <div className="hero-spark one">✦</div>
                  <div className="hero-spark two">✦</div>
                </div>
              </section>
              <div className="source-strip">
                <Icon name="file" size={15} />
                <span>
                  {state?.data.source === 'local_record'
                    ? '本地记录'
                    : state
                      ? '尚无本地记录'
                      : '正在读取本地资料'}
                </span>
                <span className="source-time">{dateText(state?.data.updated_at)}</span>
                <span className="source-hint">打开工作台不会自动整理账号</span>
              </div>
              <section className="stats-grid" aria-label="资料概览">
                <div className="stat-card">
                  <span>我的歌单</span>
                  <strong>
                    {state ? playlists.length : '—'}
                    <small>个</small>
                  </strong>
                  <Icon name="music" />
                </div>
                <div className="stat-card">
                  <span>喜欢的音乐</span>
                  <strong>
                    {numberText(playlists.find((item) => item.category === 'liked')?.track_count)}
                    <small>首</small>
                  </strong>
                  <Icon name="heart" />
                </div>
                <div className="stat-card">
                  <span>已核对任务</span>
                  <strong>
                    {numberText(history?.completed_count)}
                    <small>项</small>
                  </strong>
                  <Icon name="check" />
                </div>
              </section>
              {state && (
                <PreviewPanel
                  preview={state.data.preview}
                  artistsCompleted={state.data.artists_completed}
                />
              )}
              <div className="overview-grid">
                <section className="playlist-section">
                  <div className="section-heading">
                    <div>
                      <span className="eyebrow">YOUR COLLECTION</span>
                      <h2>最近的歌单记录</h2>
                    </div>
                    <a href="#playlists" className="text-link" onClick={() => setPage('playlists')}>
                      查看全部 <Icon name="arrow" size={16} />
                    </a>
                  </div>
                  {!state && connectionError ? (
                    <EmptyState
                      title="无法连接本机服务"
                      text="恢复连接后即可浏览歌单记录。请确认本机整理程序仍在运行。"
                    />
                  ) : !state ? (
                    <div className="skeleton-grid" aria-label="正在加载">
                      {[0, 1, 2].map((key) => (
                        <div className="skeleton-card" key={key} />
                      ))}
                    </div>
                  ) : playlists.length ? (
                    <div className="playlist-grid overview-cards">
                      {playlists.slice(0, 6).map((playlist) => (
                        <PlaylistCard key={playlist.key} playlist={playlist} onOpen={openDetails} />
                      ))}
                    </div>
                  ) : (
                    <EmptyState />
                  )}
                </section>
                <div className="right-rail">
                  {taskPanel}
                  <section className="quick-actions">
                    <span className="eyebrow">NEXT STEP</span>
                    <h2>按你的节奏整理</h2>
                    <fieldset className="preview-options">
                      <legend>读取范围</legend>
                      <label>
                        <input
                          type="radio"
                          name="preview"
                          checked={previewMode === 'names'}
                          onChange={() => setPreviewMode('names')}
                          disabled={blocked}
                        />
                        <span>
                          快速名称清单<small>只读取歌单目录，适合日常整理</small>
                        </span>
                      </label>
                      <label>
                        <input
                          type="radio"
                          name="preview"
                          checked={previewMode === 'full'}
                          onChange={() => setPreviewMode('full')}
                          disabled={blocked}
                        />
                        <span>
                          完整红心清单<small>读取歌曲明细，用于歌手精选</small>
                        </span>
                      </label>
                    </fieldset>
                    <button
                      className="button secondary full-width"
                      disabled={writeBlocked || !connectionReady}
                      onClick={() => {
                        void runAction('rename');
                      }}
                    >
                      整理现有歌单名称 <Icon name="arrow" size={17} />
                    </button>
                    <button
                      className="button quiet full-width"
                      disabled={
                        writeBlocked ||
                        !!state?.data.artists_completed ||
                        !!artistCheckpoint ||
                        !acceptPublic ||
                        !connectionReady
                      }
                      onClick={() => {
                        void runAction('artists', { accept_default_visibility: true });
                      }}
                    >
                      <Icon name={state?.data.artists_completed ? 'check' : 'music'} size={17} />
                      {state?.data.artists_completed
                        ? '歌手精选已完成'
                        : artistCheckpoint
                          ? '已有歌手精选任务'
                          : '创建歌手精选'}
                    </button>
                    {artistCheckpoint && !state?.data.artists_completed && (
                      <p className="subtle small">
                        {history?.status === 'uncertain'
                          ? '上次歌手精选结果尚未确认，请先查看任务记录；暂不开始新创建。'
                          : '已有未结束的歌手精选任务，请从任务面板继续，避免重复创建。'}
                      </p>
                    )}
                    {!state?.data.artists_completed && !artistCheckpoint && (
                      <label className="consent">
                        <input
                          type="checkbox"
                          checked={acceptPublic}
                          onChange={(event) => setAcceptPublic(event.target.checked)}
                          disabled={blocked}
                        />
                        接受歌手精选可能公开
                      </label>
                    )}
                  </section>
                </div>
              </div>
            </>
          )}
          {page === 'playlists' && (
            <>
              <div className="page-heading">
                <div>
                  <span className="eyebrow">YOUR COLLECTION</span>
                  <h1 id="page-title">我的歌单</h1>
                  <p>熟悉的分类，清晰的音乐资料。</p>
                </div>
                {setupNeeded ? (
                  <a
                    className="button primary"
                    href="#settings"
                    onClick={() => setPage('settings')}
                  >
                    <Icon name="settings" size={17} />
                    前往接入设置
                  </a>
                ) : (
                  <button
                    className="button primary"
                    disabled={blocked || !connectionReady}
                    onClick={preview}
                  >
                    <Icon name="refresh" size={17} />
                    更新清单
                  </button>
                )}
              </div>
              <div className="playlist-toolbar">
                <div className="filter-pills" role="group" aria-label="歌单分类">
                  {(['all', 'liked', 'artist', 'normal'] as const).map((item) => (
                    <button
                      key={item}
                      className={category === item ? 'selected' : ''}
                      aria-pressed={category === item}
                      onClick={() => setCategory(item)}
                    >
                      {item === 'all' ? '全部歌单' : CATEGORY[item]}
                    </button>
                  ))}
                </div>
                <label className="search-box">
                  <Icon name="search" size={18} />
                  <input
                    ref={searchInput}
                    value={query}
                    onChange={(event) => setQuery(event.target.value)}
                    placeholder="搜索歌单名称"
                    aria-label="搜索歌单名称"
                  />
                  {query && (
                    <button
                      className="icon-button"
                      aria-label="清除搜索"
                      onClick={() => {
                        setQuery('');
                        searchInput.current?.focus();
                      }}
                    >
                      <Icon name="close" size={16} />
                    </button>
                  )}
                </label>
              </div>
              {state?.data.preview && (
                <PreviewPanel
                  preview={state.data.preview}
                  artistsCompleted={state.data.artists_completed}
                />
              )}
              <div className="list-meta">
                <span aria-live="polite" aria-atomic="true">
                  显示 {filtered.length} / {playlists.length} 个歌单
                </span>
                <span>本地记录 · {dateText(state?.data.updated_at)}</span>
              </div>
              {filtered.length ? (
                <div className="playlist-grid all-playlists">
                  {filtered.map((playlist) => (
                    <PlaylistCard key={playlist.key} playlist={playlist} onOpen={openDetails} />
                  ))}
                </div>
              ) : (
                <EmptyState
                  title={playlists.length ? '没有匹配的歌单' : '还没有歌单记录'}
                  text={playlists.length ? '试试其他名称或分类。' : undefined}
                  onReset={
                    playlists.length
                      ? () => {
                          setQuery('');
                          setCategory('all');
                          searchInput.current?.focus();
                        }
                      : undefined
                  }
                />
              )}
            </>
          )}
          {(page === 'classification' || classificationVisited) && <ClassificationPanel active={page === 'classification'} />}
          {page === 'history' && (
            <>
              <div className="page-heading">
                <div>
                  <span className="eyebrow">TASK HISTORY</span>
                  <h1 id="page-title">任务记录</h1>
                  <p>每次改变，都有可以回看的记录。</p>
                </div>
                <button
                  className="button secondary"
                  disabled={
                    writeBlocked ||
                    !connectionReady ||
                    !history?.resumable ||
                    history.operation === 'classification'
                  }
                  onClick={() => {
                    void runAction('resume');
                  }}
                >
                  <Icon name="play" size={17} />
                  继续上次任务
                </button>
              </div>
              <div className="history-grid">
                <section className="history-card">
                  <div className="section-heading">
                    <div>
                      <span className="eyebrow">LOCAL RECEIPT</span>
                      <h2>上次执行记录</h2>
                    </div>
                    <span className="status-pill">
                      {history ? (STATUS[history.status] ?? '已记录') : '暂无记录'}
                    </span>
                  </div>
                  {history ? (
                    <>
                      <div className="history-summary">
                        <div className="history-complete">
                          <Icon name="check" size={25} />
                        </div>
                        <div>
                          <strong>已完成 {history.completed_count} 项</strong>
                          <p>
                            {history.operation === 'classification'
                              ? '分类任务 · 本地保存的核对结果'
                              : '本地保存的核对结果'}
                          </p>
                        </div>
                      </div>
                      <div className="history-items">
                        {history.items?.map((item, index) => (
                          <div className="history-row" key={`${item.name}-${index}`}>
                            <div className="history-row-icon">
                              <Icon name="music" size={18} />
                            </div>
                            <div>
                              <strong>{item.name}</strong>
                              <span>
                                {history.operation === 'classification'
                                  ? `已确认 ${numberText(item.added_count ?? item.count)}${typeof item.expected_count === 'number' ? ` / 计划 ${numberText(item.expected_count)}` : ''} 首`
                                  : typeof item.count === 'number'
                                    ? `${item.count} 首歌曲`
                                    : '名称整理记录'}
                              </span>
                            </div>
                            <span className="row-status">{STATUS[item.status] ?? '已记录'}</span>
                          </div>
                        ))}
                      </div>
                      {!history.items?.length && <p className="subtle">没有单项记录可展示。</p>}
                    </>
                  ) : (
                    <EmptyState
                      title="还没有执行记录"
                      text="完成或暂停一次整理任务后，核对结果会保存在这里。"
                    />
                  )}
                </section>
                <div className="right-rail">{taskPanel}</div>
              </div>
              {state?.job?.logs?.length ? (
                <section className="log-card">
                  <div className="section-heading">
                    <h2>本次运行记录</h2>
                    <span className="subtle small">仅展示公开状态</span>
                  </div>
                  <div className="log-list">
                    {state.job.logs.slice(-30).map((entry, index) => (
                      <div className={`log-row ${entry.level}`} key={`${entry.time}-${index}`}>
                        <time>{entry.time}</time>
                        <span>{entry.message}</span>
                      </div>
                    ))}
                  </div>
                </section>
              ) : null}
            </>
          )}
          {page === 'settings' && (
            <>
              <div className="page-heading">
                <div>
                  <span className="eyebrow">CONNECTION</span>
                  <h1 id="page-title">接入设置</h1>
                  <p>首次配置后，凭证由本机程序保存。</p>
                </div>
                <button
                  className="button secondary"
                  disabled={blocked}
                  onClick={() => {
                    void runAction('check');
                  }}
                >
                  <Icon name="refresh" size={17} />
                  检查本地接入
                </button>
              </div>
              {state && <ConnectionSteps connection={state.connection} />}
              <div className="settings-grid">
                <section className="settings-card">
                  <div className="section-heading">
                    <div>
                      <span className="eyebrow">OFFICIAL PLATFORM</span>
                      <h2>网易云开放平台</h2>
                    </div>
                    <span className="status-pill">
                      {state?.connection.configured ? '凭证已保存' : '尚未配置'}
                    </span>
                  </div>
                  <form
                    onSubmit={(event) => {
                      void saveCredentials(event);
                    }}
                  >
                    <label className="field-label" htmlFor="app-id">
                      App ID
                    </label>
                    <input
                      id="app-id"
                      type="password"
                      autoComplete="off"
                      maxLength={128}
                      aria-describedby="app-id-help"
                      aria-invalid={appId.length > 0 && !appIdValid}
                      value={appId}
                      onChange={(event) => setAppId(event.target.value)}
                      placeholder="输入应用的 App ID"
                      disabled={writeBlocked || !cliInstalled}
                    />
                    <p
                      id="app-id-help"
                      className={`app-id-help ${appId.length > 0 && !appIdValid ? 'invalid' : ''}`}
                    >
                      {appIdHelp}
                    </p>
                    <label className="field-label" htmlFor="private-key">
                      Private Key 文件
                    </label>
                    <label
                      className={`file-picker ${writeBlocked || !cliInstalled ? 'disabled' : ''}`}
                      htmlFor="private-key"
                    >
                      <Icon name="file" size={25} />
                      <span>
                        {keyFile?.name || '选择 UTF-8 私钥文件'}
                        <small>PEM / KEY / TXT · 最大 60 KiB</small>
                      </span>
                      <span className="file-picker-action">选择文件</span>
                    </label>
                    <input
                      className="visually-hidden"
                      id="private-key"
                      ref={keyInput}
                      type="file"
                      accept=".pem,.key,.txt"
                      disabled={writeBlocked || !cliInstalled}
                      onChange={(event) => setKeyFile(event.target.files?.[0] ?? null)}
                    />
                    <p className="subtle small">提交后输入会清空，私钥不会保存到浏览器。</p>
                    <button
                      className="button primary"
                      type="submit"
                      disabled={writeBlocked || !cliInstalled || !appIdValid || !keyFile}
                    >
                      {saving ? '正在保存…' : '保存接入凭证'}
                    </button>
                  </form>
                  <a
                    className="platform-link"
                    href="https://developer.music.163.com/st/developer/controlCenter/accountInfo"
                    target="_blank"
                    rel="noopener noreferrer"
                  >
                    打开官方开发者平台 <Icon name="arrow" size={16} />
                  </a>
                </section>
                <section className="settings-card authorization-card">
                  <div className="section-heading">
                    <div>
                      <span className="eyebrow">ACCOUNT ACCESS</span>
                      <h2>账号授权</h2>
                    </div>
                    <span className={`status-pill ${authorized ? 'success' : ''}`}>
                      {authorized === true
                        ? '本次已验证'
                        : authorized === false
                          ? '需要授权'
                          : '尚未在线验证'}
                    </span>
                  </div>
                  {authorization ? (
                    <div className="qr-section">
                      {authorization.qr ? (
                        <img
                          src={`data:image/png;base64,${authorization.qr}`}
                          alt="网易云官方账号授权二维码"
                        />
                      ) : (
                        <div className="empty-icon">
                          <Icon name="shield" size={35} />
                        </div>
                      )}
                      <h3>使用网易云 App 扫码</h3>
                      <p>确认后会自动检查授权，最多等待 5 分钟。</p>
                      <a
                        href={authorization.url}
                        target="_blank"
                        rel="noopener noreferrer"
                        className="text-link"
                      >
                        打开官方授权链接 <Icon name="arrow" size={16} />
                      </a>
                    </div>
                  ) : (
                    <div className="authorization-intro">
                      <div className="authorization-illustration">
                        <Icon name="shield" size={46} />
                      </div>
                      <h3>{authorized ? '可以按需读取与整理' : '连接你的网易云账号'}</h3>
                      <p>
                        需要账号功能时再扫码。
                        <br />
                        普通启动只读取本地记录。
                      </p>
                    </div>
                  )}
                  <div className="authorization-actions">
                    {authorization && (
                      <button
                        className="button secondary"
                        disabled={pauseRequested}
                        onClick={() => {
                          void requestPause();
                        }}
                      >
                        <Icon name="pause" size={16} />
                        停止等待扫码
                      </button>
                    )}
                    <button
                      className="button primary"
                      disabled={loginBlocked}
                      onClick={() => {
                        setAuthorization(null);
                        void runAction('login');
                      }}
                    >
                      {authorization
                        ? '重新获取二维码'
                        : canRenewForReconcile
                          ? '重新扫码后只读核对'
                          : '生成扫码授权'}
                    </button>
                    <button
                      className="button secondary"
                      disabled={blocked || !connectionReady}
                      onClick={() => {
                        void runAction('login_status');
                      }}
                    >
                      验证账号授权
                    </button>
                  </div>
                </section>
              </div>
            </>
          )}
        </main>
        {active &&
          state?.job &&
          (page === 'playlists' || page === 'settings' || page === 'classification') && (
            <section className="task-strip" aria-label="当前任务">
              <span className="task-strip-icon">
                <Icon name="refresh" size={19} />
              </span>
              <div className="task-strip-copy">
                <strong>{state.job.label}</strong>
                <p aria-live="polite">{state.job.progress?.label || '正在准备任务'}</p>
              </div>
              <span className="task-strip-count">
                {state.job.progress?.total_count ? (
                  <span>{`${state.job.progress.stage === 'classification_preflight' ? '只读核验 · ' : ''}${state.job.progress.completed_count ?? 0} / ${state.job.progress.total_count} 项`}</span>
                ) : null}
                <span>{`${Math.floor(state.job.progress?.elapsed_seconds ?? 0)} 秒`}</span>
              </span>
              <a href="#history" className="text-link" onClick={() => setPage('history')}>
                查看任务记录
              </a>
              <button
                className="button secondary"
                disabled={pauseRequested || state.job.status === 'pause_requested' || !connected}
                onClick={() => {
                  void requestPause();
                }}
              >
                <Icon name="pause" size={16} />
                {pauseRequested || state.job.status === 'pause_requested'
                  ? '正在暂停…'
                  : '暂停任务'}
              </button>
            </section>
          )}
        <footer className="app-footer">
          <span>网易云个人歌单工作台</span>
          <span>你的音乐，按你的节奏。</span>
        </footer>
        {selectedPlaylist && (
          <PlaylistDetails
            playlist={selectedPlaylist}
            returnFocus={detailTrigger.current}
            onClose={() => setSelectedPlaylist(null)}
            onRead={() => runAction('read_playlist', { key: selectedPlaylist.key })}
            readBlockedReason={playlistReadBlockedReason}
            readIntent={playlistReadIntent}
            job={state?.job}
            onPause={requestPause}
            pauseDisabled={pauseRequested || state?.job?.status === 'pause_requested' || !connected}
          />
        )}
      </div>
    </div>
  );
}
