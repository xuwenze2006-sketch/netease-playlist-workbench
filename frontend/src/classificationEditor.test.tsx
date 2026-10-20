import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';
import { ClassificationEditor } from './ClassificationEditor';
import type { ClassificationCorrection, ClassificationQuality, ClassificationRecord } from './types';

const correction: ClassificationCorrection = { styles: ['流行抒情'], scenes: ['通勤散步'], language: '国语',
  reason: '已核对专辑录音', recording_note: '专辑版' };
const quality: ClassificationQuality = { source_version: 'a'.repeat(64), revision: 1, draft_status: 'ready',
  changed_count: 1, review_count: 0, pilot_positions: [], playlist_changes: [], rules: [] };
const track: ClassificationRecord = { position: 1, record_key: 'b'.repeat(32), name: '示例歌曲', artists: '示例歌手',
  styles: ['流行抒情'], scenes: ['通勤散步'], language: '国语', pending_reasons: [],
  evidence_note: '', language_evidence_note: '', draft: correction };

function open() {
  const submit = vi.fn().mockResolvedValue(undefined);
  render(<ClassificationEditor track={track} quality={quality} disabled={false} onSubmit={submit} />);
  fireEvent.click(screen.getByRole('button', { name: '编辑本地修正' }));
  return { submit, reason: screen.getByLabelText('修正理由'), note: screen.getByLabelText('录音版本依据'),
    save: screen.getByRole('button', { name: '保存本地修正' }) };
}

describe('classification editor validation and unchanged corrections', () => {
  it('does not post an unchanged saved correction from the button, shortcut, or form', () => {
    const { submit, reason, save } = open();
    expect(save).toHaveProperty('disabled', true);
    expect(screen.getByText('当前内容与已保存修正一致，无需重复保存。')).toBeTruthy();
    fireEvent.click(save);
    fireEvent.keyDown(reason, { key: 'Enter', ctrlKey: true });
    fireEvent.submit(save.closest('form')!);
    expect(submit).not.toHaveBeenCalled();
  });

  it('disables save again after restoring the saved value', () => {
    const { reason, save } = open();
    fireEvent.change(reason, { target: { value: '新的具体依据' } });
    expect(save).toHaveProperty('disabled', false);
    fireEvent.change(reason, { target: { value: correction.reason } });
    expect(save).toHaveProperty('disabled', true);
  });

  it('allows one changed correction and still permits removing a saved correction', async () => {
    const { submit, reason, save } = open();
    fireEvent.change(reason, { target: { value: '新的具体依据' } });
    fireEvent.click(save);
    await waitFor(() => expect(submit).toHaveBeenCalledTimes(1));
    expect(submit.mock.calls[0][0]).toMatchObject({ action: 'save', reason: '新的具体依据', revision: 1 });
    await waitFor(() => expect(screen.getByRole('button', { name: '撤销本地修正' })).toHaveProperty('disabled', false));
    fireEvent.click(screen.getByRole('button', { name: '撤销本地修正' }));
    await waitFor(() => expect(submit).toHaveBeenCalledTimes(2));
    expect(submit.mock.calls[1][0]).toMatchObject({ action: 'remove', revision: 1 });
  });

  it.each(['修正理由', '录音版本依据'])('explains unsupported characters in %s and clears the feedback after correction', (label) => {
    const { submit, save } = open();
    const field = screen.getByLabelText(label);
    fireEvent.change(field, { target: { value: '复制的依据\t包含制表符' } });
    expect(field.getAttribute('aria-invalid')).toBe('true');
    const help = document.getElementById(field.getAttribute('aria-describedby')!);
    expect(help?.textContent).toContain('含有不支持的控制字符');
    expect(save).toHaveProperty('disabled', true);
    fireEvent.keyDown(field, { key: 'Enter', ctrlKey: true });
    expect(submit).not.toHaveBeenCalled();
    fireEvent.change(field, { target: { value: '清理后的依据\n支持换行' } });
    expect(field.getAttribute('aria-invalid')).not.toBe('true');
    expect(save).toHaveProperty('disabled', false);
  });

  it('counts Unicode characters consistently with the API and explains overlong evidence', () => {
    const { reason, save } = open();
    fireEvent.change(reason, { target: { value: '🎵'.repeat(1000) } });
    const help = document.getElementById(reason.getAttribute('aria-describedby')!);
    expect(help?.textContent).toContain('1000 / 1000');
    expect(reason.getAttribute('maxlength')).toBe('2000');
    expect(save).toHaveProperty('disabled', false);
    fireEvent.change(reason, { target: { value: '字'.repeat(1001) } });
    expect(help?.textContent).toContain('1001 / 1000');
    expect(help?.textContent).toContain('最多填写 1000 个字符');
    expect(reason.getAttribute('aria-invalid')).toBe('true');
    expect(save).toHaveProperty('disabled', true);
  });
});
