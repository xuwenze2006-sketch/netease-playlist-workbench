"""Run the local regression gate without installing packages or contacting an account."""

from datetime import datetime
import json
import os
from pathlib import Path
import signal
import shutil
import subprocess
import sys
import uuid


PROJECT = Path(__file__).resolve().parents[1]


def _stop_owned_tree(process):
    """Stop only the process group/tree created for this verification step."""
    try:
        if sys.platform == 'win32':
            # Keep the npm parent alive until taskkill identifies its children.
            if process.poll() is None:
                stopped = subprocess.run(['taskkill', '/PID', str(process.pid), '/T', '/F'],
                                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                         creationflags=subprocess.CREATE_NO_WINDOW, timeout=10)
                if stopped.returncode != 0:
                    return False
        else:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        process.wait(timeout=5)
        return True
    except (OSError, subprocess.TimeoutExpired):
        return False


def run_step(command, cwd, log, *, timeout=600):
    process = None
    try:
        with log.open('wb') as stream:
            options = ({'creationflags': subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW}
                       if sys.platform == 'win32' else {'start_new_session': True})
            process = subprocess.Popen(command, cwd=cwd, stdout=stream, stderr=subprocess.STDOUT, **options)
            try:
                return process.wait(timeout=timeout)
            except (subprocess.TimeoutExpired, KeyboardInterrupt) as error:
                cleaned = _stop_owned_tree(process)
                if not cleaned:
                    print(f'验证进程树清理未确认，请检查本次进程 PID {process.pid}。', file=sys.stderr)
                if isinstance(error, KeyboardInterrupt):
                    raise
                return 2 if cleaned else 3
    except OSError:
        if process is not None:
            _stop_owned_tree(process)
        return 2


def main():
    node = shutil.which('node')
    npm_cli = Path(node).parent / 'node_modules/npm/bin/npm-cli.js' if node else None
    if not node or not npm_cli.is_file():
        print('未找到本机 Node.js / npm，请先安装运行环境。', file=sys.stderr)
        return 2
    if not (PROJECT / 'frontend/node_modules').is_dir():
        print('缺少前端依赖，请先按 README 完成准备；验证不会自动安装。', file=sys.stderr)
        return 2
    output = PROJECT / 'artifacts' / 'offline-verification' / (
        datetime.now().strftime('%Y%m%d-%H%M%S-') + uuid.uuid4().hex[:8])
    output.mkdir(parents=True)
    steps = [
        # Record the last active test even if a GUI dialog or worker stalls.
        ('backend', [sys.executable, '-X', 'utf8', '-m', 'unittest', 'discover', '-s', 'tests', '-v'], PROJECT),
        ('frontend', [node, str(npm_cli), 'test', '--', '--reporter=dot'], PROJECT / 'frontend'),
        ('build', [node, str(npm_cli), 'run', 'build'], PROJECT / 'frontend'),
    ]
    report = {'status': 'RUNNING', 'steps': [], 'account_operations_requested': False}
    report_path = output / 'report.json'

    def save():
        report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')

    save()
    print(f'本地验证记录：{output}', flush=True)
    for name, command, cwd in steps:
        print(f'正在验证 {name}…', flush=True)
        log = output / f'{name}.log'
        try:
            code = run_step(command, cwd, log)
        except KeyboardInterrupt:
            report['status'] = 'INTERRUPTED'
            save()
            raise
        report['steps'].append({'name': name, 'exit_code': code, 'log': log.name})
        if code:
            report['status'] = 'FAIL'
            save()
            print(f'{name} 未通过，已停止后续步骤。日志：{log}', file=sys.stderr)
            return 1
        save()
        print(f'{name} 通过。', flush=True)
    report['status'] = 'PASS'
    save()
    print(f'本地回归及构建通过：{report_path}')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
