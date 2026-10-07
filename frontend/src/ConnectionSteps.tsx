import type { WorkbenchState } from './types';
import { Icon } from './icons';

export function ConnectionSteps({ connection }: { connection: WorkbenchState['connection'] }) {
  const { installed, configured, authorized } = connection;
  const current = !installed ? 0 : !configured ? 1 : authorized !== true ? 2 : -1;
  const steps = [
    { title: '本机 CLI', status: installed ? '已安装' : '未安装', done: installed },
    { title: '开放平台凭证', status: configured ? '已保存' : '待配置', done: configured },
    {
      title: '账号授权',
      status:
        authorized === true ? '本次已验证' : authorized === false ? '需要重新授权' : '尚未在线验证',
      done: authorized === true,
    },
  ];
  const next = !installed
    ? '下一步：安装本机 CLI'
    : !configured
      ? '下一步：保存开放平台凭证'
      : authorized === false
        ? '下一步：重新确认账号授权'
        : authorized === true
          ? '本次授权已验证'
          : '本地接入已就绪，账号授权按需确认';
  return (
    <section className="connection-steps" aria-label="接入步骤">
      <ol>
        {steps.map((step, index) => (
          <li
            key={step.title}
            className={`${step.done ? 'done' : ''} ${current === index ? 'current' : ''}`}
            aria-current={current === index ? 'step' : undefined}
          >
            <span className="connection-step-number">
              {step.done ? <Icon name="check" size={15} /> : index + 1}
            </span>
            <span>
              <strong>{step.title}</strong>
              <small>{step.status}</small>
            </span>
          </li>
        ))}
      </ol>
      <div className="connection-next">
        <strong>{next}</strong>
        {!installed ? (
          <>
            <p>在项目文件夹打开 PowerShell，手动运行下方安装脚本；完成后点击“检查本地接入”。</p>
            <code>
              powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\install-official-cli.ps1
            </code>
          </>
        ) : !configured ? (
          <p>从网易云开放平台取得 App ID 与私钥文件，再在下方保存。完成后可主动扫码授权。</p>
        ) : (
          <p>
            {authorized === false
              ? '到下方生成二维码并重新确认授权。本地记录仍可浏览。'
              : '你可以返回概览主动读取歌单。普通启动只看本地记录，需要时再扫码或验证账号授权。'}
          </p>
        )}
      </div>
    </section>
  );
}
