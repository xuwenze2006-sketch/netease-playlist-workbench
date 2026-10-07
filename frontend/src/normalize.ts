import type {
  Action,
  Category,
  History,
  Job,
  JobResult,
  LocalPreview,
  LocalTrackPage,
  MetadataFilter,
  Playlist,
  WorkbenchState,
  UpdateStatus,
  WriteRecovery,
} from './types';
import { ERROR_CODES, NEXT_STEPS } from './types';
import type { ErrorCode, NextStep } from './types';

type ObjectValue = Record<string, unknown>;
function object(value: unknown): ObjectValue {
  if (!value || typeof value !== 'object' || Array.isArray(value))
    throw new Error('invalid object');
  return value as ObjectValue;
}
function text(value: unknown, maximum = 2000): string {
  if (typeof value !== 'string') throw new Error('invalid text');
  return value.replace(/[\u0000-\u0008\u000b\u000c\u000e-\u001f\u007f]/g, '').slice(0, maximum);
}
function count(value: unknown): number {
  if (typeof value !== 'number' || !Number.isSafeInteger(value) || value < 0)
    throw new Error('invalid count');
  return value;
}
function elapsed(value: unknown): number {
  if (typeof value !== 'number' || !Number.isFinite(value) || value < 0)
    throw new Error('invalid time');
  return value;
}
function array(value: unknown, maximum: number): unknown[] {
  if (!Array.isArray(value) || value.length > maximum) throw new Error('invalid array');
  return value;
}
function optionalText(value: unknown): string | null | undefined {
  return value === undefined ? undefined : value === null ? null : text(value, 100);
}

function playlist(value: unknown): Playlist {
  const row = object(value);
  if (
    !['liked', 'artist', 'normal'].includes(row.category as string) ||
    row.source !== 'local_record'
  )
    throw new Error('invalid category');
  const key = text(row.key, 200);
  const name = text(row.name, 300);
  if (!key || !name) throw new Error('empty playlist');
  return {
    key,
    name,
    track_count: count(row.track_count),
    category: row.category as Category,
    source: 'local_record',
    updated_at: optionalText(row.updated_at),
  };
}

function history(value: unknown): History | null {
  if (value === null || value === undefined) return null;
  const row = object(value);
  const status = text(row.status, 80);
  const operation =
    row.operation === 'artists' || row.operation === 'renames' || row.operation === 'classification' ? row.operation : undefined;
  return {
    status,
    completed_count: count(row.completed_count),
    operation,
    resumable: row.resumable === true && !!operation && operation !== 'classification' && ['paused', 'partial'].includes(status),
    items: array(row.items ?? [], 2000).map((item) => {
      const entry = object(item);
      return {
        name: text(entry.name, 300),
        status: text(entry.status, 80),
        ...(entry.count === undefined ? {} : { count: count(entry.count) }),
        ...(row.operation !== 'classification' || entry.expected_count === undefined ? {} : { expected_count: count(entry.expected_count) }),
        ...(row.operation !== 'classification' || entry.added_count === undefined ? {} : { added_count: count(entry.added_count) }),
        ...(row.operation === 'classification' && ['pending', 'created', 'adding', 'added', 'completed'].includes(entry.phase as string)
          ? { phase: entry.phase as History['items'][number]['phase'] } : {}),
      };
    }),
    ...(typeof row.updated_at === 'string' ? { updated_at: text(row.updated_at, 100) } : {}),
  };
}

function publicPlaylistKey(value: unknown): string | undefined {
  return typeof value === 'string' && /^[1-9][0-9]{0,19}$/.test(value) ? value : undefined;
}

function jobResult(value: unknown, action: Action): JobResult | null {
  if (value === null || value === undefined) return null;
  const row = object(value);
  const result: JobResult = {};
  if (action === 'read_playlist') {
    const key = publicPlaylistKey(row.playlist_key);
    if (key) result.playlist_key = key;
    for (const key of [
      'expected_count',
      'count',
      'missing_count',
      'missing_metadata_count',
    ] as const) {
      const value = row[key];
      if (typeof value === 'number' && Number.isSafeInteger(value) && value >= 0 && value <= 10000)
        result[key] = value;
    }
  }
  for (const key of ['status', 'message', 'url', 'qr_png_base64', 'path'] as const) {
    if (row[key] !== undefined)
      result[key] = text(
        row[key],
        key === 'qr_png_base64' ? 256 * 1024 : key === 'url' ? 4096 : 2000,
      );
  }
  for (const key of ['completed_count', 'job_count', 'blocked_count'] as const) {
    if (row[key] !== undefined) result[key] = count(row[key]);
  }
  for (const key of [
    'authorized',
    'outcome_known',
    'write_attempted',
    'record_saved',
    'resumable',
  ] as const) {
    if (typeof row[key] === 'boolean') result[key] = row[key];
  }
  if (row.applied_to_account === null || typeof row.applied_to_account === 'boolean')
    result.applied_to_account = row.applied_to_account;
  if (ERROR_CODES.includes(row.error_code as ErrorCode))
    result.error_code = row.error_code as ErrorCode;
  if (NEXT_STEPS.includes(row.next_step as NextStep)) result.next_step = row.next_step as NextStep;
  if (row.performance !== undefined) {
    const performance = object(row.performance);
    result.performance = {
      ...(performance.call_count === undefined
        ? {}
        : { call_count: count(performance.call_count) }),
      ...(performance.elapsed_seconds === undefined
        ? {}
        : { elapsed_seconds: elapsed(performance.elapsed_seconds) }),
    };
  }
  return result;
}

function job(value: unknown): Job | null {
  if (value === null || value === undefined) return null;
  const row = object(value);
  const action = text(row.action, 80) as Action;
  const progress =
    row.progress === undefined || row.progress === null ? null : object(row.progress);
  return {
    id: text(row.id, 200),
    action,
    label: text(row.label, 200),
    status: text(row.status, 80),
    result: jobResult(row.result, action),
    ...(action === 'read_playlist' && publicPlaylistKey(row.playlist_key)
      ? { playlist_key: publicPlaylistKey(row.playlist_key) }
      : {}),
    ...(progress
      ? {
          progress: {
            ...(progress.stage === 'classification_preflight' ? { stage: 'classification_preflight' as const } : {}),
            ...(progress.label === undefined ? {} : { label: text(progress.label, 500) }),
            ...(progress.completed_count === undefined
              ? {}
              : { completed_count: count(progress.completed_count) }),
            ...(progress.total_count === undefined
              ? {}
              : { total_count: count(progress.total_count) }),
            ...(progress.elapsed_seconds === undefined
              ? {}
              : { elapsed_seconds: elapsed(progress.elapsed_seconds) }),
          },
        }
      : {}),
    logs: array(row.logs ?? [], 2000).map((item) => {
      const entry = object(item);
      return {
        time: text(entry.time, 100),
        level: text(entry.level, 30),
        message: text(entry.message, 2000),
      };
    }),
  };
}

function preview(value: unknown): LocalPreview | null {
  if (value === undefined || value === null) return null;
  try {
    const row = object(value);
    if (row.source !== 'local_record' || !['names', 'full'].includes(row.scope as string))
      return null;
    const liked = object(row.liked);
    const renames = array(row.renames, 1000).map((value) => {
      const item = object(value);
      const key = text(item.key, 200);
      const old_name = text(item.old_name, 300);
      const name = text(item.name, 300);
      if (!key || !old_name || !name) throw new Error('empty rename');
      return { key, old_name, name };
    });
    const rename_count = count(row.rename_count);
    if (
      rename_count < renames.length ||
      new Set(renames.map((item) => item.key)).size !== renames.length
    )
      throw new Error('invalid renames');
    return {
      source: 'local_record',
      scope: row.scope as 'names' | 'full',
      updated_at: text(row.updated_at, 100),
      rename_count,
      renames,
      artists: array(row.artists, 100).map((value) => {
        const item = object(value);
        const name = text(item.name, 300);
        if (!name) throw new Error('empty artist');
        const tracks = array(item.tracks, 2000).map((value) => {
          const track = object(value);
          const name = text(track.name, 300);
          if (!name) throw new Error('empty track');
          return { name, artists: array(track.artists, 20).map((name) => text(name, 300)) };
        });
        const total = count(item.count);
        if (total < tracks.length) throw new Error('invalid tracks');
        return { name, count: total, tracks };
      }),
      limitations: array(row.limitations, 20).map((value) => text(value, 500)),
      liked: {
        expected: count(liked.expected),
        observed: liked.observed === null ? null : count(liked.observed),
        missing: liked.missing === null ? null : count(liked.missing),
      },
    };
  } catch {
    // An optional old or damaged preview must not hide valid playlist and task records.
    return null;
  }
}

function recovery(value: unknown): WriteRecovery {
  if (value === undefined) return { status: 'clear', operation: 'unknown', can_reconcile: false };
  try {
    const row = object(value);
    if (
      !['clear', 'review_required'].includes(row.status as string) ||
      !['renames', 'artists', 'classification', 'unknown'].includes(row.operation as string) ||
      typeof row.can_reconcile !== 'boolean' ||
      (row.can_reconcile && (row.status !== 'review_required' || row.operation !== 'renames'))
    )
      throw new Error('invalid recovery');
    return {
      status: row.status as WriteRecovery['status'],
      operation: row.operation as WriteRecovery['operation'],
      can_reconcile: row.can_reconcile,
    };
  } catch {
    // A malformed present intent cannot safely grant another write or reconciliation.
    return { status: 'review_required', operation: 'unknown', can_reconcile: false };
  }
}

export function normalizeState(value: unknown): WorkbenchState {
  const root = object(value);
  const connection = object(root.connection);
  const data = object(root.data);
  if (
    typeof connection.installed !== 'boolean' ||
    typeof connection.configured !== 'boolean' ||
    !(connection.authorized === null || typeof connection.authorized === 'boolean') ||
    !['local_record', 'empty'].includes(data.source as string)
  )
    throw new Error('invalid state');
  const account = data.account === null || data.account === undefined ? null : object(data.account);
  let updateStatus: UpdateStatus = 'none';
  if (root.update && typeof root.update === 'object' && !Array.isArray(root.update)) {
    const status = (root.update as ObjectValue).status;
    if (['none', 'busy', 'review_required', 'stopping'].includes(status as string)) {
      updateStatus = status as UpdateStatus;
    }
  }
  return {
    update: { status: updateStatus },
    recovery: recovery(root.recovery),
    connection: {
      installed: connection.installed,
      configured: connection.configured,
      authorized: connection.authorized,
    },
    data: {
      account: account ? { nickname: text(account.nickname, 128) } : null,
      source: data.source as 'local_record' | 'empty',
      updated_at: optionalText(data.updated_at),
      playlists: array(data.playlists, 2000).map(playlist),
      history: history(data.history),
      artists_completed: data.artists_completed === true,
      preview: preview(data.preview),
    },
    job: job(root.job),
  };
}

function strictLocalText(value: unknown, maximum: number): string {
  if (
    typeof value !== 'string' ||
    !value.trim() ||
    Array.from(value).length > maximum ||
    /[\u0000-\u001f\u007f-\u009f]/.test(value) ||
    Array.from(value).some((char) => {
      const code = char.codePointAt(0)!;
      return code >= 0xd800 && code <= 0xdfff;
    })
  )
    throw new Error('invalid local text');
  return value;
}

export function normalizeLocalTracks(
  value: unknown,
  request: { key: string; offset: number; limit: number; metadata?: MetadataFilter },
): LocalTrackPage {
  const root = object(value);
  const metadata = request.metadata ?? 'all';
  if (
    !['all', 'incomplete'].includes(metadata) ||
    (root.metadata_filter !== metadata &&
      !(metadata === 'all' && root.metadata_filter === undefined))
  )
    throw new Error('wrong local metadata filter');
  if (
    root.source !== 'local_record' ||
    !['available', 'not_loaded', 'unavailable', 'missing_playlist'].includes(root.status as string)
  )
    throw new Error('invalid local page');
  const row = root.playlist === null ? null : object(root.playlist);
  let publicPlaylist: Playlist | null = null;
  if (row) {
    if (
      row.key !== request.key ||
      row.source !== 'local_record' ||
      !['liked', 'artist', 'normal'].includes(row.category as string)
    )
      throw new Error('wrong local playlist');
    publicPlaylist = {
      key: request.key,
      name: strictLocalText(row.name, 512),
      track_count: count(row.track_count),
      category: row.category as Category,
      source: 'local_record',
      updated_at: optionalText(row.updated_at),
    };
    if (publicPlaylist.track_count > 10000) throw new Error('invalid directory count');
  }
  const counts = object(root.counts);
  const bounded = (value: unknown) => {
    const result = count(value);
    if (result > 10000) throw new Error('invalid local count');
    return result;
  };
  const nullable = (value: unknown) => (value === null ? null : bounded(value));
  const publicCounts = {
    expected: nullable(counts.expected),
    observed: nullable(counts.observed),
    missing: nullable(counts.missing),
    metadata_missing: nullable(counts.metadata_missing),
  };
  const pagination = object(root.pagination);
  const offset = bounded(pagination.offset);
  const limit = count(pagination.limit);
  const total = bounded(pagination.total);
  const next = nullable(pagination.next_offset);
  if (offset !== request.offset || limit !== request.limit || limit !== 50)
    throw new Error('wrong local page request');
  const tracks = array(root.tracks, 50).map((value) => {
    const track = object(value);
    const position = bounded(track.position);
    const artists = array(track.artists, 8).map((name) => strictLocalText(name, 160));
    const artist_count = bounded(track.artist_count);
    if (
      position < 1 ||
      track.key !== `track-${position}` ||
      typeof track.metadata_available !== 'boolean' ||
      artist_count > 100 ||
      artist_count < artists.length ||
      (track.metadata_available && artists.length === 0)
    )
      throw new Error('invalid local track');
    return {
      key: `track-${position}`,
      position,
      name: strictLocalText(track.name, 512),
      artists,
      artist_count,
      metadata_available: track.metadata_available,
    };
  });
  let updated_at: string | null = null;
  if (root.status === 'available') {
    const { expected, observed, missing, metadata_missing } = publicCounts;
    if (
      !publicPlaylist ||
      expected === null ||
      observed === null ||
      missing === null ||
      metadata_missing === null ||
      observed > expected ||
      missing !== expected - observed ||
      metadata_missing > observed ||
      total > observed ||
      (metadata === 'incomplete' &&
        (total > metadata_missing || tracks.some((track) => track.metadata_available))) ||
      tracks.length !== Math.min(limit, Math.max(0, total - offset)) ||
      next !== (offset + limit < total ? offset + limit : null)
    )
      throw new Error('inconsistent local page');
    updated_at = strictLocalText(root.updated_at, 100);
    if (!Number.isFinite(new Date(updated_at).valueOf())) throw new Error('invalid local date');
    let previous = 0;
    for (const track of tracks) {
      if (track.position <= previous || track.position > observed)
        throw new Error('invalid local order');
      previous = track.position;
    }
  } else {
    if (
      tracks.length ||
      total !== 0 ||
      next !== null ||
      publicCounts.observed !== null ||
      publicCounts.missing !== null ||
      publicCounts.metadata_missing !== null ||
      root.updated_at !== null ||
      (root.status === 'missing_playlist' && publicPlaylist !== null) ||
      (root.status === 'not_loaded' && !publicPlaylist)
    )
      throw new Error('invalid unavailable page');
  }
  return {
    status: root.status as LocalTrackPage['status'],
    source: 'local_record',
    metadata_filter: metadata,
    playlist: publicPlaylist,
    updated_at,
    counts: publicCounts,
    pagination: { offset, limit, total, next_offset: next },
    tracks,
  };
}
