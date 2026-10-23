import { SCENE_LABELS } from './classificationQuality';

export type RecordingReviewCatalog = {
  status: 'available' | 'empty'; bundles: { name: string }[]; limit: 100; truncated: false;
};
export type RecordingReviewRow = {
  position: number; name: string; artists: string;
  before_scenes: string[]; after_scenes: string[]; added_scenes: string[]; removed_scenes: string[];
  scene_decisions: { scene: string; decision: 'suitable' | 'unsuitable' | 'uncertain'; reason: string }[];
  recording: {
    status: 'unresolved' | 'metadata_matched' | 'audio_verified'; version: string; note: string;
    evidence_sources: { url: string; title: string; kind: 'catalog' | 'lyrics' | 'audio'; note: string }[];
    listened_segments: { start_seconds: number; end_seconds: number; source_url: string; note: string }[];
  };
};
export type RecordingReviewSummary = {
  reviewed_records: number; metadata_matched_records: number; audio_verified_records: number;
  scene_verified_records: number; uncertain_records: number; partial_scene_records: number;
  added_count: number; removed_count: number;
};
export type RecordingReviewPreview = {
  kind: 'classification_scene_review_preview'; source_sha256: string;
  summary: RecordingReviewSummary; records: RecordingReviewRow[];
};
export type RecordingReviewDetail = {
  status: 'available'; bundle: string; created_at: string; preview: RecordingReviewPreview; review_sha256?: string;
};
export type RecordingReviewEdit = Pick<RecordingReviewRow, 'recording' | 'scene_decisions'>;
export type RecordingReviewSaveRequest = RecordingReviewEdit & {
  base_bundle: string; base_sha256: string; target_bundle: string; created_at: string; position: number;
};

function invalid(): never { throw new Error('invalid recording review data'); }
function object(value: unknown): Record<string, unknown> {
  if (!value || typeof value !== 'object' || Array.isArray(value)) invalid();
  return value as Record<string, unknown>;
}
function text(value: unknown, empty = false, max = 2000): string {
  if (typeof value !== 'string' || Array.from(value).length > max || (!empty && !value.trim()) ||
    /[\u0000-\u0009\u000b-\u001f\u007f-\u009f\ud800-\udfff]/u.test(value)) invalid();
  return value;
}
function array<T>(value: unknown, max: number, parse: (item: unknown) => T): T[] {
  if (!Array.isArray(value) || value.length > max) invalid();
  return value.map(parse);
}
function choice<T extends string>(value: unknown, choices: readonly T[]): T {
  if (typeof value !== 'string' || !choices.includes(value as T)) invalid();
  return value as T;
}
function integer(value: unknown, min = 0, max = 10000): number {
  if (typeof value !== 'number' || !Number.isSafeInteger(value) || value < min || value > max) invalid();
  return value;
}
function same(left: string[], right: string[]) { return left.length === right.length && left.every((v, i) => v === right[i]); }
function unique(values: unknown[]) { if (new Set(values).size !== values.length) invalid(); }
function scenes(value: unknown): string[] {
  const result = array(value, 3, item => choice(item, SCENE_LABELS)); unique(result); return result;
}
function url(value: unknown): string {
  const result = text(value);
  if (/\s|\\/u.test(result)) invalid();
  try {
    const parsed = new URL(result);
    if (!['http:', 'https:'].includes(parsed.protocol) || !parsed.hostname || parsed.username || parsed.password ||
      !/^https?:\/\//i.test(result) || result.slice(result.indexOf('//') + 2).split(/[/?#]/, 1)[0].includes('@')) invalid();
  } catch { invalid(); }
  return result;
}
export function validReviewBundleName(value: unknown): value is string {
  return typeof value === 'string' && /^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$/.test(value) &&
    !/^(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])$/i.test(value);
}
function name(value: unknown): string { if (!validReviewBundleName(value)) invalid(); return value; }

export function normalizeRecordingReviews(value: unknown): RecordingReviewCatalog {
  const raw = object(value), status = choice(raw.status, ['available', 'empty'] as const);
  const bundles = array(raw.bundles, 100, item => ({ name: name(object(item).name) }));
  unique(bundles.map(item => item.name));
  if (raw.limit !== 100 || raw.truncated !== false || (status === 'empty') !== (bundles.length === 0)) invalid();
  return { status, bundles, limit: 100, truncated: false };
}

function recording(value: unknown): RecordingReviewRow['recording'] {
  const raw = object(value);
  const status = choice(raw.status, ['unresolved', 'metadata_matched', 'audio_verified'] as const);
  const version = text(raw.version, status === 'unresolved'), note = text(raw.note, status === 'unresolved', 8000);
  const evidence_sources = array(raw.evidence_sources, 20, item => {
    const source = object(item);
    return { url: url(source.url), title: text(source.title),
      kind: choice(source.kind, ['catalog', 'lyrics', 'audio'] as const), note: text(source.note) };
  });
  unique(evidence_sources.map(source => JSON.stringify([source.url, source.kind])));
  const audioUrls = new Set(evidence_sources.filter(source => source.kind === 'audio').map(source => source.url));
  const listened_segments = array(raw.listened_segments, 100, item => {
    const segment = object(item), start = segment.start_seconds, end = segment.end_seconds;
    if (typeof start !== 'number' || typeof end !== 'number' || !Number.isFinite(start) || !Number.isFinite(end) ||
      start < 0 || end <= start || end > 86400) invalid();
    const source_url = url(segment.source_url);
    if (!audioUrls.has(source_url)) invalid();
    return { start_seconds: start, end_seconds: end, source_url, note: text(segment.note) };
  });
  if ((status === 'audio_verified' && (!audioUrls.size || !listened_segments.length)) ||
    (status !== 'audio_verified' && listened_segments.length > 0) ||
    (status === 'metadata_matched' && !evidence_sources.some(source => source.kind !== 'audio'))) invalid();
  return { status, version, note, evidence_sources, listened_segments };
}

function parseRow(value: unknown): RecordingReviewRow {
  const raw = object(value), rec = recording(raw.recording);
  const before_scenes = scenes(raw.before_scenes), after_scenes = scenes(raw.after_scenes);
  const added_scenes = scenes(raw.added_scenes), removed_scenes = scenes(raw.removed_scenes);
  const scene_decisions = array(raw.scene_decisions, SCENE_LABELS.length, item => {
    const rawDecision = object(item);
    const decision = choice(rawDecision.decision, ['suitable', 'unsuitable', 'uncertain'] as const);
    if (decision !== 'uncertain' && rec.status !== 'audio_verified') invalid();
    return { scene: choice(rawDecision.scene, SCENE_LABELS), decision, reason: text(rawDecision.reason) };
  });
  unique(scene_decisions.map(item => item.scene));
  const expected = [...before_scenes];
  for (const item of scene_decisions) {
    const index = expected.indexOf(item.scene);
    if (item.decision === 'unsuitable' && index >= 0) expected.splice(index, 1);
    if (item.decision === 'suitable' && index < 0) expected.push(item.scene);
  }
  if (!same(expected, after_scenes) || !same(after_scenes.filter(scene => !before_scenes.includes(scene)), added_scenes) ||
    !same(before_scenes.filter(scene => !after_scenes.includes(scene)), removed_scenes)) invalid();
  return { position: integer(raw.position, 1), name: text(raw.name), artists: text(raw.artists),
    before_scenes, after_scenes, added_scenes, removed_scenes, scene_decisions, recording: rec };
}

export function isSceneComplete(row: RecordingReviewRow): boolean {
  const scope = new Set([...row.before_scenes, ...row.scene_decisions.map(item => item.scene)]);
  return scope.size > 0 && row.scene_decisions.filter(item => item.decision !== 'uncertain').length === scope.size;
}

export function normalizeRecordingReview(value: unknown, bundle: string): RecordingReviewDetail {
  const raw = object(value), preview = object(raw.preview), rawSummary = object(preview.summary);
  if (raw.status !== 'available' || name(raw.bundle) !== bundle || preview.kind !== 'classification_scene_review_preview' ||
    typeof preview.source_sha256 !== 'string' || !/^[a-f0-9]{64}$/.test(preview.source_sha256)) invalid();
  if (raw.review_sha256 !== undefined && (typeof raw.review_sha256 !== 'string' || !/^[a-f0-9]{64}$/.test(raw.review_sha256))) invalid();
  const created_at = text(raw.created_at, false, 64);
  if (!/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?(?:Z|[+-]\d{2}:\d{2})$/.test(created_at) ||
    !Number.isFinite(Date.parse(created_at))) invalid();
  // Date.parse normalizes impossible dates such as February 30 instead of rejecting them.
  const calendarDay = new Date(`${created_at.slice(0, 10)}T00:00:00Z`);
  if (!Number.isFinite(calendarDay.valueOf()) || calendarDay.toISOString().slice(0, 10) !== created_at.slice(0, 10)) invalid();
  const records = array(preview.records, 50, parseRow);
  if (!records.length) invalid();
  unique(records.map(row => row.position));
  const summary = summarizeRecordingReviews(records);
  for (const key of Object.keys(summary) as (keyof RecordingReviewSummary)[]) {
    if (integer(rawSummary[key]) !== summary[key]) invalid();
  }
  return { status: 'available', bundle, created_at, preview: { kind: 'classification_scene_review_preview',
    source_sha256: preview.source_sha256, records, summary },
    ...(typeof raw.review_sha256 === 'string' ? {review_sha256: raw.review_sha256} : {}) };
}

function summarizeRecordingReviews(records: RecordingReviewRow[]): RecordingReviewSummary {
  const summary: RecordingReviewSummary = { reviewed_records: records.length, metadata_matched_records: 0,
    audio_verified_records: 0, scene_verified_records: 0, uncertain_records: 0, partial_scene_records: 0, added_count: 0, removed_count: 0 };
  for (const row of records) {
    summary.metadata_matched_records += Number(row.recording.status === 'metadata_matched');
    summary.audio_verified_records += Number(row.recording.status === 'audio_verified');
    const complete = isSceneComplete(row);
    summary.scene_verified_records += Number(complete);
    summary.uncertain_records += Number(!complete);
    summary.partial_scene_records += Number(!complete && row.scene_decisions.some(item => item.decision !== 'uncertain'));
    summary.added_count += row.added_scenes.length; summary.removed_count += row.removed_scenes.length;
  }
  return summary;
}

export function previewRecordingReviewEdit(row: RecordingReviewRow, edit: RecordingReviewEdit): RecordingReviewRow {
  const expected = [...row.before_scenes];
  for (const item of edit.scene_decisions) {
    const index = expected.indexOf(item.scene);
    if (item.decision === 'unsuitable' && index >= 0) expected.splice(index, 1);
    if (item.decision === 'suitable' && index < 0) expected.push(item.scene);
  }
  return parseRow({...row, ...edit, after_scenes: expected,
    added_scenes: expected.filter(scene => !row.before_scenes.includes(scene)),
    removed_scenes: row.before_scenes.filter(scene => !expected.includes(scene))});
}

export function recordingReviewEditError(row: RecordingReviewRow, edit: RecordingReviewEdit): string {
  try { previewRecordingReviewEdit(row, edit); return ''; }
  catch {
    if (edit.recording.status !== 'audio_verified' && edit.scene_decisions.some(item => item.decision !== 'uncertain'))
      return '明确适合或不适合需要具体音频来源和真实试听区间；尚未试听请选择待核验。';
    return '请检查录音状态、必填依据、来源链接、试听区间和场景理由；预览收录最多保留 3 个场景。';
  }
}

export function expectedRecordingReviewSave(base: RecordingReviewDetail, request: RecordingReviewSaveRequest): RecordingReviewDetail {
  const original = normalizeRecordingReview(base, base.bundle), raw = object(request);
  const fields = ['base_bundle', 'base_sha256', 'target_bundle', 'created_at', 'position', 'recording', 'scene_decisions'];
  if (Object.keys(raw).length !== fields.length || fields.some(field => !(field in raw)) ||
    !original.review_sha256 || raw.base_bundle !== original.bundle || raw.base_sha256 !== original.review_sha256 ||
    typeof raw.target_bundle !== 'string' || !/^review-[a-f0-9]{32}$/.test(raw.target_bundle) ||
    raw.target_bundle === raw.base_bundle) invalid();
  const position = integer(raw.position, 1), row = original.preview.records.find(item => item.position === position);
  if (!row) invalid();
  const updated = previewRecordingReviewEdit(row, request);
  const records = original.preview.records.map(item => item.position === position ? updated : item);
  const expected = normalizeRecordingReview({ status: 'available', bundle: request.target_bundle,
    created_at: request.created_at, preview: {...original.preview, records, summary: summarizeRecordingReviews(records)} }, request.target_bundle);
  if (new TextEncoder().encode(JSON.stringify(request)).byteLength > 65536)
    throw new Error('本次核验内容超过 64 KiB，请缩短来源说明或试听备注后保存。');
  return expected;
}

export function createRecordingReviewSave(base: RecordingReviewDetail, position: number, edit: RecordingReviewEdit): RecordingReviewSaveRequest {
  const request = {base_bundle: base.bundle, base_sha256: base.review_sha256 ?? '',
    target_bundle: `review-${crypto.randomUUID().replace(/-/g, '')}`, created_at: new Date().toISOString(), position,
    recording: edit.recording, scene_decisions: edit.scene_decisions};
  expectedRecordingReviewSave(base, request);
  return JSON.parse(JSON.stringify(request)) as RecordingReviewSaveRequest;
}
