import type { Job, NextStep } from './types';

export function recordNotSaved(job: Job | null | undefined): boolean {
  return job?.result?.record_saved === false && job.result.applied_to_account === true;
}

export function needsWriteInspection(job: Job | null | undefined): boolean {
  return (
    !!job &&
    (job.status === 'uncertain' ||
      job.result?.status === 'uncertain' ||
      (job.result?.write_attempted === true && job.result.outcome_known === false) ||
      recordNotSaved(job))
  );
}

const GUIDANCE: Record<NextStep, string> = {
  install_cli: '先完成本机安装步骤，再检查本地接入。',
  configure_credentials: '请先保存开放平台的 App ID 和私钥文件。',
  correct_credentials: '请检查 App ID 与 UTF-8 私钥文件，再主动保存。',
  check_local_access: '请在接入设置检查本地环境与文件访问权限。',
  load_desktop_playlists: '请在桌面网易云登录目标账号并加载歌单，再主动读取本地歌单。',
  authorize_correct_account: '请先核对桌面网易云账号，再为目标账号重新扫码授权。',
  inspect_records: '请先查看任务记录并核对结果，再决定下一步。',
};

export function DiagnosticNotice({
  job,
  onNavigate,
  onReadLocal,
  readBlocked,
}: {
  job: Job | null;
  onNavigate: (page: 'settings' | 'history') => void;
  onReadLocal: () => void;
  readBlocked: boolean;
}) {
  if (!job) return null;
  const unsafe = needsWriteInspection(job);
  const missingRecord = recordNotSaved(job);
  const next = unsafe ? 'inspect_records' : job.result?.next_step;
  if (!next) return null;
  const page = next === 'inspect_records' ? 'history' : 'settings';
  return (
    <section className="diagnostic-notice" aria-label="任务处理建议">
      <div>
        <h2>
          {missingRecord
            ? '本地记录未保存，请勿重复提交。'
            : unsafe
              ? '任务结果需要核对'
              : '接下来可以这样处理'}
        </h2>
        {job.result?.message && <p>{job.result.message}</p>}
        <p>{GUIDANCE[next]}</p>
        {unsafe && <p className="subtle">核对前已暂停新的账号修改，仍可查看记录与主动更新清单。</p>}
      </div>
      <div className="diagnostic-actions">
        {next === 'load_desktop_playlists' ? (
          <button className="button secondary" disabled={readBlocked} onClick={onReadLocal}>
            重新读取本地歌单
          </button>
        ) : (
          <a className="button secondary" href={`#${page}`} onClick={() => onNavigate(page)}>
            {page === 'history' ? '查看任务记录' : '前往接入设置'}
          </a>
        )}
      </div>
    </section>
  );
}
