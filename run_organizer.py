"""Run the local modern workbench, or an explicit command-line operation."""

import argparse
import json
import os
import sys
from pathlib import Path
from tempfile import TemporaryDirectory

def Organizer(*args, **kwargs):
    """Load the controller inside startup protection, retaining the existing test seam."""
    from netease_organizer.service import Organizer as Controller
    return Controller(*args, **kwargs)


_STARTUP_STEPS = {
    'frontend_missing': '本地页面资源尚未构建。请在项目 frontend 目录依次运行 npm ci 和 npm run build，然后重新打开。',
    'build_changed': '程序文件正在变化或页面资源不完整。请先完成更新；如资源缺失，请在项目 frontend 目录运行 npm ci 和 npm run build，再重新双击“启动歌单整理”。',
    'local_permission_denied': '本地文件无法访问。请检查项目目录的读写权限，以及安全软件是否阻止了程序，再重新打开。',
    'browser_unavailable': '未能打开本机浏览器。请确认已安装可正常打开网页的浏览器，再重新打开工作台。',
    'instance_busy': '工作台正在启动。请稍等片刻；如已有窗口，请先切换到该窗口，不要重复启动。',
    'legacy_instance': '检测到仍在运行的旧版工作台。请先正常关闭旧版窗口，再重新打开；不要删除运行中的锁文件。',
    'startup_failed': '请参照项目 README 中的“本地验证”检查安装与本地环境，然后重新打开。',
}


def show_gui_startup_error(error_code='startup_failed'):
    code = error_code if isinstance(error_code, str) and error_code in _STARTUP_STEPS else 'startup_failed'
    message = ('歌单工作台暂未能打开。\n\n' + _STARTUP_STEPS[code] +
               '\n\n本次启动不会自动重发账号整理操作。')
    if os.name == 'nt':
        try:
            import ctypes
            ctypes.windll.user32.MessageBoxW(None, message, '歌单工作台 · 启动未完成', 0x10)
            return
        except (AttributeError, OSError):
            pass
    if sys.stderr is not None:
        print(message, file=sys.stderr)


def _parse_options(argv=None):
    parser = argparse.ArgumentParser(description="网易云个人歌单整理：官方接入与本地候选清单")
    parser.add_argument("--data-dir", type=Path, help="自定义网易云桌面缓存目录")
    parser.add_argument("--authorize", action="store_true", help="仅用于 gui/tk-gui：显式生成一次账号授权二维码")
    parser.add_argument("--names-only", action="store_true",
                        help="仅用于 online-plan：核对歌单目录与红心概览，跳过红心歌曲明细")
    parser.add_argument("--accept-default-visibility", action="store_true",
                        help="仅用于 execute-artists：接受歌手精选按官方默认可见性创建（可能公开）")
    parser.add_argument("command", nargs="?", default="gui",
                        choices=("gui", "tk-gui", "doctor", "plan", "login", "login-status", "discover", "check-execution",
                                 "online-plan", "execute-renames", "reconcile-renames", "execute-artists"))
    options = parser.parse_args(argv)
    if options.authorize and options.command not in ("gui", "tk-gui"):
        parser.error("--authorize 仅能与 gui/tk-gui 命令一起使用")
    if options.accept_default_visibility and options.command != "execute-artists":
        parser.error("--accept-default-visibility 仅能与 execute-artists 命令一起使用")
    if options.names_only and options.command != "online-plan":
        parser.error("--names-only 仅能与 online-plan 命令一起使用")
    return options


def _run(options):
    if options.command in ('gui', 'tk-gui'):
        launch_error_type = ()
        try:
            project = Path(__file__).resolve().parent
            if options.command == 'gui':
                from netease_organizer.web_build import LaunchError, load_build
                launch_error_type = LaunchError
                build = load_build(project)
            controller = Organizer(project, data_dir=options.data_dir)
            if options.command == 'gui':
                from netease_organizer.web_launcher import launch_web
                launch_web(controller, authorize_on_start=options.authorize, build=build)
            else:
                from netease_organizer.ui import launch_gui
                launch_gui(controller, authorize_on_start=options.authorize)
            return 0
        except Exception as error:
            code = error.code if isinstance(error, launch_error_type) else 'startup_failed'
            if isinstance(code, str) and code in _STARTUP_STEPS and code != 'startup_failed':
                show_gui_startup_error(error_code=code)
            else:
                # Preserve the previous no-argument seam for unknown startup failures.
                show_gui_startup_error()
            return 1
    controller = Organizer(Path(__file__).resolve().parent, data_dir=options.data_dir)
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    try:
        action = {"doctor": controller.doctor, "plan": controller.prepare, "login": controller.login,
                  "login-status": controller.login_status,
                  "discover": controller.discover, "check-execution": controller.execute,
                  "online-plan": (lambda: controller.online_preview(names_only=True))
                                 if options.names_only else controller.online_preview,
                  "execute-renames": controller.execute_renames,
                  "reconcile-renames": controller.reconcile_renames,
                  "execute-artists": lambda: controller.execute_artist_playlists(
                      accept_default_visibility=options.accept_default_visibility)}[options.command]
        result = action()
        # The GUI displays this in memory. The command-line result keeps only
        # its usable authorization link, rather than dumping image data.
        printed = {key: value for key, value in result.items()
                   if key not in {'qr_png_base64', 'run_id', 'intent_digest'}}
        print(json.dumps(printed, ensure_ascii=False, indent=2, allow_nan=False))
        if result.get("status") in ("blocked", "partial", "uncertain"):
            return 3
        return 1 if result.get("installed") is False else 0
    except Exception as error:
        from netease_organizer.service import OrganizerError
        message = str(error) if isinstance(error, OrganizerError) else "本次操作未完成；内部错误及凭证不会输出。"
        print(json.dumps({"status": "error", "message": message,
                          "applied_to_account": None if options.command in ("execute-renames", "execute-artists") else False}, ensure_ascii=False))
        return 1


def main(argv=None):
    # Timestamp-based .pyc validation can accept stale code after an equal-size
    # update in the same second. Keep a private empty cache for this process's
    # complete lifetime, without deleting the project's or user's caches.
    options = _parse_options(argv)
    try:
        directory = TemporaryDirectory(prefix='organizer-python-cache-', ignore_cleanup_errors=True)
    except OSError:
        if options.command in ('gui', 'tk-gui'):
            show_gui_startup_error()
        elif sys.stdout is not None:
            print(json.dumps({'status': 'error', 'message': '本地启动环境暂不可用，请检查临时目录访问权限。',
                              'applied_to_account': None if options.command in ('execute-renames', 'execute-artists') else False}, ensure_ascii=False))
        return 1
    previous_prefix = sys.pycache_prefix
    with directory as cache:
        sys.pycache_prefix = cache
        try:
            return _run(options)
        finally:
            sys.pycache_prefix = previous_prefix


if __name__ == "__main__":
    raise SystemExit(main())
