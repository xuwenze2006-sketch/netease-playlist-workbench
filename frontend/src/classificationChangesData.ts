import type { ClassificationChangeRecord, ClassificationChangesPage, ClassificationChangesQuery } from './types';
import { validCorrection } from './classificationQuality';

const invalid = (): never => { throw new Error('invalid classification changes'); };
function object(value: unknown): Record<string, unknown> {
  if (!value || typeof value !== 'object' || Array.isArray(value)) return invalid();
  return value as Record<string, unknown>;
}
function text(value: unknown, maximum: number, empty = false): string {
  if (typeof value !== 'string' || !empty && !value.trim() || Array.from(value).length > maximum ||
    /[\u0000-\u001f\u007f-\u009f\ud800-\udfff]/u.test(value)) return invalid();
  return value;
}
function integer(value: unknown, maximum = 10000): number {
  if (typeof value !== 'number' || !Number.isSafeInteger(value) || value < 0 || value > maximum) return invalid();
  return value;
}
function array(value: unknown, maximum: number): unknown[] {
  if (!Array.isArray(value) || value.length > maximum) return invalid();
  return value;
}
function labels(value: unknown): ClassificationChangeRecord['before'] {
  const raw = object(value);
  const result = { styles: array(raw.styles, 2).map((v) => text(v, 160)),
    scenes: array(raw.scenes, 3).map((v) => text(v, 160)), language: text(raw.language, 160) };
  if (!validCorrection({ ...result, reason: '核对标签', recording_note: '' })) return invalid();
  return result;
}
export function validChangesQuery(value: ClassificationChangesQuery): boolean {
  try {
    text(value.playlist, 100); integer(value.offset); integer(value.revision, Number.MAX_SAFE_INTEGER);
    return /^[a-f0-9]{64}$/.test(text(value.source_version, 64)) && ['all', 'added', 'removed'].includes(value.change);
  } catch { return false; }
}
export function normalizeClassificationChanges(value: unknown, request: ClassificationChangesQuery): ClassificationChangesPage {
  if (!validChangesQuery(request)) return invalid();
  const root = object(value), filters = object(root.filters), counts = object(root.counts), paging = object(root.pagination);
  if (root.source !== 'local_correction_draft' || !['available', 'not_loaded', 'unavailable'].includes(root.status as string) ||
    !['ready', 'stale', 'unavailable'].includes(root.draft_status as string) || root.playlist !== request.playlist || filters.change !== request.change) return invalid();
  const version = root.source_version === null ? null : text(root.source_version, 64);
  if (version !== null && !/^[a-f0-9]{64}$/.test(version)) return invalid();
  const revision = integer(root.revision, Number.MAX_SAFE_INTEGER);
  const added = integer(counts.added), removed = integer(counts.removed);
  const offset = integer(paging.offset), limit = integer(paging.limit, 100), total = integer(paging.total);
  const next = offset + limit < total ? offset + limit : null;
  if (offset !== request.offset || limit !== 50 || paging.next_offset !== next) return invalid();
  const rows = array(root.records, 50).map((value) => {
    const raw = object(value), position = integer(raw.position), key = text(raw.record_key, 32);
    if (!position || !/^[a-f0-9]{32}$/.test(key) || !['added', 'removed'].includes(raw.change as string) ||
      request.change !== 'all' && raw.change !== request.change) return invalid();
    return { position, record_key: key, name: text(raw.name, 512), artists: text(raw.artists, 2048, true),
      change: raw.change as ClassificationChangeRecord['change'], before: labels(raw.before), after: labels(raw.after),
      reason: text(raw.reason, 1000), recording_note: text(raw.recording_note, 1000, true) };
  });
  if (new Set(rows.map((r) => r.position)).size !== rows.length || new Set(rows.map((r) => r.record_key)).size !== rows.length ||
    rows.length !== Math.min(limit, Math.max(0, total - offset))) return invalid();
  if (root.status === 'available') {
    const expected = request.change === 'all' ? added + removed : request.change === 'added' ? added : removed;
    if (version !== request.source_version || revision !== request.revision || root.draft_status !== 'ready' || total !== expected) return invalid();
  } else if (rows.length || total || added || removed) return invalid();
  return { status: root.status as ClassificationChangesPage['status'], source: 'local_correction_draft', source_version: version,
    revision, draft_status: root.draft_status as ClassificationChangesPage['draft_status'], playlist: request.playlist,
    filters: { change: request.change }, counts: { added, removed }, pagination: { offset, limit, total, next_offset: next }, records: rows };
}
