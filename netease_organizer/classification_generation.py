"""Coordinated local exports; incomplete publication never grants execution."""
from contextlib import contextmanager
import csv
from datetime import datetime, timezone
import io
import json
from pathlib import Path
import stat

from .write_journal import write_lease


PENDING_FILE = '分类生成未完成.json'
PLAN_FILE = '分类整理计划.json'
REPORT_FILE = '全库分类-逐曲结果.json'
EXECUTION_FILES = ('分类整理执行进度.json', '分类整理执行结果.json')
MAX_EXPORT_BYTES = 16 * 1024 * 1024
INCOMPLETE_MESSAGE = '分类文件尚未生成完成；请重试生成或显式补齐缺失导出，本次不会执行账号操作。'


def _present(path):
    try:
        path.lstat()
        return True
    except FileNotFoundError:
        return False
    except OSError:
        return True


def generation_incomplete(project):
    return _present(Path(project) / 'artifacts' / PENDING_FILE)


def require_complete_generation(project):
    if generation_incomplete(project):
        raise ValueError(INCOMPLETE_MESSAGE)


@contextmanager
def generation_lease(controller):
    relative = Path('.organizer/account-write.lock')
    if (controller.project / relative).is_symlink():
        raise ValueError('分类生成保护不可用，请核对本地目录。')
    with write_lease(controller._workspace_target(relative)):
        yield


def require_unexecuted(controller):
    if any(_present(controller.project / 'artifacts' / name) for name in EXECUTION_FILES):
        raise ValueError('分类已有执行记录；不会重新生成或替换批准计划。可显式补齐缺失导出。')


def _target(controller, name):
    original = controller.project / 'artifacts' / name
    if original.parent.is_symlink() or original.is_symlink():
        raise ValueError('分类输出路径不兼容，请保留原记录并核对目录。')
    target = controller._workspace_target(Path('artifacts') / name)
    if _present(target) and not stat.S_ISREG(target.lstat().st_mode):
        raise ValueError('分类输出路径不是普通文件，请保留原记录。')
    return target


def _unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('分类导出包含重复字段。')
        result[key] = value
    return result


def _json(text):
    def invalid(_):
        raise ValueError('分类导出包含无效数值。')
    return json.loads(text, object_pairs_hook=_unique, parse_constant=invalid)


def _matches_existing(path, name, text):
    with path.open('rb') as stream:
        raw = stream.read(MAX_EXPORT_BYTES + 1)
    if len(raw) > MAX_EXPORT_BYTES:
        raise ValueError('已有分类导出过大，请保留原文件并核对。')
    existing = raw.decode('utf-8')
    expected = text + '\n'  # Organizer's atomic writer appends one final newline.
    if name.endswith('.json'):
        actual, wanted = _json(existing), _json(expected)
        if name == REPORT_FILE:
            if type(actual) is not dict:
                return False
            timestamp = actual.get('created_at')
            if (type(timestamp) is not str or not 1 <= len(timestamp) <= 80
                    or any(ord(c) < 32 or 127 <= ord(c) <= 159 for c in timestamp)):
                return False
            try:
                parsed = datetime.fromisoformat(timestamp)
                if parsed.tzinfo is None:
                    return False
                parsed.astimezone(timezone.utc)
            except (ValueError, OverflowError):
                return False
            actual.pop('created_at', None)
            wanted.pop('created_at', None)
        # JSON booleans must not compare equal to integer positions/counts.
        return json.dumps(actual, sort_keys=True, ensure_ascii=False, allow_nan=False) == json.dumps(
            wanted, sort_keys=True, ensure_ascii=False, allow_nan=False)
    if name.endswith('.csv'):
        def rows(value):
            return list(csv.reader(io.StringIO(value.removeprefix('\ufeff'), newline=''), strict=True))
        return rows(existing) == rows(expected)
    return existing.replace('\r\n', '\n').replace('\r', '\n') == expected


def publish_exports(controller, plan, outputs, *, repair_missing=False):
    """Caller holds generation_lease. Never clear pending on a failed publication."""
    if repair_missing:
        from .classification_execution import validate_export_repair_plan
        validate_export_repair_plan(controller, plan)
    else:
        require_unexecuted(controller)
    targets = {name: _target(controller, name) for name, _, _ in outputs}
    marker = _target(controller, PENDING_FILE)
    # Validate all encoding and existing output compatibility before creating pending.
    missing = []
    for name, text, newline in outputs:
        text.encode('utf-8')
        if repair_missing and name == PLAN_FILE:
            continue
        if repair_missing and _present(targets[name]):
            if not _matches_existing(targets[name], name, text):
                raise ValueError('已有分类导出与当前离线输入不一致，已保留原文件；不能仅补齐缺失文件。')
        else:
            missing.append((name, text, newline))
    if not missing:
        if _present(marker):
            marker.unlink()
        return []
    controller._write_artifact(PENDING_FILE, json.dumps({
        'kind': 'classification_generation_pending', 'version': 1,
        'mode': 'repair_missing' if repair_missing else 'build'}, ensure_ascii=False))
    written = []
    for name, text, newline in missing:
        if repair_missing and _present(targets[name]):
            raise ValueError('分类导出已变化，请保留文件并重新核对。')
        controller._write_artifact(name, text, newline=newline)
        written.append(name)
    marker.unlink()
    return written
