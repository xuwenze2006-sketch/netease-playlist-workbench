"""First-use onboarding and honest offline plans for the official CLI path."""

import functools
import base64
import hashlib
import json
import math
import os
import re
import tempfile
import time
import uuid
from contextlib import ExitStack
from pathlib import Path

from netease_bridge.cache import CacheError, CacheReader, default_data_dir
from .official_cli import CliError, OfficialCli, public_error_code
from .planning import build_plan
from .authorization import safe_authorization_url
from .runtime import OperationControl, OperationPaused


_APPROVED_ARTIST_COUNTS = {'林俊杰 · 红心精选': 21, '蔡健雅 · 红心精选': 13,
                           '王力宏 · 红心精选': 12, '蔡依林 · 红心精选': 11,
                           'Taylor Swift · 红心精选': 10}


class OrganizerError(RuntimeError):
    """An intentionally public-safe explanation, suitable for the local GUI."""

    def __init__(self, *args, code="operation_failed"):
        super().__init__(*args)
        self.code = public_error_code(code)


def _public(operation):
    @functools.wraps(operation)
    def call(*args, **kwargs):
        try:
            result = operation(*args, **kwargs)
            if operation.__name__ in ('online_preview', 'execute_renames', 'execute_artist_playlists', 'resume_last_operation'):
                result = args[0]._finish_result(result)
            return result
        except OperationPaused:
            owner = args[0]
            owner._online_plan = owner._online_snapshot = None
            return owner._finish_result({'status': 'paused', 'completed_count': 0,
                'applied_to_account': False, 'write_attempted': False, 'outcome_known': True,
                'message': '已暂停，未开始下一项账号修改。'})
        except CliError as error:
            raise OrganizerError(str(error), code=error.code) from error
        except CacheError as error:
            raise OrganizerError("无法取得可靠的本地歌单快照，请启动网易云后重试。",
                                 code="local_snapshot_unavailable") from error
        except PermissionError as error:
            raise OrganizerError("本地文件或返回数据不可用；本次操作未确认完成。",
                                 code="local_permission_denied") from error
        except (OSError, UnicodeError, ValueError) as error:
            raise OrganizerError("本地文件或返回数据不可用；本次操作未确认完成。") from error
    return call


def _account_write(operation):
    """Cover every service write entrance with the same OS-managed lease."""
    @functools.wraps(operation)
    def call(owner, *args, **kwargs):
        from .write_journal import JournalError, WriteLeaseBusy, write_lease
        with ExitStack() as stack:
            try:
                relative = Path('.organizer/account-write.lock')
                if (owner.project / relative).is_symlink():
                    raise JournalError('账号执行保护不可用。')
                path = owner._workspace_target(relative)
                stack.enter_context(write_lease(path))
            except WriteLeaseBusy:
                return {'status': 'blocked', 'completed_count': 0, 'applied_to_account': False,
                        'write_attempted': False, 'outcome_known': True,
                        'message': '另一个程序正在核对或修改账号，请等待当前操作完成。'}
            except (JournalError, OrganizerError, OSError):
                return {'status': 'blocked', 'completed_count': 0, 'applied_to_account': False,
                        'write_attempted': False, 'outcome_known': True,
                        'message': '本地执行保护暂不可用，请检查项目目录权限。本次没有发送账号修改。'}
            recovery = owner.write_recovery()
            recovering_artist = (operation.__name__ == 'execute_artist_playlists'
                and kwargs.get('resume_created') is True
                and recovery['operation'] == 'artists')
            if recovery['status'] == 'review_required' and not recovering_artist:
                return owner._pending_write_result()
            return operation(owner, *args, **kwargs)
    return call


def _unique_write_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('duplicate record field')
        result[key] = value
    return result


def _invalid_write_constant(value):
    raise ValueError('invalid record number')


def _cell(value):
    return str(value).replace("\r", " ").replace("\n", " ").replace("|", "\\|")


def _plan_report(plan, snapshot):
    summary = plan["summary"]
    names = {p["id"]: p["name"] for p in snapshot["playlists"]}
    for job in plan["jobs"]:
        if job["kind"] == "rename_playlist":
            names[job["playlist_id"]] = job["name"]
    lines = ["# 自动整理候选清单", "", "这是本地缓存生成的候选，尚未修改账号。执行前需要官方在线核对。", "",
             f"候选任务 {summary['job_count']} 项；名称调整 {summary['rename_count']} 项；"
             f"排列调整 {summary['reorder_count']} 项；歌手精选 {summary['artist_playlist_count']} 项。", "",
             "| 动作 | 当前内容 | 目标内容 |", "| --- | --- | --- |"]
    for job in plan["jobs"]:
        if job["kind"] == "rename_playlist":
            lines.append(f"| 改名 | {_cell(job['old_name'])} | {_cell(job['name'])} |")
        elif job["kind"] == "reorder_playlists":
            order = " → ".join(names.get(ident, ident) for ident in job["playlist_ids"])
            lines.append(f"| 排列 | 自建歌单 | {_cell(order)} |")
        elif job["kind"] == "create_artist_playlist":
            lines.append(f"| 新建私密精选 | {_cell(job['artist']['name'])} | {_cell(job['name'])}，"
                         f"{len(job['candidate_track_ids'])} 首候选 |")
    lines += ["", f"本地红心歌曲 ID 数量：{summary['cached_liked_unique_track_count']}。",
              f"缺少完整歌曲资料：{len(summary['missing_metadata_track_ids'])} 首。",
              "本地歌曲 ID 必须映射到正式接口 ID；缺失资料需在线补齐。", "",
              "首次接入：申请开放平台凭证 → 在程序中保存 → 账号授权 → 检查接入。",
              "离线清单尚未执行；名称整理与已批准歌手精选需要通过对应在线入口逐项核对。", ""]
    return "\n".join(lines)


class Organizer:
    def __init__(self, project, *, cli=None, reader=None, data_dir=None, qr_renderer=None, time_source=None,
                 online_reader=None, rename_executor=None, artist_executor=None, control=None):
        self.project = Path(project).resolve()
        self.data_dir = Path(data_dir) if data_dir is not None else default_data_dir()
        self.cli = cli if cli is not None else OfficialCli(self.project)
        self.reader = reader if reader is not None else CacheReader(self.data_dir)
        self.qr_renderer = qr_renderer
        self.time_source = time_source or time.monotonic
        self._authorization_started = None
        self._authorization_signature = None
        self._last_authorized_at = None
        self.online_reader = online_reader
        self.rename_executor = rename_executor
        self.artist_executor = artist_executor
        self._online_plan = None
        self._online_snapshot = None
        self.control = control if control is not None else OperationControl()
        self._progress_listener = None
        self._operation_started = time.monotonic()
        self._active_progress = {}
        if callable(getattr(self.cli, 'set_observer', None)):
            self.cli.set_observer(self._progress)

    def set_progress_listener(self, callback):
        self._progress_listener = callback if callable(callback) else None

    def prepare_operation(self, action):
        """Only an explicit new GUI operation clears a previous pause request."""
        self.control.reset()
        self._operation_started = time.monotonic()
        self._active_progress = {}
        if callable(getattr(self.cli, 'reset_metrics', None)):
            self.cli.reset_metrics()

    def request_pause(self):
        self.control.request_pause()
        return {'status': 'pause_requested', 'message': '已请求暂停；当前请求会完成核对，之后停止。'}

    def _progress(self, event):
        if not isinstance(event, dict):
            return
        phases = {'checking': '核对账号与歌单', 'preflight': '核对执行条件', 'job_begin': '开始下一项',
                  'reading': '读取此歌单歌曲', 'saving': '保存本地歌曲明细',
                  'create': '创建歌单', 'add': '添加歌曲', 'reorder': '排列歌曲',
                  'created': '空歌单已核对', 'rename': '修改名称', 'skipped': '已完成，跳过', 'creating': '创建歌单',
                  'adding': '添加歌曲', 'reordering': '核对歌曲顺序',
                  'completed': '歌单已核对', 'paused': '已暂停',
                  'rename_attempted': '修改名称', 'create_attempted': '创建歌单',
                  'add_attempted': '添加歌曲', 'reorder_attempted': '排列歌曲'}
        phase = event.get('stage', event.get('phase', 'checking'))
        if not isinstance(phase, str):
            phase = 'checking'
        safe = {'kind': 'cli' if event.get('kind') == 'cli' else 'progress',
                'phase': phase if phase in phases or phase in ('started', 'finished') else 'checking',
                'label': phases.get(phase, '正在核对'),
                'elapsed_seconds': round(max(0, time.monotonic() - self._operation_started), 2)}
        if safe['kind'] == 'cli':
            # OfficialCli emits only fixed command labels, never arguments.
            labels = {'version', 'commands', 'login check', 'login background', 'login', 'config set',
                      'user info', 'user favorite', 'playlist created', 'playlist collected',
                      'playlist get', 'playlist tracks', 'playlist updateName', 'playlist create',
                      'playlist add', 'playlist reorder', '其他官方操作'}
            command = event.get('command')
            safe['command'] = command if isinstance(command, str) and command in labels else '官方操作'
            descriptions = {'version': '检查程序版本', 'commands': '读取接口定义',
                            'login check': '检查授权状态', 'login background': '生成授权链接',
                            'user info': '读取账号信息', 'user favorite': '读取红心概览',
                            'playlist created': '读取歌单目录', 'playlist get': '核对歌单信息',
                            'playlist tracks': '读取歌曲明细', 'playlist create': '创建歌单',
                            'playlist updateName': '修改名称', 'playlist add': '添加歌曲',
                            'playlist reorder': '排列歌曲', 'config set': '保存接入凭证'}
            detail = descriptions.get(safe['command'], '处理当前请求')
            active = self._active_progress
            if active:
                safe = {**active, **safe, 'phase': active['phase'],
                        'label': active['label'] + ' · ' + detail}
            else:
                safe['label'] = detail
        elif event.get('stage') == 'classification_preflight':
            steps = {'source_start': '只读核验：核对原红心歌单与账号',
                     'playlist_start': '只读核验：正在核对已创建的分类歌单',
                     'playlist_verified': '只读核验：已确认该歌单此前尝试的范围',
                     'finished': '只读核验完成；后续仅处理未尝试的步骤'}
            step = event.get('step')
            if isinstance(step, str) and step in steps:
                safe.update(stage='classification_preflight', step=step, label=steps[step],
                            phase=event.get('phase') if event.get('phase') in ('checking', 'reading', 'preflight') else 'checking')
                index, total = event.get('job_index'), event.get('total_count')
                if (step in ('playlist_start', 'playlist_verified') and type(index) is int and type(total) is int
                        and 0 <= index < total <= 64):
                    safe['job_index'] = index
        for target, source in (('completed_count', 'completed_count'), ('total_count', 'job_count')):
            value = event.get(source, event.get(target))
            if type(value) is int and 0 <= value <= 10000:
                safe[target] = value
        if safe['kind'] != 'cli':
            self._active_progress = dict(safe)
        if self._progress_listener is None:
            return
        try:
            self._progress_listener(safe)
        except Exception:
            pass

    def _last_result(self):
        """Read small local receipts only; this is never a live account check."""
        candidates = []
        for operation, filename in (('artists', '歌手精选执行结果.json'), ('renames', '名称整理执行结果.json')):
            try:
                path = self._workspace_target(Path('artifacts') / filename)
                stat = path.stat()
                if stat.st_size <= 256 * 1024:
                    candidates.append((stat.st_mtime_ns, operation, path))
            except (OSError, OrganizerError):
                continue
        recovery = self.write_recovery()
        pending_operation = recovery['operation'] if recovery['status'] == 'review_required' else None
        from .web_classification import build_classification_history
        classification, classification_time = build_classification_history(self.project)
        if classification is not None:
            candidates.append((int(classification_time * 10**9), 'classification', None))
        candidates.sort(key=lambda entry: (entry[1] == pending_operation, entry[0]), reverse=True)
        for _, operation, path in candidates:
            if operation == 'classification':
                return classification
            try:
                record = json.loads(path.read_text(encoding='utf-8'))
                if (not isinstance(record, dict) or record.get('status') not in
                        ('completed', 'paused', 'partial', 'uncertain', 'blocked')
                        or type(record.get('completed_count')) is not int
                        or not 0 <= record['completed_count'] <= 1000
                        or not isinstance(record.get('items'), list)):
                    continue
                items = []
                for item in record['items'][:5]:
                    if not isinstance(item, dict) or not isinstance(item.get('name'), str):
                        continue
                    safe = {'name': ''.join(c for c in item['name'] if ord(c) >= 32)[:100],
                            'status': item.get('status') if item.get('status') in
                            ('completed', 'paused', 'blocked', 'uncertain', 'pending', 'created', 'added', 'not_attempted') else 'recorded'}
                    if type(item.get('count')) is int and 0 <= item['count'] <= 10000:
                        safe['count'] = item['count']
                    items.append(safe)
                resumable = (recovery['status'] == 'clear' and record['status'] == 'paused'
                             and record.get('outcome_known') is True)
                if resumable and operation == 'artists':
                    progress = self._workspace_target(Path('artifacts/歌手精选执行进度.json'))
                    if progress.stat().st_size > 256 * 1024:
                        resumable = False
                    else:
                        checkpoint = json.loads(progress.read_text(encoding='utf-8'))
                        consumed = self._workspace_target(Path('artifacts') / (
                            '暂停续做-' + hashlib.sha256(progress.read_bytes()).hexdigest() + '.json'))
                        resumable = (not consumed.exists() and isinstance(checkpoint, dict) and checkpoint.get('phase') == 'paused'
                                     and checkpoint.get('status') == 'paused'
                                     and isinstance(checkpoint.get('jobs'), list)
                                     and len(checkpoint['jobs']) == 5
                                     and checkpoint.get('items') == record['items'])
                return {'source': 'local_record', 'operation': operation, 'status': record['status'],
                        'completed_count': record['completed_count'], 'items': items, 'resumable': resumable}
            except (OSError, UnicodeError, ValueError, OrganizerError):
                continue
        return None

    def _read_write_file(self, filename):
        """Bounded private-local evidence, never a credential or account read."""
        relative = Path('artifacts') / filename
        raw_path = self.project / relative
        if raw_path.is_symlink():
            raise ValueError('unsupported record link')
        path = self._workspace_target(relative)
        try:
            with path.open('rb') as stream:
                content = stream.read(1024 * 1024 + 1)
        except FileNotFoundError:
            return None
        if not content or len(content) > 1024 * 1024:
            raise ValueError('unsupported record size')
        record = json.loads(content.decode('utf-8'), object_pairs_hook=_unique_write_object,
                            parse_constant=_invalid_write_constant)
        if not isinstance(record, dict):
            raise ValueError('unsupported record structure')
        return record

    @staticmethod
    def _receipt_needs_review(record):
        if record is None:
            return False
        if (type(record.get('status')) is not str
                or record['status'] not in {'completed', 'paused', 'partial', 'uncertain', 'blocked'}
                or type(record.get('completed_count')) is not int
                or not 0 <= record['completed_count'] <= 1000
                or not isinstance(record.get('items'), list)
                or len(record['items']) > 1000):
            return True
        if any(field in record and type(record[field]) is not bool
               for field in ('write_attempted', 'outcome_known', 'applied_to_account', 'record_saved')):
            return True
        return (record['status'] == 'uncertain'
            or (record.get('write_attempted') is True and record.get('outcome_known') is not True)
            or (record.get('applied_to_account') is True and record.get('record_saved') is False))

    @staticmethod
    def _legacy_completed_artist_progress(receipt, progress):
        """Only an old, fully confirmed merged batch may omit prefix jobs."""
        from .rename import _decimal, _encrypted
        if (not isinstance(receipt, dict) or 'run_id' in receipt or 'run_id' in progress
                or progress.get('phase') != 'completed' or progress.get('stage') != 'finished'
                or receipt.get('status') != 'completed' or progress.get('status') != 'completed'
                or receipt.get('completed_count') != 5 or progress.get('completed_count') != 5
                or receipt.get('outcome_known') is not True or progress.get('outcome_known') is not True
                or receipt.get('applied_to_account') is not True or progress.get('applied_to_account') is not True
                or receipt.get('write_attempted') is not True or progress.get('write_attempted') is not True
                or _decimal(progress.get('expected_owner_id')) is None
                or progress.get('account_original_id') != progress.get('expected_owner_id')):
            return False
        items, jobs = progress.get('items'), progress.get('jobs')
        prefix = progress.get('previous_completed_items')
        if (not isinstance(items, list) or len(items) != 5 or items != receipt.get('items')
                or not isinstance(prefix, list) or not 1 <= len(prefix) <= 4
                or prefix != items[:len(prefix)] or not isinstance(jobs, list)
                or len(jobs) + len(prefix) != 5):
            return False
        encrypted_ids, original_ids, names = set(), set(), set()
        for item in items:
            if not isinstance(item, dict):
                return False
            name = item.get('name')
            if (not isinstance(name, str) or name not in _APPROVED_ARTIST_COUNTS
                    or item.get('kind') != 'create_artist_playlist' or item.get('status') != 'completed'
                    or type(item.get('count')) is not int or type(item.get('expected_count')) is not int
                    or item['count'] != item['expected_count'] or item['count'] != _APPROVED_ARTIST_COUNTS[name]
                    or not _encrypted(item.get('playlist_id')) or _decimal(item.get('original_playlist_id')) is None):
                return False
            names.add(name)
            encrypted_ids.add(item['playlist_id'].upper())
            original_ids.add(_decimal(item['original_playlist_id']))
        if len(names) != 5 or len(encrypted_ids) != 5 or len(original_ids) != 5:
            return False
        for item, job in zip(items[len(prefix):], jobs):
            if not isinstance(job, dict):
                return False
            tracks = job.get('candidate_track_ids')
            if (job.get('kind') != 'create_artist_playlist' or job.get('name') != item['name']
                    or not isinstance(tracks, list) or len(tracks) != item['count']
                    or any(not _encrypted(ident) for ident in tracks)
                    or len({ident.upper() for ident in tracks}) != len(tracks)):
                return False
        return True

    def write_recovery(self):
        """Restore unresolved writes from independent local records, without login."""
        from .write_journal import JournalError, read_rename_intent, receipt_resolves_intent
        pending, can_reconcile, valid_intent = [], False, False
        try:
            relative = Path('artifacts/名称整理执行进度.json')
            if (self.project / relative).is_symlink():
                raise JournalError('名称执行记录不可用。')
            intent = read_rename_intent(self._workspace_target(relative))
            valid_intent = intent is not None
            receipt_path = self._workspace_target(Path('artifacts/名称整理执行结果.json'))
            if intent is not None and not receipt_resolves_intent(intent, receipt_path):
                pending.append('renames')
                can_reconcile = True
            receipt = self._read_write_file('名称整理执行结果.json')
            if self._receipt_needs_review(receipt) and 'renames' not in pending:
                pending.append('renames')
        except (JournalError, OrganizerError, OSError, UnicodeError, ValueError):
            pending.append('renames')
            can_reconcile = valid_intent
        try:
            receipt = self._read_write_file('歌手精选执行结果.json')
            progress = self._read_write_file('歌手精选执行进度.json')
            artist_pending = self._receipt_needs_review(receipt)
            if progress is not None:
                safe_phases = {'create_attempted', 'add_attempted', 'reorder_attempted',
                               'playlist_identified', 'recovery_identified', 'checkpoint_identified',
                               'completed', 'paused'}
                legacy_complete = self._legacy_completed_artist_progress(receipt, progress)
                valid = (progress.get('kind') == 'artist_execution_journal'
                    and type(progress.get('phase')) is str
                    and progress.get('phase') in safe_phases
                    and isinstance(progress.get('jobs'), list) and (len(progress['jobs']) == 5 or legacy_complete)
                    and isinstance(progress.get('items'), list) and len(progress['items']) == 5)
                # A mutation-intent phase is authoritative even before the old
                # executor changes its in-memory write_attempted/known booleans.
                matches = (valid and isinstance(receipt, dict)
                    and not self._receipt_needs_review(receipt)
                    and progress.get('outcome_known') is True
                    and progress.get('status') != 'uncertain'
                    and receipt.get('outcome_known') is True
                    and progress.get('items') == receipt.get('items')
                    and progress.get('completed_count') == receipt.get('completed_count')
                    and ((isinstance(receipt.get('run_id'), str)
                          and receipt['run_id'] == progress.get('run_id'))
                         or ('run_id' not in receipt and progress.get('stage') == 'finished'
                             and progress.get('status') == receipt.get('status'))))
                artist_pending = artist_pending or not matches
            if artist_pending:
                pending.append('artists')
        except (OrganizerError, OSError, UnicodeError, ValueError):
            pending.append('artists')
        from .classification_execution import classification_pending
        if classification_pending(self):
            pending.append('classification')
        operations = set(pending)
        return {'status': 'review_required' if operations else 'clear',
                'operation': next(iter(operations)) if len(operations) == 1 else 'unknown',
                'can_reconcile': can_reconcile and operations == {'renames'}}

    @staticmethod
    def _pending_write_result():
        return {'status': 'blocked', 'completed_count': 0, 'applied_to_account': False,
                'write_attempted': False, 'outcome_known': False,
                'message': '上次账号修改仍待核对，原记录已保留。本次没有发送修改，请先只读核对或查看本地记录。'}

    def _finish_result(self, result):
        if not isinstance(result, dict):
            return result
        result = dict(result)
        result = self._with_performance(result)
        historical = self._last_result()
        if historical is not None:
            result['last_result'] = historical
        return result

    def _with_performance(self, result):
        result = dict(result)
        method = getattr(self.cli, 'performance_summary', None)
        try:
            metrics = method() if callable(method) else None
        except Exception:
            metrics = None
        if isinstance(metrics, dict) and type(metrics.get('call_count')) is int and metrics['call_count'] >= 0:
            safe = {'call_count': metrics['call_count']}
            elapsed = metrics.get('elapsed_seconds')
            if type(elapsed) in (int, float) and math.isfinite(elapsed) and elapsed >= 0:
                safe['elapsed_seconds'] = elapsed
            failures = metrics.get('failure_count')
            if type(failures) is int and 0 <= failures <= safe['call_count']:
                safe['failure_count'] = failures
            result['performance'] = safe
        return result

    @_public
    def resume_last_operation(self):
        if self.write_recovery()['status'] == 'review_required':
            return self._pending_write_result()
        historical = self._last_result()
        if not historical or historical.get('resumable') is not True:
            return {'status': 'blocked', 'message': '没有可安全续做的暂停记录；已完成和待核对任务不会重复提交。',
                    'applied_to_account': False, 'completed_count': 0, 'write_attempted': False, 'outcome_known': True}
        if historical['operation'] == 'artists':
            return self.execute_artist_playlists(accept_default_visibility=True, resume_paused=True)
        self._online_plan = self._online_snapshot = None
        # A fresh naming plan naturally excludes already confirmed new names.
        return self.execute_renames()

    def _reset_authorization(self):
        self._authorization_started = None
        self._authorization_signature = None
        self._last_authorized_at = None
        self._online_plan = None
        self._online_snapshot = None
        self._active_progress = {}

    def _workspace_target(self, relative):
        path = self.project / relative
        target = path.resolve()
        protected = (self.data_dir.absolute(), self.data_dir.resolve(), (self.data_dir / "Library").resolve())
        if (not target.is_relative_to(self.project)
                or any(target.is_relative_to(root) or path.absolute().is_relative_to(root) for root in protected)):
            raise OrganizerError("整理输出必须保存在本项目中，且不能位于网易云客户端数据目录。")
        return target

    def _write_artifact(self, filename, text):
        target = self._workspace_target(Path('artifacts') / filename)
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=target.parent,
                                             prefix=".organizer-", suffix=".tmp", delete=False) as stream:
                temporary = Path(stream.name)
                stream.write(text + "\n")
                stream.flush()
                os.fsync(stream.fileno())
            for attempt in range(5):
                try:
                    os.replace(temporary, target)
                    break
                except PermissionError as error:
                    # Windows may briefly deny replacing a file opened by an
                    # indexer or scanner. Retry only this local atomic save;
                    # account requests are never repeated here.
                    if getattr(error, 'winerror', None) not in (5, 32, 33) or attempt == 4:
                        raise
                    time.sleep(0.05)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
        return target

    def _require_credentials(self):
        if not self.cli.configured():
            raise OrganizerError("请先申请开放平台 App ID 和私钥，并在程序中保存凭证。",
                                 code="credentials_required")

    @_public
    def doctor(self):
        installed, configured = self.cli.installed(), self.cli.configured()
        version = self.cli.version() if installed else None
        if installed:
            message = f"官方 CLI {version} 已安装。" + (
                "已保存凭证；可检查现有授权或读取在线清单。" if configured
                else "首次使用需要开放平台凭证，离线整理清单可直接生成。")
        else:
            message = "尚未安装官方 CLI，请运行项目安装脚本。"
        result = {"status": "credentials_saved" if configured else "first_setup_required",
                  "installed": installed, "configured": configured, "version": version,
                  "account_writeback_available": False,
                  "message": message, "last_result": self._last_result()}
        if not installed:
            issue = getattr(self.cli, 'installation_error_code', None)
            result['error_code'] = public_error_code(issue() if callable(issue) else 'cli_missing')
        elif not configured:
            result['error_code'] = 'credentials_required'
        return result

    @_public
    def save_credentials(self, app_id, private_key):
        self._reset_authorization()
        self.cli.save_credentials(app_id, private_key)
        return {"status": "credentials_saved", "message": "凭证已交由官方 CLI 保存，请进行账号授权。"}

    @_public
    def save_credentials_file(self, app_id, path):
        path = Path(path)
        if path.stat().st_size > 65536:
            raise OrganizerError("私钥文件超过 64 KiB，请选择官方生成的 UTF-8 私钥文件。",
                                 code="invalid_private_key")
        try:
            private_key = path.read_text(encoding="utf-8-sig")
        except UnicodeError as error:
            raise OrganizerError("本地文件或返回数据不可用；本次操作未确认完成。",
                                 code="invalid_private_key") from error
        return self.save_credentials(app_id, private_key)

    @_public
    def login(self):
        self._require_credentials()
        self._reset_authorization()
        result = self.cli.run_json(["login", "--background"])
        if not isinstance(result, dict) or result.get("success") is not True:
            raise OrganizerError("官方 CLI 未生成授权链接，请检查凭证及开放平台状态。")
        url = safe_authorization_url(result.get("clickableUrl"))
        if not url:
            raise OrganizerError("官方授权链接格式不兼容，不能确认首次接入。")
        self._authorization_signature = self.cli.token_signature()
        self._authorization_started = self.time_source()
        qr_png = ""
        try:
            renderer = self.qr_renderer
            if renderer is None:
                from .qr import render_authorization_qr
                renderer = render_authorization_qr
            png = renderer(self.project, self.cli.node, url)
            if isinstance(png, bytes) and png.startswith(b"\x89PNG\r\n\x1a\n") and len(png) <= 192 * 1024:
                qr_png = base64.b64encode(png).decode("ascii")
        except Exception:
            # Local QR rendering is optional; it must never hide a valid login
            # link or send the link/exception into a log or artifact.
            pass
        return {"status": "authorization_pending", "authorized": False, "url": url,
                "qr_png_base64": qr_png, "account_writeback_available": False,
                "message": "授权已开始，请用网易云手机应用扫描二维码。程序会自动检测；也可使用授权链接。"
                if qr_png else "授权链接已生成，请自行打开并扫码；程序会自动检测扫码结果。"}

    @_public
    def login_status(self):
        """One explicit official check; only this response proves authorization."""
        self._require_credentials()
        result = self.cli.run_json(["login", "--check"])
        if not isinstance(result, dict) or not isinstance(result.get("success"), bool):
            raise OrganizerError("官方授权状态返回格式不兼容，不能确认是否已扫码。")
        authorized = result["success"] is True
        self._last_authorized_at = self.time_source() if authorized else None
        if authorized:
            self._authorization_started = None
        return {"status": "authorized" if authorized else "authorization_required", "authorized": authorized,
                "account_writeback_available": False,
                "message": "官方 CLI 已确认账号授权；当前账号与歌单归属仍待核实。" if authorized
                else "账号授权尚未完成或已过期，请先完成扫码授权。"}

    @_public
    def authorization_probe(self):
        """Bounded, local metadata watch; call the official check only on change."""
        if self._authorization_started is None:
            return {"status": "authorization_required", "authorized": False,
                    "message": "尚无正在等待的授权，请点击账号授权。"}
        if self.time_source() - self._authorization_started >= 300:
            self._authorization_started = None
            return {"status": "authorization_expired", "authorized": False,
                    "message": "自动扫码检测已结束；可手动检查接入或重新进行账号授权。"}
        signature = self.cli.token_signature()
        if signature == self._authorization_signature:
            return {"status": "authorization_pending", "authorized": False, "message": "等待手机扫码授权。"}
        # Consume the observed change before checking. A newer background write
        # during this check must stay visible to the next probe; an uncertain
        # check must not retry the same change automatically.
        self._authorization_signature = signature
        result = self.login_status()
        if result["authorized"]:
            return result
        return {"status": "authorization_pending", "authorized": False,
                "message": "尚未确认实名授权，继续等待手机扫码。"}

    @_public
    def discover(self):
        self._require_credentials()
        recently_authorized = (self._last_authorized_at is not None
                               and 0 <= self.time_source() - self._last_authorized_at <= 60)
        result = {"authorized": True} if recently_authorized else self.login_status()
        if not result["authorized"]:
            return {"status": "authorization_required", "command_count": 0,
                    "message": "账号授权尚未完成或已过期，请先完成扫码授权。"}
        # The built-in commands action synchronizes the real dynamic manifest.
        # Its human-readable stdout is intentionally discarded, never logged.
        self.cli.run_text(["commands"])
        _, commands = self.cli.manifest()
        parameter_fields = {"name", "in", "location", "type", "required", "description"}
        exported = [{"command": command["command"], "description": command["description"],
                     "parameters": [{key: value for key, value in p.items() if key in parameter_fields}
                                    for p in command["parameters"]]} for command in commands]
        path = self._write_artifact("official-commands.json", json.dumps(
            {"kind": "official_dynamic_commands", "commands": exported,
             "account_writeback_available": False}, ensure_ascii=False, indent=2, allow_nan=False))
        return {"status": "schema_available", "command_count": len(commands), "path": str(path),
                "account_writeback_available": False,
                "message": "官方命令定义已取得。可读取在线清单并核对名称整理；其他操作仍需按清单中的接口限制处理。"}

    @_public
    def prepare(self):
        snapshot = self.reader.load()
        plan = build_plan(snapshot)
        self._write_artifact("自动整理清单.json", json.dumps(plan, ensure_ascii=False, indent=2, allow_nan=False))
        report = self._write_artifact("自动整理清单.md", _plan_report(plan, snapshot))
        return {"status": "offline_plan_ready", "path": str(report), "job_count": plan["summary"]["job_count"],
                "applied_to_account": False,
                "message": "本地候选清单已生成；旧缓存不代表当前账号状态，所有动作仍待在线核对。"}

    def execute(self):
        return {"status": "blocked", "completed_count": 0, "applied_to_account": False,
                "message": "名称整理与已批准的歌手精选可单独执行；歌手精选按官方默认可见性创建（可能公开）。官方未提供私密设置或歌单列表排列接口。"}

    @_public
    def online_preview(self, *, names_only=False):
        from .online import OnlineReader, OnlineError
        from .online_planning import build_online_plan
        self._require_credentials()
        self._online_plan = None
        self._online_snapshot = None
        expected_owner = str(self.reader.load()['owner_id'])
        self.control.checkpoint()
        self._progress({'stage': 'checking'})
        try:
            live = (self.online_reader or OnlineReader(self.cli, expected_owner, control=self.control)).read_snapshot(include_tracks=not names_only)
        except OnlineError as error:
            raise OrganizerError(str(error), code=error.code) from error
        if live['account']['original_id'] != expected_owner:
            raise OrganizerError('在线账号与本地歌单账号不一致，未生成可执行清单。',
                                 code='account_mismatch')
        self.control.checkpoint()
        plan = build_online_plan(live)
        prefix = '在线名称整理' if names_only else '在线整理'
        self._write_artifact(prefix + '快照.json', json.dumps(live, ensure_ascii=False, indent=2, allow_nan=False))
        self._write_artifact(prefix + '清单.json', json.dumps(plan, ensure_ascii=False, indent=2, allow_nan=False))
        lines = ['# 在线整理清单', '',
                 f"账号：{_cell(live['account']['nickname'])}；自建歌单 {len(live['playlists'])} 个。",
                 f"红心总数：{live['liked']['track_count']}；接口返回：{len(live['liked']['tracks'])} 首；"
                 f"缺少记录：{live['liked'].get('missing_record_count', 0)}；缺少歌手资料：{len(live['liked'].get('missing_metadata_track_ids', []))} 首。",
                 '', '已核对官方在线账号、歌单归属及接口实际返回的歌曲 ID。红心缺失项不会被算作完整数据。', '',
                 '| 动作 | 目标 | 状态 |', '| --- | --- | --- |']
        for job in plan['jobs']:
            target = job.get('name', '自建歌单列表排列')
            if job['kind'] == 'rename_playlist':
                target = f"{job['old_name']} → {job['name']}"
            elif job['kind'] == 'create_artist_playlist':
                target += f"（{len(job['candidate_track_ids'])} 首）"
            status = '可执行，执行前再次核对' if job['status'] == 'ready' else job['message']
            action = {'rename_playlist': '统一名称', 'reorder_playlists': '排列歌单',
                      'create_artist_playlist': '歌手精选'}[job['kind']]
            lines.append(f"| {action} | {_cell(target)} | {_cell(status)} |")
        lines += ['', '名称整理与已批准歌手精选可通过各自入口执行；精选需接受官方默认可见性（可能公开）。官方未提供私密设置或歌单列表排列接口。', '']
        if names_only:
            lines[3] = '本次仅核对账号与歌单目录，未读取红心歌曲明细；改名时单独核对目标歌单的完整成员。'
        else:
            tracks = {track['id']: track for track in live['liked']['tracks']}
            for job in plan['jobs']:
                if job['kind'] != 'create_artist_playlist':
                    continue
                lines += ['', f"## {_cell(job['name'])}", '',
                          '候选来自接口实际返回且已取得正式 ID 的歌曲。创建与添加结果以执行记录为准；红心缺失记录不在此列表中。', '']
                for index, track_id in enumerate(job['candidate_track_ids'], 1):
                    track = tracks[track_id]
                    artists = ' / '.join(artist['name'] for artist in track['artists'])
                    lines.append(f"{index}. {_cell(track['name'])} — {_cell(artists)}")
        path = self._write_artifact(prefix + '清单.md', '\n'.join(lines))
        self._online_snapshot, self._online_plan = live, plan
        return {'status': 'online_plan_ready', 'path': str(path), 'job_count': len(plan['jobs']),
                'blocked_count': plan['summary']['blocked_count'], 'applied_to_account': False,
                'message': f"已核对账号 {live['account']['nickname']}，红心总数 {live['liked']['track_count']}，接口返回 {len(live['liked']['tracks'])} 首。"
                           f"可执行名称调整 {plan['summary']['ready_count']} 项；其余动作见接口限制说明。"}

    @_public
    def read_playlist(self, key):
        """Explicitly read one bound target; never update an account or a write receipt."""
        from .online import OnlineReader, OnlineError
        from .playlist_details import validate_record, load_record, MAX_RECORD_BYTES
        from .web_state import load_playlist_binding, directory_signatures
        from .write_journal import JournalError, WriteLeaseBusy, write_lease

        rejected = {'status': 'blocked', 'record_saved': False, 'applied_to_account': False,
                    'write_attempted': False, 'outcome_known': True,
                    'message': '这份歌单没有可靠的本地身份记录，请先主动更新歌单清单。'}
        if type(key) is not str or re.fullmatch(r'[1-9][0-9]{0,19}', key) is None:
            return rejected
        candidate = self.project/'artifacts/歌单明细'/f'{key}.json'
        if any(path.is_symlink() or (path.exists() and bool(getattr(path.stat(), 'st_file_attributes', 0) & 0x400))
               for path in (candidate, *candidate.parents) if path != self.project and path.is_relative_to(self.project)):
            return rejected
        with ExitStack() as stack:
            try:
                relative = Path('.organizer/account-write.lock')
                if (self.project / relative).is_symlink():
                    raise JournalError('账号执行保护不可用。')
                stack.enter_context(write_lease(self._workspace_target(relative)))
            except (WriteLeaseBusy, JournalError, OSError, OrganizerError):
                return {**rejected, 'message': '另一个程序正在操作账号，或本地保护不可用；请稍后主动重试读取。'}
            binding = load_playlist_binding(self.project, key)
            if binding is None:
                return rejected
            self._require_credentials()
            self.control.checkpoint()
            self._progress({'stage': 'checking'})
            try:
                live = (self.online_reader or OnlineReader(self.cli, binding['account']['original_id'],
                            control=self.control)).read_playlist(binding['playlist']['id'], key,
                            expected_account_id=binding['account']['id'], progress=self._progress)
            except OnlineError as error:
                raise OrganizerError(str(error), code=error.code) from error
            if binding['signatures'] != directory_signatures(self.project):
                return {**rejected, 'playlist_key': key,
                        'message': '读取期间本地歌单目录发生变化，原歌曲记录保留；请重新选择歌单后主动读取。'}
            record = validate_record({'kind': 'playlist_details', 'version': 1,
                'read_at': time.time_ns() // 1000000, 'account': live['account'],
                'playlist': live['playlist'], 'complete': live['complete']})
            if ((record['account']['original_id'], record['account']['id']) !=
                    (binding['account']['original_id'], binding['account']['id'])
                    or (record['playlist']['original_id'], record['playlist']['id']) !=
                    (key, binding['playlist']['id'])):
                return rejected
            detail = record['playlist']
            counts = {'expected_count': detail['track_count'], 'count': len(detail['tracks']),
                      'missing_count': detail['missing_record_count'],
                      'missing_metadata_count': len(detail['missing_metadata_track_ids'])}
            result = {'playlist_key': key, **counts, 'applied_to_account': False,
                      'write_attempted': False, 'outcome_known': True, 'record_saved': False}
            payload = json.dumps(record, ensure_ascii=False, indent=2, allow_nan=False)
            if len(payload.encode('utf-8')) + 1 > MAX_RECORD_BYTES:
                return {**result, 'status': 'blocked', 'message': '歌曲明细超出本地记录上限，原记录保留。'}
            self.control.checkpoint()
            self._progress({'stage': 'saving', 'completed_count': counts['count'],
                            'total_count': counts['expected_count']})
            self.control.checkpoint()
            if binding['signatures'] != directory_signatures(self.project):
                return {**result, 'status': 'blocked',
                        'message': '保存前本地歌单目录发生变化，原歌曲记录保留；请重新选择歌单后主动读取。'}
            try:
                self._write_artifact(Path('歌单明细') / f'{key}.json', payload)
                saved = load_record(self.project, key)
                if saved is None or saved[0] != record:
                    raise ValueError('Local detail save was not verified')
            except (OSError, UnicodeError, ValueError, OrganizerError):
                return self._finish_result({**result, 'status': 'failed',
                    'message': '新歌曲明细未能可靠保存，请查看本地记录；本次没有修改账号。'})
            return self._finish_result({**result, 'record_saved': True,
                'status': 'completed' if record['complete'] else 'partial',
                'message': '这份歌单的歌曲明细已保存。' if record['complete'] else
                           '已保存实际返回的歌曲明细，缺失歌曲或资料不完整的数量已标明。'})

    @_public
    @_account_write
    def execute_renames(self):
        from .rename import RenameExecutor, RenameError
        self._require_credentials()
        if self._online_plan is None:
            preview = self.online_preview(names_only=True)
            if isinstance(preview, dict) and preview.get('status') == 'paused':
                return preview
        jobs = [j for j in self._online_plan['jobs'] if j['kind'] == 'rename_playlist' and j['status'] == 'ready']
        expected_owner = self._online_snapshot['account']['original_id']
        from .write_journal import JournalError, MAX_INTENT_BYTES, intent_digest
        run_id, last_intent = uuid.uuid4().hex, None

        def journal(record):
            nonlocal last_intent
            intent = {**record, 'kind': 'rename_execution_journal', 'version': 1, 'run_id': run_id}
            intent_digest(intent)  # Validate before persisting and before any write.
            payload = json.dumps(intent, ensure_ascii=False, indent=2, allow_nan=False)
            if len(payload.encode('utf-8')) + 1 > MAX_INTENT_BYTES:
                raise JournalError('名称写入记录超出可核对范围，本项未提交。')
            self._write_artifact('名称整理执行进度.json', payload)
            last_intent = intent

        try:
            if self.rename_executor is None:
                self.rename_executor = RenameExecutor(self.cli, expected_owner, control=self.control,
                                                       progress_listener=self._progress, journal_writer=journal)
            elif isinstance(self.rename_executor, RenameExecutor):
                self.rename_executor.journal_writer = journal
            result = self.rename_executor.execute(jobs)
        except RenameError as error:
            raise OrganizerError(str(error)) from error
        self._online_plan = None
        self._online_snapshot = None
        result = self._with_performance(result)
        if last_intent is not None:
            result.update(run_id=run_id, intent_digest=intent_digest(last_intent))
        try:
            path = self._write_artifact('名称整理执行结果.json', json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False))
        except (OSError, OrganizerError):
            return {**result, 'record_saved': False,
                    'message': f"名称整理已确认完成 {result['completed_count']} 项，但本地结果文件保存失败。账号变化仍以已核对结果为准，请勿重复提交。"}
        return {**result, 'path': str(path), 'record_saved': True,
                'message': f"名称整理已确认完成 {result['completed_count']} 项。执行结果和未完成原因已写入本地记录。"}

    @_public
    def reconcile_renames(self):
        """Explicit account reads may finish one durable intent; never replay it."""
        from .rename import RenameExecutor, _same_id
        from .write_journal import (JournalError, WriteLeaseBusy, intent_digest,
                                    read_rename_intent, receipt_resolves_intent, write_lease)
        base = {'completed_count': 0, 'applied_to_account': False,
                'write_attempted': False, 'outcome_known': False}
        try:
            lock_path = self._workspace_target(Path('.organizer/account-write.lock'))
            with write_lease(lock_path):
                recovery = self.write_recovery()
                if not recovery['can_reconcile'] or recovery['operation'] != 'renames':
                    return {**base, 'status': 'blocked',
                            'message': '没有可只读核对的名称执行记录，请查看本地记录；本次没有发送账号修改。'}
                relative = Path('artifacts/名称整理执行进度.json')
                if (self.project / relative).is_symlink():
                    raise JournalError('名称执行记录不可用。')
                intent = read_rename_intent(self._workspace_target(relative))
                if intent is None:
                    raise JournalError('名称执行记录不可用。')
                base['items'] = json.loads(json.dumps(intent['items'], ensure_ascii=False))
                base['completed_count'] = sum(item['status'] == 'completed'
                                              for item in base['items'][:intent['job_index']])
                base['applied_to_account'] = base['completed_count'] > 0
                self._require_credentials()
                executor = RenameExecutor(self.cli, intent['expected_owner_id'], control=self.control)
                index, job = intent['job_index'], intent['jobs'][intent['job_index']]
                try:
                    self.control.checkpoint()
                    self._progress({'phase': 'checking'})
                    owner_id = executor._account()
                    if not _same_id(owner_id, intent['owner_id']):
                        raise ValueError('owner changed')
                    self.control.checkpoint()
                    before = executor._get(job, owner_id)
                    if before['name'] != job['name']:
                        raise ValueError('target not confirmed')
                    base['applied_to_account'] = True
                    if (before['trackCount'] != intent['before']['trackCount']
                            or before['specialType'] != intent['before']['specialType']):
                        raise ValueError('playlist changed')
                    self.control.checkpoint()
                    tracks = executor._tracks(job, before['trackCount'])
                    digest = hashlib.sha256('\n'.join(tracks).encode('utf-8')).hexdigest()
                    if digest != intent['tracks_sha256']:
                        raise ValueError('members changed')
                    self.control.checkpoint()
                    after = executor._get(job, owner_id)
                    if not executor._stable_detail(before, after):
                        raise ValueError('unstable playlist')
                    self.control.checkpoint()
                    if not _same_id(executor._account(), intent['owner_id']):
                        raise ValueError('owner changed')
                except OperationPaused:
                    return {**base, 'status': 'paused',
                            'message': '只读核对已暂停，原待核对记录仍保留；没有发送账号修改。'}
                except Exception:
                    return {**base, 'status': 'uncertain',
                            'message': '仍未确认目标名称、归属或完整歌曲顺序，原记录已保留；没有重复发送改名。'}
                # Preserve prior confirmed items. Remaining jobs become an
                # explicit paused batch; a read-only check never starts them.
                items = json.loads(json.dumps(intent['items'], ensure_ascii=False))
                items[index].update(status='completed', message='通过只读核对确认目标名称、歌单归属及完整歌曲顺序。')
                for item in items[index + 1:]:
                    item.update(status='pending', message='只读核对未执行本项，后续整理仍暂停。')
                completed = sum(item['status'] == 'completed' for item in items)
                finished = all(item['status'] in {'completed', 'skipped'} for item in items)
                base['items'] = items
                receipt = {'status': 'completed' if finished else 'paused', 'completed_count': completed,
                           'items': items, 'applied_to_account': True, 'write_attempted': True,
                           'outcome_known': True, 'run_id': intent['run_id'],
                           'intent_digest': intent_digest(intent), 'reconciled_read_only': True}
                try:
                    path = self._write_artifact('名称整理执行结果.json', json.dumps(
                        receipt, ensure_ascii=False, indent=2, allow_nan=False))
                    if not receipt_resolves_intent(intent, path):
                        raise JournalError('名称结果保存未确认。')
                except (JournalError, OrganizerError, OSError, UnicodeError, ValueError):
                    return {**base, 'status': 'completed', 'completed_count': completed,
                            'outcome_known': True, 'record_saved': False,
                            'message': '只读核对已确认名称与歌曲顺序，但结果记录未可靠保存；继续保留待核对保护。'}
                self._online_plan = self._online_snapshot = None
                self.rename_executor = None
                return {**base, 'status': 'completed', 'completed_count': completed,
                        'outcome_known': True, 'record_saved': True, 'path': str(path),
                        'message': '已只读核对上次改名并保存结果，后续任务仍暂停。本次没有发送账号修改。'}
        except WriteLeaseBusy:
            return {**base, 'status': 'blocked',
                    'message': '另一个程序正在操作账号，请等待其完成后再只读核对。'}
        except (JournalError, OrganizerError, OSError, UnicodeError, ValueError):
            return {**base, 'status': 'blocked',
                    'message': '名称执行记录暂不可用，待核对保护仍保留；本次没有发送账号修改。'}

    @_public
    @_account_write
    def execute_artist_playlists(self, *, accept_default_visibility=False, resume_created=False, resume_job_index=0,
                                 resume_paused=False):
        """Execute only the explicitly approved five selections after a fresh read."""
        def blocked(message):
            return {'status': 'blocked', 'completed_count': 0, 'applied_to_account': False,
                    'write_attempted': False, 'outcome_known': True, 'message': message}
        if accept_default_visibility is not True:
            return blocked('创建歌手精选需要接受官方默认可见性（可能公开）。')
        progress = self._workspace_target(Path('artifacts/歌手精选执行进度.json'))
        recovering = resume_created is True
        continuing = resume_paused is True
        if recovering and continuing:
            return blocked('恢复方式冲突，不能继续写入。')
        if type(resume_job_index) is not int or not 0 <= resume_job_index < 5:
            return blocked('恢复目标序号不兼容，不能继续写入。')
        previous_receipt = self._read_write_file('歌手精选执行结果.json')
        if (not (recovering or continuing) and isinstance(previous_receipt, dict)
                and (previous_receipt.get('applied_to_account') is True
                     or previous_receipt.get('write_attempted') is True
                     or previous_receipt.get('completed_count', 0) > 0)):
            return blocked('已存在这五个歌手精选的账号执行结果，请查看记录；不会再次创建。')
        marker_name = '歌手精选恢复开始.json' if resume_job_index == 0 else f'歌手精选恢复开始-{resume_job_index}.json'
        recovery_marker = self._workspace_target(Path('artifacts') / marker_name)
        if recovering and (not progress.exists() or recovery_marker.exists()):
            return blocked('没有可恢复的首次创建记录，或恢复已开始；不能重复提交。')
        if continuing and not progress.exists():
            return blocked('没有可续做的暂停记录。')
        if progress.exists() and not (recovering or continuing):
            return blocked('已存在歌手精选执行记录，请查看结果；本程序不会重复创建这五个歌单。')
        lock = self._workspace_target(Path('.organizer/artist-execution.lock'))
        lock.parent.mkdir(parents=True, exist_ok=True)
        try:
            lock_fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            return blocked('另一次歌手精选操作正在执行或尚未核对结束，已阻止重复提交。')
        try:
            # Recheck after acquiring the process-independent exclusive lock.
            if recovering and (not progress.exists() or recovery_marker.exists()):
                return blocked('恢复已开始或记录已变化，不能重复提交。')
            if progress.exists() and not (recovering or continuing):
                return blocked('已存在歌手精选执行记录，请查看结果；不会重复创建。')
            self._require_credentials()
            approval_path = self._workspace_target(Path('artifacts/已批准歌手精选.json'))
            try:
                if approval_path.stat().st_size > 65536:
                    return blocked('已批准候选记录不兼容，未提交账号修改。')
                approval = json.loads(approval_path.read_text(encoding='utf-8'))
            except (OSError, UnicodeError, ValueError):
                return blocked('未取得这五个歌单的已批准候选记录，未提交账号修改。')
            expected_owner = str(self.reader.load()['owner_id'])
            if (not isinstance(approval, dict) or approval.get('kind') != 'approved_artist_selection'
                    or approval.get('account_original_id') != expected_owner
                    or approval.get('intended_visibility') != 'provider_default'
                    or not isinstance(approval.get('jobs'), list)):
                return blocked('已批准范围或账号不一致，未提交账号修改。')
            approved_jobs = approval['jobs']
            if (len(approved_jobs) != 5 or any(not isinstance(j, dict) for j in approved_jobs)
                    or {j.get('name') for j in approved_jobs} != set(_APPROVED_ARTIST_COUNTS)
                    or any(j.get('kind') != 'create_artist_playlist'
                           or not isinstance(j.get('candidate_track_ids'), list)
                           or len(j['candidate_track_ids']) != _APPROVED_ARTIST_COUNTS[j['name']]
                           for j in approved_jobs)):
                return blocked('已批准候选不再对应这五个歌单，未提交账号修改。')
            recovery = None
            checkpoint = None
            checkpoint_marker = None
            if continuing:
                try:
                    if progress.stat().st_size > 256 * 1024:
                        return blocked('暂停记录过大，不能继续写入。')
                    checkpoint_bytes = progress.read_bytes()
                    checkpoint = json.loads(checkpoint_bytes.decode('utf-8'))
                    checkpoint_digest = hashlib.sha256(checkpoint_bytes).hexdigest()
                    checkpoint_marker = self._workspace_target(Path('artifacts') / f'暂停续做-{checkpoint_digest}.json')
                    if (checkpoint_marker.exists() or not isinstance(checkpoint, dict)
                            or checkpoint.get('kind') != 'artist_execution_journal'
                            or checkpoint.get('account_original_id') != expected_owner
                            or checkpoint.get('approved_visibility') != 'provider_default'
                            or checkpoint.get('status') != 'paused' or checkpoint.get('phase') != 'paused'
                            or checkpoint.get('outcome_known') is not True
                            or not isinstance(checkpoint.get('jobs'), list) or len(checkpoint['jobs']) != 5
                            or not isinstance(checkpoint.get('items'), list) or len(checkpoint['items']) != 5
                            or any(not isinstance(j, dict) or j.get('name') != approved['name']
                                   or j.get('candidate_track_ids') != approved['candidate_track_ids']
                                   or j.get('intended_visibility') != 'provider_default'
                                   for j, approved in zip(checkpoint['jobs'], approved_jobs))):
                        return blocked('暂停记录与批准范围不一致，或已被消费；未提交账号修改。')
                except (OSError, UnicodeError, ValueError, OrganizerError):
                    return blocked('暂停记录不可用，不能继续写入。')
            if recovering:
                try:
                    evidence_path = self._workspace_target(Path('artifacts/歌手精选恢复证据.json'))
                    previous_path = self._workspace_target(Path('artifacts/歌手精选执行结果.json'))
                    if evidence_path.stat().st_size > 65536 or previous_path.stat().st_size > 65536:
                        return blocked('恢复记录不兼容，不能继续写入。')
                    evidence = json.loads(evidence_path.read_text(encoding='utf-8'))
                    previous = json.loads(previous_path.read_text(encoding='utf-8'))
                    first = previous['items'][resume_job_index]
                    recovery = evidence['recovery']
                    digest = hashlib.sha256(approval_path.read_bytes()).hexdigest()
                    if (evidence.get('kind') != 'verified_create_only_recovery'
                            or evidence.get('account_original_id') != expected_owner
                            or evidence.get('approval_sha256') != digest
                            or evidence.get('observed_mutations') != ['create']
                            or evidence.get('add_or_reorder_observed') is not False
                            or previous.get('status') != 'uncertain'
                            or previous.get('completed_count') != resume_job_index
                            or previous.get('applied_to_account') is not True
                            or len(previous['items']) != 5
                            or any(i.get('status') != 'completed' for i in previous['items'][:resume_job_index])
                            or any(i.get('playlist_id') is not None for i in previous['items'][resume_job_index + 1:])
                            or recovery.get('create_only_verified') is not True
                            or type(recovery.get('job_index')) is not int or recovery['job_index'] != resume_job_index
                            or recovery.get('name') != approved_jobs[resume_job_index]['name']
                            or any(recovery.get(k) != first.get(k)
                                   for k in ('name', 'playlist_id', 'original_playlist_id'))):
                        return blocked('恢复证据不能确认首项仅创建且尚未添加，停止写入。')
                except (OSError, UnicodeError, ValueError, KeyError, TypeError, IndexError):
                    return blocked('恢复证据不可用，不能继续写入。')
            preview = self.online_preview()
            if isinstance(preview, dict) and preview.get('status') == 'paused':
                if continuing:
                    return {**preview, 'completed_count': checkpoint.get('completed_count', 0),
                            'applied_to_account': checkpoint.get('applied_to_account') is True,
                            'items': checkpoint['items'], 'outcome_known': True,
                            'message': '已暂停续做；保留上次已确认的进度，本次未提交新修改。'}
                return preview
            fresh = [j for j in self._online_plan['jobs'] if j['kind'] == 'create_artist_playlist']
            by_name = {j['name']: j for j in fresh}
            if (self._online_snapshot['account']['original_id'] != expected_owner
                    or len(fresh) != 5 or set(by_name) != set(_APPROVED_ARTIST_COUNTS)
                    or any(by_name[j['name']]['candidate_track_ids'] != j['candidate_track_ids']
                           for j in approved_jobs)):
                return blocked('在线候选已发生变化，与已批准的67项歌曲不一致，未提交账号修改。')
            jobs = [{**by_name[j['name']], 'intended_visibility': 'provider_default', 'status': 'ready'}
                    for j in approved_jobs]
            if recovering:
                previous['items'][:resume_job_index] = [
                    {**item, 'kind': 'create_artist_playlist', 'phase': 'completed',
                     'expected_count': len(jobs[index]['candidate_track_ids'])}
                    for index, item in enumerate(previous['items'][:resume_job_index])]
            run_id = uuid.uuid4().hex
            def journal(state):
                record = {**state, 'kind': 'artist_execution_journal',
                          'account_original_id': expected_owner, 'approved_visibility': 'provider_default',
                          'run_id': run_id}
                if recovering:
                    record['absolute_job_index'] = resume_job_index + state.get('job_index', 0)
                    record['previous_completed_items'] = previous['items'][:resume_job_index]
                    if len(state.get('jobs', [])) == 5 - resume_job_index:
                        record['jobs'] = [{'kind': j['kind'], 'name': j['name'],
                                           'candidate_track_ids': j['candidate_track_ids'],
                                           'intended_visibility': 'provider_default'} for j in jobs]
                        record['items'] = previous['items'][:resume_job_index] + state.get('items', [])
                        record['completed_count'] = sum(i.get('status') == 'completed' for i in record['items'])
                        if resume_job_index:
                            record['applied_to_account'] = True
                return self._write_artifact('歌手精选执行进度.json', json.dumps(
                    record, ensure_ascii=False, indent=2, allow_nan=False))
            if self.artist_executor is None:
                from .create import ArtistExecutor
                self.artist_executor = ArtistExecutor(self.cli, expected_owner, journal_writer=journal,
                                                      control=self.control, progress_listener=self._progress)
            from .create import ArtistError
            try:
                if continuing:
                    from .create import ArtistExecutor
                    # Save the consumption marker before any resumed write.
                    # A crash cannot turn an unknown write into an automatic retry.
                    self._write_artifact(checkpoint_marker.name, json.dumps(
                        {'kind': 'paused_checkpoint_consumed', 'checkpoint_sha256': checkpoint_digest,
                         'account_original_id': expected_owner, 'run_id': run_id}, ensure_ascii=False))
                    self.artist_executor = ArtistExecutor(self.cli, expected_owner, journal_writer=journal,
                                                          control=self.control, progress_listener=self._progress)
                    result = self.artist_executor.resume_checkpoint(jobs, checkpoint['items'])
                elif recovering:
                    from .create import ArtistExecutor
                    verifier = ArtistExecutor(self.cli, expected_owner, journal_writer=journal,
                                              control=self.control, progress_listener=self._progress)
                    owner_id = verifier._account()
                    for index, item in enumerate(previous['items'][:resume_job_index]):
                        if item['name'] != approved_jobs[index]['name']:
                            return blocked('已完成歌单与批准范围不一致，停止恢复。')
                        bound = {'id': item['playlist_id'], 'original_id': item['original_playlist_id'], 'name': item['name']}
                        _, current_tracks = verifier._snapshot(bound, owner_id)
                        if current_tracks != approved_jobs[index]['candidate_track_ids']:
                            return blocked('已完成歌单的内容或顺序变化，停止恢复。')
                    if verifier._account() != owner_id:
                        return blocked('恢复前账号发生变化，停止写入。')
                    # This durable marker prevents another recovery after any
                    # uncertain add, including an interrupted process.
                    backup = '歌手精选执行结果-首轮.json' if resume_job_index == 0 else f'歌手精选执行结果-续轮{resume_job_index}.json'
                    self._write_artifact(backup, json.dumps(previous, ensure_ascii=False, indent=2))
                    self._write_artifact(marker_name, json.dumps(evidence, ensure_ascii=False, indent=2))
                    self.artist_executor = ArtistExecutor(self.cli, expected_owner, journal_writer=journal,
                                                          control=self.control, progress_listener=self._progress)
                    result = self.artist_executor.resume_created(jobs[resume_job_index:], {**recovery, 'job_index': 0})
                    result['items'] = previous['items'][:resume_job_index] + result['items']
                    result['completed_count'] += resume_job_index
                    if resume_job_index:
                        result['applied_to_account'] = True
                        if result['status'] == 'blocked':
                            result['status'] = 'partial'
                else:
                    result = self.artist_executor.execute(jobs)
            except ArtistError as error:
                raise OrganizerError(str(error)) from error
            self._online_plan = None
            self._online_snapshot = None
            result = self._with_performance(result)
            result['run_id'] = run_id
            try:
                path = self._write_artifact('歌手精选执行结果.json', json.dumps(
                    result, ensure_ascii=False, indent=2, allow_nan=False))
                last_intent = json.loads(progress.read_text(encoding='utf-8')) if progress.exists() else {}
                # Keep the last mutation phase and its complete jobs. The
                # receipt enriches the journal; it does not erase the intent.
                if (isinstance(last_intent, dict) and isinstance(last_intent.get('phase'), str)
                        and isinstance(last_intent.get('jobs'), list) and len(last_intent['jobs']) == 5):
                    journal({**last_intent, **result, 'stage': 'finished'})
            except (OSError, UnicodeError, ValueError, OrganizerError):
                return {**result, 'record_saved': False,
                        'message': f"已确认完成 {result['completed_count']} 个歌手精选，但结果文件保存失败。请勿重复创建。"}
            return {**result, 'path': str(path), 'record_saved': True,
                    'message': f"已确认完成 {result['completed_count']} 个歌手精选，按官方默认可见性创建（可能公开）。结果及未完成原因见本地记录。"}
        finally:
            os.close(lock_fd)
            lock.unlink(missing_ok=True)
