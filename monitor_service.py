# monitor_service.py

import os
import re
import time
import logging
import threading
from typing import List, Optional, Any
from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler
from gevent import spawn_later

import constants
import config_manager
import handler.emby as emby
import utils
from services.emby_ingest import (
    reconcile_recent_paths,
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

    def on_moved(self, event):
        if not event.is_directory and self._is_valid_media_file(event.dest_path):
            self._enqueue_file(event.dest_path)

    def on_deleted(self, event):
        if event.is_directory:
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

        threading.Thread(target=_handle_batch_file_task, args=(processor, files_to_scrape)).start()

    # 2. 仅刷新流程
    if files_to_refresh_only:
        logger.info(f"  🚀 [实时监控] 发现 {len(files_to_refresh_only)} 个文件命中排除路径，将跳过刮削直接刷新 Emby。")
        threading.Thread(target=_handle_batch_refresh_only_task, args=(processor, files_to_refresh_only)).start()

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
        threading.Thread(target=processor.process_file_deletion_batch, args=(files_to_delete_logic,)).start()

    # 2. 排除路径逻辑：仅刷新 Emby (移除条目)
    if files_to_refresh_only:
        logger.info(f"  🗑️ [实时监控] 聚合处理删除事件: {len(files_to_refresh_only)} 个排除路径文件 (仅刷新)")
        threading.Thread(target=_handle_batch_delete_refresh_only, args=(processor, files_to_refresh_only)).start()

def _handle_batch_file_task(processor, file_paths: List[str]):
    """批量处理新增文件任务 (刮削模式)"""
    valid_files, skipped_files = wait_for_paths_stable(file_paths)
    if skipped_files:
        logger.warning(f"  ⚠️ [实时监控] {len(skipped_files)} 个文件未在时限内稳定，交给自动查漏重试。")
    if not valid_files: return
    processor.process_file_actively_batch(valid_files)

def _handle_batch_refresh_only_task(processor, file_paths: List[str]):
    """批量处理仅刷新任务 (新增/修改)"""
    valid_files, skipped_files = wait_for_paths_stable(file_paths)
    if skipped_files:
        logger.warning(f"  ⚠️ [实时监控] {len(skipped_files)} 个文件未在时限内稳定，交给自动查漏重试。")
    if not valid_files: return

    config = config_manager.APP_CONFIG
    result = refresh_and_verify_paths(
        valid_files,
        config.get(constants.CONFIG_OPTION_EMBY_SERVER_URL),
        config.get(constants.CONFIG_OPTION_EMBY_API_KEY),
        initial_delay_seconds=config.get(constants.CONFIG_OPTION_MONITOR_EXCLUDE_REFRESH_DELAY, 0),
    )
    pending = result.get('pending') or []
    if pending:
        logger.warning(
            f"  ⚠️ [实时监控] {len(pending)}/{result.get('requested', 0)} 个文件尚未被 Emby 收录，"
            "自动查漏将在下一轮继续处理。"
        )
    else:
        logger.info(f"  ✅ [实时监控] 已确认 Emby 收录 {result.get('indexed', 0)} 个文件。")
    processor.enqueue_confirmed_ingest_postprocessing(result.get('confirmed_paths') or [])

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
    if emby.notify_media_paths_updated(
        file_paths,
        base_url,
        api_key,
        update_type="Deleted",
    ):
        logger.info(f"  ✅ [实时监控-删除] 已通知 Emby {len(file_paths)} 个精确删除路径。")

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
        self.reconcile_interval_minutes = max(
            0,
            int(self.config.get(
                constants.CONFIG_OPTION_MONITOR_RECONCILE_INTERVAL_MINUTES,
                constants.DEFAULT_MONITOR_RECONCILE_INTERVAL_MINUTES,
            ) or 0),
        )
        self._reconcile_stop = threading.Event()
        self._reconcile_thread = None
        self._reconcile_retry_paths = set()
        lookback_days = max(0, int(self.config.get(
            constants.CONFIG_OPTION_MONITOR_SCAN_LOOKBACK_DAYS,
            constants.DEFAULT_MONITOR_SCAN_LOOKBACK_DAYS,
        ) or 0))
        initial_lookback_seconds = min(max(lookback_days * 86400, 3600), 86400)
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
            if self.reconcile_interval_minutes > 0 and self.exclude_dirs:
                self._reconcile_thread = threading.Thread(
                    target=self._run_reconcile_loop,
                    name="strm-ingest-reconcile",
                    daemon=True,
                )
                self._reconcile_thread.start()
                logger.info(
                    f"  🧭 STRM 自动查漏已启动，每 {self.reconcile_interval_minutes} 分钟检查排除路径。"
                )
        else:
            logger.warning("  ➜ 没有有效的监控目录，实时监控服务未启动。")

    def _run_reconcile_loop(self):
        if self._reconcile_stop.wait(60):
            return
        while not self._reconcile_stop.is_set():
            scan_started = time.time()
            try:
                result = reconcile_recent_paths(
                    self.exclude_dirs,
                    self.extensions,
                    self._reconcile_since,
                    self.config.get(constants.CONFIG_OPTION_EMBY_SERVER_URL),
                    self.config.get(constants.CONFIG_OPTION_EMBY_API_KEY),
                    strm_only=True,
                    retry_paths=self._reconcile_retry_paths,
                )
                self.processor.enqueue_confirmed_ingest_postprocessing(
                    result.get('confirmed_paths') or []
                )
                self._reconcile_retry_paths = set(result.get('unresolved_paths') or [])
                self._reconcile_since = scan_started - 60
                logger.info(
                    f"  🧭 STRM 自动查漏完成：检查 {result.get('scanned', 0)}，"
                    f"刷新前缺失 {result.get('missing_before_refresh', 0)}，"
                    f"留待重试 {len(self._reconcile_retry_paths)}。"
                )
            except Exception as exc:
                logger.error(f"  ❌ STRM 自动查漏失败，将在下一轮重试: {exc}", exc_info=True)
            if self._reconcile_stop.wait(self.reconcile_interval_minutes * 60):
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
