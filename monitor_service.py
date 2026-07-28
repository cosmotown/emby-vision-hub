# monitor_service.py

import os
import re
import time
import logging
import threading
from concurrent.futures import ThreadPoolExecutor
from typing import List, Optional, Any
from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler
from gevent import spawn_later

import constants
import config_manager
import handler.emby as emby
import utils
from database import strm_ingest_db
from services.emby_ingest import (
    check_indexed_paths,
    collect_strm_inventory,
    delete_and_verify_paths,
    reconcile_paths,
    refresh_and_verify_paths,
    wait_for_paths_stable,
)

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
_MONITOR_TASK_EXECUTOR = ThreadPoolExecutor(
    max_workers=4,
    thread_name_prefix='evh-monitor-task',
)
_MONITOR_TASK_SLOTS = threading.BoundedSemaphore(64)
_SEASON_DIRECTORY_RE = re.compile(
    r"^(?:season[\s._-]*\d+|specials?|第\s*\d+\s*季)$",
    re.IGNORECASE,
)


def _log_monitor_task_result(future):
    """Surface background task failures without creating unbounded threads."""
    try:
        future.result()
    except Exception:
        logger.exception("实时监控后台任务执行失败")
    finally:
        _MONITOR_TASK_SLOTS.release()


def _submit_monitor_task(target, *args, **kwargs):
    """Run monitor work on a bounded worker pool with bounded backlog."""
    if not _MONITOR_TASK_SLOTS.acquire(blocking=False):
        logger.warning("实时监控后台队列已满，等待空闲槽位以避免任务丢失。")
        _MONITOR_TASK_SLOTS.acquire()
    try:
        future = _MONITOR_TASK_EXECUTOR.submit(target, *args, **kwargs)
    except Exception:
        _MONITOR_TASK_SLOTS.release()
        raise
    future.add_done_callback(_log_monitor_task_result)
    return future


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
    global _ADAPTIVE_REFRESH_WORKER
    with _ADAPTIVE_REFRESH_LOCK:
        _ADAPTIVE_REFRESH_STATES.clear()
        _ADAPTIVE_REFRESH_WORKER = None


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


def _adaptive_refresh_worker_loop():
    global _ADAPTIVE_REFRESH_WORKER
    while True:
        time.sleep(ADAPTIVE_BATCH_POLL_SECONDS)
        due_batches = _pop_due_adaptive_refresh_batches()
        for batch in due_batches:
            reason = '连续无新文件' if batch['reason'] == 'quiet' else '达到最长聚合时间'
            logger.info(
                f"  📦 [自适应入库] {reason}，统一处理作品 "
                f"'{os.path.basename(batch['key'])}' 的 {len(batch['paths'])} 个文件。"
            )
            _submit_monitor_task(
                _handle_batch_refresh_only_task,
                batch['processor'],
                batch['paths'],
                bulk_mode=True,
            )

        with _ADAPTIVE_REFRESH_LOCK:
            if not _ADAPTIVE_REFRESH_STATES:
                _ADAPTIVE_REFRESH_WORKER = None
                return


def _ensure_adaptive_refresh_worker():
    global _ADAPTIVE_REFRESH_WORKER
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
        _submit_monitor_task(
            _handle_batch_refresh_only_task,
            processor,
            immediate_paths,
            bulk_mode=False,
        )

    _ensure_adaptive_refresh_worker()


class MediaFileHandler(FileSystemEventHandler):
    """
    文件系统事件处理器
    """
    def __init__(self, extensions: List[str], exclude_dirs: List[str] = None):
        self.extensions = utils.normalize_monitor_extensions(extensions)

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
        if not event.is_directory and self._is_valid_media_file(event.src_path):
            self._enqueue_file(event.src_path)

    def on_modified(self, event):
        if not event.is_directory and self._is_valid_media_file(event.src_path):
            self._enqueue_file(event.src_path)

    def on_moved(self, event):
        if event.is_directory:
            try:
                for old_path in strm_ingest_db.list_active_paths_under(event.src_path):
                    self._enqueue_delete(old_path)
                for dirpath, _, filenames in os.walk(event.dest_path):
                    for filename in filenames:
                        new_path = os.path.join(dirpath, filename)
                        if self._is_valid_media_file(new_path):
                            self._enqueue_file(new_path)
            except Exception as exc:
                logger.warning(f"  ⚠️ 无法立即展开目录移动事件，将由全目录查漏补偿: {exc}")
            return

        if self._is_valid_media_file(event.src_path):
            self._enqueue_delete(event.src_path)
        if self._is_valid_media_file(event.dest_path):
            self._enqueue_file(event.dest_path)

    def on_deleted(self, event):
        if event.is_directory:
            try:
                for file_path in strm_ingest_db.list_active_paths_under(event.src_path):
                    self._enqueue_delete(file_path)
            except Exception as exc:
                logger.warning(f"  ⚠️ 无法立即展开目录删除事件，将由全目录查漏补偿: {exc}")
            return
        
        _, ext = os.path.splitext(event.src_path)
        # 即使是删除事件，也要检查后缀是否在监控列表中，避免误报非媒体文件的删除
        if ext.lower() not in self.extensions:
            return

        self._enqueue_delete(event.src_path)

    def _enqueue_file(self, file_path: str):
        """新增/移动文件入队"""
        global DEBOUNCE_TIMER
        with QUEUE_LOCK:
            if file_path not in FILE_EVENT_QUEUE:
                logger.info(f"  🔍 [实时监控] 文件加入队列: {os.path.basename(file_path)}")
            
            FILE_EVENT_QUEUE.add(file_path)
            
            if DEBOUNCE_TIMER: DEBOUNCE_TIMER.kill()
            DEBOUNCE_TIMER = spawn_later(DEBOUNCE_DELAY, process_batch_queue)

    def _enqueue_delete(self, file_path: str):
        """删除文件入队"""
        global DELETE_DEBOUNCE_TIMER
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
    with QUEUE_LOCK:
        files_to_process = list(FILE_EVENT_QUEUE)
        FILE_EVENT_QUEUE.clear()
        DEBOUNCE_TIMER = None
    
    if not files_to_process: return
    
    processor = MonitorService.processor_instance
    if not processor: return

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

        _submit_monitor_task(_handle_batch_file_task, processor, files_to_scrape)

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
    with DELETE_QUEUE_LOCK:
        files = list(DELETE_EVENT_QUEUE)
        DELETE_EVENT_QUEUE.clear()
        DELETE_DEBOUNCE_TIMER = None
    
    if not files: return
    
    processor = MonitorService.processor_instance
    if not processor: return

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
        _submit_monitor_task(
            processor.process_file_deletion_batch,
            files_to_delete_logic,
        )

    # 2. 排除路径逻辑：仅刷新 Emby (移除条目)
    if files_to_refresh_only:
        logger.info(f"  🗑️ [实时监控] 聚合处理删除事件: {len(files_to_refresh_only)} 个排除路径文件 (仅刷新)")
        _submit_monitor_task(
            _handle_batch_delete_refresh_only,
            processor,
            files_to_refresh_only,
        )

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

    def __init__(self, config: dict, processor: 'MediaProcessor'):
        self.config = config
        self.processor = processor
        MonitorService.processor_instance = processor 
        
        self.observer: Optional[Any] = None
        self.enabled = self.config.get(constants.CONFIG_OPTION_MONITOR_ENABLED, False)
        self.paths = self.config.get(constants.CONFIG_OPTION_MONITOR_PATHS, [])
        self.extensions = self.config.get(constants.CONFIG_OPTION_MONITOR_EXTENSIONS, constants.DEFAULT_MONITOR_EXTENSIONS)
        self.exclude_dirs = self.config.get(constants.CONFIG_OPTION_MONITOR_EXCLUDE_DIRS, constants.DEFAULT_MONITOR_EXCLUDE_DIRS)
        self.full_scan_interval_hours = max(
            0,
            int(self.config.get(
                constants.CONFIG_OPTION_MONITOR_FULL_SCAN_INTERVAL_HOURS,
                constants.DEFAULT_MONITOR_FULL_SCAN_INTERVAL_HOURS,
            ) or 0),
        )
        self._reconcile_stop = threading.Event()
        self._reconcile_thread = None
        self._retry_thread = None
        self._next_terminal_recheck_at = (
            time.monotonic() + TERMINAL_RECHECK_INITIAL_DELAY_SECONDS
        )
        lookback_days = max(0, int(self.config.get(
            constants.CONFIG_OPTION_MONITOR_SCAN_LOOKBACK_DAYS,
            constants.DEFAULT_MONITOR_SCAN_LOOKBACK_DAYS,
        ) or 0))
        initial_lookback_seconds = min(max(lookback_days * 86400, 3600), 7 * 86400)
        self._reconcile_since = time.time() - initial_lookback_seconds

    def start(self):
        if not self.enabled:
            logger.info("  ➜ 实时监控功能未启用。")
            return

        if not self.paths:
            logger.warning("  ➜ 实时监控已启用，但未配置监控目录列表。")
            return

        self.observer = Observer()
        event_handler = MediaFileHandler(self.extensions, self.exclude_dirs)

        started_paths = []
        for path in self.paths:
            if os.path.exists(path) and os.path.isdir(path):
                try:
                    self.observer.schedule(event_handler, path, recursive=True)
                    started_paths.append(path)
                except Exception as e:
                    logger.error(f"  ➜ 无法监控目录 '{path}': {e}")
            else:
                logger.warning(f"  ➜ 监控目录不存在或无效，已跳过: {path}")

        if started_paths:
            self.observer.start()
            logger.info(f"  👀 实时监控服务已启动，正在监听 {len(started_paths)} 个目录: {started_paths}")
            if self.exclude_dirs:
                recovered = strm_ingest_db.recover_processing()
                if recovered:
                    logger.info(f"  🔁 已恢复 {recovered} 个中断的 STRM 入库重试任务。")
                strm_ingest_db.prune_completed(retention_days=30)
                self._retry_thread = threading.Thread(
                    target=self._run_retry_loop,
                    name="strm-ingest-retry",
                    daemon=True,
                )
                self._retry_thread.start()
                logger.info("  🔁 STRM 有限重试队列已启动，失败路径将在约 10、30、60 分钟重试。")

            if self.full_scan_interval_hours > 0 and self.exclude_dirs:
                self._reconcile_thread = threading.Thread(
                    target=self._run_reconcile_loop,
                    name="strm-ingest-full-scan",
                    daemon=True,
                )
                self._reconcile_thread.start()
                logger.info(
                    f"  🧭 STRM 全目录查漏已启动，每 {self.full_scan_interval_hours} 小时检查一次。"
                )
        else:
            logger.warning("  ➜ 没有有效的监控目录，实时监控服务未启动。")

    def _run_reconcile_loop(self):
        if self._reconcile_stop.wait(60):
            return
        while not self._reconcile_stop.is_set():
            scan_started = time.time()
            try:
                total_paths = 0
                total_added = 0
                total_changed = 0
                total_removed = 0
                total_queued = 0
                total_seeded = 0
                for root_path in self.exclude_dirs:
                    inventory = collect_strm_inventory(root_path)
                    total_paths += len(inventory)
                    diff = strm_ingest_db.reconcile_inventory(root_path, inventory)
                    total_seeded += int(diff.get('seeded') or 0)
                    if diff.get('initialized'):
                        candidate_paths = sorted(
                            set(diff.get('added') or []) | set(diff.get('changed') or [])
                        )
                    else:
                        candidate_paths = sorted(
                            path for path, fingerprint in inventory.items()
                            if fingerprint[1] >= self._reconcile_since
                        )

                    removed_paths = diff.get('removed') or []
                    if removed_paths:
                        _handle_batch_delete_refresh_only(self.processor, removed_paths)
                    total_removed += len(removed_paths)
                    total_added += len(diff.get('added') or [])
                    total_changed += len(diff.get('changed') or [])

                    result = reconcile_paths(
                        candidate_paths,
                        self.config.get(constants.CONFIG_OPTION_EMBY_SERVER_URL),
                        self.config.get(constants.CONFIG_OPTION_EMBY_API_KEY),
                    )
                    confirmed_paths = result.get('confirmed_paths') or []
                    strm_ingest_db.mark_completed(confirmed_paths)
                    self.processor.enqueue_confirmed_ingest_postprocessing(confirmed_paths)
                    queue_result = strm_ingest_db.enqueue_paths(
                        result.get('unresolved_paths') or [],
                        source='full_scan',
                        last_error='全目录查漏发现 Emby 尚未收录该 STRM',
                    )
                    total_queued += int(queue_result.get('queued') or 0)

                self._reconcile_since = scan_started - 60
                logger.info(
                    f"  🧭 STRM 全目录查漏完成：库存 {total_paths}，基线 {total_seeded}，"
                    f"新增 {total_added}，变化 {total_changed}，删除 {total_removed}，"
                    f"新增有限重试 {total_queued}。"
                )
            except Exception as exc:
                logger.error(f"  ❌ STRM 自动查漏失败，将在下一轮重试: {exc}", exc_info=True)
            if self._reconcile_stop.wait(self.full_scan_interval_hours * 3600):
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
                existing_paths = [path for path in ingest_paths if os.path.isfile(path)]
                missing_paths = sorted(set(ingest_paths) - set(existing_paths))
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
                    reappeared_paths = [path for path in delete_paths if os.path.isfile(path)]
                    missing_delete_paths = sorted(set(delete_paths) - set(reappeared_paths))
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
        self._reconcile_stop.set()
        if self.observer:
            logger.info("  ➜ 正在停止实时监控服务...")
            self.observer.stop()
            self.observer.join()
            logger.info("  ➜ 实时监控服务已停止。")
        if self._reconcile_thread and self._reconcile_thread.is_alive():
            self._reconcile_thread.join(timeout=5)
        if self._retry_thread and self._retry_thread.is_alive():
            self._retry_thread.join(timeout=5)
