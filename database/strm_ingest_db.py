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
                """,
                (max(1, int(retention_days)),),
            )
            return cursor.rowcount
