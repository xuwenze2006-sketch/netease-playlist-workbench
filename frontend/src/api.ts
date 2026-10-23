import type {
  Action,
  AcceptedAction,
  PauseAcknowledgement,
  WorkbenchState,
  LocalTrackPage,
  MetadataFilter,
  ClassificationPage,
  ClassificationQuery,
  ClassificationDraftRequest,
  ClassificationDraftResponse,
  ClassificationChangesPage,
  ClassificationChangesQuery,
} from './types';
import { normalizeLocalTracks, normalizeState } from './normalize';
import { normalizeClassification, validClassificationQuery } from './classificationData';
import { validDraftRequest } from './classificationQuality';
import { normalizeClassificationChanges, validChangesQuery } from './classificationChangesData';
import { normalizeRecordingReviews, normalizeRecordingReview, validReviewBundleName, expectedRecordingReviewSave } from './recordingReviewData';
import type { RecordingReviewCatalog, RecordingReviewDetail, RecordingReviewSaveRequest } from './recordingReviewData';

export class LocalApiError extends Error {
  readonly acceptanceUnknown: boolean;
  constructor(message: string, acceptanceUnknown = false) {
    super(message);
    this.acceptanceUnknown = acceptanceUnknown;
  }
}

export class ClassificationReadError extends LocalApiError {
  readonly reason: 'generation_incomplete' | 'unavailable';
  constructor(reason: 'generation_incomplete' | 'unavailable' = 'unavailable') {
    super(reason === 'generation_incomplete'
      ? '本地分类资料尚未生成完整，请等待分类流程完成后重试。'
      : '本地分类资料暂不可用，请重试或检查本地记录。');
    this.reason = reason;
  }
}

const REMOVED_PLAYLIST_MESSAGE = '该歌单已不在本地目录中，请更新页面后重试。';
const PAUSE_UNCONFIRMED_MESSAGE = '暂停请求未确认，请查看当前任务状态后重试。';
class RemovedLocalPlaylistError extends LocalApiError {
  constructor() {
    super(REMOVED_PLAYLIST_MESSAGE);
  }
}

async function requestJson<T>(
  session: string,
  path: string,
  body?: unknown,
  signal?: AbortSignal,
  keepalive = false,
): Promise<T> {
  const controller = new AbortController();
  let timedOut = false;
  const submitted = body !== undefined;
  const recordingSave = submitted && path === '/api/recording-reviews/save';
  const cancelledMessage = '本次请求等待已取消。已发送任务不会自动重试，请先查看任务记录。';
  if (signal?.aborted) throw new LocalApiError(cancelledMessage);
  const cancel = () => controller.abort();
  signal?.addEventListener('abort', cancel, { once: true });
  const timer = globalThis.setTimeout(() => {
    timedOut = true;
    controller.abort();
  }, 10000);
  const requestError = () =>
    new LocalApiError(
      timedOut
        ? submitted
          ? '等待响应超时，后台任务可能仍在执行；请先恢复任务状态并核对。请勿再次提交。'
          : '读取本机状态超时，正在等待连接恢复。已发送任务仍可能在执行，请勿再次提交。'
        : signal?.aborted
          ? cancelledMessage
          : '与本机服务的连接中断。已发送的任务不会自动重试，请先查看任务记录。',
      submitted,
    );
  try {
    let response: Response;
    try {
      response = await fetch(path, {
        method: body === undefined ? 'GET' : 'POST',
        headers: {
          'X-Organizer-Session': session,
          ...(body === undefined ? {} : { 'Content-Type': 'application/json' }),
        },
        cache: 'no-store',
        credentials: 'omit',
        signal: controller.signal,
        keepalive,
        ...(body === undefined ? {} : { body: JSON.stringify(body) }),
      });
    } catch {
      throw requestError();
    }
    if (response.status === 401) {
      throw new LocalApiError('页面会话已更新，请刷新。已发送的任务不会自动重试。');
    }
    if (response.status === 403) {
      throw new LocalApiError('页面会话已更新或请求来源无效，请刷新；已发送的任务不会自动重试。');
    }
    let value: unknown;
    try {
      value = await response.json();
    } catch {
      if (timedOut || signal?.aborted) throw requestError();
      throw new LocalApiError(
        submitted && response.ok
          ? '任务响应无法读取，后台任务可能已接受；请先恢复状态并核对。请勿再次提交。'
          : '本机服务返回了无法读取的结果，请重新打开工作台。',
        submitted && (response.ok || recordingSave && response.status >= 500),
      );
    }
    if (!response.ok) {
      if (path === '/api/pause') throw new LocalApiError(PAUSE_UNCONFIRMED_MESSAGE);
      if (
        response.status === 503 &&
        body === undefined &&
        path.startsWith('/api/classification?') &&
        value &&
        typeof value === 'object' &&
        !Array.isArray(value) &&
        'accepted' in value &&
        value.accepted === false &&
        'error_code' in value &&
        value.error_code === 'classification_generation_incomplete'
      )
        throw new ClassificationReadError('generation_incomplete');
      if (
        response.status === 404 &&
        value &&
        typeof value === 'object' &&
        !Array.isArray(value) &&
        'accepted' in value &&
        value.accepted === false &&
        'message' in value &&
        value.message === REMOVED_PLAYLIST_MESSAGE
      )
        throw new RemovedLocalPlaylistError();
      const message =
        value &&
        typeof value === 'object' &&
        'message' in value &&
        typeof value.message === 'string'
          ? value.message.slice(0, 400)
          : '本次请求未被接受，请检查任务状态后再操作。';
      throw new LocalApiError(message, recordingSave && response.status >= 500);
    }
    return value as T;
  } finally {
    globalThis.clearTimeout(timer);
    signal?.removeEventListener('abort', cancel);
  }
}

export async function fetchState(session: string, signal?: AbortSignal): Promise<WorkbenchState> {
  const value = await requestJson(session, '/api/state', undefined, signal);
  try {
    return normalizeState(value);
  } catch {
    throw new LocalApiError('本地状态格式不完整，请重新打开工作台或检查本机服务。');
  }
}

export async function fetchRecordingReviews(session: string, signal?: AbortSignal): Promise<RecordingReviewCatalog> {
  const value = await requestJson(session, '/api/recording-reviews', undefined, signal);
  try { return normalizeRecordingReviews(value); }
  catch { throw new LocalApiError('核验记录目录格式不完整，请重新读取或检查本地记录。'); }
}

export async function fetchRecordingReview(session: string, bundle: string, signal?: AbortSignal): Promise<RecordingReviewDetail> {
  if (!validReviewBundleName(bundle)) throw new LocalApiError('核验记录名称无效，请重新选择。');
  const value = await requestJson(session, `/api/recording-reviews/detail?bundle=${encodeURIComponent(bundle)}`, undefined, signal);
  try { return normalizeRecordingReview(value, bundle); }
  catch { throw new LocalApiError('核验内容格式不完整或与所选记录不符，请重新读取或检查本地记录。'); }
}

export async function postRecordingReview(session: string, request: RecordingReviewSaveRequest,
  base: RecordingReviewDetail, signal?: AbortSignal): Promise<RecordingReviewDetail> {
  let expected: RecordingReviewDetail;
  let frozen: RecordingReviewSaveRequest;
  try {
    expected = expectedRecordingReviewSave(base, request);
    frozen = JSON.parse(JSON.stringify(request)) as RecordingReviewSaveRequest;
  } catch (error) {
    throw new LocalApiError(error instanceof Error && error.message.includes('64 KiB') ? error.message :
      '核验填写内容或基础记录已失效，请核对来源、证据字段和场景判断后再保存。');
  }
  const value = await requestJson<unknown>(session, '/api/recording-reviews/save', frozen, signal);
  try {
    if (!value || typeof value !== 'object' || !('status' in value) || value.status !== 'saved' ||
      !('bundle' in value) || value.bundle !== frozen.target_bundle || !('replayed' in value) || typeof value.replayed !== 'boolean' ||
      !('detail' in value)) throw new Error();
    const detail = normalizeRecordingReview(value.detail, frozen.target_bundle);
    if (!detail.review_sha256 || detail.created_at !== expected.created_at ||
      JSON.stringify(detail.preview) !== JSON.stringify(expected.preview)) throw new Error();
    return detail;
  } catch {
    throw new LocalApiError('保存回执无法与提交内容完整核对；请保留本次填写，使用同一次保存请求核对并重试。', true);
  }
}

export async function fetchClassification(
  session: string,
  options: ClassificationQuery,
  signal?: AbortSignal,
  refreshLocalRecords = false,
): Promise<ClassificationPage> {
  if (!validClassificationQuery(options) || typeof refreshLocalRecords !== 'boolean') throw new LocalApiError('本地分类查询条件无效，请重新选择筛选条件。');
  const { offset, query, dimension, tag, review } = options;
  const path = `/api/classification?offset=${offset}&limit=50&q=${encodeURIComponent(query)}&dimension=${dimension}&tag=${encodeURIComponent(tag)}&review=${review}${options.basis === 'draft' ? '&basis=draft' : ''}${refreshLocalRecords ? '&refresh=local' : ''}`;
  const value = await requestJson(session, path, undefined, signal);
  let page: ClassificationPage;
  try {
    page = normalizeClassification(value, options);
  } catch {
    throw new LocalApiError('本地分类资料格式不完整，请重试或检查本地记录。');
  }
  if (page.status === 'unavailable')
    throw new ClassificationReadError(page.error_code === 'classification_generation_incomplete' ? 'generation_incomplete' : 'unavailable');
  return page;
}

export async function fetchClassificationChanges(session: string, options: ClassificationChangesQuery, signal?: AbortSignal): Promise<ClassificationChangesPage> {
  if (!validChangesQuery(options)) throw new LocalApiError('歌单变动查询条件无效，请刷新分类记录后重试。');
  const { playlist, source_version, revision, offset, change } = options;
  const path = `/api/classification/changes?playlist=${encodeURIComponent(playlist)}&source_version=${source_version}&revision=${revision}&offset=${offset}&limit=50&change=${change}`;
  const value = await requestJson(session, path, undefined, signal);
  try { return normalizeClassificationChanges(value, options); }
  catch { throw new LocalApiError('歌单变动资料格式或修订版本不匹配，请刷新分类记录后重新核对。'); }
}

export async function postClassificationDraft(
  session: string,
  payload: ClassificationDraftRequest,
): Promise<ClassificationDraftResponse> {
  if (!validDraftRequest(payload)) throw new LocalApiError('本地分类修正格式无效，请检查标签与修正理由。');
  const value = await requestJson<unknown>(session, '/api/classification/draft', payload);
  if (value && typeof value === 'object' && !Array.isArray(value)) {
    const row = value as Record<string, unknown>;
    if (row.accepted === true && typeof row.message === 'string' && row.message.trim() &&
      Array.from(row.message).length <= 400 && !/[\u0000-\u001f\u007f-\u009f\ud800-\udfff]/u.test(row.message) &&
      typeof row.revision === 'number' && Number.isSafeInteger(row.revision) && row.revision >= 0 &&
      typeof row.changed_count === 'number' && Number.isSafeInteger(row.changed_count) && row.changed_count >= 0 && row.changed_count <= 10000) {
      return { accepted: true, message: row.message, revision: row.revision, changed_count: row.changed_count };
    }
  }
  throw new LocalApiError('本地修正的保存状态无法确认，请刷新分类记录后核对。请勿再次提交。', true);
}

export async function fetchLocalTracks(
  session: string,
  key: string,
  options: { offset: number; query: string; metadata?: MetadataFilter },
  signal?: AbortSignal,
): Promise<LocalTrackPage> {
  const { offset, query, metadata = 'all' } = options;
  if (
    !/^[1-9][0-9]{0,19}$/.test(key) ||
    !Number.isSafeInteger(offset) ||
    offset < 0 ||
    offset > 10000 ||
    !['all', 'incomplete'].includes(metadata) ||
    typeof query !== 'string' ||
    Array.from(query).length > 160 ||
    /[\u0000-\u001f\u007f-\u009f]/.test(query) ||
    Array.from(query).some((char) => {
      const code = char.codePointAt(0)!;
      return code >= 0xd800 && code <= 0xdfff;
    })
  )
    throw new LocalApiError('本地歌曲查询条件无效，请重新选择歌单或搜索词。');
  const path = `/api/playlists/${key}/tracks?offset=${offset}&limit=50&q=${encodeURIComponent(query)}${metadata === 'incomplete' ? '&metadata=incomplete' : ''}`;
  let value: unknown;
  try {
    value = await requestJson(session, path, undefined, signal);
  } catch (error) {
    // This exact local deletion response is a display state only. Other HTTP
    // errors and every other endpoint retain their normal failure behavior.
    if (!(error instanceof RemovedLocalPlaylistError)) throw error;
    value = {
      status: 'missing_playlist',
      source: 'local_record',
      metadata_filter: metadata,
      playlist: null,
      updated_at: null,
      counts: { expected: null, observed: null, missing: null, metadata_missing: null },
      pagination: { offset, limit: 50, total: 0, next_offset: null },
      tracks: [],
    };
  }
  try {
    return normalizeLocalTracks(value, { key, offset, limit: 50, metadata });
  } catch {
    throw new LocalApiError('本地歌曲明细格式不完整，请重试读取。');
  }
}

export async function postAction(
  session: string,
  action: string,
  payload: unknown = {},
): Promise<AcceptedAction> {
  const value = await requestJson<unknown>(session, '/api/actions', { action, payload });
  if (value && typeof value === 'object' && !Array.isArray(value) && 'accepted' in value) {
    if (value.accepted === false) return { accepted: false };
    if (
      value.accepted === true &&
      'job_id' in value &&
      typeof value.job_id === 'string' &&
      value.job_id.length > 0 &&
      value.job_id.length <= 200 &&
      !/[\u0000-\u001f\u007f]/.test(value.job_id)
    )
      return { accepted: true, job_id: value.job_id };
  }
  throw new LocalApiError(
    '任务接受状态无法确认，后台可能已开始执行；请先恢复状态并核对。请勿再次提交。',
    true,
  );
}

export async function pauseJob(session: string, keepalive = false): Promise<PauseAcknowledgement> {
  const value = await requestJson<unknown>(session, '/api/pause', {}, undefined, keepalive);
  if (value && typeof value === 'object' && !Array.isArray(value) && 'status' in value) {
    if (value.status === 'pause_requested') return { status: 'pause_requested' };
    if (value.status === 'failed' || value.status === 'blocked')
      throw new LocalApiError(PAUSE_UNCONFIRMED_MESSAGE);
  }
  throw new LocalApiError(PAUSE_UNCONFIRMED_MESSAGE, true);
}

export function closeWorkbench(session: string): Promise<AcceptedAction> {
  return requestJson(session, '/api/close', {}, undefined, true);
}

export function readSession(): string {
  const session = document.querySelector<HTMLMetaElement>(
    'meta[name="organizer-session"]',
  )?.content;
  if (!session || session === '__ORGANIZER_SESSION__' || session.length > 256) {
    throw new LocalApiError('页面会话已失效，请通过“启动歌单整理”重新打开工作台。');
  }
  return session;
}

export function readStartupLoginJob(): string {
  const value = document.querySelector<HTMLMetaElement>(
    'meta[name="organizer-login-job"]',
  )?.content;
  return value && /^[a-f0-9]{24}$/.test(value) ? value : '';
}

export const ACTION_LABELS: Record<Action, string> = {
  preview_names: '读取名称清单',
  preview_full: '读取完整红心清单',
  rename: '整理歌单名称',
  artists: '创建歌手精选',
  resume: '继续上次任务',
  check: '检查本地接入',
  login: '生成扫码授权',
  login_status: '验证账号授权',
  authorization_probe: '等待扫码授权',
  discover: '更新可用功能',
  local_plan: '生成本地整理清单',
  reconcile_renames: '只读核对上次改名',
  read_playlist: '读取歌单明细',
  save_credentials: '保存接入凭证',
};
