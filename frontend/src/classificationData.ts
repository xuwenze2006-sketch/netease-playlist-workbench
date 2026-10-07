import type { ClassificationPage, ClassificationQuery } from './types';

type Row = Record<string, unknown>;
const fail = (): never => { throw new Error('invalid classification data'); };
function object(value: unknown): Row {
  if (!value || typeof value !== 'object' || Array.isArray(value)) return fail();
  return value as Row;
}
function integer(value: unknown, max = 10000): number {
  if (typeof value !== 'number' || !Number.isSafeInteger(value) || value < 0 || value > max) return fail();
  return value;
}
function text(value: unknown, maximum = 512, empty = false): string {
  if (typeof value !== 'string' || (!empty && !value.trim()) || Array.from(value).length > maximum ||
    /[\u0000-\u001f\u007f-\u009f\ud800-\udfff]/u.test(value)) return fail();
  return value;
}
function array(value: unknown, max: number): unknown[] {
  if (!Array.isArray(value) || value.length > max) return fail();
  return value;
}
function texts(value: unknown, max = 64): string[] {
  const result = array(value, max).map((v) => text(v, 160));
  if (new Set(result).size !== result.length) return fail();
  return result;
}
function timestamp(value: unknown): string | null {
  if (value === null) return null;
  const result = text(value, 100);
  return Number.isFinite(Date.parse(result)) ? result : fail();
}
export function validClassificationQuery(query: ClassificationQuery): boolean {
  try {
    integer(query.offset);
    text(query.query, 160, true); text(query.tag, 160, true);
    return ['all', 'scene', 'style', 'language'].includes(query.dimension) && ['all', 'pending'].includes(query.review);
  } catch { return false; }
}
export function normalizeClassification(value: unknown, request: ClassificationQuery): ClassificationPage {
  if (!validClassificationQuery(request)) return fail();
  const root = object(value);
  if (root.source !== 'local_record' || !['available', 'not_loaded', 'unavailable'].includes(root.status as string) ||
    !['verified', 'local_only'].includes(root.verification as string)) return fail();
  const filters = object(root.filters);
  for (const key of ['query', 'dimension', 'tag', 'review'] as const) if (filters[key] !== request[key]) return fail();
  const paging = object(root.pagination);
  const offset = integer(paging.offset), limit = integer(paging.limit, 100), total = integer(paging.total);
  if (offset !== request.offset || limit !== 50) return fail();
  const expectedNext = offset + limit < total ? offset + limit : null;
  if (paging.next_offset !== expectedNext) return fail();
  const options = object(root.options);
  const safeOptions = { scene: texts(options.scene), style: texts(options.style), language: texts(options.language) };
  const playlists = array(root.playlists, 64).map((value) => {
    const row = object(value);
    if (!['scene', 'style', 'language', 'review'].includes(row.dimension as string) ||
      !(row.key === null || (typeof row.key === 'string' && /^[1-9][0-9]{0,19}$/.test(row.key)))) return fail();
    return { name: text(row.name, 100), dimension: row.dimension as ClassificationPage['playlists'][number]['dimension'],
      count: integer(row.count), key: row.key as string | null };
  });
  if (new Set(playlists.map((p) => p.name)).size !== playlists.length) return fail();
  const records = array(root.records, 50).map((value) => {
    const row = object(value);
    const position = integer(row.position);
    if (position === 0) return fail();
    return { position, name: text(row.name), artists: text(row.artists, 2048, true),
      styles: texts(row.styles), scenes: texts(row.scenes), language: text(row.language, 160),
      pending_reasons: texts(row.pending_reasons, 16), evidence_note: text(row.evidence_note, 2048, true),
      language_evidence_note: text(row.language_evidence_note, 2048, true) };
  });
  if (new Set(records.map((r) => r.position)).size !== records.length ||
    records.length !== Math.min(limit, Math.max(0, total - offset))) return fail();
  let summary: ClassificationPage['summary'] = null;
  if (root.summary !== null) {
    const raw = object(root.summary);
    summary = { source_count: integer(raw.source_count), covered_count: integer(raw.covered_count),
      playlist_count: integer(raw.playlist_count, 64), pending_count: integer(raw.pending_count),
      unknown_style_count: integer(raw.unknown_style_count), unknown_language_count: integer(raw.unknown_language_count) };
    if (summary.covered_count > summary.source_count || summary.pending_count > summary.source_count ||
      summary.unknown_style_count > summary.pending_count || summary.unknown_language_count > summary.pending_count ||
      summary.playlist_count !== playlists.length || total > summary.source_count ||
      records.some((r) => r.position > summary!.source_count)) return fail();
  }
  const updated = timestamp(root.updated_at), verified = timestamp(root.verified_at);
  if (root.status === 'available') {
    if (!summary || !updated) return fail();
    if (root.verification === 'verified' && !verified) return fail();
    if (root.verification === 'local_only' && verified !== null) return fail();
  } else if (summary !== null || root.verification !== 'local_only' || verified !== null || total !== 0 ||
    records.length || playlists.length || Object.values(safeOptions).some((v) => v.length)) return fail();
  return { status: root.status as ClassificationPage['status'], source: 'local_record',
    verification: root.verification as ClassificationPage['verification'], updated_at: updated, verified_at: verified,
    summary, options: safeOptions, playlists, filters: { query: request.query, dimension: request.dimension,
      tag: request.tag, review: request.review }, pagination: { offset, limit, total, next_offset: expectedNext }, records };
}
