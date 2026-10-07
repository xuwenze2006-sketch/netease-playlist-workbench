import type { ClassificationCorrection, ClassificationPage, ClassificationQuery, ClassificationQuality } from './types';
import { REVIEW_LABELS, SCENE_LABELS, validCorrection } from './classificationQuality';

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
function text(value: unknown, maximum = 512, empty = false, multiline = false): string {
  const controls = multiline ? /[\u0000-\u0009\u000b-\u001f\u007f-\u009f\ud800-\udfff]/u
    : /[\u0000-\u001f\u007f-\u009f\ud800-\udfff]/u;
  if (typeof value !== 'string' || (!empty && !value.trim()) || Array.from(value).length > maximum ||
    controls.test(value)) return fail();
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
    return ['all', 'scene', 'style', 'language'].includes(query.dimension) && Object.hasOwn(REVIEW_LABELS, query.review) &&
      (query.basis === undefined || ['original', 'draft'].includes(query.basis));
  } catch { return false; }
}
export function normalizeClassification(value: unknown, request: ClassificationQuery): ClassificationPage {
  if (!validClassificationQuery(request)) return fail();
  const root = object(value);
  if (root.source !== 'local_record' || !['available', 'not_loaded', 'unavailable'].includes(root.status as string) ||
    !['verified', 'local_only'].includes(root.verification as string)) return fail();
  const filters = object(root.filters);
  for (const key of ['query', 'dimension', 'tag', 'review'] as const) if (filters[key] !== request[key]) return fail();
  const basis = filters.basis ?? 'original';
  if (!['original', 'draft'].includes(basis as string) || basis !== (request.basis ?? 'original')) return fail();
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
  let quality: ClassificationQuality | undefined;
  if (root.quality !== undefined) {
    const raw = object(root.quality);
    const version = text(raw.source_version, 64);
    if (!/^[a-f0-9]{64}$/.test(version) || !['ready', 'stale', 'unavailable'].includes(raw.draft_status as string)) return fail();
    const pilot = array(raw.pilot_positions, 100).map((v) => integer(v));
    if (pilot.some((v) => v === 0) || new Set(pilot).size !== pilot.length) return fail();
    const changes = array(raw.playlist_changes, 64).map((v) => {
      const row = object(v);
      return { name: text(row.name, 100), added_count: integer(row.added_count), removed_count: integer(row.removed_count) };
    });
    const rules = array(raw.rules, 6).map((v) => {
      const row = object(v); const scene = text(row.scene, 160);
      if (!SCENE_LABELS.includes(scene)) return fail();
      return { scene, include: texts(row.include, 16), exclude: texts(row.exclude, 16) };
    });
    if (new Set(changes.map((v) => v.name)).size !== changes.length || new Set(rules.map((v) => v.scene)).size !== rules.length) return fail();
    quality = { source_version: version, draft_status: raw.draft_status as ClassificationQuality['draft_status'],
      revision: integer(raw.revision, Number.MAX_SAFE_INTEGER), changed_count: integer(raw.changed_count), review_count: integer(raw.review_count),
      pilot_positions: pilot, playlist_changes: changes, rules };
    if (raw.draft_summary !== undefined) {
      const summary = object(raw.draft_summary);
      quality.draft_summary = { pending_count: integer(summary.pending_count),
        unknown_style_count: integer(summary.unknown_style_count), unknown_language_count: integer(summary.unknown_language_count) };
      if (quality.draft_summary.unknown_style_count > quality.draft_summary.pending_count ||
        quality.draft_summary.unknown_language_count > quality.draft_summary.pending_count) return fail();
    }
  }
  const records = array(root.records, 50).map((value) => {
    const row = object(value);
    const position = integer(row.position);
    if (position === 0) return fail();
    const record = { position, name: text(row.name), artists: text(row.artists, 2048, true),
      styles: texts(row.styles), scenes: texts(row.scenes), language: text(row.language, 160),
      pending_reasons: texts(row.pending_reasons, 16), evidence_note: text(row.evidence_note, 2048, true),
      language_evidence_note: text(row.language_evidence_note, 2048, true),
      ...(row.recording_hints !== undefined ? { recording_hints: texts(row.recording_hints, 16) } : {}) };
    if (!quality) return record;
    const recordKey = text(row.record_key, 32);
    const score = row.style_judgment_score;
    if (!/^[a-f0-9]{32}$/.test(recordKey) || !(score === null || typeof score === 'number' && Number.isFinite(score) && score >= 0 && score <= 1) ||
      typeof row.review_note !== 'boolean' || typeof row.needs_review !== 'boolean') return fail();
    let draft: ClassificationCorrection | null = null;
    if (row.draft !== null) {
      const raw = object(row.draft);
      draft = { styles: texts(raw.styles, 2), scenes: texts(raw.scenes, 3), language: text(raw.language, 160),
        reason: text(raw.reason, 1000, false, true), recording_note: text(raw.recording_note, 1000, true, true) };
      if (!validCorrection(draft)) return fail();
    }
    return { ...record, record_key: recordKey, style_judgment_score: score as number | null,
      review_note: row.review_note, needs_review: row.needs_review, review_reasons: texts(row.review_reasons, 16), draft };
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
    if (quality && (quality.changed_count > summary.source_count || quality.review_count > summary.source_count ||
      quality.pilot_positions.some((v) => v > summary!.source_count) ||
      quality.draft_summary && quality.draft_summary.pending_count > summary.source_count)) return fail();
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
      tag: request.tag, review: request.review, ...(filters.basis !== undefined || request.basis !== undefined ? { basis: basis as 'original' | 'draft' } : {}) }, pagination: { offset, limit, total, next_offset: expectedNext }, records,
    ...(quality ? { quality } : {}) };
}
