"""Launch the local workbench in an app window without using a shared browser profile."""

import json
from contextlib import contextmanager
import os
import re
import secrets
import shutil
import subprocess
import tempfile
import threading
import time
import urllib.request
import urllib.error
import webbrowser
from pathlib import Path
from urllib.parse import urlsplit

from .web_build import LaunchError, load_build, preload_runtime


class InstanceBusy(RuntimeError):
    pass


def _private_path(project, filename):
    project = Path(project).resolve()
    path = project / '.organizer' / filename
    if not path.resolve().is_relative_to(project) or path.is_symlink():
        raise ValueError('程序本地配置目录不能指向项目之外。')
    return path


def _local_url(url):
    try:
        parsed = urlsplit(url)
        valid = (parsed.scheme == 'http' and parsed.hostname == '127.0.0.1'
                 and parsed.username is None and parsed.password is None
                 and parsed.port is not None and 1 <= parsed.port <= 65535
                 and parsed.path in ('', '/') and not parsed.query and not parsed.fragment)
    except (TypeError, ValueError):
        valid = False
    if not valid:
        raise ValueError('工作台只能打开本机服务地址。')
    return f'http://127.0.0.1:{parsed.port}/'


@contextmanager
def instance_lock(project):
    """OS-managed lease: process exit releases it, including a crash."""
    path = _private_path(project, 'web-running.lock')
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('a+b') as stream:
        if path.stat().st_size == 0:
            stream.write(b'\0')
            stream.flush()
        stream.seek(0)
        try:
            if os.name == 'nt':
                import msvcrt
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            raise InstanceBusy('工作台正在启动，请稍后重新打开。') from None
        try:
            yield
        finally:
            stream.seek(0)
            if os.name == 'nt':
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


def browser_command(project, url, *, executable=None):
    url = _local_url(url)
    if executable is None:
        candidates = [shutil.which('msedge')]
        for variable in ('PROGRAMFILES(X86)', 'PROGRAMFILES', 'LOCALAPPDATA'):
            if os.environ.get(variable):
                candidates.append(str(Path(os.environ[variable]) / 'Microsoft/Edge/Application/msedge.exe'))
        executable = next((candidate for candidate in candidates if candidate and Path(candidate).is_file()), None)
    if executable is None:
        return None
    profile = _private_path(project, 'web-browser-profile')
    return [str(executable), f'--app={url}', '--window-size=1440,940', f'--user-data-dir={profile}',
            '--no-first-run', '--no-default-browser-check']


def write_instance(project, *, port, instance, pid, revision=None, control_key=None):
    if (type(port) is not int or not 1 <= port <= 65535 or type(pid) is not int or pid <= 0
            or not isinstance(instance, str) or re.fullmatch(r'[a-f0-9]{32}', instance) is None):
        raise ValueError('本机服务记录格式不兼容。')
    data = {'port': port, 'instance': instance, 'pid': pid}
    if revision is not None or control_key is not None:
        if not all(type(value) is str and re.fullmatch(r'[a-f0-9]{64}', value)
                   for value in (revision, control_key)):
            raise ValueError('本机服务记录格式不兼容。')
        data.update(revision=revision, control_key=control_key)
    target = _private_path(project, 'web-instance.json')
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=target.parent,
                                         prefix='.web-instance-', suffix='.tmp', delete=False) as stream:
            temporary = Path(stream.name)
            json.dump(data, stream)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, target)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, response, code, message, headers, newurl):
        return None


def _health_reader(url):
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())
    with opener.open(url, timeout=1) as response:
        raw = response.read(1025)
        if len(raw) > 1024:
            raise ValueError('本机服务响应过大。')
        return json.loads(raw)


def _running_instance(project, *, health_reader=None):
    try:
        path = _private_path(project, 'web-instance.json')
        if path.stat().st_size > 1024:
            return None
        with path.open('rb') as stream:
            raw = stream.read(1025)
        if len(raw) > 1024:
            return None
        data = json.loads(raw.decode('utf-8'))
        if (not isinstance(data, dict) or set(data) not in (
                {'port', 'instance', 'pid'}, {'port', 'instance', 'pid', 'revision', 'control_key'})
                or type(data['port']) is not int or not 1 <= data['port'] <= 65535
                or type(data['pid']) is not int or data['pid'] <= 0
                or not isinstance(data['instance'], str) or re.fullmatch(r'[a-f0-9]{32}', data['instance']) is None):
            return None
        if 'revision' in data and not all(type(data[key]) is str and re.fullmatch(r'[a-f0-9]{64}', data[key])
                                          for key in ('revision', 'control_key')):
            return None
        url = f"http://127.0.0.1:{data['port']}/"
        result = (health_reader or _health_reader)(url + 'health')
        if isinstance(result, dict) and result.get('kind') == 'netease-organizer-local' and result.get('instance') == data['instance']:
            if 'revision' in data and result.get('revision') != data['revision']:
                return None
            return dict(data, url=url)
    except (OSError, ValueError, UnicodeError):
        pass
    return None


def read_running_instance(project, *, health_reader=None):
    """Expose only the verified local URL, never the private control credential."""
    record = _running_instance(project, health_reader=health_reader)
    return record['url'] if record else None


def _request_update(record, revision):
    """One local control request; failures never terminate or pause the service."""
    url = _local_url(record['url']) + 'api/update'
    request = urllib.request.Request(url, method='POST', headers={
        'Content-Type': 'application/json', 'X-Organizer-Control': record['control_key'],
    }, data=json.dumps({'instance': record['instance'], 'revision': revision}).encode('utf-8'))
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())
    try:
        try:
            response = opener.open(request, timeout=2)
        except urllib.error.HTTPError as error:
            response = error
        with response:
            status = response.code
            raw = response.read(1025)
        if len(raw) > 1024:
            raise ValueError()
        data = json.loads(raw)
        allowed = {200: ('current',), 202: ('stopping',), 409: ('busy', 'review_required')}
        if (not isinstance(data, dict) or type(data.get('accepted')) is not bool
                or data.get('reason') not in allowed.get(status, ())
                or data['accepted'] != (status == 202)):
            raise ValueError()
        return {'accepted': data['accepted'], 'reason': data['reason']}
    except (OSError, ValueError, UnicodeError):
        raise LaunchError(code='instance_busy') from None


def _open_window(project, url):
    command = browser_command(project, url)
    if command is None:
        if not webbrowser.open(_local_url(url), new=1):
            raise LaunchError(code='browser_unavailable')
        return
    flags = subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0
    try:
        _private_path(project, 'web-browser-profile').mkdir(parents=True, exist_ok=True)
        subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL, creationflags=flags)
    except PermissionError:
        raise LaunchError(code='local_permission_denied') from None
    except OSError:
        raise LaunchError(code='browser_unavailable') from None


def launch_web(controller, *, authorize_on_start=False, open_window=True, idle_seconds=300, build=None):
    """Keep the backend alive until the page leaves, finishing in-flight readback."""
    build = build or load_build(controller.project)
    preload_runtime()
    if build.helper_content is not None:
        from .qr import pin_authorization_helper
        pin_authorization_helper(controller.project, build.helper_content)
    if load_build(controller.project).revision != build.revision:
        raise LaunchError(code='build_changed')
    updating = None
    for attempt in range(40):
        existing = _running_instance(controller.project)
        if existing:
            if 'revision' not in existing:
                raise LaunchError(code='legacy_instance')
            if existing['revision'] != build.revision:
                if updating != existing['instance']:
                    result = _request_update(existing, build.revision)
                    if result['accepted']:
                        updating = existing['instance']
                        time.sleep(0.25)
                        continue
                    if result['reason'] not in ('busy', 'review_required', 'current'):
                        raise LaunchError(code='instance_busy')
                else:
                    time.sleep(0.25)
                    continue
            if open_window:
                _open_window(controller.project, existing['url'])
            return existing['url']
        try:
            with instance_lock(controller.project):
                return _serve_window(controller, authorize_on_start=authorize_on_start,
                                     open_window=open_window, idle_seconds=idle_seconds, build=build)
        except InstanceBusy:
            if attempt == 39:
                raise LaunchError(code='instance_busy') from None
            time.sleep(0.25)
        except PermissionError:
            raise LaunchError(code='local_permission_denied') from None
    raise LaunchError(code='instance_busy')


def _serve_window(controller, *, authorize_on_start, open_window, idle_seconds, build):
    from .web_server import create_server, shutdown_server
    from .web_state import build_local_state

    project = controller.project
    assets = Path(project) / 'frontend/dist'
    if load_build(project).revision != build.revision:
        raise LaunchError(code='build_changed')
    control_key = secrets.token_hex(32)
    server = create_server(controller, assets=assets,
                           state_provider=lambda: build_local_state(project, organizer=controller),
                           revision=build.revision, control_key=control_key, asset_snapshot=build.assets)
    port = server.server_address[1]
    instance = server.application.instance_id
    url = f'http://127.0.0.1:{port}/'
    thread = threading.Thread(target=server.serve_forever, name='organizer-local-http', daemon=True)
    thread.start()
    try:
        write_instance(project, port=port, instance=instance, pid=os.getpid(),
                       revision=build.revision, control_key=control_key)
        if authorize_on_start:
            server.application.submit_startup_login()
        if open_window:
            _open_window(project, url)
        while not server.application.stop_requested:
            if not thread.is_alive():
                raise RuntimeError('本机页面服务已停止。')
            if server.application.claim_idle_stop(idle_seconds):
                break
            time.sleep(0.25)
    except KeyboardInterrupt:
        pass
    finally:
        # The cooperative stop joins the non-daemon account worker. It cannot
        # abandon a request between mutation and its readback.
        shutdown_server(server)
        thread.join(timeout=2)
        path = _private_path(project, 'web-instance.json')
        try:
            current = json.loads(path.read_text(encoding='utf-8'))
            if current.get('instance') == instance:
                path.unlink(missing_ok=True)
        except (OSError, UnicodeError, ValueError):
            pass
    return url
