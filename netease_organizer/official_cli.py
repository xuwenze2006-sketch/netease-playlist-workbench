"""A subprocess boundary for the documented, project-local official CLI."""

import json
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
from pathlib import Path


PUBLIC_ERROR_CODES = frozenset({
    "node_missing", "cli_missing", "credentials_required", "cli_timeout",
    "invalid_app_id", "invalid_private_key", "local_permission_denied",
    "local_snapshot_unavailable", "account_mismatch", "operation_failed",
})


def public_error_code(value):
    """A diagnostic reason only; it does not establish account write outcomes."""
    return value if type(value) is str and value in PUBLIC_ERROR_CODES else "operation_failed"


class CliError(RuntimeError):
    """Public-safe message; never includes CLI output or secret arguments."""

    def __init__(self, *args, code="operation_failed"):
        super().__init__(*args)
        self.code = public_error_code(code)


_COMMAND_LABELS = {
    ("--version",): "version",
    ("commands",): "commands",
    ("login", "--check"): "login check",
    ("login", "--background"): "login background",
    ("login",): "login",
    ("config", "set"): "config set",
    ("user", "info"): "user info",
    ("user", "favorite"): "user favorite",
    ("playlist", "created"): "playlist created",
    ("playlist", "collected"): "playlist collected",
    ("playlist", "get"): "playlist get",
    ("playlist", "tracks"): "playlist tracks",
    ("playlist", "updateName"): "playlist updateName",
    ("playlist", "create"): "playlist create",
    ("playlist", "add"): "playlist add",
    ("playlist", "reorder"): "playlist reorder",
}


def flatten_manifest(manifest):
    resources = manifest.get("manifests") if isinstance(manifest, dict) else None
    if not isinstance(resources, dict) or not isinstance(resources.get("root"), dict):
        raise CliError("官方命令缓存格式不兼容，不能猜测歌单参数。")
    commands, identities = [], set()
    # Count work, not just emitted commands: shared empty branches and root
    # defaults can otherwise expand exponentially without filling commands.
    remaining = 10000

    def consume(amount):
        nonlocal remaining
        remaining -= amount
        if remaining < 0:
            raise CliError("官方命令缓存的层级或大小异常。")

    def walk(path, prefix, ancestors):
        consume(1)
        if path in ancestors or len(prefix) > 8 or len(commands) > 1000:
            raise CliError("官方命令缓存的层级或大小异常。")
        resource = resources.get(path)
        if not isinstance(resource, dict):
            raise CliError("官方命令缓存缺少资源定义，请重新检查接入。")
        methods = resource.get("methods", [])
        children = resource.get("sub_resources", [])
        if not isinstance(methods, list) or not isinstance(children, list):
            raise CliError("官方命令缓存格式不兼容。")
        consume(len(methods) + len(children))
        for method in methods:
            name = method.get("name") if isinstance(method, dict) else None
            if name != "$default" and (not isinstance(name, str) or not re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]*", name)):
                raise CliError("官方命令名称不合法。")
            command = prefix if name == "$default" else prefix + [name]
            if name == "$default" and not prefix:
                # Root defaults describe the CLI entry itself, not a named
                # playlist command. They must not hide all child resources.
                continue
            parameters = method.get("parameters", [])
            if (not command or tuple(command) in identities or not isinstance(parameters, list)
                    or len(parameters) > 128 or len(commands) >= 1000):
                raise CliError("官方命令或参数定义不兼容。")
            for parameter in parameters:
                if (not isinstance(parameter, dict) or not isinstance(parameter.get("name"), str)
                        or not re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]*", parameter["name"])):
                    raise CliError("官方参数名称不合法。")
            identities.add(tuple(command))
            commands.append({"command": command, "description": str(method.get("description", ""))[:2000],
                             "parameters": parameters})
        for child in children:
            if (not isinstance(child, dict) or not isinstance(child.get("name"), str)
                    or not re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]*", child["name"])
                    or not isinstance(child.get("path"), str)):
                raise CliError("官方子命令定义不兼容。")
            walk(child["path"], prefix + [child["name"]], ancestors | {path})

    walk("root", [], set())
    return commands


class OfficialCli:
    def __init__(self, project, *, node=None, run_process=None):
        self.project = Path(project).resolve()
        self.home = self.project / ".organizer" / "cli-home"
        self.config_dir = self.home / ".config" / "ncm-cli"
        self.marker = self.home / "onboarding.json"
        self.script = self.project / ".tools/ncm-cli/node_modules/@music163/ncm-cli/dist/index.js"
        bundled = Path.home() / ".cache/codex-runtimes/codex-primary-runtime/dependencies/node/bin/node.exe"
        self.node = str(node or shutil.which("node") or (bundled if bundled.is_file() else ""))
        self.run_process = run_process or subprocess.run
        self._observer = None
        self._metrics_lock = threading.Lock()
        self._metrics = {}
        self._version_cache = None

    def set_observer(self, callback):
        """Report fixed command labels and timings, never arguments or output."""
        if callback is not None and not callable(callback):
            raise CliError("操作进度回调必须可调用。")
        self._observer = callback

    def _notify(self, event):
        callback = self._observer
        if callback is not None:
            try:
                callback(dict(event))
            except Exception:
                # Display/diagnostic failures must not change an account action.
                pass

    def reset_metrics(self):
        with self._metrics_lock:
            self._metrics.clear()

    def performance_summary(self):
        """Completed subprocess calls; success means process exit code zero."""
        with self._metrics_lock:
            commands = [{"command": command, "call_count": counts["call_count"],
                         "elapsed_seconds": round(counts["elapsed_seconds"], 6),
                         "failure_count": counts["failure_count"]}
                        for command, counts in sorted(self._metrics.items())]
            return {"call_count": sum(item["call_count"] for item in commands),
                    "elapsed_seconds": round(sum(item["elapsed_seconds"] for item in commands), 6),
                    "failure_count": sum(item["failure_count"] for item in commands),
                    "commands": commands}

    def _record_call(self, command, elapsed, success):
        with self._metrics_lock:
            counts = self._metrics.setdefault(command, {"call_count": 0, "elapsed_seconds": 0.0,
                                                       "failure_count": 0})
            counts["call_count"] += 1
            counts["elapsed_seconds"] += elapsed
            counts["failure_count"] += int(not success)

    def installed(self):
        return bool(self.node) and self.script.is_file()

    def installation_error_code(self):
        """Use the same local preflight as installed(), without CLI execution."""
        if not self.node:
            return "node_missing"
        if not self.script.is_file():
            return "cli_missing"
        return None

    def _environment(self):
        if not self.home.resolve().is_relative_to(self.project):
            raise CliError("程序配置目录必须位于本项目中。")
        isolated_root = self.home.resolve()
        paths = [self.config_dir, self.marker, self.config_dir / "cache", self.config_dir / "logs",
                 self.home / "AppData/Roaming", self.home / "AppData/Local"]
        paths += [self.config_dir / name for name in
                  ("config.json", "credentials.enc.json", "tokens.enc.json", "cache/manifest.json")]
        if any(not path.resolve().is_relative_to(isolated_root) for path in paths):
            raise CliError("配置目录含指向项目隔离目录之外的链接，不能保存或读取账号配置。")
        try:
            self.home.mkdir(parents=True, exist_ok=True)
        except PermissionError as error:
            raise CliError("本地文件或返回数据不可用；本次操作未确认完成。",
                           code="local_permission_denied") from error
        env = {key: value for key, value in os.environ.items()
               if not key.upper().startswith(("NETEASE_", "NCM_", "LANGBASE_"))
               and key.upper() not in ("NODE_OPTIONS", "NODE_PATH", "NODE_COMPILE_CACHE", "NODE_V8_COVERAGE")}
        env.update(USERPROFILE=str(self.home), HOME=str(self.home),
                   APPDATA=str(self.home / "AppData/Roaming"), LOCALAPPDATA=str(self.home / "AppData/Local"))
        return env

    def run_text(self, arguments, *, timeout=45):
        if not self.installed():
            raise CliError("官方 CLI 或 Node.js 未安装，请运行项目的安装脚本。",
                           code=self.installation_error_code())
        if not isinstance(arguments, list) or not all(isinstance(arg, str) for arg in arguments):
            raise CliError("官方命令参数必须是文本列表。")
        settings = {"cwd": str(self.project), "env": self._environment(), "capture_output": True,
                    "encoding": "utf-8", "errors": "replace", "timeout": timeout}
        if os.name == "nt":
            settings["creationflags"] = subprocess.CREATE_NO_WINDOW
        command = _COMMAND_LABELS.get(tuple(arguments[:2]),
                                      _COMMAND_LABELS.get(tuple(arguments[:1]), "其他官方操作"))
        self._notify({"kind": "cli", "command": command, "phase": "started"})
        started, success = time.perf_counter(), False
        try:
            result = self.run_process([self.node, str(self.script), *arguments], **settings)
            if result.returncode != 0:
                raise CliError("官方 CLI 未接受本次操作，请检查配置、授权或接口状态。")
            success = True
            return result.stdout
        except subprocess.TimeoutExpired as error:
            raise CliError("官方 CLI 操作超时，结果可能未确定，请核对后再继续。",
                           code="cli_timeout") from error
        except PermissionError as error:
            raise CliError("无法启动官方 CLI，请检查本地安装。",
                           code="local_permission_denied") from error
        except OSError as error:
            raise CliError("无法启动官方 CLI，请检查本地安装。") from error
        finally:
            elapsed = max(0.0, time.perf_counter() - started)
            self._record_call(command, elapsed, success)
            self._notify({"kind": "cli", "command": command, "phase": "finished",
                          "elapsed_seconds": round(elapsed, 6), "success": success})

    def run_json(self, arguments):
        output = self.run_text([*arguments, "--output", "json"])
        try:
            value = json.loads(output)
        except (ValueError, RecursionError) as error:
            raise CliError("官方 CLI 未返回可解析的 JSON，操作结果不能确认。") from error
        if not isinstance(value, (dict, list)):
            raise CliError("官方 CLI 返回格式不兼容。")
        return value

    def _version_signature(self):
        node_path = Path(self.node)
        if not node_path.is_absolute() and len(node_path.parts) == 1:
            located = shutil.which(self.node)
            if not located:
                return None
            node_path = Path(located)
        try:
            signature = []
            for path in (node_path, self.script):
                resolved = path.resolve()
                stat = resolved.stat()
                signature.append((str(resolved), stat.st_mtime_ns, stat.st_size,
                                  stat.st_ctime_ns, stat.st_ino, stat.st_dev))
            return tuple(signature)
        except OSError:
            return None

    def version(self):
        if not self.installed():
            raise CliError("官方 CLI 或 Node.js 未安装，请运行项目的安装脚本。",
                           code=self.installation_error_code())
        # A warm cache must still honor the isolation-directory boundary.
        self._environment()
        signature = self._version_signature()
        if signature is not None and self._version_cache is not None and self._version_cache[0] == signature:
            return self._version_cache[1]
        self._version_cache = None
        version = self.run_text(["--version"]).strip()
        if not re.fullmatch(r"\d+\.\d+\.\d+(?:-[A-Za-z0-9.-]+)?", version):
            raise CliError("官方 CLI 的版本结果不兼容。")
        if signature is not None and signature == self._version_signature():
            self._version_cache = (signature, version)
        return version

    def configured(self):
        try:
            marker = json.loads(self.marker.read_text(encoding="utf-8"))
            credentials = self.config_dir / "credentials.enc.json"
            return marker.get("configured") is True and credentials.is_file() and credentials.stat().st_size > 0
        except (OSError, ValueError, AttributeError):
            return False

    def token_signature(self):
        """Watch only encrypted-file metadata; this never proves authorization."""
        self._environment()
        try:
            stat = (self.config_dir / "tokens.enc.json").stat()
            return (stat.st_mtime_ns, stat.st_size, stat.st_ctime_ns, stat.st_ino)
        except OSError:
            return None

    def save_credentials(self, app_id, private_key):
        if not isinstance(app_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", app_id.strip()):
            raise CliError("App ID 不能为空，且只能含字母、数字、横线或下划线。",
                           code="invalid_app_id")
        if not isinstance(private_key, str) or not private_key.strip() or len(private_key) > 65536:
            raise CliError("请填写完整私钥，长度不能超过 64 KiB。", code="invalid_private_key")
        temporary = None
        try:
            self._environment()
            self.marker.write_text('{"configured": false}', encoding="utf-8")
            try:
                self.run_text(["config", "set", "appId", app_id.strip()])
                with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=self.home,
                                                 prefix="private-key-", suffix=".tmp", delete=False) as stream:
                    temporary = Path(stream.name)
                    stream.write(private_key)
                self.run_text(["config", "set", "privateKey", str(temporary)])
                if not (self.config_dir / "credentials.enc.json").is_file():
                    raise CliError("官方 CLI 未保存凭证，请检查版本兼容性。")
                self.marker.write_text('{"configured": true}', encoding="utf-8")
            finally:
                if temporary is not None:
                    temporary.unlink(missing_ok=True)
        except PermissionError as error:
            raise CliError("本地文件或返回数据不可用；本次操作未确认完成。",
                           code="local_permission_denied") from error

    def manifest(self):
        path = self.config_dir / "cache/manifest.json"
        try:
            if path.stat().st_size > 8 * 1024 * 1024:
                raise CliError("官方命令缓存过大，不能确认兼容性。")
            raw = path.read_text(encoding="utf-8")
            manifest = json.loads(raw)
        except (OSError, ValueError, RecursionError) as error:
            raise CliError("尚未取得官方动态命令，请配置并授权后检查接入。") from error
        return manifest, flatten_manifest(manifest)
