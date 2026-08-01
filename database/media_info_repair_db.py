"""Persistence for the latest MediaInfo observation and repair job per Item."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional, Tuple

from database.connection import get_db_connection


ACTIVE_STATES = ("pending", "running", "submitting", "submitted")


def _as_json(value: Dict[str, Any]) -> str:
    return json.dumps(value or {}, ensure_ascii=False, separators=(",", ":"))


def _serialize(row: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    if not row:
        return None
    result = dict(row)
    for key in (
        "submitted_at",
        "started_at",
        "completed_at",
        "retry_after",
        "created_at",
        "updated_at",
    ):
        value = result.get(key)
        if isinstance(value, datetime):
            result[key] = value.astimezone(timezone.utc).isoformat()
    return result


def get_by_item_id(item_id: str) -> Optional[Dict[str, Any]]:
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT * FROM media_info_repair_jobs WHERE exact_item_id = %s",
            (str(item_id),),
        )
        return _serialize(cursor.fetchone())


def get_by_id(job_id: int) -> Optional[Dict[str, Any]]:
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT * FROM media_info_repair_jobs WHERE id = %s",
            (int(job_id),),
        )
        return _serialize(cursor.fetchone())


def get_active_conflict(snapshot: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Find active same-path work or currently executing same-root work."""
    identity = snapshot["identity"]
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT *
              FROM media_info_repair_jobs
             WHERE exact_item_id <> %s
               AND (
                    (
                        exact_strm_path_hash = %s
                        AND state IN ('pending', 'running', 'submitting', 'submitted')
                    )
                    OR
                    (
                        root_series_key = %s
                        AND state IN ('running', 'submitting', 'submitted')
                    )
               )
             ORDER BY
                 CASE WHEN exact_strm_path_hash = %s THEN 0 ELSE 1 END,
                 updated_at DESC
             LIMIT 1
            """,
            (
                identity["exact_item_id"],
                identity["exact_strm_path_hash"],
                identity["root_series_key"],
                identity["exact_strm_path_hash"],
            ),
        )
        return _serialize(cursor.fetchone())


def next_generation() -> int:
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT COALESCE(MAX(generation), 0) + 1 AS generation "
            "FROM media_info_repair_jobs"
        )
        row = cursor.fetchone() or {}
        return int(row.get("generation") or 1)


def recover_interrupted_jobs(ambiguous_minutes: int = 60) -> Dict[str, int]:
    """Fail closed on restart; never replay an old possibly submitted POST."""
    retry_after = datetime.now(timezone.utc) + timedelta(
        minutes=max(1, int(ambiguous_minutes))
    )
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
            """
            UPDATE media_info_repair_jobs
               SET state = 'shutdown_before_start',
                   reason_code = 'shutdown_before_start',
                   completed_at = NOW(),
                   updated_at = NOW()
             WHERE state = 'pending'
            """
        )
        pending = cursor.rowcount
        cursor.execute(
            """
            UPDATE media_info_repair_jobs
               SET state = 'ambiguous',
                   reason_code = 'post_result_ambiguous',
                   retry_after = %s,
                   completed_at = NOW(),
                   updated_at = NOW()
             WHERE state IN ('running', 'submitting', 'submitted')
            """,
            (retry_after,),
        )
        ambiguous = cursor.rowcount
        conn.commit()
    return {"pending_cancelled": pending, "ambiguous": ambiguous}


def upsert_observation(snapshot: Dict[str, Any]) -> Dict[str, Any]:
    identity = snapshot["identity"]
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
            """
            INSERT INTO media_info_repair_jobs (
                exact_item_id, item_type, exact_strm_path_hash,
                redacted_path_hint, root_series_key, state, reason_code,
                precheck_fingerprint, final_fingerprint, snapshot_json,
                updated_at
            )
            VALUES (%s, %s, %s, %s, %s, 'idle', NULL, %s, %s, %s::jsonb, NOW())
            ON CONFLICT (exact_item_id) DO UPDATE SET
                item_type = EXCLUDED.item_type,
                exact_strm_path_hash = EXCLUDED.exact_strm_path_hash,
                redacted_path_hint = EXCLUDED.redacted_path_hint,
                root_series_key = EXCLUDED.root_series_key,
                snapshot_json = EXCLUDED.snapshot_json,
                final_fingerprint = EXCLUDED.final_fingerprint,
                updated_at = NOW()
            RETURNING *
            """,
            (
                identity["exact_item_id"],
                identity["item_type"],
                identity["exact_strm_path_hash"],
                identity.get("redacted_path_hint"),
                identity["root_series_key"],
                snapshot.get("snapshot_fingerprint"),
                snapshot.get("snapshot_fingerprint"),
                _as_json(snapshot),
            ),
        )
        row = cursor.fetchone()
        conn.commit()
        return _serialize(row) or {}


def create_job(
    snapshot: Dict[str, Any],
    generation: int,
    pending_limit: int,
) -> Tuple[str, Dict[str, Any]]:
    """Create/reuse one per-Item job under a transaction-wide queue lock."""
    identity = snapshot["identity"]
    now = datetime.now(timezone.utc)
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT pg_advisory_xact_lock(hashtext('media_info_repair_queue'))"
        )
        cursor.execute(
            "SELECT * FROM media_info_repair_jobs "
            "WHERE exact_item_id = %s FOR UPDATE",
            (identity["exact_item_id"],),
        )
        existing = cursor.fetchone()
        if existing and existing.get("state") in ACTIVE_STATES:
            return "existing", _serialize(existing) or {}
        if existing and existing.get("retry_after"):
            retry_after = existing["retry_after"]
            if retry_after.tzinfo is None:
                retry_after = retry_after.replace(tzinfo=timezone.utc)
            if retry_after > now:
                return "cooldown", _serialize(existing) or {}

        cursor.execute(
            "SELECT COUNT(*) AS total FROM media_info_repair_jobs "
            "WHERE state = 'pending'"
        )
        pending_count = int((cursor.fetchone() or {}).get("total") or 0)
        if pending_count >= max(1, int(pending_limit)):
            return "full", {}

        cursor.execute(
            """
            INSERT INTO media_info_repair_jobs (
                exact_item_id, item_type, exact_strm_path_hash,
                redacted_path_hint, root_series_key, state, reason_code,
                generation, post_attempts, submitted_at, started_at,
                completed_at, retry_after, precheck_fingerprint,
                final_fingerprint, response_kind, snapshot_json, updated_at
            )
            VALUES (
                %s, %s, %s, %s, %s, 'pending', NULL,
                %s, 0, NOW(), NULL, NULL, NULL, %s, NULL, NULL, %s::jsonb, NOW()
            )
            ON CONFLICT (exact_item_id) DO UPDATE SET
                item_type = EXCLUDED.item_type,
                exact_strm_path_hash = EXCLUDED.exact_strm_path_hash,
                redacted_path_hint = EXCLUDED.redacted_path_hint,
                root_series_key = EXCLUDED.root_series_key,
                state = 'pending',
                reason_code = NULL,
                generation = EXCLUDED.generation,
                post_attempts = 0,
                submitted_at = NOW(),
                started_at = NULL,
                completed_at = NULL,
                retry_after = NULL,
                precheck_fingerprint = EXCLUDED.precheck_fingerprint,
                final_fingerprint = NULL,
                response_kind = NULL,
                snapshot_json = EXCLUDED.snapshot_json,
                updated_at = NOW()
            RETURNING *
            """,
            (
                identity["exact_item_id"],
                identity["item_type"],
                identity["exact_strm_path_hash"],
                identity.get("redacted_path_hint"),
                identity["root_series_key"],
                int(generation),
                snapshot.get("snapshot_fingerprint"),
                _as_json(snapshot),
            ),
        )
        row = cursor.fetchone()
        conn.commit()
        return "created", _serialize(row) or {}


def mark_running(job_id: int, generation: int) -> Optional[Dict[str, Any]]:
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
            """
            UPDATE media_info_repair_jobs
               SET state = 'running',
                   reason_code = NULL,
                   started_at = NOW(),
                   updated_at = NOW()
             WHERE id = %s AND generation = %s AND state = 'pending'
            RETURNING *
            """,
            (int(job_id), int(generation)),
        )
        row = cursor.fetchone()
        conn.commit()
        return _serialize(row)


def mark_submitting(job_id: int, generation: int) -> Optional[Dict[str, Any]]:
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
            """
            UPDATE media_info_repair_jobs
               SET state = 'submitting',
                   post_attempts = post_attempts + 1,
                   updated_at = NOW()
             WHERE id = %s AND generation = %s AND state = 'running'
            RETURNING *
            """,
            (int(job_id), int(generation)),
        )
        row = cursor.fetchone()
        conn.commit()
        return _serialize(row)


def finish_job(
    job_id: int,
    generation: int,
    *,
    state: str,
    reason_code: str,
    response_kind: str,
    snapshot: Dict[str, Any],
    cooldown_minutes: int = 0,
) -> Optional[Dict[str, Any]]:
    retry_after = None
    if cooldown_minutes > 0:
        retry_after = datetime.now(timezone.utc) + timedelta(
            minutes=int(cooldown_minutes)
        )
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
            """
            UPDATE media_info_repair_jobs
               SET state = %s,
                   reason_code = %s,
                   response_kind = %s,
                   final_fingerprint = %s,
                   snapshot_json = %s::jsonb,
                   retry_after = %s,
                   completed_at = NOW(),
                   updated_at = NOW()
             WHERE id = %s AND generation = %s
            RETURNING *
            """,
            (
                state,
                reason_code,
                response_kind,
                snapshot.get("snapshot_fingerprint"),
                _as_json(snapshot),
                retry_after,
                int(job_id),
                int(generation),
            ),
        )
        row = cursor.fetchone()
        conn.commit()
        return _serialize(row)


def cancel_pending(job_id: int) -> Optional[Dict[str, Any]]:
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
            """
            UPDATE media_info_repair_jobs
               SET state = 'cancelled',
                   reason_code = 'cancelled_before_start',
                   completed_at = NOW(),
                   updated_at = NOW()
             WHERE id = %s AND state = 'pending'
            RETURNING *
            """,
            (int(job_id),),
        )
        row = cursor.fetchone()
        conn.commit()
        return _serialize(row)


def mark_pending_shutdown(job_ids: list[int]) -> int:
    if not job_ids:
        return 0
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
            """
            UPDATE media_info_repair_jobs
               SET state = 'shutdown_before_start',
                   reason_code = 'shutdown_before_start',
                   completed_at = NOW(),
                   updated_at = NOW()
             WHERE id = ANY(%s) AND state = 'pending'
            """,
            (list(map(int, job_ids)),),
        )
        count = cursor.rowcount
        conn.commit()
        return count
