import { useEffect, useState } from 'react';
import type { ClassificationCorrection, ClassificationDraftRequest, ClassificationQuality, ClassificationRecord } from './types';
import { LANGUAGE_LABELS, SCENE_LABELS, STYLE_LABELS, validCorrection } from './classificationQuality';

type Props = {
  track: ClassificationRecord;
  quality: ClassificationQuality;
  disabled: boolean;
  onSubmit: (request: ClassificationDraftRequest) => Promise<void>;
};
function initialValue(track: ClassificationRecord): ClassificationCorrection {
  return track.draft ?? { styles: track.styles, scenes: track.scenes, language: track.language,
    reason: '', recording_note: '' };
}
function labels(value: string[]) { return value.length ? value.join('、') : '未指定'; }

export function ClassificationEditor({ track, quality, disabled, onSubmit }: Props) {
  const [editing, setEditing] = useState(false);
  const [value, setValue] = useState(() => initialValue(track));
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState('');
  const draftIdentity = JSON.stringify(track.draft);
  useEffect(() => {
    if (!editing) setValue(initialValue(track));
  }, [draftIdentity, editing, track.styles, track.scenes, track.language]);
  const blocked = disabled || submitting || quality.draft_status !== 'ready';
  function toggle(kind: 'styles' | 'scenes', label: string) {
    setValue((current) => {
      let selected = current[kind].includes(label) ? current[kind].filter((v) => v !== label) : [...current[kind], label];
      if (kind === 'styles' && label === '待辨识' && selected.includes(label)) selected = [label];
      else if (kind === 'styles' && label !== '待辨识') selected = selected.filter((v) => v !== '待辨识');
      return { ...current, [kind]: selected };
    });
  }
  async function submit(action: 'save' | 'remove') {
    if (blocked || !track.record_key || action === 'save' && !validCorrection(value)) return;
    setSubmitting(true); setError('');
    try {
      const common = { source_version: quality.source_version, revision: quality.revision, record_key: track.record_key };
      await onSubmit(action === 'save' ? { ...common, action, ...value } : { ...common, action });
      setEditing(false);
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : '本地修正未保存，请刷新记录后核对。');
    } finally { setSubmitting(false); }
  }

  return (
    <div className="classification-correction">
      {track.draft && (
        <div className="classification-diff" aria-label="本地修正前后对照">
          <strong>本地修正草稿</strong>
          <dl>
            <dt>风格</dt><dd>{labels(track.styles)} → {labels(track.draft.styles)}</dd>
            <dt>场景</dt><dd>{labels(track.scenes)} → {labels(track.draft.scenes)}</dd>
            <dt>语言</dt><dd>{track.language} → {track.draft.language}</dd>
          </dl>
          <p>{track.draft.reason}</p>
          {track.draft.recording_note && <p>录音版本依据：{track.draft.recording_note}</p>}
        </div>
      )}
      <div className="classification-correction-actions">
        <button className="classification-inline" disabled={blocked} onClick={() => { setError(''); setEditing((v) => !v); }}>
          {editing ? '收起修正' : track.draft ? '编辑本地修正' : '修正分类'}
        </button>
        {track.draft && <button className="classification-inline" disabled={blocked} onClick={() => { void submit('remove'); }}>撤销本地修正</button>}
      </div>
      {editing && (
        <form onSubmit={(event) => { event.preventDefault(); void submit('save'); }}>
          <fieldset className="classification-editor" aria-label={`修正${track.name}`} disabled={blocked}>
            <legend>修正 {track.name}</legend>
            <p>结合具体录音版本核对标签，再记录修正理由。</p>
            <fieldset className="classification-editor-labels">
              <legend>风格（选 1–2 项）</legend>
              {STYLE_LABELS.map((label) => (
                <label key={label}><input type="checkbox" checked={value.styles.includes(label)}
                  disabled={!value.styles.includes(label) && label !== '待辨识' && value.styles.filter((v) => v !== '待辨识').length >= 2}
                  onChange={() => toggle('styles', label)} />{label === '待辨识' ? '风格待辨识' : label}</label>
              ))}
            </fieldset>
            <fieldset className="classification-editor-labels">
              <legend>场景（最多 3 项）</legend>
              {SCENE_LABELS.map((label) => (
                <label key={label}><input type="checkbox" checked={value.scenes.includes(label)}
                  disabled={!value.scenes.includes(label) && value.scenes.length >= 3}
                  onChange={() => toggle('scenes', label)} />{label}</label>
              ))}
            </fieldset>
            <label className="classification-editor-input">语言
              <select value={value.language} onChange={(event) => setValue((v) => ({ ...v, language: event.target.value }))}>
                {LANGUAGE_LABELS.map((label) => <option key={label}>{label}</option>)}
              </select>
            </label>
            <label className="classification-editor-input">修正理由
              <input value={value.reason} maxLength={1000} required placeholder="记录试听或核查依据，以及标签为什么需要调整"
                onChange={(event) => setValue((v) => ({ ...v, reason: event.target.value }))} />
            </label>
            <label className="classification-editor-input">录音版本依据
              <input value={value.recording_note} maxLength={1000} placeholder="例如专辑版本、现场、翻唱、混音或伴奏；尚未核对可留空"
                onChange={(event) => setValue((v) => ({ ...v, recording_note: event.target.value }))} />
            </label>
            <button className="button primary" disabled={blocked || !validCorrection(value)} type="submit">保存本地修正</button>
          </fieldset>
        </form>
      )}
      {error && <p className="classification-editor-error" role="alert">{error}</p>}
    </div>
  );
}

export function ClassificationQualitySummary({ quality, onReview }: { quality: ClassificationQuality; onReview: (review: 'needs_review' | 'pilot' | 'draft') => void }) {
  return (
    <section className="classification-quality" aria-label="分类质量与修正草稿">
      <div className="classification-quality-heading">
        <strong>需复核 · {quality.review_count.toLocaleString('zh-CN')} 首</strong>
        <button className="classification-inline" onClick={() => onReview('needs_review')}>查看全部需复核</button>
        <button className="classification-inline" disabled={!quality.pilot_positions.length} onClick={() => onReview('pilot')}>查看 {quality.pilot_positions.length} 首试点样本</button>
        <strong>本地修正 · {quality.changed_count.toLocaleString('zh-CN')} 首</strong>
        <button className="classification-inline" disabled={!quality.changed_count} onClick={() => onReview('draft')}>查看本地修正</button>
      </div>
      <p>草稿尚未写入网易云歌单；下方对照展示现有分类与本地修正。</p>
      {quality.draft_status === 'stale' && <p className="classification-editor-error" role="status">来源记录已变化，已有草稿需要重新核对，暂时无法保存修正。</p>}
      {quality.draft_status === 'unavailable' && <p className="classification-editor-error" role="status">本地修正草稿暂不可用，请检查记录后重新读取。</p>}
      {quality.playlist_changes.length > 0 && (
        <details className="classification-change-preview">
          <summary>查看歌单增删预览</summary>
          <ul>{quality.playlist_changes.map((change) => <li key={change.name}><strong>{change.name}</strong><span>加入 {change.added_count} 首</span><span>移出 {change.removed_count} 首</span></li>)}</ul>
        </details>
      )}
      {quality.rules.length > 0 && (
        <details className="classification-scene-rules">
          <summary>查看场景歌单收录标准</summary>
          <div>{quality.rules.map((rule) => <article key={rule.scene}><h3>{rule.scene}</h3>
            <p>收录：{rule.include.join('；')}</p><p>排除：{rule.exclude.join('；')}</p></article>)}</div>
        </details>
      )}
    </section>
  );
}
