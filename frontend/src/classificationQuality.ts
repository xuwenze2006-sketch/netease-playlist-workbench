import type { ClassificationCorrection, ClassificationDraftRequest, ClassificationReview } from './types';

export const STYLE_LABELS = ['流行抒情', 'R&B 与灵魂', 'Funk 与 Disco', '爵士与 Lo-Fi', '嘻哈说唱',
  '摇滚与独立', '民谣与木吉他', '电子舞曲', '纯音乐与影视配乐', '古典与合唱', '待辨识'];
export const SCENE_LABELS = ['学习专注', '通勤散步', '放松睡前', '运动提神', '快乐律动', '独处释怀'];
export const LANGUAGE_LABELS = ['国语', '粤语', '英语', '日语', '韩语', '俄语', '其他与多语', '器乐或配乐录音', '待辨识'];
export const REVIEW_LABELS: Record<ClassificationReview, string> = {
  all: '全部曲目', pending: '待辨识', needs_review: '全部需复核', undrafted_review: '需复核且无草稿', conflict: '证据冲突',
  low_confidence: '低把握', weak_evidence: '依据不足', pilot: '试点样本', draft: '已有本地修正',
  version: '版本线索',
};

export function correctionTextError(value: unknown, required = false): string {
  if (typeof value !== 'string') return '请填写有效文本。';
  if (required && !value.trim()) return '请填写具体的修正理由。';
  if (Array.from(value).length > 1000) return '最多填写 1000 个字符，请缩短内容。';
  if (/[\u0000-\u0009\u000b-\u001f\u007f-\u009f\ud800-\udfff]/u.test(value))
    return '含有不支持的控制字符，请删除后重试。';
  return '';
}
function safeText(value: unknown, empty = false): value is string {
  return correctionTextError(value, !empty) === '';
}
function labels(value: unknown, allowed: string[], maximum: number): value is string[] {
  return Array.isArray(value) && value.length <= maximum && new Set(value).size === value.length &&
    value.every((label) => typeof label === 'string' && allowed.includes(label));
}
export function validCorrection(value: ClassificationCorrection): boolean {
  return labels(value.styles, STYLE_LABELS, 2) && value.styles.length >= 1 &&
    (!value.styles.includes('待辨识') || value.styles.length === 1) && labels(value.scenes, SCENE_LABELS, 3) &&
    LANGUAGE_LABELS.includes(value.language) && safeText(value.reason) && safeText(value.recording_note, true);
}
export function validDraftRequest(value: ClassificationDraftRequest): boolean {
  return typeof value.source_version === 'string' && /^[a-f0-9]{64}$/.test(value.source_version) &&
    typeof value.record_key === 'string' && /^[a-f0-9]{32}$/.test(value.record_key) &&
    Number.isSafeInteger(value.revision) && value.revision >= 0 &&
    (value.action === 'remove' || value.action === 'save' && validCorrection(value));
}
