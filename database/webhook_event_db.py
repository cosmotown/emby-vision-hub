import json
import logging
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

ACTIVE_PENDING_STATUSES = ('pending', 'retry')
RETRY_DELAYS_SECONDS = (5, 20, 60)

# Only a completed media_process with the same exact target and payload semantics
# can prove recovery. A title match, a later timestamp, or a Series-only refresh
# is NOT proof that an earlier Episode batch was handled.
_RECOVERY_MATCH_SQL = """
    s.status = 'completed' AND s.task_kind = 'media_process'
    AND f.task_kind = s.task_kind AND f.event_source = s.event_source
    AND s.event_source = 'emby' AND f.dedupe_key = s.dedupe_key
    AND f.item_id = s.item_id AND f.item_id IS NOT NULL AND btrim(f.item_id) <> ''
    AND f.item_type IS NOT DISTINCT FROM s.item_type
    AND s.id > f.id AND s.started_at >= f.completed_at
    AND s.completed_at >= s.started_at
    AND f.payload_json->>'item_id' = f.item_id
    AND s.payload_json->>'item_id' = s.item_id
    AND jsonb_typeof(f.payload_json->'item_id') = 'string'
    AND jsonb_typeof(s.payload_json->'item_id') = 'string'
    AND jsonb_typeof(f.payload_json->'force_full_update') = 'boolean'
    AND jsonb_typeof(f.payload_json->'is_new_item') = 'boolean'
    AND (f.payload_json - 'new_episode_ids') = (s.payload_json - 'new_episode_ids')
    AND (f.payload_json - ARRAY[
        'item_id', 'force_full_update', 'is_new_item', 'new_episode_ids'
    ]::text[]) = '{}'::jsonb
    AND COALESCE(jsonb_typeof(f.payload_json->'new_episode_ids'), 'null') IN ('null', 'array')
    AND COALESCE(jsonb_typeof(s.payload_json->'new_episode_ids'), 'null') IN ('null', 'array')
    AND NOT EXISTS (
        SELECT 1 FROM jsonb_array_elements(fe.ids || se.ids) AS episode(value)
        WHERE jsonb_typeof(value) <> 'string' OR btrim(value #>> '{}') = ''
    )
    AND se.ids @> fe.ids
    AND (fe.ids <> '[]'::jsonb OR se.ids = '[]'::jsonb)
"""


def _reconcile_failed_events(cursor, successful_event_id=None) -> int:
    """One set-based update; never replay a task or claim current media health."""
    cursor.execute(
        """
        WITH covered AS (
            SELECT f.id, proof.success_id
            FROM webhook_event_queue f
            JOIN LATERAL (
                SELECT s.id AS success_id
                FROM webhook_event_queue s
                CROSS JOIN LATERAL (SELECT CASE
                    WHEN jsonb_typeof(f.payload_json->'new_episode_ids') = 'array'
                    THEN f.payload_json->'new_episode_ids' ELSE '[]'::jsonb END AS ids) fe
                CROSS JOIN LATERAL (SELECT CASE
                    WHEN jsonb_typeof(s.payload_json->'new_episode_ids') = 'array'
                    THEN s.payload_json->'new_episode_ids' ELSE '[]'::jsonb END AS ids) se
                WHERE """ + _RECOVERY_MATCH_SQL + """
                  AND (%s::bigint IS NULL OR s.id = %s::bigint)
                ORDER BY s.id DESC LIMIT 1
            ) proof ON TRUE
            WHERE f.status = 'failed'
        )
        UPDATE webhook_event_queue f
        SET status = 'recovered', resolved_at = NOW(), updated_at = NOW(),
            resolution_json = jsonb_build_object(
                'method', 'covered_by_success', 'event_id', covered.success_id)
        FROM covered
        WHERE f.id = covered.id AND f.status = 'failed'
        """,
        (successful_event_id, successful_event_id),
    )
    return cursor.rowcount


def reconcile_failed_events() -> int:
    """Backfill provable recovery after upgrading, using queue evidence only."""
    with get_db_connection() as conn, conn.cursor() as cursor:
        return _reconcile_failed_events(cursor)


def archive_failed_event(event_id: int, actor_id: str) -> bool:
    """Explicit administrator acknowledgement, not execution success or replay."""
    with get_db_connection() as conn, conn.cursor() as cursor:
        cursor.execute(
            """
            UPDATE webhook_event_queue
            SET status = 'archived', resolved_at = NOW(), updated_at = NOW(),
                resolution_json = jsonb_build_object(
                    'method', 'manual_confirmation', 'actor_id', %s::text)
            WHERE id = %s AND status = 'failed'
            """,
            (str(actor_id or 'auth_disabled')[:128], event_id),
        )
        return cursor.rowcount > 0


def get_db_connection():
    # Keep payload helpers importable in lightweight test environments where
    # the PostgreSQL driver is only installed inside the application image.
    from .connection import get_db_connection as connection_factory
    return connection_factory()


def _normalize_episode_ids(value: Any) -> List[str]:
    if not value:
        return []
    if not isinstance(value, (list, tuple, set)):
        value = [value]
    return sorted({str(item_id) for item_id in value if item_id})


def merge_payload(existing: Optional[Dict[str, Any]], incoming: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """Merge repeated events without losing episodes from the same series batch."""
    merged = dict(existing or {})
    incoming = dict(incoming or {})
    merged.update(incoming)

    episode_ids = _normalize_episode_ids((existing or {}).get('new_episode_ids'))
    episode_ids.extend(_normalize_episode_ids(incoming.get('new_episode_ids')))
    if episode_ids:
        merged['new_episode_ids'] = sorted(set(episode_ids))

    if (existing or {}).get('is_new_item') or incoming.get('is_new_item'):
        merged['is_new_item'] = True
    return merged


def enqueue_event(
    *,
    dedupe_key: str,
    task_kind: str,
    task_name: str,
    payload: Dict[str, Any],
    item_id: Optional[str] = None,
    item_name: Optional[str] = None,
    item_type: Optional[str] = None,
    event_source: str = 'emby',
    max_attempts: int = 4,
) -> Tuple[int, bool]:
    """Insert a pending event or merge it into an existing pending retry."""
    with get_db_connection() as conn:
        with conn.cursor() as cursor:
            cursor.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", (dedupe_key,))
            cursor.execute(
                """
                SELECT id, payload_json
                FROM webhook_event_queue
                WHERE dedupe_key = %s AND status IN ('pending', 'retry')
                ORDER BY id DESC
                LIMIT 1
                FOR UPDATE
                """,
                (dedupe_key,),
            )
            existing = cursor.fetchone()
            if existing:
                merged_payload = merge_payload(existing.get('payload_json'), payload)
                cursor.execute(
                    """
                    UPDATE webhook_event_queue
                    SET task_name = %s,
                        item_name = COALESCE(%s, item_name),
                        item_type = COALESCE(%s, item_type),
                        payload_json = %s::jsonb,
                        status = 'pending',
                        next_attempt_at = LEAST(next_attempt_at, NOW()),
                        last_error = NULL,
                        updated_at = NOW()
                    WHERE id = %s
                    """,
                    (
                        task_name,
                        item_name,
                        item_type,
                        json.dumps(merged_payload, ensure_ascii=False),
                        existing['id'],
                    ),
                )
                return int(existing['id']), False

            cursor.execute(
                """
                INSERT INTO webhook_event_queue (
                    dedupe_key, event_source, task_kind, task_name,
                    item_id, item_name, item_type, payload_json, max_attempts
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s)
                RETURNING id
                """,
                (
                    dedupe_key,
                    event_source,
                    task_kind,
                    task_name,
                    str(item_id) if item_id else None,
                    item_name,
                    item_type,
                    json.dumps(payload or {}, ensure_ascii=False),
                    max(1, int(max_attempts)),
                ),
            )
            return int(cursor.fetchone()['id']), True


def recover_interrupted_events(stale_minutes: int = 15) -> int:
    """Return events left in processing by a restart to the retry queue."""
    with get_db_connection() as conn:
        with conn.cursor() as cursor:
            cursor.execute(
                """
                SELECT id, dedupe_key, payload_json
                FROM webhook_event_queue
                WHERE status = 'processing'
                  AND started_at < NOW() - (%s * INTERVAL '1 minute')
                ORDER BY id ASC
                FOR UPDATE
                """,
                (max(0, int(stale_minutes)),),
            )
            interrupted = list(cursor.fetchall())
            for event in interrupted:
                cursor.execute(
                    """
                    SELECT id, payload_json
                    FROM webhook_event_queue
                    WHERE dedupe_key = %s
                      AND id <> %s
                      AND status IN ('pending', 'retry')
                    ORDER BY id ASC
                    LIMIT 1
                    FOR UPDATE
                    """,
                    (event['dedupe_key'], event['id']),
                )
                successor = cursor.fetchone()
                if successor:
                    merged_payload = merge_payload(event.get('payload_json'), successor.get('payload_json'))
                    cursor.execute(
                        """
                        UPDATE webhook_event_queue
                        SET payload_json = %s::jsonb,
                            status = 'pending',
                            next_attempt_at = NOW(),
                            updated_at = NOW()
                        WHERE id = %s
                        """,
                        (json.dumps(merged_payload, ensure_ascii=False), successor['id']),
                    )
                    cursor.execute(
                        """
                        UPDATE webhook_event_queue
                        SET status = 'superseded',
                            completed_at = NOW(),
                            last_error = %s,
                            updated_at = NOW()
                        WHERE id = %s
                        """,
                        (f"重启恢复时已合并到后续事件 {successor['id']}", event['id']),
                    )
                else:
                    cursor.execute(
                        """
                        UPDATE webhook_event_queue
                        SET status = 'retry',
                            next_attempt_at = NOW(),
                            last_error = COALESCE(last_error, '容器重启或任务超时，已自动恢复'),
                            updated_at = NOW()
                        WHERE id = %s
                        """,
                        (event['id'],),
                    )
            return len(interrupted)


def claim_next_event() -> Optional[Dict[str, Any]]:
    with get_db_connection() as conn:
        with conn.cursor() as cursor:
            cursor.execute(
                """
                SELECT *
                FROM webhook_event_queue
                WHERE status IN ('pending', 'retry')
                  AND next_attempt_at <= NOW()
                ORDER BY next_attempt_at ASC, id ASC
                LIMIT 1
                FOR UPDATE SKIP LOCKED
                """
            )
            row = cursor.fetchone()
            if not row:
                return None
            cursor.execute(
                """
                UPDATE webhook_event_queue
                SET status = 'processing',
                    attempt_count = attempt_count + 1,
                    started_at = NOW(),
                    updated_at = NOW(),
                    last_error = NULL
                WHERE id = %s
                RETURNING *
                """,
                (row['id'],),
            )
            return dict(cursor.fetchone())


def defer_claim(event_id: int, delay_seconds: int = 5) -> None:
    """Release a claimed event when task_manager became busy before submission."""
    with get_db_connection() as conn:
        with conn.cursor() as cursor:
            cursor.execute(
                """
                UPDATE webhook_event_queue
                SET status = 'retry',
                    attempt_count = GREATEST(attempt_count - 1, 0),
                    next_attempt_at = NOW() + (%s * INTERVAL '1 second'),
                    started_at = NULL,
                    updated_at = NOW()
                WHERE id = %s
                """,
                (max(1, int(delay_seconds)), event_id),
            )


def mark_completed(event_id: int) -> None:
    with get_db_connection() as conn:
        with conn.cursor() as cursor:
            cursor.execute(
                """
                UPDATE webhook_event_queue
                SET status = 'completed',
                    completed_at = NOW(),
                    updated_at = NOW(),
                    last_error = NULL
                WHERE id = %s
                """,
                (event_id,),
            )
            _reconcile_failed_events(cursor, successful_event_id=event_id)


def mark_failed(event_id: int, error: str) -> str:
    """Retry transient failures three times, then retain a visible failed row."""
    with get_db_connection() as conn:
        with conn.cursor() as cursor:
            cursor.execute(
                """
                SELECT id, dedupe_key, payload_json, attempt_count, max_attempts
                FROM webhook_event_queue
                WHERE id = %s
                FOR UPDATE
                """,
                (event_id,),
            )
            row = cursor.fetchone()
            if not row:
                return 'missing'

            attempts = int(row['attempt_count'] or 0)
            max_attempts = int(row['max_attempts'] or 4)
            if attempts < max_attempts:
                cursor.execute(
                    """
                    SELECT id, payload_json
                    FROM webhook_event_queue
                    WHERE dedupe_key = %s
                      AND id <> %s
                      AND status IN ('pending', 'retry')
                    ORDER BY id ASC
                    LIMIT 1
                    FOR UPDATE
                    """,
                    (row['dedupe_key'], event_id),
                )
                successor = cursor.fetchone()
                if successor:
                    merged_payload = merge_payload(row.get('payload_json'), successor.get('payload_json'))
                    cursor.execute(
                        """
                        UPDATE webhook_event_queue
                        SET payload_json = %s::jsonb,
                            status = 'pending',
                            next_attempt_at = NOW(),
                            updated_at = NOW()
                        WHERE id = %s
                        """,
                        (json.dumps(merged_payload, ensure_ascii=False), successor['id']),
                    )
                    cursor.execute(
                        """
                        UPDATE webhook_event_queue
                        SET status = 'superseded',
                            completed_at = NOW(),
                            last_error = %s,
                            updated_at = NOW()
                        WHERE id = %s
                        """,
                        (f"执行失败，已合并到后续事件 {successor['id']}: {str(error)[:3800]}", event_id),
                    )
                    return 'superseded'

                delay = RETRY_DELAYS_SECONDS[min(max(0, attempts - 1), len(RETRY_DELAYS_SECONDS) - 1)]
                status = 'retry'
                cursor.execute(
                    """
                    UPDATE webhook_event_queue
                    SET status = 'retry',
                        next_attempt_at = NOW() + (%s * INTERVAL '1 second'),
                        last_error = %s,
                        updated_at = NOW()
                    WHERE id = %s
                    """,
                    (delay, str(error)[:4000], event_id),
                )
            else:
                status = 'failed'
                cursor.execute(
                    """
                    UPDATE webhook_event_queue
                    SET status = 'failed',
                        completed_at = NOW(),
                        last_error = %s,
                        updated_at = NOW()
                    WHERE id = %s
                    """,
                    (str(error)[:4000], event_id),
                )
            return status


def retry_event(event_id: int) -> bool:
    with get_db_connection() as conn:
        with conn.cursor() as cursor:
            cursor.execute(
                """
                SELECT dedupe_key, payload_json
                FROM webhook_event_queue
                WHERE id = %s AND status = 'failed'
                FOR UPDATE
                """,
                (event_id,),
            )
            failed_event = cursor.fetchone()
            if not failed_event:
                return False

            cursor.execute(
                """
                SELECT id, payload_json
                FROM webhook_event_queue
                WHERE dedupe_key = %s
                  AND id <> %s
                  AND status IN ('pending', 'retry')
                ORDER BY id ASC
                LIMIT 1
                FOR UPDATE
                """,
                (failed_event['dedupe_key'], event_id),
            )
            successor = cursor.fetchone()
            if successor:
                merged_payload = merge_payload(failed_event.get('payload_json'), successor.get('payload_json'))
                cursor.execute(
                    """
                    UPDATE webhook_event_queue
                    SET payload_json = %s::jsonb,
                        status = 'pending',
                        next_attempt_at = NOW(),
                        updated_at = NOW()
                    WHERE id = %s
                    """,
                    (json.dumps(merged_payload, ensure_ascii=False), successor['id']),
                )
                cursor.execute(
                    """
                    UPDATE webhook_event_queue
                    SET status = 'superseded',
                        completed_at = NOW(),
                        last_error = %s,
                        updated_at = NOW()
                    WHERE id = %s
                    """,
                    (f"手动重试已合并到事件 {successor['id']}", event_id),
                )
                return True

            cursor.execute(
                """
                UPDATE webhook_event_queue
                SET status = 'pending',
                    attempt_count = 0,
                    next_attempt_at = NOW(),
                    completed_at = NULL,
                    last_error = NULL,
                    updated_at = NOW()
                WHERE id = %s
                """,
                (event_id,),
            )
            return cursor.rowcount > 0


def list_recent_events(limit: int = 50) -> List[Dict[str, Any]]:
    with get_db_connection() as conn:
        with conn.cursor() as cursor:
            cursor.execute(
                """
                SELECT id, event_source, task_kind, task_name, item_id, item_name,
                       item_type, status, attempt_count, max_attempts, next_attempt_at,
                       last_error, created_at, updated_at, started_at, completed_at,
                       resolution_json, resolved_at
                FROM webhook_event_queue
                ORDER BY id DESC
                LIMIT %s
                """,
                (max(1, min(int(limit), 200)),),
            )
            return [dict(row) for row in cursor.fetchall()]


def get_queue_summary() -> Dict[str, Any]:
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
                    COUNT(*) FILTER (WHERE status = 'recovered') AS recovered_count,
                    COUNT(*) FILTER (WHERE status = 'archived') AS archived_count,
                    MIN(created_at) FILTER (
                        WHERE status IN ('pending', 'retry')
                    ) AS oldest_pending_at,
                    MAX(completed_at) FILTER (
                        WHERE status = 'completed'
                    ) AS last_completed_at
                FROM webhook_event_queue
                """
            )
            row = cursor.fetchone() or {}
            return dict(row)


def has_pending_events() -> bool:
    with get_db_connection() as conn:
        with conn.cursor() as cursor:
            cursor.execute(
                "SELECT EXISTS(SELECT 1 FROM webhook_event_queue WHERE status IN ('pending', 'retry')) AS exists"
            )
            row = cursor.fetchone()
            return bool(row and row.get('exists'))


def prune_old_events(retention_days: int = 30) -> int:
    with get_db_connection() as conn:
        with conn.cursor() as cursor:
            cursor.execute(
                """
                DELETE FROM webhook_event_queue
                WHERE status IN ('completed', 'superseded', 'recovered', 'archived')
                  AND COALESCE(resolved_at, completed_at) < NOW() - (%s * INTERVAL '1 day')
                """,
                (max(1, int(retention_days)),),
            )
            return cursor.rowcount
