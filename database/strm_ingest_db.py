import os
from typing import Dict, Iterable, List, Optional, Tuple

from psycopg2.extras import execute_values

from .connection import get_db_connection


RETRY_DELAYS_SECONDS = (10 * 60, 20 * 60, 30 * 60)


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
                    last_error = NULL,
                    updated_at = NOW()
                """,
                (root, directory, parent),
            )
            return cursor.rowcount == 1


def record_file_event(root_path: str, file_path: str, *, event_kind: str) -> None:
    root = os.path.normpath(str(root_path))
    path = os.path.normpath(str(file_path))
    directory = os.path.dirname(path)
    mark_directory_dirty(root, directory, event_kind=event_kind)
    with get_db_connection() as conn:
        with conn.cursor() as cursor:
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
                        claim_owner = NULL, claim_expires_at = NULL, updated_at = NOW()
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
) -> List[Dict]:
    safe_limit = max(1, min(int(limit), 32))
    safe_lease = max(30, min(int(lease_seconds), 900))
    with get_db_connection() as conn:
        with conn.cursor() as cursor:
            cursor.execute(
                """
                WITH due AS (
                    SELECT root_path, directory_path
                    FROM strm_ingest_inventory_directories
                    WHERE active = TRUE
                      AND next_audit_at <= NOW()
                      AND (claim_expires_at IS NULL OR claim_expires_at <= NOW())
                    ORDER BY dirty DESC, next_audit_at ASC, directory_path ASC
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
                (safe_limit, owner, safe_lease),
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


def record_inventory_audit_batch(
    claim: Dict,
    *,
    files: Dict[str, Tuple[int, float]],
    child_directories: Iterable[str],
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
    db_batches = 0

    with get_db_connection() as conn:
        with conn.cursor() as cursor:
            cursor.execute(
                """
                SELECT event_version
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
            if int(current['event_version']) != event_version:
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

            child_rows = [
                (root, child, directory, generation)
                for child in children
            ]
            for offset in range(0, len(child_rows), safe_batch_size):
                execute_values(
                    cursor,
                    """
                    INSERT INTO strm_ingest_inventory_directories (
                        root_path, directory_path, parent_path, active,
                        next_audit_at, seen_generation
                    ) VALUES %s
                    ON CONFLICT (root_path, directory_path) DO UPDATE
                    SET parent_path = EXCLUDED.parent_path, active = TRUE,
                        seen_generation = EXCLUDED.seen_generation, updated_at = NOW()
                    """,
                    child_rows[offset : offset + safe_batch_size],
                    template=(
                        "(%s, %s, %s, TRUE, NOW(), %s)"
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
                cursor.execute(
                    """
                    SELECT directory_path
                    FROM strm_ingest_inventory_directories
                    WHERE root_path = %s AND parent_path = %s AND active = TRUE
                      AND seen_generation != %s
                    """,
                    (root, directory, generation),
                )
                missing_children = [row['directory_path'] for row in cursor.fetchall()]
                for child in missing_children:
                    child_like = child.replace('\\', '\\\\').replace('%', '\\%').replace('_', '\\_') + '/%'
                    cursor.execute(
                        """
                        SELECT file_path FROM strm_ingest_retry_queue
                        WHERE inventory_root_path = %s
                          AND (inventory_directory_path = %s OR inventory_directory_path LIKE %s ESCAPE '\\')
                          AND LOWER(file_path) LIKE '%%.strm'
                          AND operation != 'delete'
                          AND status NOT IN ('deleted', 'cancelled')
                        """,
                        (root, child, child_like),
                    )
                    removed.extend(row['file_path'] for row in cursor.fetchall())
                    cursor.execute(
                        """
                        UPDATE strm_ingest_inventory_directories
                        SET active = FALSE, dirty = FALSE, claim_owner = NULL,
                            claim_expires_at = NULL, updated_at = NOW()
                        WHERE root_path = %s
                          AND (directory_path = %s OR directory_path LIKE %s ESCAPE '\\')
                        """,
                        (root, child, child_like),
                    )
                removed = sorted(set(removed))
                if removed:
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
                        (removed,),
                    )
                cursor.execute(
                    """
                    UPDATE strm_ingest_inventory_directories
                    SET dirty = FALSE, dirty_since = NULL, last_verified_at = NOW(),
                        next_audit_at = NOW() + (%s * INTERVAL '1 hour'),
                        audit_cursor = NULL, claim_owner = NULL, claim_expires_at = NULL,
                        last_error = NULL, updated_at = NOW()
                    WHERE root_path = %s AND directory_path = %s AND claim_owner = %s
                    """,
                    (max(1, int(audit_interval_hours)), root, directory, owner),
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
