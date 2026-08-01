"""PostgreSQL persistence for MediaInfo observations and repair ownership."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional, Tuple

import psycopg2

from database.connection import get_db_connection


ACTIVE_STATES = ("pending", "running", "submitting")
TERMINAL_RETENTION_DAYS = 90
TERMINAL_CLEANUP_LIMIT = 500


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
        "heartbeat_at",
        "lease_expires_at",
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
    """Read-only diagnostic; database unique indexes are the admission boundary."""
    identity = snapshot["identity"]
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT *
              FROM media_info_repair_jobs
             WHERE state IN ('pending', 'running', 'submitting')
               AND exact_item_id <> %s
               AND (
                    exact_strm_path_hash = %s
                    OR root_series_key = %s
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
    """Allocate a cross-process unique owner generation."""
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT nextval('media_info_repair_generation_seq') AS generation")
        row = cursor.fetchone() or {}
        conn.commit()
        return int(row.get("generation") or 0)


def recover_expired_jobs(
    instance_id: str,
    ambiguous_minutes: int = 60,
    *,
    now: Optional[datetime] = None,
) -> Dict[str, int]:
    """Recover only expired/legacy owners, and never enqueue old attempts."""
    current = now or datetime.now(timezone.utc)
    retry_after = current + timedelta(minutes=max(1, int(ambiguous_minutes)))
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
            """
            UPDATE media_info_repair_jobs
               SET state = 'shutdown_before_start',
                   reason_code = CASE
                       WHEN state = 'running' THEN 'worker_lost_before_submit'
                       ELSE 'shutdown_before_start'
                   END,
                   completed_at = %s,
                   owner_instance_id = NULL,
                   owner_generation = NULL,
                   heartbeat_at = NULL,
                   lease_expires_at = NULL,
                   updated_at = %s
             WHERE state IN ('pending', 'running')
               AND post_attempts = 0
               AND (lease_expires_at IS NULL OR lease_expires_at < %s)
            """,
            (current, current, current),
        )
        safe = cursor.rowcount
        cursor.execute(
            """
            UPDATE media_info_repair_jobs
               SET state = 'ambiguous',
                   reason_code = 'post_result_ambiguous',
                   retry_after = %s,
                   completed_at = %s,
                   owner_instance_id = NULL,
                   owner_generation = NULL,
                   heartbeat_at = NULL,
                   lease_expires_at = NULL,
                   updated_at = %s
             WHERE state IN ('pending', 'running', 'submitting', 'submitted')
               AND (post_attempts > 0 OR state IN ('submitting', 'submitted'))
               AND (lease_expires_at IS NULL OR lease_expires_at < %s)
            """,
            (retry_after, current, current, current),
        )
        ambiguous = cursor.rowcount
        conn.commit()
    return {
        "instance_id": str(instance_id),
        "safe_terminalized": safe,
        "ambiguous": ambiguous,
    }


# Backward-compatible name for external imports. The caller must provide an
# instance ID; deliberately do not retain the old global-recovery behavior.
def recover_interrupted_jobs(
    ambiguous_minutes: int = 60,
    *,
    instance_id: Optional[str] = None,
) -> Dict[str, int]:
    if not instance_id:
        raise ValueError("instance_id is required for lease-safe recovery")
    return recover_expired_jobs(instance_id, ambiguous_minutes)


def heartbeat_owned_jobs(
    instance_id: str,
    generation: int,
    lease_seconds: float,
) -> int:
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
            """
            UPDATE media_info_repair_jobs
               SET heartbeat_at = NOW(),
                   lease_expires_at = NOW() + (%s * INTERVAL '1 second'),
                   updated_at = NOW()
             WHERE owner_instance_id = %s
               AND owner_generation = %s
               AND state IN ('pending', 'running', 'submitting')
            """,
            (max(1.0, float(lease_seconds)), str(instance_id), int(generation)),
        )
        count = cursor.rowcount
        conn.commit()
        return count


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
            WHERE media_info_repair_jobs.state NOT IN
                  ('pending', 'running', 'submitting')
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
        if not row:
            cursor.execute(
                "SELECT * FROM media_info_repair_jobs WHERE exact_item_id = %s",
                (identity["exact_item_id"],),
            )
            row = cursor.fetchone()
        conn.commit()
        return _serialize(row) or {}


def _constraint_result(constraint_name: Optional[str]) -> str:
    return {
        "media_info_repair_jobs_exact_item_id_key": "same_item_active",
        "uq_media_info_repair_active_item": "same_item_active",
        "uq_media_info_repair_active_path": "same_path_active",
        "uq_media_info_repair_active_root": "same_root_active",
    }.get(str(constraint_name or ""), "active_conflict")


def create_job(
    snapshot: Dict[str, Any],
    generation: int,
    pending_limit: int,
    instance_id: str,
    lease_seconds: float,
) -> Tuple[str, Dict[str, Any]]:
    """Atomically claim Item, path and root before returning accepted."""
    identity = snapshot["identity"]
    now = datetime.now(timezone.utc)
    try:
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
                return "same_item_active", _serialize(existing) or {}
            if existing and existing.get("retry_after"):
                retry_after = existing["retry_after"]
                if retry_after.tzinfo is None:
                    retry_after = retry_after.replace(tzinfo=timezone.utc)
                if retry_after > now:
                    return "cooldown", _serialize(existing) or {}

            # Diagnostic classification is repeated while the transaction-wide
            # admission lock is held. Partial unique indexes remain the truth.
            cursor.execute(
                """
                SELECT * FROM media_info_repair_jobs
                 WHERE state IN ('pending', 'running', 'submitting')
                   AND exact_item_id <> %s
                   AND (exact_strm_path_hash = %s OR root_series_key = %s)
                 ORDER BY CASE WHEN exact_strm_path_hash = %s THEN 0 ELSE 1 END,
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
            conflict = cursor.fetchone()
            if conflict:
                code = (
                    "same_path_active"
                    if conflict.get("exact_strm_path_hash")
                    == identity["exact_strm_path_hash"]
                    else "same_root_active"
                )
                return code, _serialize(conflict) or {}

            cursor.execute(
                "SELECT COUNT(*) AS total FROM media_info_repair_jobs "
                "WHERE state = 'pending'"
            )
            pending_count = int((cursor.fetchone() or {}).get("total") or 0)
            if pending_count >= max(1, int(pending_limit)):
                return "full", {}

            values = (
                identity["exact_item_id"],
                identity["item_type"],
                identity["exact_strm_path_hash"],
                identity.get("redacted_path_hint"),
                identity["root_series_key"],
                int(generation),
                str(instance_id),
                int(generation),
                max(1.0, float(lease_seconds)),
                snapshot.get("snapshot_fingerprint"),
                _as_json(snapshot),
            )
            cursor.execute(
                """
                INSERT INTO media_info_repair_jobs (
                    exact_item_id, item_type, exact_strm_path_hash,
                    redacted_path_hint, root_series_key, state, reason_code,
                    generation, owner_instance_id, owner_generation,
                    heartbeat_at, lease_expires_at, post_attempts, submitted_at,
                    started_at, completed_at, retry_after, precheck_fingerprint,
                    final_fingerprint, response_kind, snapshot_json, updated_at
                )
                VALUES (
                    %s, %s, %s, %s, %s, 'pending', NULL,
                    %s, %s, %s, NOW(), NOW() + (%s * INTERVAL '1 second'),
                    0, NOW(), NULL, NULL, NULL, %s, NULL, NULL, %s::jsonb, NOW()
                )
                ON CONFLICT (exact_item_id) DO UPDATE SET
                    item_type = EXCLUDED.item_type,
                    exact_strm_path_hash = EXCLUDED.exact_strm_path_hash,
                    redacted_path_hint = EXCLUDED.redacted_path_hint,
                    root_series_key = EXCLUDED.root_series_key,
                    state = 'pending', reason_code = NULL,
                    generation = EXCLUDED.generation,
                    owner_instance_id = EXCLUDED.owner_instance_id,
                    owner_generation = EXCLUDED.owner_generation,
                    heartbeat_at = NOW(),
                    lease_expires_at = EXCLUDED.lease_expires_at,
                    post_attempts = 0, submitted_at = NOW(), started_at = NULL,
                    completed_at = NULL, retry_after = NULL,
                    precheck_fingerprint = EXCLUDED.precheck_fingerprint,
                    final_fingerprint = NULL, response_kind = NULL,
                    snapshot_json = EXCLUDED.snapshot_json, updated_at = NOW()
                WHERE media_info_repair_jobs.state NOT IN
                      ('pending', 'running', 'submitting')
                RETURNING *
                """,
                values,
            )
            row = cursor.fetchone()
            if not row:
                cursor.execute(
                    "SELECT * FROM media_info_repair_jobs WHERE exact_item_id = %s",
                    (identity["exact_item_id"],),
                )
                return "same_item_active", _serialize(cursor.fetchone()) or {}
            conn.commit()
            return "created", _serialize(row) or {}
    except psycopg2.IntegrityError as exc:
        constraint_name = getattr(exc.diag, "constraint_name", None)
        code = _constraint_result(constraint_name)
        if code == "active_conflict":
            raise
        return code, get_active_conflict(snapshot) or get_by_item_id(
            identity["exact_item_id"]
        ) or {}


def mark_running(
    job_id: int,
    generation: int,
    instance_id: str,
    lease_seconds: float,
) -> Optional[Dict[str, Any]]:
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
            """
            UPDATE media_info_repair_jobs
               SET state = 'running', reason_code = NULL, started_at = NOW(),
                   heartbeat_at = NOW(),
                   lease_expires_at = NOW() + (%s * INTERVAL '1 second'),
                   updated_at = NOW()
             WHERE id = %s AND generation = %s
               AND owner_instance_id = %s AND owner_generation = %s
               AND state = 'pending' AND post_attempts = 0
            RETURNING *
            """,
            (
                max(1.0, float(lease_seconds)),
                int(job_id),
                int(generation),
                str(instance_id),
                int(generation),
            ),
        )
        row = cursor.fetchone()
        conn.commit()
        return _serialize(row)


def mark_submitting(
    job_id: int,
    generation: int,
    instance_id: str,
    snapshot: Dict[str, Any],
    lease_seconds: float,
) -> Optional[Dict[str, Any]]:
    """Commit the irreversible attempt ledger before a caller may POST."""
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
            """
            UPDATE media_info_repair_jobs
               SET state = 'submitting', post_attempts = 1,
                   precheck_fingerprint = %s, snapshot_json = %s::jsonb,
                   heartbeat_at = NOW(),
                   lease_expires_at = NOW() + (%s * INTERVAL '1 second'),
                   updated_at = NOW()
             WHERE id = %s AND generation = %s
               AND owner_instance_id = %s AND owner_generation = %s
               AND state = 'running' AND post_attempts = 0
               AND lease_expires_at >= NOW()
            RETURNING *
            """,
            (
                snapshot.get("snapshot_fingerprint"),
                _as_json(snapshot),
                max(1.0, float(lease_seconds)),
                int(job_id),
                int(generation),
                str(instance_id),
                int(generation),
            ),
        )
        row = cursor.fetchone()
        conn.commit()
        return _serialize(row)


def finish_pre_submit(
    job_id: int,
    generation: int,
    instance_id: str,
    *,
    reason_code: str,
    snapshot: Dict[str, Any],
) -> Optional[Dict[str, Any]]:
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
            """
            UPDATE media_info_repair_jobs
               SET state = 'skipped', reason_code = %s,
                   final_fingerprint = %s, snapshot_json = %s::jsonb,
                   completed_at = NOW(), owner_instance_id = NULL,
                   owner_generation = NULL, heartbeat_at = NULL,
                   lease_expires_at = NULL, updated_at = NOW()
             WHERE id = %s AND generation = %s
               AND owner_instance_id = %s AND owner_generation = %s
               AND state = 'running' AND post_attempts = 0
            RETURNING *
            """,
            (
                str(reason_code),
                snapshot.get("snapshot_fingerprint"),
                _as_json(snapshot),
                int(job_id),
                int(generation),
                str(instance_id),
                int(generation),
            ),
        )
        row = cursor.fetchone()
        conn.commit()
        return _serialize(row)


def finish_job(
    job_id: int,
    generation: int,
    instance_id: str,
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
               SET state = %s, reason_code = %s, response_kind = %s,
                   final_fingerprint = %s, snapshot_json = %s::jsonb,
                   retry_after = %s, completed_at = NOW(),
                   owner_instance_id = NULL, owner_generation = NULL,
                   heartbeat_at = NULL, lease_expires_at = NULL,
                   updated_at = NOW()
             WHERE id = %s AND generation = %s
               AND owner_instance_id = %s AND owner_generation = %s
               AND state = 'submitting' AND post_attempts = 1
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
                str(instance_id),
                int(generation),
            ),
        )
        row = cursor.fetchone()
        conn.commit()
        return _serialize(row)


def mark_post_ambiguous(
    job_id: int,
    generation: int,
    instance_id: str,
    *,
    snapshot: Dict[str, Any],
    response_kind: str = "persistence_uncertain",
    cooldown_minutes: int = 60,
) -> Optional[Dict[str, Any]]:
    retry_after = datetime.now(timezone.utc) + timedelta(
        minutes=max(1, int(cooldown_minutes))
    )
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
            """
            UPDATE media_info_repair_jobs
               SET state = 'ambiguous', reason_code = 'post_result_ambiguous',
                   response_kind = %s, final_fingerprint = %s,
                   snapshot_json = %s::jsonb, retry_after = %s,
                   completed_at = NOW(), owner_instance_id = NULL,
                   owner_generation = NULL, heartbeat_at = NULL,
                   lease_expires_at = NULL, updated_at = NOW()
             WHERE id = %s AND generation = %s
               AND owner_instance_id = %s AND owner_generation = %s
               AND state = 'submitting' AND post_attempts = 1
            RETURNING *
            """,
            (
                str(response_kind),
                snapshot.get("snapshot_fingerprint"),
                _as_json(snapshot),
                retry_after,
                int(job_id),
                int(generation),
                str(instance_id),
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
               SET state = 'cancelled', reason_code = 'cancelled_before_start',
                   completed_at = NOW(), owner_instance_id = NULL,
                   owner_generation = NULL, heartbeat_at = NULL,
                   lease_expires_at = NULL, updated_at = NOW()
             WHERE id = %s AND state = 'pending' AND post_attempts = 0
            RETURNING *
            """,
            (int(job_id),),
        )
        row = cursor.fetchone()
        conn.commit()
        return _serialize(row)


def mark_pending_shutdown(
    job_ids: list[int],
    instance_id: str,
    generation: int,
) -> int:
    if not job_ids:
        return 0
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
            """
            UPDATE media_info_repair_jobs
               SET state = 'shutdown_before_start',
                   reason_code = 'shutdown_before_start', completed_at = NOW(),
                   owner_instance_id = NULL, owner_generation = NULL,
                   heartbeat_at = NULL, lease_expires_at = NULL,
                   updated_at = NOW()
             WHERE id = ANY(%s) AND state = 'pending' AND post_attempts = 0
               AND owner_instance_id = %s AND owner_generation = %s
            """,
            (list(map(int, job_ids)), str(instance_id), int(generation)),
        )
        count = cursor.rowcount
        conn.commit()
        return count


def cleanup_terminal_jobs(
    retention_days: int = TERMINAL_RETENTION_DAYS,
    limit: int = TERMINAL_CLEANUP_LIMIT,
) -> int:
    """Delete a bounded set of old terminal rows; preserve active/cooldown work."""
    cutoff = datetime.now(timezone.utc) - timedelta(
        days=max(1, int(retention_days))
    )
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
            """
            WITH doomed AS (
                SELECT id FROM media_info_repair_jobs
                 WHERE state NOT IN ('pending', 'running', 'submitting')
                   AND COALESCE(completed_at, updated_at) < %s
                   AND NOT (
                       state = 'ambiguous'
                       AND retry_after IS NOT NULL
                       AND retry_after > NOW()
                   )
                 ORDER BY COALESCE(completed_at, updated_at), id
                 LIMIT %s
                 FOR UPDATE SKIP LOCKED
            )
            DELETE FROM media_info_repair_jobs jobs
             USING doomed
             WHERE jobs.id = doomed.id
            """,
            (cutoff, max(1, min(int(limit), TERMINAL_CLEANUP_LIMIT))),
        )
        count = cursor.rowcount
        conn.commit()
        return count
