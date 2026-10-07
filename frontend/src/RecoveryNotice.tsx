import type { WriteRecovery } from './types';

export function RecoveryNotice({
  recovery,
  blocked,
  onReconcile,
  onRecords,
  canRenewAuthorization = false,
  onRenewAuthorization,
}: {
  recovery?: WriteRecovery;
  blocked: boolean;
  onReconcile: () => void;
  onRecords: () => void;
  canRenewAuthorization?: boolean;
  onRenewAuthorization?: () => void;
}) {
  if (recovery?.status !== 'review_required') return null;
  const canReconcile = recovery.operation === 'renames' && recovery.can_reconcile;
  const classification = recovery.operation === 'classification';
  const canRenew = canReconcile && canRenewAuthorization;
  return (
    <section className="diagnostic-notice recovery-notice" aria-label="上次操作待核对">
      <div>
        <span className="eyebrow">LOCAL RECOVERY</span>
        <h2>上次操作待核对</h2>
        <p>
          {classification
            ? '分类记录与已确认批次已保留。核对完成前，新的账号修改暂时暂停。'
            : '本地保留了未确认的操作记录。核对完成前，新的账号修改暂时暂停。'}
        </p>
        <p className="subtle">
          {classification
            ? '分类任务需专用核验后续做：先确认账号与每个已完成批次，再决定是否继续未尝试的部分。当前工作台未提供该写入入口，请使用分类工具；普通继续任务和只读核对都不会续做分类。'
            : canReconcile
              ? '只读取账号和歌单，核对上次改名结果；不会重新修改名称或创建歌单。'
              : '请先查看本地任务记录，确认已经执行的步骤，避免重复提交。'}
        </p>
        {canRenew && (
          <p className="subtle">
            沿用已保存凭证重新扫码。授权完成后仍需主动只读核对，整理任务不会自动继续。
          </p>
        )}
      </div>
      <div className="diagnostic-actions">
        {canRenew && onRenewAuthorization && (
          <button className="button secondary" disabled={blocked} onClick={onRenewAuthorization}>
            重新扫码后只读核对
          </button>
        )}
        {canReconcile && (
          <button className="button secondary" disabled={blocked} onClick={onReconcile}>
            只读核对上次改名
          </button>
        )}
        <a className="button quiet" href="#history" onClick={onRecords}>
          查看任务记录
        </a>
      </div>
    </section>
  );
}
