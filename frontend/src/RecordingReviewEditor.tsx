import { useId } from 'react';
import { SCENE_LABELS } from './classificationQuality';
import { previewRecordingReviewEdit, recordingReviewEditError } from './recordingReviewData';
import type { RecordingReviewDetail, RecordingReviewEdit, RecordingReviewRow, RecordingReviewSaveRequest } from './recordingReviewData';

export type RecordingReviewSession = {
  base: RecordingReviewDetail; edit: RecordingReviewEdit; expanded: boolean;
  saving?: boolean; error?: string; confirmDiscard?: boolean;
  pending?: { request: RecordingReviewSaveRequest; base: RecordingReviewDetail };
};
type Props = { row: RecordingReviewRow; session: RecordingReviewSession; blocked: string;
  onChange: (edit: RecordingReviewEdit) => void; onSave: () => void;
  onCollapse: () => void; onDiscard: (confirmed: boolean) => void };

export function RecordingReviewEditor({ row, session, blocked, onChange, onSave, onCollapse, onDiscard }: Props) {
  const id = useId();
  const { edit } = session;
  const locked = !!session.pending || !!session.saving;
  const error = recordingReviewEditError(row, edit);
  let preview: RecordingReviewRow | undefined;
  if (!error) { try { preview = previewRecordingReviewEdit(row, edit); } catch { /* validation message below */ } }
  const recording = (patch: Partial<RecordingReviewEdit['recording']>) => onChange({ ...edit, recording: { ...edit.recording, ...patch } });
  function decision(scene: string, value: string) {
    const existing = edit.scene_decisions.find(item => item.scene === scene);
    const others = edit.scene_decisions.filter(item => item.scene !== scene);
    onChange({ ...edit, scene_decisions: value ? [...others, { scene,
      decision: value as RecordingReviewRow['scene_decisions'][number]['decision'], reason: existing?.reason ?? '' }] : others });
  }
  const memberships = (value: string[]) => value.length ? value.join('、') : '无';
  return <form className="recording-review-editor" aria-label={`${row.name}填写核验`} onSubmit={event => {
    event.preventDefault(); if (!session.saving && (!blocked || session.pending) && !error) onSave();
  }} onKeyDown={event => {
    if ((event.ctrlKey || event.metaKey) && event.key === 'Enter' && !event.repeat && !event.nativeEvent.isComposing) {
      event.preventDefault(); if (!session.saving && (!blocked || session.pending) && !error) onSave();
    }
  }}>
    <div className="recording-review-editor-heading"><h4>填写本曲核验</h4>
      <button type="button" onClick={onCollapse}>收起填写</button></div>
    <p>另存后保留原包与其他曲目。未填写或待核验的场景保留原收录；本页输入会在切换页面时保留，关闭或重载浏览器后不保留。</p>
    {blocked && <p role="alert" className="recording-review-warning">{blocked}</p>}
    <fieldset disabled={locked}>
      <legend>录音依据</legend>
      <label htmlFor={`${id}-status`}>录音核验状态</label>
      <select id={`${id}-status`} value={edit.recording.status} onChange={event => recording({ status: event.currentTarget.value as RecordingReviewEdit['recording']['status'] })}>
        <option value="unresolved">录音版本待确认</option><option value="metadata_matched">目录信息已匹配</option>
        <option value="audio_verified">有人工试听记录（保存声明）</option>
      </select>
      {edit.recording.status === 'audio_verified' && <p className="recording-review-warning">这是您的人工试听声明，程序无法证明已试听或判断准确。请填写版本、备注、音频来源和试听区间。</p>}
      <label htmlFor={`${id}-version`}>版本说明</label>
      <input id={`${id}-version`} value={edit.recording.version} onChange={event => recording({ version: event.currentTarget.value })} />
      <label htmlFor={`${id}-note`}>核验备注</label>
      <textarea id={`${id}-note`} rows={3} value={edit.recording.note} onChange={event => recording({ note: event.currentTarget.value })} />
      <h5>依据来源（{edit.recording.evidence_sources.length}/20）</h5>
      <p>链接需为 HTTP(S)，填写标题与说明；目录匹配需要目录或歌词来源。</p>
      {edit.recording.evidence_sources.map((source, index) => {
        const update = (patch: Partial<typeof source>) => recording({ evidence_sources: edit.recording.evidence_sources.map((item, i) => i === index ? { ...item, ...patch } : item) });
        return <fieldset className="recording-review-editor-item" key={index}><legend>来源 {index + 1}</legend>
          <label htmlFor={`${id}-source-${index}-kind`}>来源 {index + 1} 类型</label>
          <select id={`${id}-source-${index}-kind`} value={source.kind} onChange={event => update({ kind: event.currentTarget.value as typeof source.kind })}>
            <option value="catalog">目录</option><option value="lyrics">歌词</option><option value="audio">音频</option>
          </select>
          {(['url', 'title', 'note'] as const).map((field, fieldIndex) => <div className="recording-review-editor-field" key={field}>
            <label htmlFor={`${id}-source-${index}-${field}`}>来源 {index + 1} {['链接', '标题', '说明'][fieldIndex]}</label>
            <input id={`${id}-source-${index}-${field}`} value={source[field]} onChange={event => update({ [field]: event.currentTarget.value })} />
          </div>)}
          <button type="button" onClick={() => recording({ evidence_sources: edit.recording.evidence_sources.filter((_, i) => i !== index) })}>移除来源 {index + 1}</button>
        </fieldset>;
      })}
      <button type="button" disabled={edit.recording.evidence_sources.length >= 20} onClick={() => recording({ evidence_sources: [...edit.recording.evidence_sources,
        { kind: 'catalog', url: '', title: '', note: '' }] })}>添加来源</button>
      <h5>试听区间（{edit.recording.listened_segments.length}/100）</h5>
      <p>起止时间单位为秒，结束须大于开始且不超过 86400 秒。音频链接须与一个音频来源完全一致。</p>
      {edit.recording.listened_segments.map((segment, index) => {
        const update = (patch: Partial<typeof segment>) => recording({ listened_segments: edit.recording.listened_segments.map((item, i) => i === index ? { ...item, ...patch } : item) });
        return <fieldset className="recording-review-editor-item" key={index}><legend>片段 {index + 1}</legend>
          {(['start_seconds', 'end_seconds'] as const).map((field, fieldIndex) => <div className="recording-review-editor-field" key={field}>
            <label htmlFor={`${id}-segment-${index}-${field}`}>片段 {index + 1} {fieldIndex ? '结束秒数' : '开始秒数'}</label>
            <input type="number" step="any" min="0" max="86400" id={`${id}-segment-${index}-${field}`} value={Number.isFinite(segment[field]) ? segment[field] : ''}
              onChange={event => update({ [field]: event.currentTarget.valueAsNumber })} />
          </div>)}
          {(['source_url', 'note'] as const).map((field, fieldIndex) => <div className="recording-review-editor-field" key={field}>
            <label htmlFor={`${id}-segment-${index}-${field}`}>片段 {index + 1} {fieldIndex ? '说明' : '音频链接'}</label>
            <input id={`${id}-segment-${index}-${field}`} value={segment[field]} onChange={event => update({ [field]: event.currentTarget.value })} />
          </div>)}
          <button type="button" onClick={() => recording({ listened_segments: edit.recording.listened_segments.filter((_, i) => i !== index) })}>移除片段 {index + 1}</button>
        </fieldset>;
      })}
      <button type="button" disabled={edit.recording.listened_segments.length >= 100} onClick={() => recording({ listened_segments: [...edit.recording.listened_segments,
        { start_seconds: Number.NaN, end_seconds: Number.NaN, source_url: '', note: '' }] })}>添加试听片段</button>
    </fieldset>
    <fieldset disabled={locked}><legend>场景判断</legend>
      <p>明确选择适合或不适合前，须有人工试听记录；每项已填写判断都需要理由。新收录最多 3 个场景。</p>
      <div className="recording-review-editor-scenes">{SCENE_LABELS.map((scene, index) => {
        const value = edit.scene_decisions.find(item => item.scene === scene);
        return <div className="recording-review-editor-field" key={scene}>
          <label htmlFor={`${id}-scene-${index}`}>{scene}判断</label>
          <select id={`${id}-scene-${index}`} value={value?.decision ?? ''} onChange={event => decision(scene, event.currentTarget.value)}>
            <option value="">未填写（保留原收录）</option><option value="uncertain">待核验（保留原收录）</option>
            <option value="suitable">适合收录</option><option value="unsuitable">不适合收录</option>
          </select>
          {value && <><label htmlFor={`${id}-reason-${index}`}>{scene}理由</label>
            <textarea rows={2} id={`${id}-reason-${index}`} value={value.reason} onChange={event => onChange({ ...edit,
              scene_decisions: edit.scene_decisions.map(item => item.scene === scene ? { ...item, reason: event.currentTarget.value } : item) })} /></>}
        </div>;
      })}</div>
    </fieldset>
    {error && <p role="alert" className="recording-review-warning">{error}</p>}
    {preview ? <dl className="recording-review-memberships" aria-label="填写预览">
      <dt>原收录</dt><dd>{memberships(preview.before_scenes)}</dd><dt>新收录</dt><dd>{memberships(preview.after_scenes)}</dd>
      <dt>拟新增</dt><dd>{memberships(preview.added_scenes)}</dd><dt>拟移除</dt><dd>{memberships(preview.removed_scenes)}</dd>
    </dl> : <p>填写有效后显示收录变化预览。</p>}
    {session.pending && !session.saving && <p role="alert" className="recording-review-warning">保存结果尚未确认，输入已冻结。请核对并重试同一保存，避免生成重复核验包。目标：{session.pending.request.target_bundle}</p>}
    {session.error && <p role="alert" className="recording-review-error">{session.error}</p>}
    <div className="recording-review-editor-actions">
      <button className="button primary" type="submit" disabled={session.saving || !!error || (!!blocked && !session.pending)}>
        {session.saving ? '正在保存并核对…' : session.pending ? '核对并重试保存' : '另存新核验包'}</button>
      {!locked && <button type="button" onClick={() => onDiscard(false)}>放弃本曲填写</button>}
      {!locked && session.confirmDiscard && <><span>确认清除本曲未保存输入？</span>
        <button type="button" onClick={() => onDiscard(true)}>确认放弃填写</button></>}
    </div><p>快捷键：Ctrl / ⌘ + Enter 保存。</p>
  </form>;
}
