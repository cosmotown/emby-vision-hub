import logging
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Dict, Iterable, List, Optional, Sequence, Set, Tuple

import handler.emby as emby


logger = logging.getLogger(__name__)

INGEST_REFRESH_LOCK = threading.Lock()
DEFAULT_VERIFY_DELAYS = (8, 20, 45)


def normalize_paths(file_paths: Iterable[str], require_existing: bool = True) -> List[str]:
    paths = []
    seen = set()
    for raw_path in file_paths or []:
        raw_value = str(raw_path or '').strip()
        if not raw_value:
            continue
        path = os.path.normpath(raw_value)
        if path in seen:
            continue
        if require_existing:
            try:
                if not os.path.isfile(path) or os.path.getsize(path) <= 0:
                    continue
            except OSError:
                continue
        seen.add(path)
        paths.append(path)
    return sorted(paths)


def _notification_paths(file_paths: Iterable[str]) -> List[str]:
    paths = set(file_paths)
    paths.update(os.path.dirname(path) for path in list(paths))
    return sorted(paths)


def wait_for_paths_stable(
    file_paths: Iterable[str],
    timeout_seconds: int = 60,
    poll_interval: float = 1.0,
) -> Tuple[List[str], List[str]]:
    """Wait for every path in parallel, avoiding the old N-files-times-3s delay."""
    paths = normalize_paths(file_paths, require_existing=False)
    states: Dict[str, Dict[str, int]] = {
        path: {'last_size': -1, 'stable_count': 0}
        for path in paths
    }
    stable = set()
    deadline = time.monotonic() + max(1, int(timeout_seconds))

    while states and time.monotonic() < deadline:
        for path in list(states):
            try:
                if not os.path.isfile(path):
                    continue
                size = os.path.getsize(path)
                state = states[path]
                if size > 0 and size == state['last_size']:
                    state['stable_count'] += 1
                else:
                    state['stable_count'] = 0
                state['last_size'] = size

                required_matches = 2 if path.lower().endswith('.strm') else 3
                if state['stable_count'] >= required_matches:
                    stable.add(path)
                    states.pop(path, None)
            except OSError:
                continue
        if states:
            time.sleep(max(0.1, float(poll_interval)))

    skipped = sorted(set(paths) - stable)
    return sorted(stable), skipped


def check_indexed_paths(
    file_paths: Iterable[str],
    base_url: str,
    api_key: str,
    max_workers: int = 5,
) -> Tuple[Set[str], Set[str], Set[str]]:
    paths = normalize_paths(file_paths)
    indexed: Set[str] = set()
    missing: Set[str] = set()
    failed: Set[str] = set()
    if not paths:
        return indexed, missing, failed

    worker_count = max(1, min(int(max_workers), len(paths), 8))
    with ThreadPoolExecutor(max_workers=worker_count) as executor:
        futures = {
            executor.submit(emby.is_media_path_indexed, path, base_url, api_key): path
            for path in paths
        }
        for future in as_completed(futures):
            path = futures[future]
            try:
                result = future.result()
            except Exception:
                result = None
            if result is True:
                indexed.add(path)
            elif result is False:
                missing.add(path)
            else:
                failed.add(path)
    return indexed, missing, failed


def get_confirmed_media_items(
    file_paths: Iterable[str],
    base_url: str,
    api_key: str,
    max_workers: int = 5,
) -> List[Dict[str, object]]:
    """Resolve exact indexed paths to Emby items and deduplicate by item ID."""
    paths = normalize_paths(file_paths)
    if not paths or not base_url or not api_key:
        return []

    items_by_id: Dict[str, Dict[str, object]] = {}
    worker_count = max(1, min(int(max_workers), len(paths), 8))
    with ThreadPoolExecutor(max_workers=worker_count) as executor:
        futures = {
            executor.submit(emby.get_media_item_by_path, path, base_url, api_key): path
            for path in paths
        }
        for future in as_completed(futures):
            try:
                item = future.result()
            except Exception:
                item = None
            item_id = str((item or {}).get('Id') or '').strip()
            if item_id:
                items_by_id[item_id] = item
    return list(items_by_id.values())


def _refresh_parent_targets(
    file_paths: Iterable[str],
    base_url: str,
    api_key: str,
) -> bool:
    parent_dirs = sorted({os.path.dirname(path) for path in file_paths})
    libraries = emby.get_all_libraries_with_paths(base_url, api_key)
    library_root_ids = {
        str((library.get('info') or {}).get('Id'))
        for library in libraries
        if (library.get('info') or {}).get('Id')
    }
    if not library_root_ids:
        logger.warning(
            "  ⚠️ 无法确认 Emby 媒体库根 ID，本轮只发送精确路径通知，"
            "不执行任何递归刷新。"
        )
        return True
    anchors: Dict[str, str] = {}
    for parent_dir in parent_dirs:
        anchor_id, anchor_name = emby.find_nearest_library_anchor(parent_dir, base_url, api_key)
        if anchor_id and str(anchor_id) not in library_root_ids:
            anchors[str(anchor_id)] = anchor_name or str(anchor_id)
        else:
            logger.info(
                f"  ➜ '{parent_dir}' 尚无安全的剧集/季锚点，仅发送精确路径通知，"
                "不会递归刷新整个媒体库。"
            )

    success = True
    for anchor_id, anchor_name in anchors.items():
        logger.info(f"  ➜ STRM 入库刷新 Emby 锚点: '{anchor_name}' ({anchor_id})")
        if not emby.refresh_item_by_id(anchor_id, base_url, api_key):
            success = False
    return success


def refresh_and_verify_paths(
    file_paths: Iterable[str],
    base_url: str,
    api_key: str,
    initial_delay_seconds: int = 0,
    verify_delays: Sequence[int] = DEFAULT_VERIFY_DELAYS,
) -> Dict[str, object]:
    """Notify, refresh, verify, and retry until Emby indexes every path or attempts end."""
    paths = normalize_paths(file_paths)
    result: Dict[str, object] = {
        'requested': len(paths),
        'indexed': 0,
        'confirmed_paths': [],
        'pending': [],
        'query_failed': [],
        'refresh_ok': True,
    }
    if not paths:
        return result
    if not base_url or not api_key:
        result['refresh_ok'] = False
        result['pending'] = paths
        return result

    if initial_delay_seconds > 0:
        time.sleep(max(0, int(initial_delay_seconds)))

    with INGEST_REFRESH_LOCK:
        refresh_ok = emby.notify_media_paths_updated(
            _notification_paths(paths),
            base_url,
            api_key,
        )
        refresh_ok = _refresh_parent_targets(paths, base_url, api_key) and refresh_ok
        pending = set(paths)
        query_failed: Set[str] = set()

        previous_deadline = 0
        for attempt, deadline_seconds in enumerate(verify_delays, start=1):
            wait_seconds = max(1, int(deadline_seconds) - previous_deadline)
            previous_deadline = int(deadline_seconds)
            time.sleep(wait_seconds)
            indexed, missing, failed = check_indexed_paths(pending, base_url, api_key)
            pending -= indexed
            query_failed = failed
            logger.info(
                f"  ➜ STRM 入库确认 {attempt}/{len(verify_delays)}: "
                f"已入库 {len(paths) - len(pending)}/{len(paths)}，待重试 {len(pending)}。"
            )
            if not pending:
                break
            if attempt < len(verify_delays):
                retry_paths = sorted(missing | failed)
                refresh_ok = emby.notify_media_paths_updated(
                    _notification_paths(retry_paths),
                    base_url,
                    api_key,
                ) and refresh_ok
                refresh_ok = _refresh_parent_targets(retry_paths, base_url, api_key) and refresh_ok

        result.update({
            'indexed': len(paths) - len(pending),
            'confirmed_paths': sorted(set(paths) - pending),
            'pending': sorted(pending),
            'query_failed': sorted(query_failed & pending),
            'refresh_ok': refresh_ok,
        })
        return result


def collect_recent_media_paths(
    root_paths: Iterable[str],
    extensions: Iterable[str],
    cutoff_timestamp: float,
    strm_only: bool = False,
) -> List[str]:
    normalized_extensions = {
        ext.lower() if str(ext).startswith('.') else f".{str(ext).lower()}"
        for ext in extensions or []
        if str(ext or '').strip()
    }
    if strm_only:
        normalized_extensions &= {'.strm'}
    paths = []
    for root_path in normalize_paths(root_paths, require_existing=False):
        if not os.path.isdir(root_path):
            continue
        for dirpath, dirnames, filenames in os.walk(root_path):
            dirnames[:] = [name for name in dirnames if not name.startswith('.')]
            for filename in filenames:
                if filename.startswith('.'):
                    continue
                path = os.path.join(dirpath, filename)
                if os.path.splitext(filename)[1].lower() not in normalized_extensions:
                    continue
                try:
                    stat = os.stat(path)
                except OSError:
                    continue
                if max(stat.st_mtime, stat.st_ctime) >= cutoff_timestamp and stat.st_size > 0:
                    paths.append(os.path.normpath(path))
    return sorted(set(paths))


def reconcile_recent_paths(
    root_paths: Iterable[str],
    extensions: Iterable[str],
    cutoff_timestamp: float,
    base_url: str,
    api_key: str,
    strm_only: bool = False,
    retry_paths: Iterable[str] = (),
) -> Dict[str, object]:
    recent_paths = collect_recent_media_paths(
        root_paths,
        extensions,
        cutoff_timestamp,
        strm_only=strm_only,
    )
    paths = sorted(set(recent_paths) | set(normalize_paths(retry_paths)))
    indexed, missing, failed = check_indexed_paths(paths, base_url, api_key)
    pending = sorted(missing | failed)
    stable_pending, unstable = wait_for_paths_stable(pending) if pending else ([], [])
    refresh_result = refresh_and_verify_paths(stable_pending, base_url, api_key) if stable_pending else {
        'requested': 0,
        'indexed': 0,
        'confirmed_paths': [],
        'pending': [],
        'query_failed': [],
        'refresh_ok': True,
    }
    unresolved_paths = sorted(
        set(unstable)
        | set(refresh_result.get('pending') or [])
        | set(refresh_result.get('query_failed') or [])
    )
    return {
        'scanned': len(paths),
        'already_indexed': len(indexed),
        'confirmed_paths': sorted(indexed | set(refresh_result.get('confirmed_paths') or [])),
        'unresolved_paths': unresolved_paths,
        'missing_before_refresh': len(pending),
        'unstable': len(unstable),
        'refresh': refresh_result,
    }
