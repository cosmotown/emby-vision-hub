# monitor_service.py

import os
import re
import stat
import time
import logging
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from typing import List, Optional, Any, Callable
from watchdog.events import FileSystemEventHandler
from gevent import spawn_later

import constants
import config_manager
import handler.emby as emby
import utils
from database import strm_ingest_db
from services.emby_ingest import (
    check_indexed_paths,
    delete_and_verify_paths,
    reconcile_paths,
    refresh_and_verify_paths,
    wait_for_paths_stable,
)
from services.strm_inventory import IncrementalStrmInventory
from services.persisted_directory_watcher import PersistedDirectoryObserver

from typing import TYPE_CHECKING
if TYPE_CHECKING:
    from core_processor import MediaProcessor

logger = logging.getLogger(__name__)

# --- 全局队列和锁 ---
FILE_EVENT_QUEUE = set() 
QUEUE_LOCK = threading.Lock()
DEBOUNCE_TIMER = None
DELETE_EVENT_QUEUE = set()
DELETE_QUEUE_LOCK = threading.Lock()
DELETE_DEBOUNCE_TIMER = None

DEBOUNCE_DELAY = 3 # 防抖延迟秒数

# --- STRM 自适应批处理 -------------------------------------------------------
# 小批量保持原有 3 秒防抖后的快速处理；同一作品在短时间持续到达时，自动切换
# 为大批量静默聚合，等待写入安静或达到最长等待后再统一通知 Emby。
ADAPTIVE_BURST_WINDOW_SECONDS = 75
ADAPTIVE_BULK_THRESHOLD = 4
ADAPTIVE_BULK_PATH_THRESHOLD = 25
ADAPTIVE_BULK_QUIET_SECONDS = 60
ADAPTIVE_BULK_MAX_HOLD_SECONDS = 600
ADAPTIVE_BULK_VERIFY_DELAY_SECONDS = 30
ADAPTIVE_BATCH_POLL_SECONDS = 2
TERMINAL_RECHECK_INITIAL_DELAY_SECONDS = 300
TERMINAL_RECHECK_INTERVAL_SECONDS = 3600
TERMINAL_RECHECK_BATCH_SIZE = 200

_ADAPTIVE_REFRESH_LOCK = threading.Lock()
_ADAPTIVE_REFRESH_STATES = {}
_ADAPTIVE_REFRESH_WORKER = None
_ADAPTIVE_REFRESH_STOP = threading.Event()
_SEASON_DIRECTORY_RE = re.compile(
    r"^(?:season[\s._-]*\d+|specials?|第\s*\d+\s*季)$",
    re.IGNORECASE,
)


class _MonitorTaskPool:
    """Lifecycle-owned fixed worker pool with bounded, interruptible backlog."""

    MAX_WORKERS = 4
    MAX_TASKS = 64
    SLOT_WAIT_SECONDS = 0.1

    def __init__(self):
        self._lock = threading.RLock()
        self._state = 'stopped'
        self._executor = None
        self._slots = None
        self._futures = set()
        self._stop_waiters = threading.Event()
        self._stop_waiters.set()
        self._generation = 0

    @property
    def state(self) -> str:
        with self._lock:
            return self._state

    @property
    def accepting(self) -> bool:
        return self.state == 'accepting'

    @property
    def generation(self) -> int:
        with self._lock:
            return self._generation

    @property
    def active_count(self) -> int:
        with self._lock:
            return len(self._futures)

    def start(self) -> int:
        with self._lock:
            if self._state == 'accepting':
                return self._generation
            if self._state == 'draining':
                raise RuntimeError("实时监控任务池仍在停止中")
            self._generation += 1
            self._executor = ThreadPoolExecutor(
                max_workers=self.MAX_WORKERS,
                thread_name_prefix=f'evh-monitor-task-{self._generation}',
            )
            self._slots = threading.BoundedSemaphore(self.MAX_TASKS)
            self._futures = set()
            self._stop_waiters = threading.Event()
            self._state = 'accepting'
            return self._generation

    def stop_accepting(self) -> None:
        with self._lock:
            if self._state == 'accepting':
                self._state = 'draining'
            self._stop_waiters.set()

    def submit(
        self,
        target,
        *args,
        on_cancel: Optional[Callable[[], None]] = None,
        **kwargs,
    ):
        warned = False
        while True:
            with self._lock:
                if self._state != 'accepting':
                    return None
                slots = self._slots
                executor = self._executor
                generation = self._generation
            if slots.acquire(timeout=self.SLOT_WAIT_SECONDS):
                break
            if self._stop_waiters.is_set():
                return None
            if not warned:
                warned = True
                logger.warning(
                    "实时监控后台队列已满，等待空闲槽位；停止监控可中断等待。"
                )

        with self._lock:
            if (
                self._state != 'accepting'
                or self._generation != generation
                or self._executor is not executor
            ):
                slots.release()
                return None
            try:
                future = executor.submit(target, *args, **kwargs)
            except Exception:
                slots.release()
                self._state = 'draining'
                self._stop_waiters.set()
                logger.exception("实时监控后台任务提交失败，任务池已停止接收新任务")
                return None
            self._futures.add(future)

            def _complete(completed):
                try:
                    if completed.cancelled():
                        if on_cancel:
                            try:
                                on_cancel()
                            except Exception:
                                logger.exception("实时监控取消任务回放来源状态失败")
                    else:
                        try:
                            completed.result()
                        except Exception:
                            logger.exception("实时监控后台任务执行失败")
                finally:
                    with self._lock:
                        self._futures.discard(completed)
                    try:
                        slots.release()
                    except ValueError:
                        logger.critical("实时监控任务槽位发生重复释放", exc_info=True)

            future.add_done_callback(_complete)
            return future

    def shutdown(self) -> None:
        self.stop_accepting()
        with self._lock:
            executor = self._executor
            futures = list(self._futures)
        if executor is None:
            with self._lock:
                self._state = 'stopped'
            return

        for future in futures:
            if not future.running() and not future.done():
                future.cancel()
        executor.shutdown(wait=True, cancel_futures=True)

        with self._lock:
            self._executor = None
            self._slots = None
            self._futures.clear()
            self._state = 'stopped'
            self._stop_waiters.set()


_MONITOR_TASK_POOL = _MonitorTaskPool()


def _submit_monitor_task(target, *args, on_cancel=None, **kwargs):
    """Submit monitor work only while the current monitor generation accepts it."""
    return _MONITOR_TASK_POOL.submit(
        target,
        *args,
        on_cancel=on_cancel,
        **kwargs,
    )


def _restore_file_event_paths(file_paths: List[str]) -> None:
    """Idempotently return an unaccepted batch to the normal debounce source."""
    global DEBOUNCE_TIMER
    paths = {
        os.path.normpath(str(path))
        for path in (file_paths or [])
        if str(path or '').strip()
    }
    if not paths:
        return
    with QUEUE_LOCK:
        FILE_EVENT_QUEUE.update(paths)
        if (
            _MONITOR_TASK_POOL.accepting
            and config_manager.APP_CONFIG.get(
                constants.CONFIG_OPTION_MONITOR_ENABLED,
                False,
            )
        ):
            if DEBOUNCE_TIMER:
                DEBOUNCE_TIMER.kill()
            DEBOUNCE_TIMER = spawn_later(DEBOUNCE_DELAY, process_batch_queue)


def _restore_delete_event_paths(file_paths: List[str]) -> None:
    """Idempotently return an unaccepted delete batch to its debounce source."""
    global DELETE_DEBOUNCE_TIMER
    paths = {
        os.path.normpath(str(path))
        for path in (file_paths or [])
        if str(path or '').strip()
    }
    if not paths:
        return
    with DELETE_QUEUE_LOCK:
        DELETE_EVENT_QUEUE.update(paths)
        if (
            _MONITOR_TASK_POOL.accepting
            and config_manager.APP_CONFIG.get(
                constants.CONFIG_OPTION_MONITOR_ENABLED,
                False,
            )
        ):
            if DELETE_DEBOUNCE_TIMER:
                DELETE_DEBOUNCE_TIMER.kill()
            DELETE_DEBOUNCE_TIMER = spawn_later(
                DEBOUNCE_DELAY,
                process_delete_batch_queue,
            )


def _classify_retry_paths(paths):
    """Use one successful parent snapshot per directory before inferring missing."""
    existing = []
    unresolved_by_parent = {}
    for raw_path in paths or []:
        path = os.path.normpath(str(raw_path))
        if os.path.isfile(path):
            existing.append(path)
        else:
            unresolved_by_parent.setdefault(os.path.dirname(path), []).append(path)

    confirmed_missing = []
    inaccessible = []
    for parent, parent_paths in unresolved_by_parent.items():
        try:
            with os.scandir(parent) as entries:
                names = {entry.name for entry in entries}
        except OSError:
            inaccessible.extend(parent_paths)
            continue
        for path in parent_paths:
            if os.path.basename(path) in names:
                inaccessible.append(path)
            else:
                confirmed_missing.append(path)
    return sorted(existing), sorted(confirmed_missing), sorted(inaccessible)


def _restore_adaptive_refresh_batch(batch: dict) -> None:
    """Return a popped adaptive batch without widening or duplicating paths."""
    paths = {
        os.path.normpath(str(path))
        for path in (batch.get('paths') or [])
        if str(path or '').strip()
    }
    if not paths:
        return
    current = time.monotonic()
    with _ADAPTIVE_REFRESH_LOCK:
        state = _ADAPTIVE_REFRESH_STATES.setdefault(batch['key'], {
            'processor': batch.get('processor'),
            'arrivals': [],
            'pending': set(),
            'bulk': True,
            'first_pending_at': current,
            'last_seen': current,
        })
        state['processor'] = batch.get('processor')
        state['bulk'] = True
        state['pending'].update(paths)
        if state.get('first_pending_at') is None:
            state['first_pending_at'] = current
        state['last_seen'] = current


def _preserve_adaptive_paths_for_restart() -> None:
    """Move pending adaptive paths back to the neutral file-event source."""
    with _ADAPTIVE_REFRESH_LOCK:
        pending = {
            path
            for state in _ADAPTIVE_REFRESH_STATES.values()
            for path in (state.get('pending') or set())
        }
        _ADAPTIVE_REFRESH_STATES.clear()
    _restore_file_event_paths(sorted(pending))


def _adaptive_work_key(file_path: str, exclude_paths: List[str] = None) -> str:
    """Return a stable per-title key without ever widening to a library root."""
    path = os.path.normpath(str(file_path or '').strip())
    parent = os.path.dirname(path)
    if _SEASON_DIRECTORY_RE.match(os.path.basename(parent)):
        work_dir = os.path.dirname(parent)
    else:
        work_dir = parent

    normalized_excludes = {
        os.path.normcase(os.path.normpath(str(value)))
        for value in (exclude_paths or [])
        if str(value or '').strip()
    }
    if os.path.normcase(os.path.normpath(work_dir)) in normalized_excludes:
        # Flat files directly below an excluded/library root must remain isolated.
        return path
    return work_dir or path


def _reset_adaptive_refresh_state():
    """Clear process-local adaptive batching state (used by tests/restarts)."""
    global _ADAPTIVE_REFRESH_WORKER, _ADAPTIVE_REFRESH_STOP
    _ADAPTIVE_REFRESH_STOP.set()
    with _ADAPTIVE_REFRESH_LOCK:
        worker = _ADAPTIVE_REFRESH_WORKER
        _ADAPTIVE_REFRESH_STATES.clear()
        _ADAPTIVE_REFRESH_WORKER = None
    if worker and worker.is_alive() and worker is not threading.current_thread():
        worker.join(timeout=1)
    _ADAPTIVE_REFRESH_STOP = threading.Event()


def _register_adaptive_refresh_paths(
    processor,
    file_paths: List[str],
    exclude_paths: List[str] = None,
    now: float = None,
):
    """Register one debounced arrival and return paths that should run fast now."""
    current = time.monotonic() if now is None else float(now)
    grouped = {}
    for raw_path in file_paths or []:
        path = os.path.normpath(str(raw_path or '').strip())
        if not path:
            continue
        key = _adaptive_work_key(path, exclude_paths)
        grouped.setdefault(key, set()).add(path)

    immediate_paths = []
    activated_keys = []
    with _ADAPTIVE_REFRESH_LOCK:
        for key, paths in grouped.items():
            state = _ADAPTIVE_REFRESH_STATES.setdefault(key, {
                'processor': processor,
                'arrivals': [],
                'pending': set(),
                'bulk': False,
                'first_pending_at': None,
                'last_seen': current,
            })
            state['processor'] = processor
            arrivals = [
                stamp for stamp in state.get('arrivals', [])
                if current - stamp <= ADAPTIVE_BURST_WINDOW_SECONDS
            ]
            arrivals.append(current)
            state['arrivals'] = arrivals
            state['last_seen'] = current

            should_bulk = (
                state.get('bulk', False)
                or len(arrivals) >= ADAPTIVE_BULK_THRESHOLD
                or len(paths) >= ADAPTIVE_BULK_PATH_THRESHOLD
            )
            if should_bulk:
                if not state.get('bulk', False):
                    state['bulk'] = True
                    activated_keys.append(key)
                state['pending'].update(paths)
                if state.get('first_pending_at') is None:
                    state['first_pending_at'] = current
            else:
                immediate_paths.extend(paths)

    return sorted(set(immediate_paths)), sorted(activated_keys)


def _pop_due_adaptive_refresh_batches(now: float = None):
    """Return quiet/max-hold bulk batches and prune expired fast-mode history."""
    current = time.monotonic() if now is None else float(now)
    due_batches = []
    with _ADAPTIVE_REFRESH_LOCK:
        for key, state in list(_ADAPTIVE_REFRESH_STATES.items()):
            last_seen_value = state.get('last_seen')
            last_seen = current if last_seen_value is None else float(last_seen_value)
            pending = set(state.get('pending') or set())
            if not state.get('bulk', False):
                if current - last_seen > ADAPTIVE_BURST_WINDOW_SECONDS:
                    _ADAPTIVE_REFRESH_STATES.pop(key, None)
                continue

            quiet_due = current - last_seen >= ADAPTIVE_BULK_QUIET_SECONDS
            first_pending_at = state.get('first_pending_at')
            max_due = (
                first_pending_at is not None
                and current - float(first_pending_at) >= ADAPTIVE_BULK_MAX_HOLD_SECONDS
            )

            if pending and (quiet_due or max_due):
                due_batches.append({
                    'key': key,
                    'processor': state.get('processor'),
                    'paths': sorted(pending),
                    'reason': 'quiet' if quiet_due else 'max_hold',
                })
                state['pending'].clear()
                state['first_pending_at'] = None

            if quiet_due and not state.get('pending'):
                _ADAPTIVE_REFRESH_STATES.pop(key, None)

    return due_batches


def _submit_adaptive_refresh_batch(batch: dict):
    """Submit one due adaptive batch or restore it if not formally accepted."""
    future = _submit_monitor_task(
        _handle_batch_refresh_only_task,
        batch['processor'],
        batch['paths'],
        bulk_mode=True,
        on_cancel=lambda batch=batch: _restore_adaptive_refresh_batch(batch),
    )
    if future is None:
        _restore_adaptive_refresh_batch(batch)
    return future


def _adaptive_refresh_worker_loop():
    global _ADAPTIVE_REFRESH_WORKER
    while not _ADAPTIVE_REFRESH_STOP.wait(ADAPTIVE_BATCH_POLL_SECONDS):
        due_batches = _pop_due_adaptive_refresh_batches()
        for batch in due_batches:
            reason = '连续无新文件' if batch['reason'] == 'quiet' else '达到最长聚合时间'
            logger.info(
                f"  📦 [自适应入库] {reason}，统一处理作品 "
                f"'{os.path.basename(batch['key'])}' 的 {len(batch['paths'])} 个文件。"
            )
            _submit_adaptive_refresh_batch(batch)

        with _ADAPTIVE_REFRESH_LOCK:
            if not _ADAPTIVE_REFRESH_STATES:
                _ADAPTIVE_REFRESH_WORKER = None
                return
    with _ADAPTIVE_REFRESH_LOCK:
        if _ADAPTIVE_REFRESH_WORKER is threading.current_thread():
            _ADAPTIVE_REFRESH_WORKER = None


def _ensure_adaptive_refresh_worker():
    global _ADAPTIVE_REFRESH_WORKER
    if not _MONITOR_TASK_POOL.accepting or _ADAPTIVE_REFRESH_STOP.is_set():
        return
    with _ADAPTIVE_REFRESH_LOCK:
        if _ADAPTIVE_REFRESH_WORKER and _ADAPTIVE_REFRESH_WORKER.is_alive():
            return
        _ADAPTIVE_REFRESH_WORKER = threading.Thread(
            target=_adaptive_refresh_worker_loop,
            name='adaptive-strm-ingest',
            daemon=True,
        )
        _ADAPTIVE_REFRESH_WORKER.start()


def _enqueue_adaptive_refresh_only(
    processor,
    file_paths: List[str],
    exclude_paths: List[str] = None,
):
    immediate_paths, activated_keys = _register_adaptive_refresh_paths(
        processor,
        file_paths,
        exclude_paths=exclude_paths,
    )
    for key in activated_keys:
        logger.info(
            f"  📦 [自适应入库] 检测到作品 '{os.path.basename(key)}' 持续写入，"
            f"切换为大批量静默聚合；安静 {ADAPTIVE_BULK_QUIET_SECONDS} 秒或 "
            f"最多 {ADAPTIVE_BULK_MAX_HOLD_SECONDS // 60} 分钟后统一处理。"
        )

    if immediate_paths:
        future = _submit_monitor_task(
            _handle_batch_refresh_only_task,
            processor,
            immediate_paths,
            bulk_mode=False,
            on_cancel=lambda: _restore_file_event_paths(immediate_paths),
        )
        if future is None:
            _restore_file_event_paths(immediate_paths)

    _ensure_adaptive_refresh_worker()


class MediaFileHandler(FileSystemEventHandler):
    """
    文件系统事件处理器
    """
    def __init__(
        self,
        extensions: List[str],
        exclude_dirs: List[str] = None,
        inventory_roots: List[str] = None,
        inventory_audit_notifier: Optional[Callable[[], None]] = None,
    ):
        self.extensions = utils.normalize_monitor_extensions(extensions)
        self.inventory_roots = sorted(
            {os.path.normpath(path) for path in inventory_roots or []},
            key=len,
            reverse=True,
        )
        self.inventory_audit_notifier = inventory_audit_notifier

        # 记录一下最终生效的监控后缀，方便调试
        logger.trace(f"  [实时监控] 已加载监控后缀: {self.extensions}")

        # 注意：exclude_dirs 参数在这里不再用于过滤，过滤逻辑已移至 process_batch_queue
        # 这里保留参数是为了兼容调用签名

    def _is_valid_media_file(self, file_path: str) -> bool:
        # 1. 忽略文件夹
        if os.path.exists(file_path) and os.path.isdir(file_path): 
            return False
        
        # 2. 检查扩展名
        _, ext = os.path.splitext(file_path)
        # os.path.splitext 提取的后缀是带点的 (如 .mp4)，所以我们的 self.extensions 也必须带点
        if ext.lower() not in self.extensions: 
            # 调试日志：如果扩展名不匹配，记录一下（仅在调试模式下）
            # logger.trace(f"  [监控忽略] 扩展名不匹配: {os.path.basename(file_path)}")
            return False
        
        filename = os.path.basename(file_path)
        if filename.startswith('.'): return False
        if filename.endswith(('.part', '.!qB', '.crdownload', '.tmp', '.aria2')): return False

        # ★★★ 关键：此处不再进行任何排除目录的检查 ★★★
        # 只要是媒体文件，全部放行进入队列，由后续逻辑决定是“刮削”还是“仅刷新”
        return True

    def on_created(self, event):
        if event.is_directory:
            root = self._inventory_root(event.src_path)
            if root:
                strm_ingest_db.record_directory_created(root, event.src_path)
                self._notify_inventory_audit()
            return
        if self._is_valid_media_file(event.src_path):
            self._enqueue_file(event.src_path)

    def on_modified(self, event):
        if not event.is_directory and self._is_valid_media_file(event.src_path):
            self._enqueue_file(event.src_path)

    def on_moved(self, event):
        if event.is_directory:
            try:
                old_root = self._inventory_root(event.src_path)
                new_root = self._inventory_root(event.dest_path)
                if old_root and old_root == new_root:
                    pairs = strm_ingest_db.record_directory_moved(
                        old_root, event.src_path, event.dest_path,
                    )
                else:
                    old_paths = strm_ingest_db.record_directory_removed(
                        old_root, event.src_path,
                    ) if old_root else []
                    pairs = [
                        (old_path, os.path.normpath(event.dest_path + old_path[len(event.src_path):]))
                        for old_path in old_paths
                    ]
                    if new_root:
                        strm_ingest_db.record_directory_created(new_root, event.dest_path)
                for old_path, new_path in pairs:
                    self._enqueue_delete(old_path)
                    if self._is_valid_media_file(new_path):
                        self._enqueue_file(new_path)
                self._notify_inventory_audit()
            except Exception as exc:
                logger.warning(f"  ⚠️ 无法持久化目录移动库存，将由增量目录审计补偿: {exc}")
            return

        if self._is_valid_media_file(event.src_path):
            self._enqueue_delete(event.src_path)
        if self._is_valid_media_file(event.dest_path):
            self._enqueue_file(event.dest_path)

    def on_deleted(self, event):
        if event.is_directory:
            try:
                root = self._inventory_root(event.src_path)
                paths = strm_ingest_db.record_directory_removed(root, event.src_path) if root else []
                for file_path in paths:
                    self._enqueue_delete(file_path)
                if root:
                    self._notify_inventory_audit()
            except Exception as exc:
                logger.warning(f"  ⚠️ 无法持久化目录删除库存，将由增量目录审计补偿: {exc}")
            return
        
        _, ext = os.path.splitext(event.src_path)
        # 即使是删除事件，也要检查后缀是否在监控列表中，避免误报非媒体文件的删除
        if ext.lower() not in self.extensions:
            return

        self._enqueue_delete(event.src_path)

    def _inventory_root(self, file_path: str) -> Optional[str]:
        path = os.path.normpath(str(file_path))
        for root in self.inventory_roots:
            try:
                if os.path.commonpath([path, root]) == root:
                    return root
            except ValueError:
                continue
        return None

    def _notify_inventory_audit(self) -> None:
        """Wake bounded reconciliation only for an explicit directory event."""
        if self.inventory_audit_notifier:
            self.inventory_audit_notifier()

    def _persist_file_event(self, file_path: str, *, operation: str) -> None:
        if not str(file_path).lower().endswith('.strm'):
            return
        root = self._inventory_root(file_path)
        if not root:
            return
        strm_ingest_db.enqueue_paths(
            [file_path],
            operation=operation,
            source='watchdog_inventory',
            last_error='watchdog 事件等待精确入库确认',
            initial_delay_seconds=10 * 60,
        )
        strm_ingest_db.record_file_event(root, file_path, event_kind=operation)

    def _enqueue_file(self, file_path: str):
        """新增/移动文件入队"""
        global DEBOUNCE_TIMER
        try:
            self._persist_file_event(file_path, operation='ingest')
        except Exception as exc:
            logger.error("STRM create/modify 库存持久化失败: %s", type(exc).__name__)
        if not _MONITOR_TASK_POOL.accepting:
            return
        with QUEUE_LOCK:
            if file_path not in FILE_EVENT_QUEUE:
                logger.info(f"  🔍 [实时监控] 文件加入队列: {os.path.basename(file_path)}")
            
            FILE_EVENT_QUEUE.add(file_path)
            
            if DEBOUNCE_TIMER: DEBOUNCE_TIMER.kill()
            DEBOUNCE_TIMER = spawn_later(DEBOUNCE_DELAY, process_batch_queue)

    def _enqueue_delete(self, file_path: str):
        """删除文件入队"""
        global DELETE_DEBOUNCE_TIMER
        try:
            self._persist_file_event(file_path, operation='delete')
        except Exception as exc:
            logger.error("STRM delete 库存持久化失败: %s", type(exc).__name__)
        if not _MONITOR_TASK_POOL.accepting:
            return
        with DELETE_QUEUE_LOCK:
            if file_path not in DELETE_EVENT_QUEUE:
                logger.info(f"  🗑️ [实时监控] 删除事件入队: {os.path.basename(file_path)}")
            
            DELETE_EVENT_QUEUE.add(file_path)
            
            if DELETE_DEBOUNCE_TIMER: DELETE_DEBOUNCE_TIMER.kill()
            DELETE_DEBOUNCE_TIMER = spawn_later(DEBOUNCE_DELAY, process_delete_batch_queue)

def _is_path_excluded(file_path: str, exclude_paths: List[str]) -> bool:
    """
    检查文件路径是否命中排除规则（严谨的路径匹配）
    """
    if not exclude_paths:
        return False
        
    norm_file = os.path.normpath(file_path).lower()
    
    for exc in exclude_paths:
        norm_exc = os.path.normpath(exc).lower()
        
        # ★★★ 修复：确保是目录层级的匹配，防止 /foo 匹配到 /foobar ★★★
        # 1. 完全相等
        if norm_file == norm_exc:
            return True
        # 2. 是子目录 (以 排除路径 + 分隔符 开头)
        if norm_file.startswith(norm_exc + os.sep):
            return True
            
    return False

def process_batch_queue():
    """
    处理新增/修改队列 (分组优化 + 排除路径分流版)
    """
    if not config_manager.APP_CONFIG.get(constants.CONFIG_OPTION_MONITOR_ENABLED, False):
        with QUEUE_LOCK:
            FILE_EVENT_QUEUE.clear()
        return
    global DEBOUNCE_TIMER
    if not _MONITOR_TASK_POOL.accepting:
        with QUEUE_LOCK:
            DEBOUNCE_TIMER = None
        return
    with QUEUE_LOCK:
        files_to_process = list(FILE_EVENT_QUEUE)
        FILE_EVENT_QUEUE.clear()
        DEBOUNCE_TIMER = None
    
    if not files_to_process: return
    
    processor = MonitorService.processor_instance
    if not processor:
        _restore_file_event_paths(files_to_process)
        return

    exclude_paths = config_manager.APP_CONFIG.get(constants.CONFIG_OPTION_MONITOR_EXCLUDE_DIRS, [])

    # ★★★ 分流逻辑 ★★★
    files_to_scrape = []
    files_to_refresh_only = []

    for file_path in files_to_process:
        if _is_path_excluded(file_path, exclude_paths):
            files_to_refresh_only.append(file_path)
        else:
            files_to_scrape.append(file_path)

    # 1. 正常刮削流程
    if files_to_scrape:
        grouped_files = {}
        for file_path in files_to_scrape:
            parent_dir = os.path.dirname(file_path)
            if parent_dir not in grouped_files: 
                grouped_files[parent_dir] = []
            grouped_files[parent_dir].append(file_path)

        logger.info(f"  🚀 [实时监控] 准备刮削 {len(files_to_scrape)} 个文件，聚合为 {len(grouped_files)} 个任务组。")

        for parent_dir, files in grouped_files.items():
            rep_file = files[0]
            folder_name = os.path.basename(parent_dir)
            if len(files) > 1:
                logger.info(f"    ├─ [刮削] 目录 '{folder_name}' 含 {len(files)} 个文件，选取代表: {os.path.basename(rep_file)}")
            else:
                logger.info(f"    ├─ [刮削] 目录 '{folder_name}' 单文件: {os.path.basename(rep_file)}")

        future = _submit_monitor_task(
            _handle_batch_file_task,
            processor,
            files_to_scrape,
            on_cancel=lambda: _restore_file_event_paths(files_to_scrape),
        )
        if future is None:
            _restore_file_event_paths(files_to_scrape)

    # 2. 仅刷新流程：小批量立即处理，持续大批量按作品静默聚合。
    if files_to_refresh_only:
        logger.info(f"  🚀 [实时监控] 发现 {len(files_to_refresh_only)} 个文件命中排除路径，将跳过刮削并进入自适应 Emby 入库。")
        _enqueue_adaptive_refresh_only(
            processor,
            files_to_refresh_only,
            exclude_paths=exclude_paths,
        )

def process_delete_batch_queue():
    """
    处理删除队列 (批量版 + 排除路径分流版)
    """
    if not config_manager.APP_CONFIG.get(constants.CONFIG_OPTION_MONITOR_ENABLED, False):
        with DELETE_QUEUE_LOCK:
            DELETE_EVENT_QUEUE.clear()
        return
    
    global DELETE_DEBOUNCE_TIMER
    if not _MONITOR_TASK_POOL.accepting:
        with DELETE_QUEUE_LOCK:
            DELETE_DEBOUNCE_TIMER = None
        return
    with DELETE_QUEUE_LOCK:
        files = list(DELETE_EVENT_QUEUE)
        DELETE_EVENT_QUEUE.clear()
        DELETE_DEBOUNCE_TIMER = None
    
    if not files: return
    
    processor = MonitorService.processor_instance
    if not processor:
        _restore_delete_event_paths(files)
        return

    exclude_paths = config_manager.APP_CONFIG.get(constants.CONFIG_OPTION_MONITOR_EXCLUDE_DIRS, [])
    
    files_to_delete_logic = []
    files_to_refresh_only = []

    for file_path in files:
        if _is_path_excluded(file_path, exclude_paths):
            files_to_refresh_only.append(file_path)
        else:
            files_to_delete_logic.append(file_path)

    # 1. 正常逻辑：走处理器删除流程 (清理DB等)
    if files_to_delete_logic:
        logger.info(f"  🗑️ [实时监控] 聚合处理删除事件: {len(files_to_delete_logic)} 个常规文件")
        future = _submit_monitor_task(
            processor.process_file_deletion_batch,
            files_to_delete_logic,
            on_cancel=lambda: _restore_delete_event_paths(
                files_to_delete_logic
            ),
        )
        if future is None:
            _restore_delete_event_paths(files_to_delete_logic)

    # 2. 排除路径逻辑：仅刷新 Emby (移除条目)
    if files_to_refresh_only:
        logger.info(f"  🗑️ [实时监控] 聚合处理删除事件: {len(files_to_refresh_only)} 个排除路径文件 (仅刷新)")
        future = _submit_monitor_task(
            _handle_batch_delete_refresh_only,
            processor,
            files_to_refresh_only,
            on_cancel=lambda: _restore_delete_event_paths(
                files_to_refresh_only
            ),
        )
        if future is None:
            _restore_delete_event_paths(files_to_refresh_only)

def _handle_batch_file_task(processor, file_paths: List[str]):
    """批量处理新增文件任务 (刮削模式)"""
    valid_files, skipped_files = wait_for_paths_stable(file_paths)
    if skipped_files:
        logger.warning(f"  ⚠️ [实时监控] {len(skipped_files)} 个文件未在时限内稳定，交给自动查漏重试。")
    if not valid_files: return
    processor.process_file_actively_batch(valid_files)

def _handle_batch_refresh_only_task(
    processor,
    file_paths: List[str],
    bulk_mode: bool = False,
):
    """批量处理仅刷新任务；大批量只做一次延迟确认，后续交给有限重试。"""
    valid_files, skipped_files = wait_for_paths_stable(file_paths)
    if skipped_files:
        logger.warning(f"  ⚠️ [实时监控] {len(skipped_files)} 个文件未在时限内稳定，交给自动查漏重试。")
    if not valid_files:
        return

    config = config_manager.APP_CONFIG
    refresh_kwargs = {}
    if bulk_mode:
        refresh_kwargs['verify_delays'] = (ADAPTIVE_BULK_VERIFY_DELAY_SECONDS,)
        logger.info(
            f"  📦 [自适应入库] 大批量模式一次通知 {len(valid_files)} 个文件，"
            f"{ADAPTIVE_BULK_VERIFY_DELAY_SECONDS} 秒后统一确认；未完成项交给有限重试。"
        )

    result = refresh_and_verify_paths(
        valid_files,
        config.get(constants.CONFIG_OPTION_EMBY_SERVER_URL),
        config.get(constants.CONFIG_OPTION_EMBY_API_KEY),
        initial_delay_seconds=config.get(constants.CONFIG_OPTION_MONITOR_EXCLUDE_REFRESH_DELAY, 0),
        **refresh_kwargs,
    )
    pending = result.get('pending') or []
    if pending:
        queue_result = strm_ingest_db.enqueue_paths(
            pending,
            source='realtime_bulk' if bulk_mode else 'realtime',
            last_error=(
                'Emby 在大批量聚合通知后仍未确认入库'
                if bulk_mode
                else 'Emby 在首次精确通知后仍未确认入库'
            ),
        )
        logger.warning(
            f"  ⚠️ [实时监控] {len(pending)}/{result.get('requested', 0)} 个文件尚未被 Emby 收录，"
            f"已加入有限重试队列（新增 {queue_result.get('queued', 0)}）。"
        )
    else:
        logger.info(f"  ✅ [实时监控] 已确认 Emby 收录 {result.get('indexed', 0)} 个文件。")
    confirmed_paths = result.get('confirmed_paths') or []
    strm_ingest_db.mark_completed(confirmed_paths)
    processor.enqueue_confirmed_ingest_postprocessing(confirmed_paths)

def _handle_batch_delete_refresh_only(processor, file_paths: List[str]):
    """
    批量处理仅刷新任务 (删除)
    注意：删除不需要等待文件稳定，因为文件已经没了。
    """
    config = config_manager.APP_CONFIG
    if not config.get(constants.CONFIG_OPTION_MONITOR_ENABLED, False):
        return
    processor.cleanup_file_deletion_records(file_paths)
    base_url = config.get(constants.CONFIG_OPTION_EMBY_SERVER_URL)
    api_key = config.get(constants.CONFIG_OPTION_EMBY_API_KEY)
    delay_seconds = config.get(constants.CONFIG_OPTION_MONITOR_EXCLUDE_REFRESH_DELAY, 0)
    if not base_url or not api_key:
        logger.error("  ❌ [实时监控-删除] 无法执行刷新：Emby 配置缺失。")
        return
    if delay_seconds > 0:
        logger.info(f"  ⏳ [实时监控-删除] 等待 {delay_seconds} 秒后通知 Emby...")
        time.sleep(delay_seconds)
        if not config_manager.APP_CONFIG.get(constants.CONFIG_OPTION_MONITOR_ENABLED, False):
            logger.info("  🛑 [实时监控] 监控已关闭，取消挂起的删除通知。")
            return
    result = delete_and_verify_paths(file_paths, base_url, api_key)
    confirmed_paths = result.get('confirmed_paths') or []
    pending_paths = result.get('pending') or []
    strm_ingest_db.mark_deleted(confirmed_paths)
    if confirmed_paths:
        logger.info(f"  ✅ [实时监控-删除] 已确认 Emby 移除 {len(confirmed_paths)} 个精确路径。")
    if pending_paths:
        queue_result = strm_ingest_db.enqueue_paths(
            pending_paths,
            operation='delete',
            source='realtime_delete',
            last_error='Emby 在首次精确删除通知后仍保留该路径',
        )
        logger.warning(
            f"  ⚠️ [实时监控-删除] {len(pending_paths)} 个路径尚未从 Emby 消失，"
            f"已加入有限删除重试（新增 {queue_result.get('queued', 0)}）。"
        )

class MonitorService:
    processor_instance = None
    active_instance = None

    def __init__(self, config: dict, processor: 'MediaProcessor'):
        self.config = config
        self.processor = processor
        
        self.observer: Optional[Any] = None
        self.enabled = self.config.get(constants.CONFIG_OPTION_MONITOR_ENABLED, False)
        self.paths = self.config.get(constants.CONFIG_OPTION_MONITOR_PATHS, [])
        self.extensions = self.config.get(constants.CONFIG_OPTION_MONITOR_EXTENSIONS, constants.DEFAULT_MONITOR_EXTENSIONS)
        self.exclude_dirs = self.config.get(constants.CONFIG_OPTION_MONITOR_EXCLUDE_DIRS, constants.DEFAULT_MONITOR_EXCLUDE_DIRS)
        self._reconcile_stop = threading.Event()
        self._inventory_requested = threading.Event()
        self._reconcile_thread = None
        self._retry_thread = None
        self._started = False
        self._pool_generation = None
        self._next_terminal_recheck_at = (
            time.monotonic() + TERMINAL_RECHECK_INITIAL_DELAY_SECONDS
        )
        self._inventory_owner = f"inventory-{os.getpid()}-{uuid.uuid4().hex}"
        self._inventory = IncrementalStrmInventory(
            owner=self._inventory_owner,
            # This timestamp remains useful for failure backoff and diagnostics,
            # but no timer consumes it to start a periodic audit.
            audit_interval_hours=24,
        )

    def start(self):
        global _ADAPTIVE_REFRESH_STOP
        if self._started:
            logger.warning("  ➜ 实时监控服务已经启动，忽略重复 start。")
            return
        if not self.enabled:
            logger.info("  ➜ 实时监控功能未启用。")
            return

        if not self.paths:
            logger.warning("  ➜ 实时监控已启用，但未配置监控目录列表。")
            return
        if (
            _MONITOR_TASK_POOL.accepting
            and MonitorService.processor_instance is not self.processor
        ):
            logger.error("  ➜ 另一实时监控实例仍在运行，拒绝启动新的任务池世代。")
            return

        event_handler = MediaFileHandler(
            self.extensions,
            self.exclude_dirs,
            inventory_roots=self.exclude_dirs,
            inventory_audit_notifier=self.request_inventory_audit_processing,
        )

        started_paths = []
        for path in self.paths:
            normalized = os.path.normpath(str(path))
            try:
                metadata = os.lstat(normalized)
            except OSError as exc:
                # Keep the configured root in the persisted audit lifecycle.
                # A missing/offline mount gets no watch, but must not disable
                # retry/backoff or be interpreted as an empty directory.
                started_paths.append(normalized)
                logger.warning(
                    "  ➜ 监控根当前不可访问，将保持持久库存并等待恢复: %s (%s)",
                    normalized,
                    type(exc).__name__,
                )
                continue
            if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
                logger.warning(f"  ➜ 监控目录不是安全的物理目录，已跳过: {normalized}")
                continue
            started_paths.append(normalized)

        if started_paths:
            if self.exclude_dirs:
                try:
                    strm_ingest_db.register_inventory_roots(
                        self.exclude_dirs,
                        audit_interval_hours=24,
                    )
                    recovered = strm_ingest_db.recover_processing()
                    if recovered:
                        logger.info(f"  🔁 已恢复 {recovered} 个中断的 STRM 入库重试任务。")
                    strm_ingest_db.prune_completed(retention_days=30)
                except Exception:
                    logger.exception("  ➜ 无法读取 STRM 持久目录状态，实时监控保持停止。")
                    return

            self.observer = PersistedDirectoryObserver(
                event_handler,
                watch_roots=started_paths,
                persisted_directory_provider=lambda: (
                    strm_ingest_db.list_active_inventory_directories(self.exclude_dirs)
                    if self.exclude_dirs else []
                ),
                anchor_path=os.environ.get('APP_DATA_DIR') or os.getcwd(),
            )
            self._reconcile_stop = threading.Event()
            self._inventory_requested = threading.Event()
            self._reconcile_thread = None
            self._retry_thread = None
            _ADAPTIVE_REFRESH_STOP = threading.Event()
            self._pool_generation = _MONITOR_TASK_POOL.start()
            MonitorService.processor_instance = self.processor
            try:
                self.observer.start()
            except Exception:
                try:
                    self.observer.stop()
                    self.observer.join(timeout=5)
                except Exception:
                    logger.exception("  ➜ STRM non-recursive watcher 启动回滚失败。")
                _MONITOR_TASK_POOL.shutdown()
                self._pool_generation = None
                if MonitorService.processor_instance is self.processor:
                    MonitorService.processor_instance = None
                logger.exception("  ➜ 实时监控 Observer 启动失败，任务池已安全关闭。")
                return
            self._started = True
            MonitorService.active_instance = self
            logger.info(
                "  👀 实时监控服务已启动：%s 个配置根，%s 个显式 non-recursive watch，"
                "%s 个 inotify backend 线程，max_user_watches=%s。",
                len(started_paths),
                self.observer.watch_count,
                self.observer.backend_thread_count,
                self.observer.max_user_watches or 'unknown',
            )
            with QUEUE_LOCK:
                pending_files = list(FILE_EVENT_QUEUE)
            with DELETE_QUEUE_LOCK:
                pending_deletes = list(DELETE_EVENT_QUEUE)
            _restore_file_event_paths(pending_files)
            _restore_delete_event_paths(pending_deletes)
            if self.exclude_dirs:
                self._retry_thread = threading.Thread(
                    target=self._run_retry_loop,
                    name="strm-ingest-retry",
                    daemon=True,
                )
                self._retry_thread.start()
                logger.info("  🔁 STRM 有限重试队列已启动，失败路径将在约 10、30、60 分钟重试。")

            if self.exclude_dirs:
                self._reconcile_thread = threading.Thread(
                    target=self._run_requested_inventory_loop,
                    name="strm-inventory-explicit",
                    daemon=True,
                )
                self._reconcile_thread.start()
                logger.info(
                    "  🧭 STRM Inventory v2 已就绪：仅响应目录事件或人工‘STRM 查漏’，"
                    "不会执行启动或周期性自动审计。"
                )
        else:
            logger.warning("  ➜ 没有有效的监控目录，实时监控服务未启动。")

    def request_inventory_audit_processing(self) -> bool:
        """Wake the bounded worker without adding or widening inventory work."""
        if not self._started or self._reconcile_stop.is_set():
            return False
        self._inventory_requested.set()
        return True

    def _run_requested_inventory_loop(self):
        def process_ingest(candidate_paths):
            result = reconcile_paths(
                candidate_paths,
                self.config.get(constants.CONFIG_OPTION_EMBY_SERVER_URL),
                self.config.get(constants.CONFIG_OPTION_EMBY_API_KEY),
            )
            confirmed_paths = result.get('confirmed_paths') or []
            strm_ingest_db.mark_completed(confirmed_paths)
            self.processor.enqueue_confirmed_ingest_postprocessing(confirmed_paths)

        def process_delete(removed_paths):
            _handle_batch_delete_refresh_only(self.processor, list(removed_paths))

        while not self._reconcile_stop.is_set():
            self._inventory_requested.wait()
            self._inventory_requested.clear()
            if self._reconcile_stop.is_set():
                return

            # Directory-event reconciliation is isolated from manual audit
            # generations. Exact file events never enter either claim set.
            while not self._reconcile_stop.is_set():
                summary = {'claimed': 0}
                try:
                    summary = self._inventory.run_once(
                        on_ingest=process_ingest,
                        on_delete=process_delete,
                    )
                    if summary['claimed']:
                        watch_summary = self.observer.sync_from_persistence() if self.observer else {}
                        logger.info(
                            "  🧭 STRM 显式目录核对：认领 %(claimed)s，完成 %(completed)s，"
                            "新增/变化 %(ingest)s，删除 %(delete)s，失败 %(failed)s，"
                            "物理枚举 %(physical_enumerations)s，条目 %(entries_seen)s，"
                            "DB 批次 %(db_batches)s，watch %(watch_count)s。",
                            {**summary, 'watch_count': watch_summary.get('watched', 0)},
                        )
                except Exception as exc:
                    logger.error(f"  ❌ STRM 显式目录核对失败，保留持久状态: {exc}", exc_info=True)
                    break
                if not summary.get('claimed'):
                    break
                # The web app runs on gevent. A long sequence of short,
                # synchronous PostgreSQL/scandir claims must explicitly yield
                # so task status and stop requests remain observable.
                time.sleep(0.001)

            # A manual generation remains active until all of its persisted
            # directory rows complete or the task centre cancels it. Claim one
            # directory at a time so a stop request cannot widen in-flight work.
            for manual_audit_id in strm_ingest_db.list_active_manual_inventory_audits():
                while not self._reconcile_stop.is_set():
                    status = strm_ingest_db.get_manual_inventory_audit(manual_audit_id)
                    if not status or status.get('state') not in {'queued', 'running'}:
                        break
                    if self.processor.is_stop_requested():
                        logger.info(
                            "  🛑 STRM 查漏 generation=%s 已响应停止请求；"
                            "未认领目录保留在 PostgreSQL。",
                            manual_audit_id,
                        )
                        break
                    summary = {'claimed': 0}
                    try:
                        summary = self._inventory.run_once(
                            on_ingest=process_ingest,
                            on_delete=process_delete,
                            manual_audit_id=manual_audit_id,
                            claim_limit=1,
                        )
                        if summary['claimed']:
                            watch_summary = self.observer.sync_from_persistence() if self.observer else {}
                            logger.info(
                                f"  🧭 STRM 人工查漏 generation={manual_audit_id}：认领 %(claimed)s，"
                                "完成 %(completed)s，新增/变化 %(ingest)s，删除 %(delete)s，"
                                "失败 %(failed)s，物理枚举 %(physical_enumerations)s，"
                                "条目 %(entries_seen)s，DB 批次 %(db_batches)s，watch %(watch_count)s。",
                                {**summary, 'watch_count': watch_summary.get('watched', 0)},
                            )
                    except Exception as exc:
                        logger.error(
                            "  ❌ STRM 人工查漏 generation=%s 执行失败，持久状态保留: %s",
                            manual_audit_id,
                            type(exc).__name__,
                            exc_info=True,
                        )
                    if summary.get('claimed'):
                        time.sleep(0.001)
                    status = strm_ingest_db.get_manual_inventory_audit(manual_audit_id)
                    if not status or status.get('state') not in {'queued', 'running'}:
                        break
                    if not summary.get('claimed') and self._reconcile_stop.wait(0.5):
                        return


    def _retry_existing_ingest_paths(self, existing_paths: List[str]) -> None:
        """Check first; only notify Emby for paths that are still unresolved."""
        if not existing_paths:
            return
        base_url = self.config.get(constants.CONFIG_OPTION_EMBY_SERVER_URL)
        api_key = self.config.get(constants.CONFIG_OPTION_EMBY_API_KEY)
        indexed, missing, query_failed = check_indexed_paths(
            existing_paths,
            base_url,
            api_key,
        )

        preconfirmed = sorted(indexed)
        if preconfirmed:
            strm_ingest_db.mark_completed(preconfirmed)
            self.processor.enqueue_confirmed_ingest_postprocessing(preconfirmed)
            logger.info(
                f"  ✅ STRM 重试前核对：{len(preconfirmed)} 个路径已在 Emby 中，"
                "直接完成，不再发送刷新请求。"
            )

        refresh_paths = sorted(set(missing) | set(query_failed))
        if not refresh_paths:
            return
        result = refresh_and_verify_paths(
            refresh_paths,
            base_url,
            api_key,
        )
        confirmed_paths = result.get('confirmed_paths') or []
        unresolved_paths = sorted(
            set(result.get('pending') or [])
            | set(result.get('query_failed') or [])
        )
        if confirmed_paths:
            strm_ingest_db.mark_completed(confirmed_paths)
            self.processor.enqueue_confirmed_ingest_postprocessing(confirmed_paths)
        retry_result = strm_ingest_db.mark_failed_attempts(
            unresolved_paths,
            'Emby 在有限重试后仍未确认入库',
        )
        if retry_result.get('failed'):
            logger.warning(
                f"  🚨 {retry_result['failed']} 个 STRM 达到重试上限，"
                "已停止自动刷新，请在 STRM 入库诊断中人工处理。"
            )

    def _recheck_terminal_ingest_paths(
        self,
        limit: int = TERMINAL_RECHECK_BATCH_SIZE,
    ) -> int:
        """Read-only recheck terminal failures and complete paths now in Emby."""
        paths = strm_ingest_db.list_failed_ingest_paths(limit=limit)
        existing_paths = [path for path in paths if os.path.isfile(path)]
        if not existing_paths:
            return 0
        indexed, _, query_failed = check_indexed_paths(
            existing_paths,
            self.config.get(constants.CONFIG_OPTION_EMBY_SERVER_URL),
            self.config.get(constants.CONFIG_OPTION_EMBY_API_KEY),
        )
        confirmed_paths = sorted(indexed)
        if confirmed_paths:
            strm_ingest_db.mark_completed(confirmed_paths)
            self.processor.enqueue_confirmed_ingest_postprocessing(confirmed_paths)
        logger.info(
            f"  🩺 STRM 终态自愈核对：检查 {len(existing_paths)}，"
            f"已自动完成 {len(confirmed_paths)}，查询异常 {len(query_failed)}；"
            "未收录路径保持人工处理状态，不重新发送刷新。"
        )
        return len(confirmed_paths)

    def _run_retry_loop(self):
        while not self._reconcile_stop.is_set():
            if time.monotonic() >= self._next_terminal_recheck_at:
                try:
                    self._recheck_terminal_ingest_paths()
                except Exception as exc:
                    logger.error(
                        f"  ❌ STRM 终态自愈核对失败，将在下一周期重试: {exc}",
                        exc_info=True,
                    )
                finally:
                    self._next_terminal_recheck_at = (
                        time.monotonic() + TERMINAL_RECHECK_INTERVAL_SECONDS
                    )

            events = []
            try:
                events = strm_ingest_db.claim_due_paths(limit=20)
                if not events:
                    if self._reconcile_stop.wait(60):
                        return
                    continue

                ingest_events = [event for event in events if event.get('operation') != 'delete']
                delete_events = [event for event in events if event.get('operation') == 'delete']
                ingest_paths = [event['file_path'] for event in ingest_events]
                existing_paths, missing_paths, inaccessible_ingest_paths = _classify_retry_paths(
                    ingest_paths
                )
                if inaccessible_ingest_paths:
                    strm_ingest_db.defer_claimed_paths(
                        inaccessible_ingest_paths,
                        'STRM 父目录当前不可访问，已保留原操作并延后重试',
                    )
                if missing_paths:
                    strm_ingest_db.enqueue_paths(
                        missing_paths,
                        operation='delete',
                        source='ingest_disappeared',
                        last_error='等待入库期间 STRM 已被删除',
                        initial_delay_seconds=0,
                    )

                if existing_paths:
                    self._retry_existing_ingest_paths(existing_paths)

                if delete_events:
                    delete_paths = [event['file_path'] for event in delete_events]
                    reappeared_paths, missing_delete_paths, inaccessible_delete_paths = _classify_retry_paths(
                        delete_paths
                    )
                    if inaccessible_delete_paths:
                        strm_ingest_db.defer_claimed_paths(
                            inaccessible_delete_paths,
                            'STRM 父目录当前不可访问，删除确认已延后',
                        )
                    if reappeared_paths:
                        strm_ingest_db.enqueue_paths(
                            reappeared_paths,
                            operation='ingest',
                            source='delete_reappeared',
                            last_error='等待删除期间 STRM 重新出现',
                            initial_delay_seconds=0,
                        )
                    if missing_delete_paths:
                        result = delete_and_verify_paths(
                            missing_delete_paths,
                            self.config.get(constants.CONFIG_OPTION_EMBY_SERVER_URL),
                            self.config.get(constants.CONFIG_OPTION_EMBY_API_KEY),
                        )
                        confirmed_paths = result.get('confirmed_paths') or []
                        unresolved_paths = result.get('pending') or []
                        strm_ingest_db.mark_deleted(confirmed_paths)
                        retry_result = strm_ingest_db.mark_failed_attempts(
                            unresolved_paths,
                            'Emby 在有限删除重试后仍保留该 STRM 路径',
                        )
                        if retry_result.get('failed'):
                            logger.warning(
                                f"  🚨 {retry_result['failed']} 个 STRM 删除达到重试上限，"
                                "已停止自动刷新，请在 STRM 入库诊断中人工处理。"
                            )
            except Exception as exc:
                if events:
                    strm_ingest_db.mark_failed_attempts(
                        [event['file_path'] for event in events],
                        f"STRM 重试任务异常: {exc}",
                    )
                logger.error(f"  ❌ STRM 有限重试任务失败: {exc}", exc_info=True)

            if self._reconcile_stop.wait(30):
                return

    def stop(self):
        global DEBOUNCE_TIMER, DELETE_DEBOUNCE_TIMER
        if not self._started:
            return
        self._reconcile_stop.set()
        self._inventory_requested.set()
        if self.observer:
            logger.info("  ➜ 正在停止实时监控服务...")
            self.observer.stop()
            self.observer.join()
        owns_pool = (
            self._pool_generation is not None
            and self._pool_generation == _MONITOR_TASK_POOL.generation
        )
        if owns_pool:
            _ADAPTIVE_REFRESH_STOP.set()
            _MONITOR_TASK_POOL.stop_accepting()
            file_timer = None
            with QUEUE_LOCK:
                if DEBOUNCE_TIMER:
                    file_timer = DEBOUNCE_TIMER
                    DEBOUNCE_TIMER = None
            if file_timer:
                file_timer.kill()
            delete_timer = None
            with DELETE_QUEUE_LOCK:
                if DELETE_DEBOUNCE_TIMER:
                    delete_timer = DELETE_DEBOUNCE_TIMER
                    DELETE_DEBOUNCE_TIMER = None
            if delete_timer:
                delete_timer.kill()
        if owns_pool:
            with _ADAPTIVE_REFRESH_LOCK:
                adaptive_worker = _ADAPTIVE_REFRESH_WORKER
            if (
                adaptive_worker
                and adaptive_worker.is_alive()
                and adaptive_worker is not threading.current_thread()
            ):
                adaptive_worker.join()
            _MONITOR_TASK_POOL.shutdown()
            _preserve_adaptive_paths_for_restart()
        if self._reconcile_thread and self._reconcile_thread.is_alive():
            self._reconcile_thread.join(timeout=5)
        if self._retry_thread and self._retry_thread.is_alive():
            self._retry_thread.join(timeout=5)
        if MonitorService.processor_instance is self.processor:
            MonitorService.processor_instance = None
        if MonitorService.active_instance is self:
            MonitorService.active_instance = None
        self._pool_generation = None
        self._started = False
        logger.info("  ➜ 实时监控服务已停止。")


def inventory_audit_processing_available() -> bool:
    instance = MonitorService.active_instance
    return bool(instance and instance._started and not instance._reconcile_stop.is_set())


def request_inventory_audit_processing() -> bool:
    instance = MonitorService.active_instance
    return bool(instance and instance.request_inventory_audit_processing())
