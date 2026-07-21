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
ANCHOR_REFRESH_COOLDOWN_SECONDS = 300

_ANCHOR_REFRESH_STATE_LOCK = threading.Lock()
_ANCHOR_REFRESH_INFLIGHT: Set[Tuple[str, str]] = set()
_ANCHOR_REFRESHED_AT: Dict[Tuple[str, str], float] = {}


def _claim_anchor_refresh(base_url: str, anchor_id: str) -> Optional[Tuple[str, str]]:
    """Reserve one Series/Season refresh and suppress concurrent/recent duplicates."""
    key = (str(base_url or '').rstrip('/'), str(anchor_id or '').strip())
    if not key[1]:
        return None

    now = time.monotonic()
    with _ANCHOR_REFRESH_STATE_LOCK:
        last_refresh = _ANCHOR_REFRESHED_AT.get(key)
        if key in _ANCHOR_REFRESH_INFLIGHT:
            return None
        if last_refresh is not None and now - last_refresh < ANCHOR_REFRESH_COOLDOWN_SECONDS:
            return None
        _ANCHOR_REFRESH_INFLIGHT.add(key)
    return key


def _finish_anchor_refresh(key: Tuple[str, str], succeeded: bool) -> None:
    with _ANCHOR_REFRESH_STATE_LOCK:
        _ANCHOR_REFRESH_INFLIGHT.discard(key)
        if succeeded:
            _ANCHOR_REFRESHED_AT[key] = time.monotonic()

        expiry = time.monotonic() - (ANCHOR_REFRESH_COOLDOWN_SECONDS * 2)
        stale_keys = [
            cached_key
            for cached_key, refreshed_at in _ANCHOR_REFRESHED_AT.items()
            if refreshed_at < expiry and cached_key not in _ANCHOR_REFRESH_INFLIGHT
        ]
        for cached_key in stale_keys:
            _ANCHOR_REFRESHED_AT.pop(cached_key, None)


def _reset_ingest_refresh_state() -> None:
    """Clear process-local refresh deduplication state for tests and clean restarts."""
    with _ANCHOR_REFRESH_STATE_LOCK:
        _ANCHOR_REFRESH_INFLIGHT.clear()
        _ANCHOR_REFRESHED_AT.clear()


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


def _notification_paths(
    file_paths: Iterable[str],
    base_url: str,
    api_key: str,
) -> List[str]:
    """
    将同一媒体作品的多个文件聚合为一个作品目录通知。

    电视剧、动漫、纪录片、特摄剧和目录化电影：
    通知媒体库根目录下的作品目录。

    直接平铺在电影库根目录的电影：
    仍然通知具体文件。

    本函数永远不返回媒体库根目录。
    """
    paths = normalize_paths(file_paths)
    if not paths:
        return []

    libraries = emby.get_all_libraries_with_paths(base_url, api_key)
    library_roots = sorted(
        {
            os.path.normpath(str(root_path))
            for library in libraries
            for root_path in (library.get('paths') or [])
            if str(root_path or '').strip()
        },
        key=len,
        reverse=True,
    )

    targets: Set[str] = set()

    for path in paths:
        normalized_path = os.path.normcase(os.path.normpath(path))
        matched_root: Optional[str] = None

        for root_path in library_roots:
            normalized_root = os.path.normcase(
                os.path.normpath(root_path)
            )

            try:
                inside_root = (
                    os.path.commonpath(
                        [normalized_path, normalized_root]
                    )
                    == normalized_root
                )
            except ValueError:
                inside_root = False

            if inside_root:
                matched_root = root_path
                break

        # 无法匹配媒体库根目录时，宁可通知具体文件，
        # 也绝不向上扩大成媒体库根目录。
        if not matched_root:
            targets.add(path)
            continue

        relative_path = os.path.relpath(path, matched_root)
        parts = [
            part
            for part in relative_path.split(os.sep)
            if part not in ('', '.')
        ]

        # 文件直接平铺在媒体库根目录：
        # 只能通知具体文件，不能通知媒体库根目录。
        if len(parts) <= 1:
            targets.add(path)
            continue

        # 取媒体库根目录下的第一级目录，即作品目录。
        item_directory = os.path.normpath(
            os.path.join(matched_root, parts[0])
        )

        if (
            os.path.normcase(item_directory)
            == os.path.normcase(os.path.normpath(matched_root))
        ):
            targets.add(path)
        else:
            targets.add(item_directory)

    logger.info(
        f"  ➜ STRM 精确通知聚合：{len(paths)} 个文件 -> "
        f"{len(targets)} 个作品目标。"
    )

    return sorted(targets)


def _deletion_notification_paths(
    file_paths: Iterable[str],
    base_url: str,
    api_key: str,
    libraries: Optional[Sequence[Dict[str, object]]] = None,
) -> Tuple[List[str], List[str]]:
    """Split vanished paths from the nearest surviving parent directories."""
    paths = normalize_paths(file_paths, require_existing=False)
    libraries = list(libraries) if libraries is not None else emby.get_all_libraries_with_paths(
        base_url,
        api_key,
    )
    library_roots = {
        os.path.normcase(os.path.normpath(str(path)))
        for library in libraries
        for path in (library.get('paths') or [])
        if str(path or '').strip()
    }
    if not library_roots:
        return paths, sorted({os.path.dirname(path) for path in paths})

    deleted = set(paths)
    modified: Set[str] = set()
    for path in paths:
        current = os.path.dirname(path)
        for _ in range(12):
            normalized = os.path.normcase(os.path.normpath(current))
            if normalized in library_roots:
                modified.add(current)
                break
            if os.path.isdir(current):
                modified.add(current)
                break
            deleted.add(current)
            parent = os.path.dirname(current)
            if not parent or parent == current:
                break
            current = parent
    return sorted(deleted), sorted(modified)


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
    require_existing: bool = True,
) -> List[Dict[str, object]]:
    """Resolve exact indexed paths to Emby items and deduplicate by item ID."""
    paths = normalize_paths(file_paths, require_existing=require_existing)
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


def verify_deleted_paths(
    file_paths: Iterable[str],
    base_url: str,
    api_key: str,
    verify_delays: Sequence[int] = (3, 8, 15),
) -> Dict[str, object]:
    """Confirm that exact Emby paths disappeared without touching the library root."""
    paths = normalize_paths(file_paths, require_existing=False)
    remaining = set(paths)
    query_failed: Set[str] = set()
    previous_deadline = 0
    for deadline_seconds in verify_delays:
        wait_seconds = max(1, int(deadline_seconds) - previous_deadline)
        previous_deadline = int(deadline_seconds)
        time.sleep(wait_seconds)
        worker_count = max(1, min(len(remaining), 8))
        states: Dict[str, Optional[bool]] = {}
        with ThreadPoolExecutor(max_workers=worker_count) as executor:
            futures = {
                executor.submit(emby.is_catalog_path_indexed, path, base_url, api_key): path
                for path in remaining
            }
            for future in as_completed(futures):
                path = futures[future]
                try:
                    states[path] = future.result()
                except Exception:
                    states[path] = None
        removed = {path for path, state in states.items() if state is False}
        query_failed = {path for path, state in states.items() if state is None}
        remaining -= removed
        if not remaining:
            break
    return {
        'requested': len(paths),
        'confirmed_paths': sorted(set(paths) - remaining),
        'pending': sorted(remaining),
        'query_failed': sorted(query_failed & remaining),
    }


def delete_and_verify_paths(
    file_paths: Iterable[str],
    base_url: str,
    api_key: str,
    verify_delays: Sequence[int] = (10,),
) -> Dict[str, object]:
    """Notify exact deletions and refresh each affected TV series at most once."""
    paths = normalize_paths(file_paths, require_existing=False)
    result: Dict[str, object] = {
        'requested': len(paths),
        'confirmed_paths': [],
        'pending': paths,
        'query_failed': [],
        'refresh_ok': True,
    }
    if not paths or not base_url or not api_key:
        result['refresh_ok'] = False
        return result

    # Read only catalog fields before notifying Emby. Requesting MediaSources
    # here makes Emby reopen every STRM which has already disappeared.
    series_ids: Set[str] = set()
    worker_count = max(1, min(len(paths), 4))
    with ThreadPoolExecutor(max_workers=worker_count) as executor:
        futures = {
            executor.submit(emby.get_catalog_item_by_path, path, base_url, api_key): path
            for path in paths
        }
        for future in as_completed(futures):
            try:
                item = future.result() or {}
            except Exception:
                item = {}
            series_id = str(item.get('SeriesId') or '').strip()
            if series_id:
                series_ids.add(series_id)

    series_items: Dict[str, Dict[str, object]] = {
        series_id: emby.get_catalog_item_by_id(series_id, base_url, api_key) or {}
        for series_id in series_ids
    }
    libraries = emby.get_all_libraries_with_paths(base_url, api_key)

    # The exact Deleted notification is authoritative. A single recursive
    # refresh per deduplicated Series lets Emby discard missing children while
    # avoiding the old one-refresh-per-episode storm and any library-root scan.
    # Never refresh a Series whose own directory has disappeared: Emby 4.9
    # raises DirectoryNotFoundException while validating that stale item and
    # may leave the scan progress stuck until the server is restarted.
    deleted_notification_paths, modified_notification_paths = _deletion_notification_paths(
        paths,
        base_url,
        api_key,
        libraries=libraries,
    )
    refresh_ok = emby.notify_media_paths_updated(
        deleted_notification_paths,
        base_url,
        api_key,
        update_type='Deleted',
    )
    if modified_notification_paths:
        refresh_ok = emby.notify_media_paths_updated(
            modified_notification_paths,
            base_url,
            api_key,
            update_type='Modified',
        ) and refresh_ok
    missing_series_paths: Set[str] = set()
    for series_id in sorted(series_ids):
        series_item = series_items.get(series_id) or {}
        series_path = str(series_item.get('Path') or '').strip()
        if not series_path or not os.path.isdir(series_path):
            if series_path:
                missing_series_paths.add(series_path)
            logger.info(
                f"  ➜ 剧集目录已删除，跳过失效剧集递归刷新: "
                f"'{series_path or series_id}'。精确 Deleted 通知将负责清理目录项。"
            )
            continue
        refresh_ok = emby.refresh_item_by_id(series_id, base_url, api_key) and refresh_ok

    # A vanished Series cannot refresh itself. Refresh only the direct children
    # of its physical library root so Emby drops that stale Series row without
    # recursively rescanning every remaining show in the library.
    shallow_library_ids: Set[str] = set()
    for library in libraries:
        library_id = str((library.get('info') or {}).get('Id') or '').strip()
        if not library_id:
            continue
        for root_path in library.get('paths') or []:
            normalized_root = os.path.normcase(os.path.normpath(str(root_path)))
            for series_path in missing_series_paths:
                normalized_series = os.path.normcase(os.path.normpath(series_path))
                try:
                    is_within_root = os.path.commonpath(
                        [normalized_series, normalized_root]
                    ) == normalized_root
                except ValueError:
                    is_within_root = False
                if is_within_root:
                    shallow_library_ids.add(library_id)
                    break
    for library_id in sorted(shallow_library_ids):
        logger.info(
            f"  ➜ 剧集目录整体消失，浅层刷新物理媒体库 ({library_id})，"
            "仅校验直属剧集，不递归扫描库内容。"
        )
        refresh_ok = emby.refresh_item_by_id(
            library_id,
            base_url,
            api_key,
            recursive=False,
        ) and refresh_ok
    result.update(verify_deleted_paths(paths, base_url, api_key, verify_delays=verify_delays))
    result['refresh_ok'] = refresh_ok
    return result


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
    library_root_paths = {
        os.path.normcase(os.path.normpath(str(path)))
        for library in libraries
        for path in (library.get('paths') or [])
        if str(path or '').strip()
    }
    if not library_root_ids:
        logger.warning(
            "  ⚠️ 无法确认 Emby 媒体库根 ID，本轮只发送精确路径通知，"
            "不执行任何递归刷新。"
        )
        return True
    anchors: Dict[str, str] = {}
    for parent_dir in parent_dirs:
        anchor = emby.find_nearest_library_anchor_details(
            parent_dir,
            base_url,
            api_key,
            allowed_types=('Series', 'Season'),
            blocked_paths=library_root_paths,
        )
        anchor_id = str((anchor or {}).get('Id') or '')
        anchor_name = str((anchor or {}).get('Name') or '')
        anchor_type = str((anchor or {}).get('Type') or '').lower()
        anchor_path = str((anchor or {}).get('Path') or '')
        normalized_anchor_path = (
            os.path.normcase(os.path.normpath(anchor_path))
            if anchor_path else ''
        )
        if (
            anchor_id
            and anchor_id not in library_root_ids
            and anchor_type in {'series', 'season'}
            and normalized_anchor_path not in library_root_paths
        ):
            anchors[str(anchor_id)] = anchor_name or str(anchor_id)
        else:
            logger.info(
                f"  ➜ '{parent_dir}' 尚无安全的剧集/季锚点，仅发送精确路径通知，"
                "不会递归刷新整个媒体库。"
            )

    success = True
    for anchor_id, anchor_name in anchors.items():
        refresh_key = _claim_anchor_refresh(base_url, anchor_id)
        if refresh_key is None:
            logger.info(
                f"  ➜ STRM 入库锚点近期已刷新或正在刷新，跳过重复请求: "
                f"'{anchor_name}' ({anchor_id})"
            )
            continue
        logger.info(f"  ➜ STRM 入库刷新 Emby 锚点: '{anchor_name}' ({anchor_id})")
        refreshed = False
        try:
            refreshed = emby.refresh_item_by_id(
                anchor_id,
                base_url,
                api_key,
                recursive=False,
            )
        finally:
            _finish_anchor_refresh(refresh_key, refreshed)
        if not refreshed:
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

    # Serialize only the lightweight notification/refresh dispatch. Verification
    # waits must not hold this lock: MoviePilot and other STRM writers can emit
    # several debounced batches while the first batch is waiting for Emby. The
    # old lock scope turned every batch into another 45-second queue slot.
    with INGEST_REFRESH_LOCK:
        refresh_ok = emby.notify_media_paths_updated(
            _notification_paths(paths, base_url, api_key),
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
            with INGEST_REFRESH_LOCK:
                refresh_ok = emby.notify_media_paths_updated(
                    _notification_paths(
                        retry_paths,
                        base_url,
                        api_key,
                    ),
                    base_url,
                    api_key,
                ) and refresh_ok

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


def collect_strm_inventory(root_path: str) -> Dict[str, Tuple[int, float]]:
    """Return exact STRM paths and fingerprints for one low-frequency inventory pass."""
    root = os.path.normpath(str(root_path or '').strip())
    if not root or not os.path.isdir(root):
        return {}
    inventory: Dict[str, Tuple[int, float]] = {}
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [name for name in dirnames if not name.startswith('.')]
        for filename in filenames:
            if filename.startswith('.') or not filename.lower().endswith('.strm'):
                continue
            path = os.path.normpath(os.path.join(dirpath, filename))
            try:
                stat = os.stat(path)
            except OSError:
                continue
            if stat.st_size > 0:
                inventory[path] = (int(stat.st_size), float(stat.st_mtime))
    return inventory


def reconcile_paths(
    file_paths: Iterable[str],
    base_url: str,
    api_key: str,
) -> Dict[str, object]:
    """Check exact paths, refresh only missing entries, and return unresolved paths."""
    paths = normalize_paths(file_paths)
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
    return reconcile_paths(paths, base_url, api_key)
