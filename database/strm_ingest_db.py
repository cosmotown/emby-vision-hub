import os
import uuid
from typing import Dict, Iterable, List, Optional, Tuple

from psycopg2.extras import execute_values

from .connection import get_db_connection


RETRY_DELAYS_SECONDS = (10 * 60, 20 * 60, 30 * 60)


def _escape_like_path(path: str) -> str:
    return str(path).replace('\\', '\\\\').replace('%', '\\%').replace('_', '\\_')


def _first_hop_below(root_path: str, directory_path: str, descendant_path: str) -> Optional[str]:
    """Return the lexical first-hop branch without allowing root escape."""
    root = os.path.normpath(str(root_path))
    directory = os.path.normpath(str(directory_path))
    descendant = os.path.normpath(str(descendant_path))
    if not all(os.path.isabs(value) for value in (root, directory, descendant)):
        return None
    try:
        if os.path.commonpath((root, directory)) != root:
            return None
        if descendant == directory or os.path.commonpath((directory, descendant)) != directory:
            return None
    except ValueError:
        return None
    relative = os.path.relpath(descendant, directory)
    first_name = relative.split(os.sep, 1)[0]
    if first_name in {'', '.', '..'}:
        return None
    first_hop = os.path.normpath(os.path.join(directory, first_name))
    try:
        if os.path.commonpath((root, first_hop)) != root:
            return None
    except ValueError:
        return None
    return first_hop


def _fingerprint(file_path: str) -> Tuple[Optional[int], Optional[float]]:
    try:
        stat = os.stat(file_path)
        return int(stat.st_size), float(stat.st_mtime)
    except OSError:
        return None, None


def enqueue_paths(
    file_paths: Iterable[str],
    *,
    source: str,
    last_error: str,
    operation: str = 'ingest',
    initial_delay_seconds: int = 10 * 60,
    max_attempts: int = 3,
) -> Dict[str, int]:
    """Persist unresolved STRM paths without reviving unchanged terminal rows."""
    normalized = sorted({os.path.normpath(str(path)) for path in file_paths or [] if str(path or '').strip()})
    safe_operation = 'delete' if operation == 'delete' else 'ingest'
    result = {'queued': 0, 'active': 0, 'terminal': 0}
    if not normalized:
        return result

    with get_db_connection() as conn:
        with conn.cursor() as cursor:
            for file_path in normalized:
                file_size, file_mtime = _fingerprint(file_path)
                cursor.execute(
                    "SELECT * FROM strm_ingest_retry_queue WHERE file_path = %s FOR UPDATE",
                    (file_path,),
                )
                row = cursor.fetchone()
                if not row:
                    cursor.execute(
                        """
                        INSERT INTO strm_ingest_retry_queue (
                            file_path, operation, source, status, attempt_count, max_attempts,
                            next_attempt_at, file_size, file_mtime, last_error
                        ) VALUES (%s, %s, %s, 'pending', 0, %s,
                                  NOW() + (%s * INTERVAL '1 second'), %s, %s, %s)
                        """,
                        (
                            file_path, safe_operation, source, max(1, int(max_attempts)),
                            max(0, int(initial_delay_seconds)), file_size, file_mtime,
                            str(last_error or '')[:4000],
                        ),
                    )
                    result['queued'] += 1
                    continue

                changed = (
                    row.get('operation') != safe_operation
                    or
                    file_size is not None
                    and file_mtime is not None
                    and (
                        row.get('file_size') != file_size
                        or row.get('file_mtime') != file_mtime
                    )
                )
                if row.get('status') in {'failed', 'ignored'} and not changed:
                    result['terminal'] += 1
                    continue
                if row.get('status') in {'pending', 'retry', 'processing'} and not changed:
                    result['active'] += 1
                    continue
                if row.get('status') == 'completed' and not changed:
                    continue

                cursor.execute(
                    """
                    UPDATE strm_ingest_retry_queue
                    SET operation = %s,
                        source = %s,
                        status = 'pending',
                        attempt_count = 0,
                        max_attempts = %s,
                        next_attempt_at = NOW() + (%s * INTERVAL '1 second'),
                        file_size = %s,
                        file_mtime = %s,
                        last_error = %s,
                        updated_at = NOW(),
                        last_checked_at = NULL,
                        completed_at = NULL
                    WHERE id = %s
                    """,
                    (
                        safe_operation, source, max(1, int(max_attempts)), max(0, int(initial_delay_seconds)),
                        file_size, file_mtime, str(last_error or '')[:4000], row['id'],
                    ),
                )
                result['queued'] += 1
    return result


def recover_processing() -> int:
    with get_db_connection() as conn:
        with conn.cursor() as cursor:
            cursor.execute(
                """
                UPDATE strm_ingest_retry_queue
                SET status = 'retry', next_attempt_at = NOW(), updated_at = NOW(),
                    last_error = COALESCE(last_error, 'Toolkit 重启后恢复未完成任务')
                WHERE status = 'processing'
                """
            )
            return cursor.rowcount


def claim_due_paths(limit: int = 20) -> List[Dict]:
    with get_db_connection() as conn:
        with conn.cursor() as cursor:
            cursor.execute(
                """
                WITH due AS (
                    SELECT id
                    FROM strm_ingest_retry_queue
                    WHERE status IN ('pending', 'retry')
                      AND next_attempt_at <= NOW()
                    ORDER BY next_attempt_at, id
                    LIMIT %s
                    FOR UPDATE SKIP LOCKED
                )
                UPDATE strm_ingest_retry_queue q
                SET status = 'processing', last_checked_at = NOW(), updated_at = NOW()
                FROM due
                WHERE q.id = due.id
                RETURNING q.*
                """,
                (max(1, min(int(limit), 100)),),
            )
            return [dict(row) for row in cursor.fetchall()]


def defer_claimed_paths(
    file_paths: Iterable[str],
    error: str,
    *,
    delay_seconds: int = 300,
) -> int:
    """Release claimed rows without changing operation or consuming an attempt."""
    paths = sorted({
        os.path.normpath(str(path))
        for path in file_paths or []
        if str(path or '').strip()
    })
    if not paths:
        return 0
    safe_delay = max(30, min(int(delay_seconds), 3600))
    with get_db_connection() as conn:
        with conn.cursor() as cursor:
            cursor.execute(
                """
                UPDATE strm_ingest_retry_queue
                SET status = 'retry',
                    next_attempt_at = NOW() + (%s * INTERVAL '1 second'),
                    updated_at = NOW(), last_error = %s
                WHERE file_path = ANY(%s) AND status = 'processing'
                """,
                (safe_delay, str(error or '')[:4000], paths),
            )
            return cursor.rowcount


def mark_completed(file_paths: Iterable[str]) -> int:
    paths = sorted({os.path.normpath(str(path)) for path in file_paths or [] if str(path or '').strip()})
    if not paths:
        return 0
    rows = []
    for file_path in paths:
        file_size, file_mtime = _fingerprint(file_path)
        rows.append((file_path, file_size, file_mtime))
    with get_db_connection() as conn:
        with conn.cursor() as cursor:
            execute_values(
                cursor,
                """
                INSERT INTO strm_ingest_retry_queue (
                    file_path, operation, source, status, file_size, file_mtime, completed_at
                ) VALUES %s
                ON CONFLICT (file_path) DO UPDATE
                SET operation = 'ingest',
                    status = CASE
                        WHEN strm_ingest_retry_queue.status = 'ignored' THEN 'ignored'
                        ELSE 'completed'
                    END,
                    file_size = EXCLUDED.file_size,
                    file_mtime = EXCLUDED.file_mtime,
                    completed_at = CASE
                        WHEN strm_ingest_retry_queue.status = 'ignored'
                            THEN strm_ingest_retry_queue.completed_at
                        ELSE NOW()
                    END,
                    updated_at = NOW(),
                    last_error = CASE
                        WHEN strm_ingest_retry_queue.status = 'ignored'
                            THEN strm_ingest_retry_queue.last_error
                        ELSE NULL
                    END
                """,
                rows,
                template="(%s, 'ingest', 'confirmed', 'completed', %s, %s, NOW())",
            )
            return len(rows)


def reconcile_inventory(root_path: str, entries: Dict[str, Tuple[int, float]]) -> Dict[str, object]:
    """Compare one mounted STRM root with its persisted path inventory."""
    root = os.path.normpath(str(root_path))
    current_paths = set(entries)
    like_root = root.replace('\\', '\\\\').replace('%', '\\%').replace('_', '\\_') + '/%'
    with get_db_connection() as conn:
        with conn.cursor() as cursor:
            cursor.execute(
                "SELECT 1 FROM strm_ingest_inventory_roots WHERE root_path = %s",
                (root,),
            )
            initialized = cursor.fetchone() is not None
            cursor.execute(
                """
                SELECT file_path, file_size, file_mtime, status, operation
                FROM strm_ingest_retry_queue
                WHERE file_path = %s OR file_path LIKE %s ESCAPE '\\'
                """,
                (root, like_root),
            )
            known = {row['file_path']: dict(row) for row in cursor.fetchall()}

            if not initialized:
                seed_rows = [
                    (path, fingerprint[0], fingerprint[1])
                    for path, fingerprint in entries.items()
                ]
                if seed_rows:
                    execute_values(
                        cursor,
                        """
                        INSERT INTO strm_ingest_retry_queue (
                            file_path, operation, source, status, file_size, file_mtime, completed_at
                        ) VALUES %s
                        ON CONFLICT (file_path) DO NOTHING
                        """,
                        seed_rows,
                        template="(%s, 'ingest', 'baseline', 'observed', %s, %s, NULL)",
                    )
                cursor.execute(
                    """
                    INSERT INTO strm_ingest_inventory_roots (root_path)
                    VALUES (%s)
                    ON CONFLICT (root_path) DO UPDATE SET last_scan_at = NOW()
                    """,
                    (root,),
                )
                return {
                    'initialized': False,
                    'added': [],
                    'changed': [],
                    'removed': [],
                    'seeded': len(seed_rows),
                }

            known_active = {
                path for path, row in known.items()
                if row.get('status') not in {'deleted', 'cancelled'}
                and row.get('operation') != 'delete'
            }
            revived = {
                path for path in current_paths & set(known)
                if known[path].get('status') in {'deleted', 'cancelled'}
                or known[path].get('operation') == 'delete'
            }
            added = sorted((current_paths - set(known)) | revived)
            removed = sorted(known_active - current_paths)
            changed = sorted(
                path for path in (current_paths & set(known)) - revived
                if (
                    known[path].get('file_size') != entries[path][0]
                    or known[path].get('file_mtime') != entries[path][1]
                )
            )
            cursor.execute(
                "UPDATE strm_ingest_inventory_roots SET last_scan_at = NOW() WHERE root_path = %s",
                (root,),
            )
            return {
                'initialized': True,
                'added': added,
                'changed': changed,
                'removed': removed,
                'seeded': 0,
            }


def list_active_paths_under(directory_path: str) -> List[str]:
    directory = os.path.normpath(str(directory_path or '').strip())
    if not directory:
        return []
    like_directory = directory.replace('\\', '\\\\').replace('%', '\\%').replace('_', '\\_') + '/%'
    with get_db_connection() as conn:
        with conn.cursor() as cursor:
            cursor.execute(
                """
                SELECT file_path
                FROM strm_ingest_retry_queue
                WHERE (file_path = %s OR file_path LIKE %s ESCAPE '\\')
                  AND status NOT IN ('deleted', 'cancelled')
                  AND operation != 'delete'
                ORDER BY file_path
                """,
                (directory, like_directory),
            )
            return [row['file_path'] for row in cursor.fetchall()]


def mark_cancelled(file_paths: Iterable[str], reason: str) -> int:
    paths = sorted({os.path.normpath(str(path)) for path in file_paths or [] if str(path or '').strip()})
    if not paths:
        return 0
    with get_db_connection() as conn:
        with conn.cursor() as cursor:
            cursor.execute(
                """
                UPDATE strm_ingest_retry_queue
                SET status = 'cancelled', completed_at = NOW(), updated_at = NOW(), last_error = %s
                WHERE file_path = ANY(%s)
                """,
                (str(reason or '')[:4000], paths),
            )
            return cursor.rowcount


def mark_deleted(file_paths: Iterable[str], reason: str = 'STRM 文件已删除') -> int:
    paths = sorted({os.path.normpath(str(path)) for path in file_paths or [] if str(path or '').strip()})
    if not paths:
        return 0
    with get_db_connection() as conn:
        with conn.cursor() as cursor:
            cursor.execute(
                """
                UPDATE strm_ingest_retry_queue
                SET operation = 'delete', status = 'deleted', completed_at = NOW(),
                    updated_at = NOW(), last_error = %s
                WHERE file_path = ANY(%s)
                """,
                (str(reason or '')[:4000], paths),
            )
            return cursor.rowcount


def mark_failed_attempts(file_paths: Iterable[str], error: str) -> Dict[str, int]:
    paths = sorted({os.path.normpath(str(path)) for path in file_paths or [] if str(path or '').strip()})
    result = {'retry': 0, 'failed': 0}
    if not paths:
        return result

    with get_db_connection() as conn:
        with conn.cursor() as cursor:
            for file_path in paths:
                cursor.execute(
                    "SELECT id, attempt_count, max_attempts FROM strm_ingest_retry_queue WHERE file_path = %s FOR UPDATE",
                    (file_path,),
                )
                row = cursor.fetchone()
                if not row:
                    continue
                attempts = int(row.get('attempt_count') or 0) + 1
                max_attempts = max(1, int(row.get('max_attempts') or 3))
                if attempts >= max_attempts:
                    cursor.execute(
                        """
                        UPDATE strm_ingest_retry_queue
                        SET status = 'failed', attempt_count = %s, completed_at = NOW(),
                            updated_at = NOW(), last_error = %s
                        WHERE id = %s
                        """,
                        (attempts, str(error or '')[:4000], row['id']),
                    )
                    result['failed'] += 1
                else:
                    delay = RETRY_DELAYS_SECONDS[min(attempts, len(RETRY_DELAYS_SECONDS) - 1)]
                    cursor.execute(
                        """
                        UPDATE strm_ingest_retry_queue
                        SET status = 'retry', attempt_count = %s,
                            next_attempt_at = NOW() + (%s * INTERVAL '1 second'),
                            updated_at = NOW(), last_error = %s
                        WHERE id = %s
                        """,
                        (attempts, delay, str(error or '')[:4000], row['id']),
                    )
                    result['retry'] += 1
    return result



def list_failed_ingest_paths(limit: int = 200) -> List[str]:
    """Return terminal ingest failures for read-only Emby self-healing checks."""
    with get_db_connection() as conn:
        with conn.cursor() as cursor:
            cursor.execute(
                """
                SELECT file_path
                FROM strm_ingest_retry_queue
                WHERE operation = 'ingest'
                  AND status = 'failed'
                ORDER BY updated_at ASC, id ASC
                LIMIT %s
                """,
                (max(1, min(int(limit), 1000)),),
            )
            return [row['file_path'] for row in cursor.fetchall()]

def list_recent(limit: int = 100) -> List[Dict]:
    with get_db_connection() as conn:
        with conn.cursor() as cursor:
            cursor.execute(
                """
                SELECT * FROM strm_ingest_retry_queue
                WHERE status IN ('pending', 'processing', 'retry', 'failed', 'ignored', 'cancelled')
                ORDER BY
                    CASE status
                        WHEN 'failed' THEN 0
                        WHEN 'processing' THEN 1
                        WHEN 'retry' THEN 2
                        WHEN 'pending' THEN 3
                        ELSE 4
                    END,
                    updated_at DESC
                LIMIT %s
                """,
                (max(1, min(int(limit), 500)),),
            )
            return [dict(row) for row in cursor.fetchall()]


def get_summary() -> Dict[str, int]:
    with get_db_connection() as conn:
        with conn.cursor() as cursor:
            cursor.execute(
                """
                SELECT
                    COUNT(*) FILTER (WHERE status = 'pending') AS pending_count,
                    COUNT(*) FILTER (WHERE status = 'processing') AS processing_count,
                    COUNT(*) FILTER (WHERE status = 'retry') AS retry_count,
                    COUNT(*) FILTER (WHERE status = 'failed') AS failed_count,
                    COUNT(*) FILTER (WHERE status = 'completed') AS completed_count,
                    COUNT(*) FILTER (WHERE status = 'ignored') AS ignored_count
                FROM strm_ingest_retry_queue
                """
            )
            row = cursor.fetchone() or {}
            return {key: int(value or 0) for key, value in row.items()}


def retry_path(item_id: int) -> bool:
    with get_db_connection() as conn:
        with conn.cursor() as cursor:
            cursor.execute(
                """
                UPDATE strm_ingest_retry_queue
                SET status = 'pending', attempt_count = 0, next_attempt_at = NOW(),
                    updated_at = NOW(), completed_at = NULL, last_error = '用户手动重试'
                WHERE id = %s AND status IN ('failed', 'ignored', 'cancelled')
                """,
                (item_id,),
            )
            return cursor.rowcount == 1


def ignore_path(item_id: int) -> bool:
    with get_db_connection() as conn:
        with conn.cursor() as cursor:
            cursor.execute(
                """
                UPDATE strm_ingest_retry_queue
                SET status = 'ignored', completed_at = NOW(), updated_at = NOW(),
                    last_error = '用户已忽略'
                WHERE id = %s AND status IN ('pending', 'retry', 'failed', 'cancelled')
                """,
                (item_id,),
            )
            return cursor.rowcount == 1


def prune_completed(retention_days: int = 30) -> int:
    with get_db_connection() as conn:
        with conn.cursor() as cursor:
            cursor.execute(
                """
                DELETE FROM strm_ingest_retry_queue
                WHERE status IN ('completed', 'cancelled', 'deleted')
                  AND completed_at < NOW() - (%s * INTERVAL '1 day')
                  AND (
                      inventory_root_path IS NULL
                      OR status IN ('cancelled', 'deleted')
                  )
                """,
                (max(1, int(retention_days)),),
            )
            return cursor.rowcount


# STRM Inventory v2 ---------------------------------------------------------

def register_inventory_roots(
    root_paths: Iterable[str],
    *,
    audit_interval_hours: int = 24,
) -> int:
    """Register roots without touching the filesystem or recursively scanning it."""
    roots = sorted({os.path.normpath(str(path)) for path in root_paths or [] if str(path or '').strip()})
    if not roots:
        return 0
    interval_hours = max(1, min(int(audit_interval_hours), 24 * 30))
    interval_seconds = interval_hours * 3600
    with get_db_connection() as conn:
        with conn.cursor() as cursor:
            for root in roots:
                cursor.execute(
                    """
                    INSERT INTO strm_ingest_inventory_roots (root_path)
                    VALUES (%s)
                    ON CONFLICT (root_path) DO NOTHING
                    """,
                    (root,),
                )
                cursor.execute(
                    """
                    INSERT INTO strm_ingest_inventory_directories (
                        root_path, directory_path, parent_path, active, next_audit_at
                    ) VALUES (%s, %s, NULL, TRUE, NOW() + (%s * INTERVAL '1 hour'))
                    ON CONFLICT (root_path, directory_path) DO UPDATE
                    SET active = TRUE, updated_at = NOW()
                    """,
                    (root, root, interval_hours),
                )
                like_root = root.replace('\\', '\\\\').replace('%', '\\%').replace('_', '\\_') + '/%'
                cursor.execute(
                    """
                    UPDATE strm_ingest_retry_queue
                    SET inventory_root_path = %s,
                        inventory_directory_path = regexp_replace(file_path, '/[^/]+$', ''),
                        updated_at = updated_at
                    WHERE inventory_root_path IS NULL
                      AND file_path LIKE %s ESCAPE '\\'
                      AND LOWER(file_path) LIKE '%%.strm'
                    """,
                    (root, like_root),
                )
                cursor.execute(
                    """
                    INSERT INTO strm_ingest_inventory_directories (
                        root_path, directory_path, parent_path, active, next_audit_at
                    )
                    SELECT DISTINCT
                        %s,
                        q.inventory_directory_path,
                        CASE
                            WHEN q.inventory_directory_path = %s THEN NULL
                            ELSE regexp_replace(q.inventory_directory_path, '/[^/]+$', '')
                        END,
                        TRUE,
                        NOW() + (
                            MOD(ABS(hashtext(q.inventory_directory_path)::bigint), %s)
                            * INTERVAL '1 second'
                        )
                    FROM strm_ingest_retry_queue q
                    WHERE q.inventory_root_path = %s
                      AND q.inventory_directory_path IS NOT NULL
                      AND LOWER(q.file_path) LIKE '%%.strm'
                    ON CONFLICT (root_path, directory_path) DO NOTHING
                    """,
                    (root, root, interval_seconds, root),
                )
    return len(roots)


def mark_directory_dirty(root_path: str, directory_path: str, *, event_kind: str = 'watchdog') -> bool:
    root = os.path.normpath(str(root_path))
    directory = os.path.normpath(str(directory_path))
    parent = None if directory == root else os.path.dirname(directory)
    with get_db_connection() as conn:
        with conn.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO strm_ingest_inventory_directories (
                    root_path, directory_path, parent_path, active, dirty,
                    dirty_since, last_event_at, next_audit_at, event_version, last_error
                ) VALUES (%s, %s, %s, TRUE, TRUE, NOW(), NOW(), NOW(), 1, NULL)
                ON CONFLICT (root_path, directory_path) DO UPDATE
                SET parent_path = EXCLUDED.parent_path,
                    active = TRUE,
                    dirty = TRUE,
                    dirty_since = COALESCE(strm_ingest_inventory_directories.dirty_since, NOW()),
                    last_event_at = NOW(),
                    next_audit_at = NOW(),
                    audit_cursor = NULL,
                    event_version = strm_ingest_inventory_directories.event_version + 1,
                    claim_owner = NULL,
                    claim_expires_at = NULL,
                    manual_audit_id = NULL,
                    last_error = NULL,
                    updated_at = NOW()
                """,
                (root, directory, parent),
            )
            return cursor.rowcount == 1


def record_file_event(root_path: str, file_path: str, *, event_kind: str) -> None:
    """Persist exact-file ownership without scheduling a directory scandir."""
    root = os.path.normpath(str(root_path))
    path = os.path.normpath(str(file_path))
    directory = os.path.dirname(path)
    parent = None if directory == root else os.path.dirname(directory)
    with get_db_connection() as conn:
        with conn.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO strm_ingest_inventory_directories (
                    root_path, directory_path, parent_path, active, dirty,
                    last_event_at, next_audit_at
                ) VALUES (%s, %s, %s, TRUE, FALSE, NOW(), NOW() + INTERVAL '24 hours')
                ON CONFLICT (root_path, directory_path) DO UPDATE
                SET parent_path = EXCLUDED.parent_path,
                    active = TRUE,
                    last_event_at = NOW(),
                    updated_at = NOW()
                """,
                (root, directory, parent),
            )
            cursor.execute(
                """
                UPDATE strm_ingest_retry_queue
                SET inventory_root_path = %s,
                    inventory_directory_path = %s,
                    updated_at = NOW()
                WHERE file_path = %s
                """,
                (root, directory, path),
            )


def record_directory_created(root_path: str, directory_path: str) -> None:
    root = os.path.normpath(str(root_path))
    directory = os.path.normpath(str(directory_path))
    mark_directory_dirty(root, os.path.dirname(directory), event_kind='directory_created')
    mark_directory_dirty(root, directory, event_kind='directory_created')


def record_directory_removed(root_path: str, directory_path: str) -> List[str]:
    root = os.path.normpath(str(root_path))
    directory = os.path.normpath(str(directory_path))
    paths = list_active_paths_under(directory)
    like_directory = directory.replace('\\', '\\\\').replace('%', '\\%').replace('_', '\\_') + '/%'
    with get_db_connection() as conn:
        with conn.cursor() as cursor:
            cursor.execute(
                """
                UPDATE strm_ingest_inventory_directories
                SET active = FALSE, dirty = FALSE, claim_owner = NULL,
                    claim_expires_at = NULL, updated_at = NOW()
                WHERE root_path = %s
                  AND (directory_path = %s OR directory_path LIKE %s ESCAPE '\\')
                """,
                (root, directory, like_directory),
            )
    mark_directory_dirty(root, os.path.dirname(directory), event_kind='directory_removed')
    return paths


def record_directory_moved(root_path: str, old_path: str, new_path: str) -> List[Tuple[str, str]]:
    """Map known inventory paths to the destination without walking the moved tree."""
    root = os.path.normpath(str(root_path))
    old = os.path.normpath(str(old_path))
    new = os.path.normpath(str(new_path))
    known = list_active_paths_under(old)
    pairs = [(path, os.path.normpath(new + path[len(old):])) for path in known]
    old_like = old.replace('\\', '\\\\').replace('%', '\\%').replace('_', '\\_') + '/%'
    with get_db_connection() as conn:
        with conn.cursor() as cursor:
            cursor.execute(
                """
                SELECT directory_path
                FROM strm_ingest_inventory_directories
                WHERE root_path = %s
                  AND active = TRUE
                  AND (directory_path = %s OR directory_path LIKE %s ESCAPE '\\')
                ORDER BY directory_path
                """,
                (root, old, old_like),
            )
            directories = [row['directory_path'] for row in cursor.fetchall()]
            for directory in directories or [old]:
                mapped = os.path.normpath(new + directory[len(old):])
                parent = None if mapped == root else os.path.dirname(mapped)
                cursor.execute(
                    """
                    INSERT INTO strm_ingest_inventory_directories (
                        root_path, directory_path, parent_path, active, dirty,
                        dirty_since, last_event_at, next_audit_at, event_version
                    ) VALUES (%s, %s, %s, TRUE, TRUE, NOW(), NOW(), NOW(), 1)
                    ON CONFLICT (root_path, directory_path) DO UPDATE
                    SET parent_path = EXCLUDED.parent_path, active = TRUE, dirty = TRUE,
                        dirty_since = COALESCE(strm_ingest_inventory_directories.dirty_since, NOW()),
                        last_event_at = NOW(), next_audit_at = NOW(), audit_cursor = NULL,
                        event_version = strm_ingest_inventory_directories.event_version + 1,
                        claim_owner = NULL, claim_expires_at = NULL,
                        manual_audit_id = NULL, updated_at = NOW()
                    """,
                    (root, mapped, parent),
                )
            cursor.execute(
                """
                UPDATE strm_ingest_inventory_directories
                SET active = FALSE, dirty = FALSE, claim_owner = NULL,
                    claim_expires_at = NULL, updated_at = NOW()
                WHERE root_path = %s
                  AND (directory_path = %s OR directory_path LIKE %s ESCAPE '\\')
                """,
                (root, old, old_like),
            )
    mark_directory_dirty(root, os.path.dirname(old), event_kind='directory_move_old')
    mark_directory_dirty(root, os.path.dirname(new), event_kind='directory_move_new')
    mark_directory_dirty(root, new, event_kind='directory_move_new')
    return pairs


def claim_inventory_directories(
    owner: str,
    *,
    limit: int = 4,
    lease_seconds: int = 120,
    manual_audit_id: Optional[str] = None,
) -> List[Dict]:
    safe_limit = max(1, min(int(limit), 32))
    safe_lease = max(30, min(int(lease_seconds), 900))
    with get_db_connection() as conn:
        with conn.cursor() as cursor:
            cursor.execute(
                """
                WITH due AS (
                    SELECT root_path, directory_path
                    FROM strm_ingest_inventory_directories candidate
                    WHERE candidate.active = TRUE
                      AND candidate.dirty = TRUE
                      AND candidate.next_audit_at <= NOW()
                      AND (
                          candidate.claim_expires_at IS NULL
                          OR candidate.claim_expires_at <= NOW()
                      )
                      AND (
                          (%s IS NULL AND candidate.manual_audit_id IS NULL)
                          OR (
                              candidate.manual_audit_id = %s
                              AND EXISTS (
                                  SELECT 1
                                  FROM strm_ingest_inventory_manual_audits a
                                  WHERE a.audit_id = candidate.manual_audit_id
                                    AND a.state IN ('queued', 'running')
                              )
                          )
                      )
                    ORDER BY candidate.dirty DESC,
                             candidate.next_audit_at ASC,
                             candidate.directory_path ASC
                    LIMIT %s
                    FOR UPDATE SKIP LOCKED
                )
                UPDATE strm_ingest_inventory_directories d
                SET claim_owner = %s,
                    claim_expires_at = NOW() + (%s * INTERVAL '1 second'),
                    audit_generation = d.audit_generation + 1,
                    audit_cursor = NULL,
                    updated_at = NOW()
                FROM due
                WHERE d.root_path = due.root_path
                  AND d.directory_path = due.directory_path
                RETURNING d.*
                """,
                (manual_audit_id, manual_audit_id, safe_limit, owner, safe_lease),
            )
            return [dict(row) for row in cursor.fetchall()]


def get_inventory_files_for_directory(root_path: str, directory_path: str) -> Dict[str, Dict]:
    root = os.path.normpath(str(root_path))
    directory = os.path.normpath(str(directory_path))
    with get_db_connection() as conn:
        with conn.cursor() as cursor:
            cursor.execute(
                """
                SELECT file_path, file_size, file_mtime, status, operation,
                       inventory_seen_generation
                FROM strm_ingest_retry_queue
                WHERE inventory_root_path = %s
                  AND inventory_directory_path = %s
                  AND LOWER(file_path) LIKE '%%.strm'
                """,
                (root, directory),
            )
            return {row['file_path']: dict(row) for row in cursor.fetchall()}


def get_inventory_ancestor_candidates(root_path: str, directory_path: str) -> List[Dict]:
    """Return persisted lexical ancestors, deepest first, for proof-only recovery."""
    root = os.path.normpath(str(root_path))
    directory = os.path.normpath(str(directory_path))
    if _first_hop_below(root, root, directory) is None:
        return []
    with get_db_connection() as conn:
        with conn.cursor() as cursor:
            cursor.execute(
                """
                SELECT root_path, directory_path, parent_path, active,
                       event_version, manual_audit_id, last_verified_at,
                       last_error, dirty, claim_owner, claim_expires_at
                FROM strm_ingest_inventory_directories
                WHERE root_path = %s
                  AND directory_path != %s
                  AND (
                      directory_path = %s
                      OR LEFT(%s, LENGTH(directory_path) + 1)
                          = directory_path || '/'
                  )
                ORDER BY LENGTH(directory_path) DESC, directory_path
                """,
                (root, directory, root, directory),
            )
            return [dict(row) for row in cursor.fetchall()]


def _queue_inventory_removed_files(cursor, removed: Iterable[str]) -> List[str]:
    paths = sorted(set(removed or []))
    if paths:
        cursor.execute(
            """
            UPDATE strm_ingest_retry_queue
            SET operation = 'delete', source = 'inventory_v2', status = 'pending',
                attempt_count = 0,
                next_attempt_at = NOW() + (600 * INTERVAL '1 second'),
                updated_at = NOW(), completed_at = NULL,
                last_error = '增量目录库存发现 STRM 已消失'
            WHERE file_path = ANY(%s)
            """,
            (paths,),
        )
    return paths


def _terminalize_missing_first_hop_branches(
    cursor,
    *,
    root: str,
    directory: str,
    observed_entry_paths: Iterable[str],
    manual_audit_id: Optional[str],
    include_generation_descendants: bool = False,
) -> Dict[str, object]:
    """Apply deletion proof from one successful, complete directory snapshot.

    A descendant is terminal only when the first-hop branch below the scanned
    directory is absent from the complete snapshot.  All entry names, including
    symlinks and non-directories, block proof for that branch.
    """
    directory_like = _escape_like_path(directory) + '/%'
    cursor.execute(
        """
        SELECT directory_path
        FROM strm_ingest_inventory_directories
        WHERE root_path = %s
          AND active = TRUE
          AND parent_path = %s
        """,
        (root, directory),
    )
    active_descendants = {row['directory_path'] for row in cursor.fetchall()}
    if manual_audit_id and include_generation_descendants:
        cursor.execute(
            """
            SELECT directory_path
            FROM strm_ingest_inventory_directories
            WHERE root_path = %s
              AND active = TRUE
              AND manual_audit_id = %s
              AND directory_path LIKE %s ESCAPE '\\'
            """,
            (root, manual_audit_id, directory_like),
        )
        active_descendants.update(row['directory_path'] for row in cursor.fetchall())
    descendant_branches = {
        branch
        for path in active_descendants
        for branch in [_first_hop_below(root, directory, path)]
        if branch
    }
    observed_branches = {
        branch
        for path in observed_entry_paths or []
        for branch in [_first_hop_below(root, directory, path)]
        if branch
    }
    missing_branches = sorted(descendant_branches - observed_branches)
    removed = []
    manual_terminalized = 0
    for branch in missing_branches:
        branch_like = _escape_like_path(branch) + '/%'
        cursor.execute(
            """
            SELECT file_path FROM strm_ingest_retry_queue
            WHERE inventory_root_path = %s
              AND (
                  inventory_directory_path = %s
                  OR inventory_directory_path LIKE %s ESCAPE '\\'
              )
              AND LOWER(file_path) LIKE '%%.strm'
              AND operation != 'delete'
              AND status NOT IN ('deleted', 'cancelled')
            """,
            (root, branch, branch_like),
        )
        removed.extend(row['file_path'] for row in cursor.fetchall())
        if manual_audit_id:
            cursor.execute(
                """
                SELECT COUNT(*) AS count
                FROM strm_ingest_inventory_directories
                WHERE root_path = %s
                  AND active = TRUE
                  AND manual_audit_id = %s
                  AND (
                      directory_path = %s
                      OR directory_path LIKE %s ESCAPE '\\'
                  )
                """,
                (root, manual_audit_id, branch, branch_like),
            )
            manual_terminalized += int((cursor.fetchone() or {}).get('count') or 0)
        cursor.execute(
            """
            UPDATE strm_ingest_inventory_directories
            SET active = FALSE, dirty = FALSE, dirty_since = NULL,
                claim_owner = NULL, claim_expires_at = NULL,
                manual_audit_id = CASE
                    WHEN manual_audit_id = %s THEN NULL
                    ELSE manual_audit_id
                END,
                last_error = NULL, updated_at = NOW()
            WHERE root_path = %s
              AND (
                  directory_path = %s
                  OR directory_path LIKE %s ESCAPE '\\'
              )
            """,
            (manual_audit_id, root, branch, branch_like),
        )
    return {
        'missing_branches': missing_branches,
        'removed': sorted(set(removed)),
        'manual_terminalized': manual_terminalized,
        'watch_set_changed': bool(missing_branches),
    }


def _advance_manual_inventory_progress(cursor, audit_id: Optional[str], delta: int) -> None:
    if not audit_id or int(delta or 0) <= 0:
        return
    cursor.execute(
        """
        UPDATE strm_ingest_inventory_manual_audits
        SET state = 'running',
            started_at = COALESCE(started_at, NOW()),
            completed_directories = completed_directories + %s,
            total_directories = completed_directories + %s + (
                SELECT COUNT(*)
                FROM strm_ingest_inventory_directories
                WHERE active = TRUE AND manual_audit_id = %s
            ),
            updated_at = NOW()
        WHERE audit_id = %s AND state IN ('queued', 'running')
        """,
        (int(delta), int(delta), audit_id, audit_id),
    )


def record_inventory_audit_batch(
    claim: Dict,
    *,
    files: Dict[str, Tuple[int, float]],
    child_directories: Iterable[str],
    observed_entry_paths: Optional[Iterable[str]] = None,
    next_cursor: Optional[str],
    complete: bool,
    db_batch_size: int = 500,
    audit_interval_hours: int,
) -> Dict[str, object]:
    """Persist one complete physical directory snapshot in bounded SQL batches."""
    root = os.path.normpath(claim['root_path'])
    directory = os.path.normpath(claim['directory_path'])
    generation = int(claim['audit_generation'])
    event_version = int(claim['event_version'])
    owner = claim['claim_owner']
    manual_audit_id = claim.get('manual_audit_id')
    safe_batch_size = max(1, min(int(db_batch_size), 500))
    existing = get_inventory_files_for_directory(root, directory)
    added, changed = [], []
    for path, fingerprint in files.items():
        row = existing.get(path)
        if not row or row.get('operation') == 'delete' or row.get('status') in {'deleted', 'cancelled'}:
            added.append(path)
        elif row.get('file_size') != fingerprint[0] or row.get('file_mtime') != fingerprint[1]:
            changed.append(path)

    file_rows = []
    for path, (size, mtime) in sorted(files.items()):
        pending = path in added or path in changed
        file_rows.append(
            (
                path,
                'pending' if pending else 'observed',
                size,
                mtime,
                root,
                directory,
                generation,
            )
        )
    children = sorted({os.path.normpath(path) for path in child_directories})
    observed_entries = sorted({
        os.path.normpath(path)
        for path in (
            observed_entry_paths
            if observed_entry_paths is not None
            else list(children) + list(files)
        )
    })
    db_batches = 0
    watch_set_changed = False

    with get_db_connection() as conn:
        with conn.cursor() as cursor:
            cursor.execute(
                """
                SELECT event_version, manual_audit_id
                FROM strm_ingest_inventory_directories
                WHERE root_path = %s AND directory_path = %s
                  AND claim_owner = %s
                FOR UPDATE
                """,
                (root, directory, owner),
            )
            current = cursor.fetchone()
            if not current:
                return {'accepted': False, 'stale': True, 'added': [], 'changed': [], 'removed': []}
            if (
                int(current['event_version']) != event_version
                or current.get('manual_audit_id') != manual_audit_id
            ):
                cursor.execute(
                    """
                    UPDATE strm_ingest_inventory_directories
                    SET dirty = TRUE, audit_cursor = NULL, claim_owner = NULL,
                        claim_expires_at = NULL, next_audit_at = NOW(), updated_at = NOW()
                    WHERE root_path = %s AND directory_path = %s AND claim_owner = %s
                    """,
                    (root, directory, owner),
                )
                return {'accepted': False, 'stale': True, 'added': [], 'changed': [], 'removed': []}

            for offset in range(0, len(file_rows), safe_batch_size):
                execute_values(
                    cursor,
                    """
                    INSERT INTO strm_ingest_retry_queue (
                        file_path, operation, source, status, next_attempt_at,
                        file_size, file_mtime, inventory_root_path,
                        inventory_directory_path, inventory_seen_generation
                    ) VALUES %s
                    ON CONFLICT (file_path) DO UPDATE
                    SET operation = CASE WHEN EXCLUDED.status = 'pending' THEN 'ingest' ELSE strm_ingest_retry_queue.operation END,
                        source = CASE WHEN EXCLUDED.status = 'pending' THEN 'inventory_v2' ELSE strm_ingest_retry_queue.source END,
                        status = CASE WHEN EXCLUDED.status = 'pending' THEN 'pending' ELSE strm_ingest_retry_queue.status END,
                        attempt_count = CASE WHEN EXCLUDED.status = 'pending' THEN 0 ELSE strm_ingest_retry_queue.attempt_count END,
                        next_attempt_at = CASE
                            WHEN EXCLUDED.status = 'pending' THEN NOW() + (600 * INTERVAL '1 second')
                            ELSE strm_ingest_retry_queue.next_attempt_at
                        END,
                        completed_at = CASE
                            WHEN EXCLUDED.status = 'pending' THEN NULL
                            ELSE strm_ingest_retry_queue.completed_at
                        END,
                        file_size = EXCLUDED.file_size,
                        file_mtime = EXCLUDED.file_mtime,
                        inventory_root_path = EXCLUDED.inventory_root_path,
                        inventory_directory_path = EXCLUDED.inventory_directory_path,
                        inventory_seen_generation = EXCLUDED.inventory_seen_generation,
                        updated_at = NOW()
                    """,
                    file_rows[offset : offset + safe_batch_size],
                    template=(
                        "(%s, 'ingest', 'inventory_v2', %s, "
                        "NOW() + (600 * INTERVAL '1 second'), %s, %s, %s, %s, %s)"
                    ),
                    page_size=safe_batch_size,
                )
                db_batches += 1

            existing_active_children = set()
            if children:
                cursor.execute(
                    """
                    SELECT directory_path
                    FROM strm_ingest_inventory_directories
                    WHERE root_path = %s
                      AND directory_path = ANY(%s)
                      AND active = TRUE
                    """,
                    (root, children),
                )
                existing_active_children = {
                    row['directory_path'] for row in cursor.fetchall()
                }
                watch_set_changed = bool(
                    set(children) - existing_active_children
                )

            child_rows = [
                (root, child, directory, generation, manual_audit_id)
                for child in children
            ]
            for offset in range(0, len(child_rows), safe_batch_size):
                execute_values(
                    cursor,
                    """
                    INSERT INTO strm_ingest_inventory_directories (
                        root_path, directory_path, parent_path, active, dirty,
                        next_audit_at, seen_generation, manual_audit_id
                    ) VALUES %s
                    ON CONFLICT (root_path, directory_path) DO UPDATE
                    SET parent_path = EXCLUDED.parent_path, active = TRUE,
                        dirty = CASE
                            WHEN NOT strm_ingest_inventory_directories.active THEN TRUE
                            WHEN EXCLUDED.manual_audit_id IS NOT NULL
                                 AND strm_ingest_inventory_directories.manual_audit_id
                                     = EXCLUDED.manual_audit_id
                                THEN TRUE
                            ELSE strm_ingest_inventory_directories.dirty
                        END,
                        dirty_since = CASE
                            WHEN NOT strm_ingest_inventory_directories.active
                                 OR (
                                     EXCLUDED.manual_audit_id IS NOT NULL
                                     AND strm_ingest_inventory_directories.manual_audit_id
                                         = EXCLUDED.manual_audit_id
                                 )
                                THEN COALESCE(
                                    strm_ingest_inventory_directories.dirty_since,
                                    NOW()
                                )
                            ELSE strm_ingest_inventory_directories.dirty_since
                        END,
                        next_audit_at = CASE
                            WHEN NOT strm_ingest_inventory_directories.active
                                 OR (
                                     EXCLUDED.manual_audit_id IS NOT NULL
                                     AND strm_ingest_inventory_directories.manual_audit_id
                                         = EXCLUDED.manual_audit_id
                                 ) THEN NOW()
                            ELSE strm_ingest_inventory_directories.next_audit_at
                        END,
                        seen_generation = EXCLUDED.seen_generation,
                        manual_audit_id = CASE
                            WHEN NOT strm_ingest_inventory_directories.active
                                THEN EXCLUDED.manual_audit_id
                            ELSE strm_ingest_inventory_directories.manual_audit_id
                        END,
                        updated_at = NOW()
                    """,
                    child_rows[offset : offset + safe_batch_size],
                    template=(
                        "(%s, %s, %s, TRUE, TRUE, NOW(), %s, %s)"
                    ),
                    page_size=safe_batch_size,
                )
                db_batches += 1

            removed = []
            if complete:
                cursor.execute(
                    """
                    SELECT file_path
                    FROM strm_ingest_retry_queue
                    WHERE inventory_root_path = %s
                      AND inventory_directory_path = %s
                      AND LOWER(file_path) LIKE '%%.strm'
                      AND inventory_seen_generation != %s
                      AND operation != 'delete'
                      AND status NOT IN ('deleted', 'cancelled')
                    """,
                    (root, directory, generation),
                )
                removed.extend(row['file_path'] for row in cursor.fetchall())
                terminalized = _terminalize_missing_first_hop_branches(
                    cursor,
                    root=root,
                    directory=directory,
                observed_entry_paths=observed_entries,
                manual_audit_id=manual_audit_id,
                include_generation_descendants=False,
                )
                removed.extend(terminalized['removed'])
                removed = sorted(set(removed))
                _queue_inventory_removed_files(cursor, removed)
                watch_set_changed = bool(
                    watch_set_changed or terminalized['watch_set_changed']
                )
                manual_terminalized = int(terminalized['manual_terminalized'])
                cursor.execute(
                    """
                    UPDATE strm_ingest_inventory_directories
                    SET dirty = FALSE, dirty_since = NULL, last_verified_at = NOW(),
                        next_audit_at = NOW() + (%s * INTERVAL '1 hour'),
                        audit_cursor = NULL, claim_owner = NULL, claim_expires_at = NULL,
                        manual_audit_id = CASE
                            WHEN manual_audit_id = %s THEN NULL
                            ELSE manual_audit_id
                        END,
                        last_error = NULL, updated_at = NOW()
                    WHERE root_path = %s AND directory_path = %s AND claim_owner = %s
                    """,
                    (
                        max(1, int(audit_interval_hours)), manual_audit_id,
                        root, directory, owner,
                    ),
                )
                if cursor.rowcount == 1:
                    _advance_manual_inventory_progress(
                        cursor,
                        manual_audit_id,
                        1 + manual_terminalized,
                    )
            else:
                cursor.execute(
                    """
                    UPDATE strm_ingest_inventory_directories
                    SET dirty = TRUE, audit_cursor = %s, next_audit_at = NOW(),
                        claim_owner = NULL, claim_expires_at = NULL, updated_at = NOW()
                    WHERE root_path = %s AND directory_path = %s AND claim_owner = %s
                    """,
                    (next_cursor, root, directory, owner),
                )
            return {
                'accepted': True,
                'stale': False,
                'added': sorted(added),
                'changed': sorted(changed),
                'removed': removed if complete else [],
                'complete': complete,
                'db_batches': db_batches,
                'directories_discovered': len(children),
                'watch_set_changed': watch_set_changed,
            }


def record_inventory_ancestor_proof(
    claim: Dict,
    *,
    ancestor: Dict,
    observed_entry_paths: Iterable[str],
) -> Dict[str, object]:
    """Terminalize a missing branch proven by a fresh ancestor snapshot.

    The missing claim remains the ownership fence.  The ancestor row's event
    version is also compared after scandir so a concurrent watcher event makes
    the proof stale instead of permitting deletion.
    """
    root = os.path.normpath(str(claim.get('root_path') or ''))
    missing_directory = os.path.normpath(str(claim.get('directory_path') or ''))
    ancestor_directory = os.path.normpath(str(ancestor.get('directory_path') or ''))
    manual_audit_id = claim.get('manual_audit_id')
    if (
        not manual_audit_id
        or _first_hop_below(root, ancestor_directory, missing_directory) is None
    ):
        return {'accepted': False, 'stale': True, 'proven': False, 'removed': []}
    claim_branch = _first_hop_below(root, ancestor_directory, missing_directory)
    observed_branches = {
        branch
        for path in observed_entry_paths or []
        for branch in [_first_hop_below(root, ancestor_directory, path)]
        if branch
    }
    if claim_branch in observed_branches:
        return {'accepted': False, 'stale': False, 'proven': False, 'removed': []}

    with get_db_connection() as conn:
        with conn.cursor() as cursor:
            cursor.execute(
                """
                SELECT event_version, manual_audit_id
                FROM strm_ingest_inventory_directories
                WHERE root_path = %s AND directory_path = %s
                  AND claim_owner = %s
                FOR UPDATE
                """,
                (root, missing_directory, claim.get('claim_owner')),
            )
            current_claim = cursor.fetchone()
            if (
                not current_claim
                or int(current_claim.get('event_version') or 0)
                    != int(claim.get('event_version') or 0)
                or current_claim.get('manual_audit_id') != manual_audit_id
            ):
                return {'accepted': False, 'stale': True, 'proven': False, 'removed': []}

            cursor.execute(
                """
                SELECT event_version
                FROM strm_ingest_inventory_directories
                WHERE root_path = %s AND directory_path = %s
                FOR UPDATE
                """,
                (root, ancestor_directory),
            )
            current_ancestor = cursor.fetchone()
            if (
                not current_ancestor
                or int(current_ancestor.get('event_version') or 0)
                    != int(ancestor.get('event_version') or 0)
            ):
                return {'accepted': False, 'stale': True, 'proven': False, 'removed': []}

            terminalized = _terminalize_missing_first_hop_branches(
                cursor,
                root=root,
                directory=ancestor_directory,
                observed_entry_paths=observed_entry_paths,
                manual_audit_id=manual_audit_id,
                include_generation_descendants=True,
            )
            missing_branches = terminalized['missing_branches']
            proven = claim_branch in missing_branches
            if not proven:
                return {
                    'accepted': False,
                    'stale': False,
                    'proven': False,
                    'removed': [],
                }

            removed = _queue_inventory_removed_files(cursor, terminalized['removed'])
            cursor.execute(
                """
                UPDATE strm_ingest_inventory_directories
                SET active = TRUE, dirty = FALSE, dirty_since = NULL,
                    last_verified_at = NOW(), last_error = NULL,
                    updated_at = NOW()
                WHERE root_path = %s AND directory_path = %s
                """,
                (root, ancestor_directory),
            )
            _advance_manual_inventory_progress(
                cursor,
                manual_audit_id,
                int(terminalized['manual_terminalized']),
            )
            return {
                'accepted': True,
                'stale': False,
                'proven': True,
                'complete': True,
                'removed': removed,
                'missing_branches': missing_branches,
                'manual_terminalized': int(terminalized['manual_terminalized']),
                'watch_set_changed': True,
            }


def fail_inventory_directory_claim(claim: Dict, error: str, *, delay_seconds: int = 300) -> None:
    with get_db_connection() as conn:
        with conn.cursor() as cursor:
            cursor.execute(
                """
                UPDATE strm_ingest_inventory_directories
                SET dirty = TRUE, claim_owner = NULL, claim_expires_at = NULL,
                    next_audit_at = NOW() + (%s * INTERVAL '1 second'),
                    last_error = %s, updated_at = NOW()
                WHERE root_path = %s AND directory_path = %s AND claim_owner = %s
                """,
                (
                    max(30, min(int(delay_seconds), 3600)), str(error or '')[:1000],
                    claim['root_path'], claim['directory_path'], claim['claim_owner'],
                ),
            )


def release_inventory_directory_claims(claims: Iterable[Dict]) -> int:
    """Release leased-but-unstarted directories without changing generation ownership."""
    rows = [
        (
            str(claim.get('root_path') or ''),
            str(claim.get('directory_path') or ''),
            str(claim.get('claim_owner') or ''),
        )
        for claim in claims or []
        if claim.get('root_path') and claim.get('directory_path') and claim.get('claim_owner')
    ]
    if not rows:
        return 0
    with get_db_connection() as conn:
        with conn.cursor() as cursor:
            execute_values(
                cursor,
                """
                UPDATE strm_ingest_inventory_directories AS directory
                SET dirty = TRUE,
                    next_audit_at = NOW(),
                    claim_owner = NULL,
                    claim_expires_at = NULL,
                    updated_at = NOW()
                FROM (VALUES %s) AS released(root_path, directory_path, claim_owner)
                WHERE directory.root_path = released.root_path
                  AND directory.directory_path = released.directory_path
                  AND directory.claim_owner = released.claim_owner
                """,
                rows,
                template='(%s, %s, %s)',
                page_size=min(len(rows), 500),
            )
            return int(cursor.rowcount or 0)


def request_full_inventory_audit(root_paths: Iterable[str]) -> int:
    """Explicitly schedule a logical full audit; workers still scan bounded directories."""
    roots = sorted({os.path.normpath(str(path)) for path in root_paths or [] if str(path or '').strip()})
    if not roots:
        return 0
    register_inventory_roots(roots)
    with get_db_connection() as conn:
        with conn.cursor() as cursor:
            cursor.execute(
                """
                UPDATE strm_ingest_inventory_directories
                SET dirty = TRUE, dirty_since = COALESCE(dirty_since, NOW()),
                    next_audit_at = NOW(), audit_cursor = NULL,
                    event_version = event_version + 1,
                    claim_owner = NULL, claim_expires_at = NULL,
                    updated_at = NOW()
                WHERE active = TRUE AND root_path = ANY(%s)
                """,
                (roots,),
            )
            return cursor.rowcount


def create_manual_inventory_audit(root_paths: Iterable[str]) -> Dict[str, object]:
    """Create or resume the one persisted manual Inventory generation."""
    roots = sorted({os.path.normpath(str(path)) for path in root_paths or [] if str(path or '').strip()})
    if not roots:
        raise ValueError('manual inventory audit requires at least one root')
    register_inventory_roots(roots)
    audit_id = uuid.uuid4().hex
    with get_db_connection() as conn:
        with conn.cursor() as cursor:
            cursor.execute(
                "SELECT pg_advisory_xact_lock(hashtext('strm_inventory_manual_audit'))"
            )
            cursor.execute(
                """
                SELECT audit_id
                FROM strm_ingest_inventory_manual_audits
                WHERE state IN ('queued', 'running')
                ORDER BY created_at
                LIMIT 1
                FOR UPDATE
                """
            )
            existing = cursor.fetchone()
            if existing:
                return {
                    'audit_id': existing['audit_id'],
                    'existing': True,
                }
            cursor.execute(
                """
                INSERT INTO strm_ingest_inventory_manual_audits (audit_id, state)
                VALUES (%s, 'queued')
                """,
                (audit_id,),
            )
            cursor.execute(
                """
                UPDATE strm_ingest_inventory_directories
                SET dirty = TRUE,
                    dirty_since = COALESCE(dirty_since, NOW()),
                    next_audit_at = NOW(),
                    audit_cursor = NULL,
                    event_version = event_version + 1,
                    claim_owner = NULL,
                    claim_expires_at = NULL,
                    manual_audit_id = %s,
                    updated_at = NOW()
                WHERE active = TRUE
                  AND root_path = ANY(%s)
                  AND (claim_owner IS NULL OR claim_expires_at <= NOW())
                  AND (
                      (dirty = FALSE AND manual_audit_id IS NULL)
                      OR EXISTS (
                          SELECT 1
                          FROM strm_ingest_inventory_manual_audits previous
                          WHERE previous.audit_id = strm_ingest_inventory_directories.manual_audit_id
                            AND previous.state = 'cancelled'
                      )
                  )
                """,
                (audit_id, roots),
            )
            total = int(cursor.rowcount or 0)
            cursor.execute(
                """
                UPDATE strm_ingest_inventory_manual_audits
                SET total_directories = %s,
                    state = CASE WHEN %s = 0 THEN 'completed' ELSE state END,
                    completed_at = CASE WHEN %s = 0 THEN NOW() ELSE NULL END,
                    updated_at = NOW()
                WHERE audit_id = %s
                """,
                (total, total, total, audit_id),
            )
    return {'audit_id': audit_id, 'existing': False, 'total_directories': total}


def list_active_manual_inventory_audits() -> List[str]:
    with get_db_connection() as conn:
        with conn.cursor() as cursor:
            cursor.execute(
                """
                SELECT audit_id
                FROM strm_ingest_inventory_manual_audits
                WHERE state IN ('queued', 'running')
                ORDER BY created_at
                """
            )
            return [str(row['audit_id']) for row in cursor.fetchall()]


def get_manual_inventory_audit(audit_id: str) -> Optional[Dict[str, object]]:
    """Refresh and return durable progress for one manual audit generation."""
    safe_id = str(audit_id or '').strip()
    if not safe_id:
        return None
    with get_db_connection() as conn:
        with conn.cursor() as cursor:
            cursor.execute(
                """
                SELECT *
                FROM strm_ingest_inventory_manual_audits
                WHERE audit_id = %s
                FOR UPDATE
                """,
                (safe_id,),
            )
            audit = cursor.fetchone()
            if not audit:
                return None
            if str(audit.get('state') or '') in {'queued', 'running'}:
                # v7.2.13 could persist active generation ownership on a clean
                # row. Such a row was counted as pending but could never be
                # claimed. Repair the invalid state itself instead of hiding it
                # in progress SQL: every active generation row is claimable.
                cursor.execute(
                    """
                    UPDATE strm_ingest_inventory_directories
                    SET dirty = TRUE,
                        dirty_since = COALESCE(dirty_since, NOW()),
                        next_audit_at = NOW(),
                        claim_owner = NULL,
                        claim_expires_at = NULL,
                        last_error = COALESCE(
                            last_error,
                            'manual generation pending ownership repaired'
                        ),
                        updated_at = NOW()
                    WHERE manual_audit_id = %s
                      AND active = TRUE
                      AND dirty = FALSE
                    """,
                    (safe_id,),
                )
            cursor.execute(
                """
                SELECT
                    COUNT(*) FILTER (WHERE active = TRUE) AS pending_directories,
                    COUNT(*) FILTER (
                        WHERE active = TRUE AND claim_expires_at > NOW()
                    ) AS claimed_directories
                FROM strm_ingest_inventory_directories
                WHERE manual_audit_id = %s
                """,
                (safe_id,),
            )
            counts = cursor.fetchone() or {}
            pending = int(counts.get('pending_directories') or 0)
            claimed = int(counts.get('claimed_directories') or 0)
            completed = int(audit.get('completed_directories') or 0)
            total = completed + pending
            state = str(audit.get('state') or 'queued')
            if state in {'queued', 'running'} and pending == 0:
                state = 'completed'
                cursor.execute(
                    """
                    UPDATE strm_ingest_inventory_manual_audits
                    SET state = 'completed', total_directories = %s,
                        completed_at = NOW(), updated_at = NOW()
                    WHERE audit_id = %s AND state IN ('queued', 'running')
                    """,
                    (total, safe_id),
                )
            elif total != int(audit.get('total_directories') or 0):
                cursor.execute(
                    """
                    UPDATE strm_ingest_inventory_manual_audits
                    SET total_directories = %s, updated_at = NOW()
                    WHERE audit_id = %s
                    """,
                    (total, safe_id),
                )
            progress = 100 if state == 'completed' else (
                min(99, int((completed * 100) / total)) if total else 0
            )
            return {
                **dict(audit),
                'state': state,
                'total_directories': total,
                'completed_directories': completed,
                'pending_directories': pending,
                'claimed_directories': claimed,
                'progress': progress,
            }


def cancel_manual_inventory_audit(audit_id: str) -> bool:
    """Stop new claims while preserving every unprocessed dirty directory."""
    with get_db_connection() as conn:
        with conn.cursor() as cursor:
            cursor.execute(
                """
                UPDATE strm_ingest_inventory_manual_audits
                SET state = 'cancelled', completed_at = NOW(), updated_at = NOW()
                WHERE audit_id = %s AND state IN ('queued', 'running')
                """,
                (str(audit_id),),
            )
            return cursor.rowcount == 1


def get_inventory_summary() -> Dict[str, int]:
    with get_db_connection() as conn:
        with conn.cursor() as cursor:
            cursor.execute(
                """
                SELECT
                    COUNT(*) FILTER (WHERE active = TRUE) AS directory_count,
                    COUNT(*) FILTER (WHERE active = TRUE AND dirty = TRUE) AS dirty_count,
                    COUNT(*) FILTER (
                        WHERE active = TRUE AND claim_expires_at > NOW()
                    ) AS claimed_count
                FROM strm_ingest_inventory_directories
                """
            )
            row = cursor.fetchone() or {}
            return {key: int(value or 0) for key, value in row.items()}


def list_active_inventory_directories(root_paths: Iterable[str]) -> List[str]:
    """Return persisted watch targets without consulting the filesystem."""
    roots = sorted({os.path.normpath(str(path)) for path in root_paths or [] if str(path or '').strip()})
    if not roots:
        return []
    with get_db_connection() as conn:
        with conn.cursor() as cursor:
            cursor.execute(
                """
                SELECT directory_path
                FROM strm_ingest_inventory_directories
                WHERE active = TRUE AND root_path = ANY(%s)
                ORDER BY directory_path
                """,
                (roots,),
            )
            return [row['directory_path'] for row in cursor.fetchall()]
