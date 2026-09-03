import json
import hashlib
import logging
import secrets
import uuid
from typing import Any, Dict, Iterable, List, Optional

from .connection import get_db_connection
from services.person_cleanup_safety import (
    build_person_name_protection_keys,
    candidate_fingerprint,
    canonical_person_provider_identities,
    is_explicit_verified_orphan,
    person_name_protection_keys,
)


logger = logging.getLogger(__name__)


VERIFICATION_STATES = {
    'unverified',
    'linked',
    'orphan',
    'identity_alias_only',
    'people_unavailable',
    'connection_failed',
    'invalid_response',
}

ALIAS_PROOF_STATES = {
    'verified_alias_orphan',
    'linked',
    'protected',
    'identity_unavailable',
    'identity_not_found',
    'identity_ambiguous',
    'people_unavailable',
    'invalid_response',
    'connection_failed',
    'candidate_changed',
    'failed_safe',
}

ALIAS_PROOF_FAILED_STATES = {
    'people_unavailable', 'invalid_response', 'connection_failed',
    'candidate_changed', 'failed_safe',
}
ALIAS_PROOF_REJECTED_STATES = {
    'linked', 'identity_unavailable', 'identity_not_found', 'identity_ambiguous',
}

STALE_INDEX_FORENSIC_STATES = {
    'verified_stale_index_signature',
    'stable_stale_index_signature',
    'query_disappeared',
    'linked',
    'protected',
    'people_unavailable',
    'candidate_changed',
    'person_missing',
    'identity_owner_live',
    'failed_safe',
}
STALE_INDEX_SIGNATURE_STATES = {
    'verified_stale_index_signature',
    'stable_stale_index_signature',
}

def _exclude_protected_candidates(candidates: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
    protected_ids = get_protected_person_ids()
    protected_ids.update(get_protected_alias_statuses())
    protected_names = build_person_name_protection_keys(get_protected_person_names())
    protected_identities = get_protected_provider_identities()
    filtered = []
    for candidate in candidates:
        person_id = str(candidate.get('person_id') or candidate.get('Id') or '')
        if person_id in protected_ids:
            continue
        if not person_name_protection_keys(
            candidate.get('person_name') or candidate.get('Name')
        ).isdisjoint(protected_names):
            continue
        try:
            identities = canonical_person_provider_identities(
                candidate.get('provider_ids_json') or candidate.get('ProviderIds'),
                strict=True,
            )
        except ValueError:
            # Malformed provider identity is never made eligible by filtering.
            continue
        if not identities.isdisjoint(protected_identities):
            continue
        filtered.append(candidate)
    return filtered


def _normalize_candidates(candidates: Iterable[Dict[str, Any]]) -> List[tuple]:
    normalized = []
    for candidate in _exclude_protected_candidates(candidates):
        person_id = str(candidate.get('Id') or candidate.get('person_id') or '').strip()
        if not person_id:
            continue
        normalized.append((
            person_id,
            candidate.get('Name') or candidate.get('person_name') or '未知人物',
            json.dumps(
                candidate.get('ProviderIds') or candidate.get('provider_ids_json') or {},
                ensure_ascii=False,
            ),
        ))
    return normalized


def replace_candidates(candidates: Iterable[Dict[str, Any]]) -> int:
    normalized = _normalize_candidates(candidates)

    with get_db_connection() as conn:
        with conn.cursor() as cursor:
            cursor.execute("DELETE FROM person_cleanup_candidates")
            if normalized:
                cursor.executemany(
                    """
                    INSERT INTO person_cleanup_candidates (
                        person_id, person_name, provider_ids_json,
                        verification_status, verification_snapshot_generation,
                        verification_fingerprint, last_checked_at, last_error
                    )
                    VALUES (%s, %s, %s::jsonb, 'unverified', NULL, NULL, NULL, NULL)
                    """,
                    normalized,
                )
    return len(normalized)


def start_readonly_alias_scan(
    candidates: Iterable[Dict[str, Any]],
    snapshot_generation: int,
) -> Dict[str, Any]:
    """Atomically replace phase-1 candidates and persist phase-2 work."""
    normalized = _normalize_candidates(candidates)
    scan_id = str(uuid.uuid4())
    with get_db_connection() as conn:
        with conn.cursor() as cursor:
            cursor.execute(
                """
                UPDATE person_cleanup_readonly_scans
                SET state = 'superseded', updated_at = NOW(),
                    completed_at = COALESCE(completed_at, NOW())
                WHERE state IN ('running', 'stopped', 'interrupted')
                """
            )
            cursor.execute("DELETE FROM person_cleanup_candidates")
            cursor.execute(
                """
                INSERT INTO person_cleanup_readonly_scans (
                    scan_id, state, phase, snapshot_generation, candidate_total
                )
                VALUES (%s, 'running', 'protected_alias_verification', %s, %s)
                """,
                (scan_id, int(snapshot_generation), len(normalized)),
            )
            if normalized:
                cursor.executemany(
                    """
                    INSERT INTO person_cleanup_candidates (
                        person_id, person_name, provider_ids_json,
                        verification_status, verification_snapshot_generation,
                        verification_fingerprint, last_checked_at, last_error,
                        alias_scan_id, alias_scan_status,
                        alias_scan_checked_at, alias_scan_error
                    )
                    VALUES (
                        %s, %s, %s::jsonb, 'unverified', NULL, NULL, NULL, NULL,
                        %s, 'pending', NULL, NULL
                    )
                    """,
                    [(*row, scan_id) for row in normalized],
                )
    return get_readonly_scan(scan_id)


def get_readonly_scan(scan_id: Optional[str] = None) -> Optional[Dict[str, Any]]:
    with get_db_connection() as conn:
        with conn.cursor() as cursor:
            if scan_id:
                cursor.execute(
                    """
                    SELECT * FROM person_cleanup_readonly_scans
                    WHERE scan_id = %s
                    """,
                    (str(scan_id),),
                )
            else:
                cursor.execute(
                    """
                    SELECT * FROM person_cleanup_readonly_scans
                    ORDER BY started_at DESC, scan_id DESC
                    LIMIT 1
                    """
                )
            row = cursor.fetchone()
            if not row:
                return None
            result = dict(row)
            result['pending_count'] = max(
                0,
                int(result.get('candidate_total') or 0)
                - int(result.get('checked_count') or 0),
            )
            return result


def get_resumable_readonly_scan(snapshot_generation: int) -> Optional[Dict[str, Any]]:
    with get_db_connection() as conn:
        with conn.cursor() as cursor:
            cursor.execute(
                """
                SELECT * FROM person_cleanup_readonly_scans
                WHERE snapshot_generation = %s
                  AND state IN ('running', 'stopped', 'interrupted')
                ORDER BY started_at DESC, scan_id DESC
                LIMIT 1
                """,
                (int(snapshot_generation),),
            )
            row = cursor.fetchone()
            if not row:
                return None
            scan_id = str(row['scan_id'])
            cursor.execute(
                """
                UPDATE person_cleanup_candidates
                SET alias_scan_status = 'pending', alias_scan_error = NULL
                WHERE alias_scan_id = %s AND alias_scan_status = 'checking'
                """,
                (scan_id,),
            )
            cursor.execute(
                """
                UPDATE person_cleanup_readonly_scans
                SET state = 'running', stopped_at = NULL, last_error = NULL,
                    updated_at = NOW()
                WHERE scan_id = %s
                RETURNING *
                """,
                (scan_id,),
            )
            resumed = dict(cursor.fetchone())
            resumed['pending_count'] = max(
                0,
                int(resumed.get('candidate_total') or 0)
                - int(resumed.get('checked_count') or 0),
            )
            return resumed


def claim_readonly_alias_candidates(scan_id: str, limit: int = 4) -> List[Dict[str, Any]]:
    safe_limit = max(1, min(int(limit), 6))
    with get_db_connection() as conn:
        with conn.cursor() as cursor:
            cursor.execute(
                """
                WITH claimed AS (
                    SELECT person_id
                    FROM person_cleanup_candidates
                    WHERE alias_scan_id = %s AND alias_scan_status = 'pending'
                    ORDER BY person_id ASC
                    FOR UPDATE SKIP LOCKED
                    LIMIT %s
                )
                UPDATE person_cleanup_candidates candidates
                SET alias_scan_status = 'checking', alias_scan_error = NULL
                FROM claimed
                WHERE candidates.person_id = claimed.person_id
                RETURNING candidates.person_id, candidates.person_name,
                          candidates.provider_ids_json, candidates.discovered_at,
                          candidates.verification_status,
                          candidates.verification_snapshot_generation,
                          candidates.verification_fingerprint,
                          candidates.alias_scan_id, candidates.alias_scan_status
                """,
                (str(scan_id), safe_limit),
            )
            return [dict(row) for row in cursor.fetchall()]


def finish_readonly_alias_candidate(
    scan_id: str,
    candidate: Dict[str, Any],
    outcome: str,
    *,
    library_id: Optional[str] = None,
    evidence_item_id: Optional[str] = None,
    error: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    """Persist one GET-only result and advance progress exactly once."""
    person_id = str(candidate.get('person_id') or candidate.get('Id') or '').strip()
    if not person_id:
        raise ValueError('只读 alias 核验缺少 Person ID')
    protected = outcome in {
        'protected_library_alias',
        'protected_library_unverifiable',
    }
    linked = outcome == 'linked'
    failed = outcome in {
        'connection_failed', 'invalid_response', 'people_unavailable',
        'identity_alias_only',
    }
    if protected and (not library_id or not evidence_item_id):
        raise ValueError('保护库 alias 结果缺少 ownership 证据')

    with get_db_connection() as conn:
        with conn.cursor() as cursor:
            cursor.execute(
                """
                SELECT alias_scan_status
                FROM person_cleanup_candidates
                WHERE person_id = %s AND alias_scan_id = %s
                FOR UPDATE
                """,
                (person_id, str(scan_id)),
            )
            row = cursor.fetchone()
            if not row or row['alias_scan_status'] != 'checking':
                return None

            if protected:
                cursor.execute(
                    """
                    INSERT INTO person_cleanup_protected_aliases (
                        library_id, person_id, person_name, candidate_fingerprint,
                        protection_status, evidence_item_id, captured_at, updated_at
                    )
                    VALUES (%s, %s, %s, %s, %s, %s, NOW(), NOW())
                    ON CONFLICT (library_id, person_id) DO UPDATE SET
                        person_name = EXCLUDED.person_name,
                        candidate_fingerprint = EXCLUDED.candidate_fingerprint,
                        protection_status = CASE
                            WHEN person_cleanup_protected_aliases.protection_status =
                                 'protected_library_alias'
                            THEN person_cleanup_protected_aliases.protection_status
                            ELSE EXCLUDED.protection_status
                        END,
                        evidence_item_id = EXCLUDED.evidence_item_id,
                        updated_at = NOW()
                    """,
                    (
                        str(library_id), person_id,
                        candidate.get('person_name') or candidate.get('Name'),
                        candidate_fingerprint(candidate), outcome,
                        str(evidence_item_id),
                    ),
                )

            if protected or linked:
                cursor.execute(
                    """
                    DELETE FROM person_cleanup_candidates
                    WHERE person_id = %s AND alias_scan_id = %s
                      AND alias_scan_status = 'checking'
                    """,
                    (person_id, str(scan_id)),
                )
            else:
                cursor.execute(
                    """
                    UPDATE person_cleanup_candidates
                    SET alias_scan_status = 'checked',
                        alias_scan_checked_at = NOW(), alias_scan_error = %s
                    WHERE person_id = %s AND alias_scan_id = %s
                      AND alias_scan_status = 'checking'
                    """,
                    (str(error)[:4000] if error else None, person_id, str(scan_id)),
                )
            if cursor.rowcount != 1:
                return None

            cursor.execute(
                """
                UPDATE person_cleanup_readonly_scans
                SET checked_count = checked_count + 1,
                    protected_count = protected_count + %s,
                    linked_count = linked_count + %s,
                    failed_count = failed_count + %s,
                    updated_at = NOW()
                WHERE scan_id = %s AND state = 'running'
                RETURNING *
                """,
                (int(protected), int(linked), int(failed), str(scan_id)),
            )
            updated = cursor.fetchone()
            if not updated:
                raise RuntimeError('只读 alias scan 已不再运行，拒绝提交结果')
            result = dict(updated)
            result['pending_count'] = max(
                0,
                int(result.get('candidate_total') or 0)
                - int(result.get('checked_count') or 0),
            )
            return result


def stop_readonly_scan(scan_id: str, error: Optional[str] = None) -> None:
    with get_db_connection() as conn:
        with conn.cursor() as cursor:
            cursor.execute(
                """
                UPDATE person_cleanup_readonly_scans
                SET state = 'stopped', stopped_at = NOW(), updated_at = NOW(),
                    last_error = %s
                WHERE scan_id = %s AND state = 'running'
                """,
                (str(error)[:4000] if error else None, str(scan_id)),
            )
            cursor.execute(
                """
                UPDATE person_cleanup_candidates
                SET alias_scan_status = 'pending'
                WHERE alias_scan_id = %s AND alias_scan_status = 'checking'
                """,
                (str(scan_id),),
            )


def complete_readonly_scan(scan_id: str) -> Dict[str, Any]:
    with get_db_connection() as conn:
        with conn.cursor() as cursor:
            cursor.execute(
                """
                SELECT COUNT(*) AS pending
                FROM person_cleanup_candidates
                WHERE alias_scan_id = %s
                  AND alias_scan_status IN ('pending', 'checking')
                """,
                (str(scan_id),),
            )
            if int(cursor.fetchone()['pending']) != 0:
                raise RuntimeError('只读 alias scan 仍有未核验候选，拒绝完成')
            cursor.execute(
                """
                UPDATE person_cleanup_readonly_scans
                SET state = 'completed', completed_at = NOW(), updated_at = NOW()
                WHERE scan_id = %s AND state = 'running'
                RETURNING scan_id
                """,
                (str(scan_id),),
            )
            if not cursor.fetchone():
                raise RuntimeError('只读 alias scan 状态已变化，拒绝完成')
    return get_readonly_scan(scan_id)


def fail_readonly_scan(scan_id: str, error: str) -> None:
    with get_db_connection() as conn:
        with conn.cursor() as cursor:
            cursor.execute(
                """
                UPDATE person_cleanup_readonly_scans
                SET state = 'failed', last_error = %s, updated_at = NOW()
                WHERE scan_id = %s AND state = 'running'
                """,
                (str(error or '只读 alias 核验失败')[:4000], str(scan_id)),
            )
            cursor.execute(
                """
                UPDATE person_cleanup_candidates
                SET alias_scan_status = 'pending'
                WHERE alias_scan_id = %s AND alias_scan_status = 'checking'
                """,
                (str(scan_id),),
            )


def create_alias_proof_run(
    snapshot_generation: int,
    protection_snapshot_hash: str,
    normal_snapshot_hash: str,
    person_snapshot_hash: str,
    candidates: Iterable[Dict[str, Any]],
) -> Dict[str, Any]:
    """Create a persistent GET-only proof run from explicit alias candidates."""
    eligible = []
    seen = set()
    for candidate in candidates:
        person_id = str(candidate.get('person_id') or '').strip()
        if (
            not person_id or person_id in seen
            or candidate.get('verification_status') != 'identity_alias_only'
        ):
            continue
        seen.add(person_id)
        eligible.append(candidate)
    proof_id = str(uuid.uuid4())
    with get_db_connection() as conn:
        with conn.cursor() as cursor:
            cursor.execute(
                """
                UPDATE person_cleanup_alias_proof_runs
                SET state = 'superseded', completed_at = COALESCE(completed_at, NOW()),
                    updated_at = NOW()
                WHERE state IN ('running', 'stopped', 'interrupted')
                """
            )
            cursor.execute(
                """
                INSERT INTO person_cleanup_alias_proof_runs (
                    proof_id, state, snapshot_generation,
                    protection_snapshot_hash, normal_snapshot_hash,
                    person_snapshot_hash, candidate_total
                ) VALUES (%s, 'running', %s, %s, %s, %s, %s)
                """,
                (
                    proof_id, int(snapshot_generation), str(protection_snapshot_hash),
                    str(normal_snapshot_hash),
                    str(person_snapshot_hash), len(eligible),
                ),
            )
            if eligible:
                cursor.executemany(
                    """
                    INSERT INTO person_cleanup_alias_proof_items (
                        proof_id, person_id, person_name, candidate_fingerprint,
                        candidate_provider_ids, proof_state
                    ) VALUES (%s, %s, %s, %s, %s::jsonb, 'pending')
                    """,
                    [
                        (
                            proof_id,
                            str(candidate['person_id']),
                            candidate.get('person_name'),
                            candidate_fingerprint(candidate),
                            json.dumps(candidate.get('provider_ids_json') or {}, ensure_ascii=False),
                        )
                        for candidate in eligible
                    ],
                )
    return get_alias_proof_run(proof_id)


def get_alias_proof_run(proof_id: Optional[str] = None) -> Optional[Dict[str, Any]]:
    with get_db_connection() as conn:
        with conn.cursor() as cursor:
            if proof_id:
                cursor.execute(
                    "SELECT * FROM person_cleanup_alias_proof_runs WHERE proof_id = %s",
                    (str(proof_id),),
                )
            else:
                cursor.execute(
                    """
                    SELECT * FROM person_cleanup_alias_proof_runs
                    ORDER BY started_at DESC, proof_id DESC LIMIT 1
                    """
                )
            row = cursor.fetchone()
            if not row:
                return None
            result = dict(row)
            result['pending_count'] = max(
                0,
                int(result.get('candidate_total') or 0)
                - int(result.get('checked_count') or 0),
            )
            return result


def resume_alias_proof_run(
    proof_id: str,
    snapshot_generation: int,
    protection_snapshot_hash: str,
    normal_snapshot_hash: str,
    person_snapshot_hash: str,
) -> Dict[str, Any]:
    with get_db_connection() as conn:
        with conn.cursor() as cursor:
            cursor.execute(
                """
                UPDATE person_cleanup_alias_proof_runs
                SET state = 'running', stop_requested = FALSE,
                    updated_at = NOW(), last_error = NULL
                WHERE proof_id = %s
                  AND state IN ('stopped', 'interrupted')
                  AND snapshot_generation = %s
                  AND protection_snapshot_hash = %s
                  AND normal_snapshot_hash = %s
                  AND person_snapshot_hash = %s
                RETURNING proof_id
                """,
                (
                    str(proof_id), int(snapshot_generation),
                    str(protection_snapshot_hash),
                    str(normal_snapshot_hash), str(person_snapshot_hash),
                ),
            )
            if not cursor.fetchone():
                raise RuntimeError('只读证明 snapshot/generation 不兼容，拒绝继续')
            cursor.execute(
                """
                UPDATE person_cleanup_alias_proof_items
                SET proof_state = 'pending', error = NULL
                WHERE proof_id = %s AND proof_state = 'checking'
                """,
                (str(proof_id),),
            )
    return get_alias_proof_run(proof_id)


def requeue_changed_alias_proof_items(
    proof_id: str,
    current_candidates: Iterable[Dict[str, Any]],
) -> int:
    """Requeue only completed items whose current candidate fingerprint drifted."""
    current = {
        str(item.get('person_id') or ''): item
        for item in current_candidates
        if str(item.get('person_id') or '')
    }
    changed = 0
    with get_db_connection() as conn:
        with conn.cursor() as cursor:
            cursor.execute(
                """
                SELECT person_id, candidate_fingerprint, proof_state
                FROM person_cleanup_alias_proof_items
                WHERE proof_id = %s AND proof_state NOT IN ('pending', 'checking')
                """,
                (str(proof_id),),
            )
            for row in cursor.fetchall():
                candidate = current.get(str(row['person_id']))
                current_fingerprint = candidate_fingerprint(candidate) if candidate else None
                if current_fingerprint == row['candidate_fingerprint']:
                    continue
                cursor.execute(
                    """
                    UPDATE person_cleanup_alias_proof_items
                    SET person_name = COALESCE(%s, person_name),
                        candidate_provider_ids = %s::jsonb,
                        candidate_fingerprint = COALESCE(%s, candidate_fingerprint),
                        proof_state = 'pending', matched_live_person_id = NULL,
                        matched_live_provider_ids = '{}'::jsonb,
                        query_count = 0, exact_reference_count = 0,
                        error = NULL, checked_at = NULL
                    WHERE proof_id = %s AND person_id = %s
                    """,
                    (
                        candidate.get('person_name') if candidate else None,
                        json.dumps(candidate.get('provider_ids_json') or {}, ensure_ascii=False)
                        if candidate else '{}',
                        current_fingerprint,
                        str(proof_id), str(row['person_id']),
                    ),
                )
                changed += cursor.rowcount
            cursor.execute(
                """
                UPDATE person_cleanup_alias_proof_runs runs
                SET checked_count = counts.checked_count,
                    verified_alias_orphan_count = counts.verified_count,
                    protected_count = counts.protected_count,
                    rejected_count = counts.rejected_count,
                    failed_count = counts.failed_count,
                    updated_at = NOW()
                FROM (
                    SELECT
                        COUNT(*) FILTER (WHERE proof_state NOT IN ('pending', 'checking'))::INTEGER AS checked_count,
                        COUNT(*) FILTER (WHERE proof_state = 'verified_alias_orphan')::INTEGER AS verified_count,
                        COUNT(*) FILTER (WHERE proof_state = 'protected')::INTEGER AS protected_count,
                        COUNT(*) FILTER (WHERE proof_state = ANY(%s))::INTEGER AS rejected_count,
                        COUNT(*) FILTER (WHERE proof_state = ANY(%s))::INTEGER AS failed_count
                    FROM person_cleanup_alias_proof_items WHERE proof_id = %s
                ) counts
                WHERE runs.proof_id = %s
                """,
                (
                    sorted(ALIAS_PROOF_REJECTED_STATES),
                    sorted(ALIAS_PROOF_FAILED_STATES),
                    str(proof_id), str(proof_id),
                ),
            )
    return changed


def claim_alias_proof_items(proof_id: str, limit: int = 4) -> List[Dict[str, Any]]:
    safe_limit = max(1, min(int(limit), 4))
    with get_db_connection() as conn:
        with conn.cursor() as cursor:
            cursor.execute(
                """
                SELECT state, stop_requested
                FROM person_cleanup_alias_proof_runs
                WHERE proof_id = %s
                FOR UPDATE
                """,
                (str(proof_id),),
            )
            run = cursor.fetchone()
            if not run or run['state'] != 'running' or run['stop_requested']:
                return []
            cursor.execute(
                """
                WITH claimed AS (
                    SELECT person_id
                    FROM person_cleanup_alias_proof_items
                    WHERE proof_id = %s AND proof_state = 'pending'
                    ORDER BY person_id ASC
                    FOR UPDATE SKIP LOCKED
                    LIMIT %s
                )
                UPDATE person_cleanup_alias_proof_items items
                SET proof_state = 'checking', error = NULL
                FROM claimed
                WHERE items.proof_id = %s AND items.person_id = claimed.person_id
                RETURNING items.*
                """,
                (str(proof_id), safe_limit, str(proof_id)),
            )
            return [dict(row) for row in cursor.fetchall()]


def finish_alias_proof_item(
    proof_id: str,
    person_id: str,
    outcome: Dict[str, Any],
) -> bool:
    state = str(outcome.get('proof_state') or '')
    if state not in ALIAS_PROOF_STATES:
        raise ValueError(f'不支持的 alias proof 状态: {state}')
    with get_db_connection() as conn:
        with conn.cursor() as cursor:
            cursor.execute(
                """
                UPDATE person_cleanup_alias_proof_items
                SET proof_state = %s,
                    matched_live_person_id = %s,
                    matched_live_provider_ids = %s::jsonb,
                    query_count = %s,
                    exact_reference_count = %s,
                    error = %s,
                    checked_at = NOW()
                WHERE proof_id = %s AND person_id = %s
                  AND proof_state = 'checking'
                RETURNING person_id
                """,
                (
                    state,
                    outcome.get('matched_live_person_id'),
                    json.dumps(outcome.get('matched_live_provider_ids') or {}, ensure_ascii=False),
                    max(0, int(outcome.get('query_count') or 0)),
                    max(0, int(outcome.get('exact_reference_count') or 0)),
                    str(outcome.get('error'))[:4000] if outcome.get('error') else None,
                    str(proof_id), str(person_id),
                ),
            )
            if not cursor.fetchone():
                return False
            cursor.execute(
                """
                UPDATE person_cleanup_alias_proof_runs
                SET checked_count = checked_count + 1,
                    verified_alias_orphan_count = verified_alias_orphan_count + %s,
                    protected_count = protected_count + %s,
                    rejected_count = rejected_count + %s,
                    failed_count = failed_count + %s,
                    updated_at = NOW()
                WHERE proof_id = %s AND state IN ('running', 'stop_requested')
                RETURNING proof_id
                """,
                (
                    int(state == 'verified_alias_orphan'),
                    int(state == 'protected'),
                    int(state in ALIAS_PROOF_REJECTED_STATES),
                    int(state in ALIAS_PROOF_FAILED_STATES),
                    str(proof_id),
                ),
            )
            if not cursor.fetchone():
                raise RuntimeError('只读证明 run 已停止，拒绝提交 item 结果')
            return True


def request_alias_proof_stop(proof_id: str) -> bool:
    with get_db_connection() as conn:
        with conn.cursor() as cursor:
            cursor.execute(
                """
                UPDATE person_cleanup_alias_proof_runs
                SET stop_requested = TRUE, state = 'stop_requested', updated_at = NOW()
                WHERE proof_id = %s AND state = 'running'
                RETURNING proof_id
                """,
                (str(proof_id),),
            )
            return bool(cursor.fetchone())


def alias_proof_stop_requested(proof_id: str) -> bool:
    run = get_alias_proof_run(proof_id)
    return bool(not run or run.get('stop_requested') or run.get('state') != 'running')


def stop_alias_proof_run(proof_id: str) -> None:
    with get_db_connection() as conn:
        with conn.cursor() as cursor:
            cursor.execute(
                """
                UPDATE person_cleanup_alias_proof_items
                SET proof_state = 'pending', error = NULL
                WHERE proof_id = %s AND proof_state = 'checking'
                """,
                (str(proof_id),),
            )
            cursor.execute(
                """
                UPDATE person_cleanup_alias_proof_runs
                SET state = 'stopped', stop_requested = FALSE, updated_at = NOW()
                WHERE proof_id = %s AND state IN ('running', 'stop_requested')
                """,
                (str(proof_id),),
            )


def fail_alias_proof_run(proof_id: str, error: str, stale: bool = False) -> None:
    with get_db_connection() as conn:
        with conn.cursor() as cursor:
            cursor.execute(
                """
                UPDATE person_cleanup_alias_proof_items
                SET proof_state = 'failed_safe', error = %s, checked_at = NOW()
                WHERE proof_id = %s
                  AND proof_state IN ('pending', 'checking', 'verified_alias_orphan')
                """,
                (str(error)[:4000], str(proof_id)),
            )
            cursor.execute(
                """
                UPDATE person_cleanup_alias_proof_runs runs
                SET state = %s, completed_at = NOW(), updated_at = NOW(),
                    last_error = %s,
                    verified_alias_orphan_count = 0,
                    checked_count = counts.checked_count,
                    protected_count = counts.protected_count,
                    rejected_count = counts.rejected_count,
                    failed_count = counts.failed_count
                FROM (
                    SELECT
                        COUNT(*) FILTER (WHERE proof_state NOT IN ('pending', 'checking'))::INTEGER AS checked_count,
                        COUNT(*) FILTER (WHERE proof_state = 'protected')::INTEGER AS protected_count,
                        COUNT(*) FILTER (WHERE proof_state = ANY(%s))::INTEGER AS rejected_count,
                        COUNT(*) FILTER (WHERE proof_state = ANY(%s))::INTEGER AS failed_count
                    FROM person_cleanup_alias_proof_items WHERE proof_id = %s
                ) counts
                WHERE runs.proof_id = %s
                """,
                (
                    'stale' if stale else 'failed', str(error)[:4000],
                    sorted(ALIAS_PROOF_REJECTED_STATES),
                    sorted(ALIAS_PROOF_FAILED_STATES),
                    str(proof_id), str(proof_id),
                ),
            )


def complete_alias_proof_run(proof_id: str, snapshot_generation: int) -> Dict[str, Any]:
    with get_db_connection() as conn:
        with conn.cursor() as cursor:
            cursor.execute(
                """
                SELECT generation, snapshot_state
                FROM person_cleanup_protection_state WHERE singleton = TRUE
                """
            )
            protection = cursor.fetchone()
            if (
                not protection or protection['snapshot_state'] != 'ready'
                or int(protection['generation']) != int(snapshot_generation)
            ):
                raise RuntimeError('保护快照 generation 已漂移')
            cursor.execute(
                """
                SELECT runs.candidate_total, runs.checked_count,
                       COUNT(items.person_id)::INTEGER AS items_total,
                       COUNT(items.person_id) FILTER (
                           WHERE items.proof_state IN ('pending', 'checking')
                       )::INTEGER AS pending,
                       COUNT(items.person_id) FILTER (
                           WHERE NOT (items.proof_state = ANY(%s))
                       )::INTEGER AS unknown
                FROM person_cleanup_alias_proof_runs runs
                LEFT JOIN person_cleanup_alias_proof_items items
                  ON items.proof_id = runs.proof_id
                WHERE runs.proof_id = %s
                GROUP BY runs.candidate_total, runs.checked_count
                """,
                (sorted(ALIAS_PROOF_STATES | {'pending', 'checking'}), str(proof_id)),
            )
            counts = cursor.fetchone()
            if not counts:
                raise RuntimeError('只读证明任务不存在')
            expected = int(counts['candidate_total'] or 0)
            if (
                int(counts['pending'] or 0)
                or int(counts['unknown'] or 0)
                or int(counts['items_total'] or 0) != expected
                or int(counts['checked_count'] or 0) != expected
            ):
                raise RuntimeError('只读证明项目状态或计数不完整')
            cursor.execute(
                """
                UPDATE person_cleanup_alias_proof_runs
                SET state = 'completed', completed_at = NOW(), updated_at = NOW()
                WHERE proof_id = %s AND state = 'running'
                  AND snapshot_generation = %s
                  AND checked_count = candidate_total
                RETURNING proof_id
                """,
                (str(proof_id), int(snapshot_generation)),
            )
            if not cursor.fetchone():
                raise RuntimeError('只读证明计数或状态不完整')
    return get_alias_proof_run(proof_id)


def list_alias_proof_items(
    proof_id: str,
    proof_state: str,
    page: int = 1,
    page_size: int = 20,
) -> Dict[str, Any]:
    safe_page = max(1, int(page))
    safe_size = max(1, min(100, int(page_size)))
    offset = (safe_page - 1) * safe_size
    with get_db_connection() as conn:
        with conn.cursor() as cursor:
            cursor.execute(
                """
                SELECT COUNT(*)::INTEGER AS total
                FROM person_cleanup_alias_proof_items
                WHERE proof_id = %s AND proof_state = %s
                """,
                (str(proof_id), str(proof_state)),
            )
            total = int(cursor.fetchone()['total'] or 0)
            cursor.execute(
                """
                SELECT person_id, person_name, candidate_provider_ids,
                       proof_state, matched_live_person_id,
                       matched_live_provider_ids, query_count,
                       exact_reference_count, error, checked_at
                FROM person_cleanup_alias_proof_items
                WHERE proof_id = %s AND proof_state = %s
                ORDER BY person_name ASC NULLS LAST, person_id ASC
                LIMIT %s OFFSET %s
                """,
                (str(proof_id), str(proof_state), safe_size, offset),
            )
            items = [dict(row) for row in cursor.fetchall()]
    return {'items': items, 'total': total, 'page': safe_page, 'page_size': safe_size}


def get_alias_proof_summary(proof_id: str) -> Dict[str, Any]:
    run = get_alias_proof_run(proof_id)
    if not run:
        raise KeyError('只读证明任务不存在')
    with get_db_connection() as conn:
        with conn.cursor() as cursor:
            cursor.execute(
                """
                SELECT proof_state, COUNT(*)::INTEGER AS count
                FROM person_cleanup_alias_proof_items
                WHERE proof_id = %s
                GROUP BY proof_state ORDER BY count DESC, proof_state ASC
                """,
                (str(proof_id),),
            )
            states = [dict(row) for row in cursor.fetchall()]
    run['states'] = states
    run['items_total'] = sum(int(row['count']) for row in states)
    run['consistent'] = run['items_total'] == int(run.get('candidate_total') or 0)
    return run


def get_latest_completed_alias_proof_source() -> Optional[Dict[str, Any]]:
    with get_db_connection() as conn:
        with conn.cursor() as cursor:
            cursor.execute(
                """
                SELECT runs.*
                FROM person_cleanup_alias_proof_runs runs
                WHERE runs.state = 'completed'
                  AND runs.checked_count = runs.candidate_total
                ORDER BY runs.completed_at DESC NULLS LAST, runs.proof_id DESC
                LIMIT 1
                """
            )
            row = cursor.fetchone()
            return dict(row) if row else None


def _require_complete_alias_proof_source(cursor, source_proof_id: str) -> Dict[str, Any]:
    """Lock and validate the immutable Alias Proof source contract."""
    cursor.execute(
        """
        SELECT state, candidate_total, checked_count,
               verified_alias_orphan_count, protected_count,
               rejected_count, failed_count
        FROM person_cleanup_alias_proof_runs
        WHERE proof_id = %s
        FOR SHARE
        """,
        (str(source_proof_id),),
    )
    source = cursor.fetchone()
    if not source or source['state'] != 'completed':
        raise RuntimeError('Alias Proof source 不是 completed')
    cursor.execute(
        """
        SELECT COUNT(*)::INTEGER AS items_total,
               COUNT(*) FILTER (
                   WHERE proof_state IN ('pending', 'checking')
               )::INTEGER AS pending,
               COUNT(*) FILTER (
                   WHERE NOT (proof_state = ANY(%s))
               )::INTEGER AS unknown,
               COUNT(*) FILTER (
                   WHERE proof_state = 'identity_not_found'
               )::INTEGER AS source_total,
               COUNT(*) FILTER (
                   WHERE proof_state = 'verified_alias_orphan'
               )::INTEGER AS verified_count,
               COUNT(*) FILTER (
                   WHERE proof_state = 'protected'
               )::INTEGER AS protected_count,
               COUNT(*) FILTER (
                   WHERE proof_state = ANY(%s)
               )::INTEGER AS rejected_count,
               COUNT(*) FILTER (
                   WHERE proof_state = ANY(%s)
               )::INTEGER AS failed_count
        FROM person_cleanup_alias_proof_items
        WHERE proof_id = %s
        """,
        (
            sorted(ALIAS_PROOF_STATES | {'pending', 'checking'}),
            sorted(ALIAS_PROOF_REJECTED_STATES),
            sorted(ALIAS_PROOF_FAILED_STATES),
            str(source_proof_id),
        ),
    )
    counts = cursor.fetchone()
    expected = int(source['candidate_total'] or 0)
    if (
        int(source['checked_count'] or 0) != expected
        or int(counts['items_total'] or 0) != expected
        or int(counts['pending'] or 0)
        or int(counts['unknown'] or 0)
        or int(counts['verified_count'] or 0)
           != int(source['verified_alias_orphan_count'] or 0)
        or int(counts['protected_count'] or 0) != int(source['protected_count'] or 0)
        or int(counts['rejected_count'] or 0) != int(source['rejected_count'] or 0)
        or int(counts['failed_count'] or 0) != int(source['failed_count'] or 0)
    ):
        raise RuntimeError('Alias Proof source summary 不完整或不一致')
    cursor.execute(
        """
        SELECT person_id, person_name, candidate_fingerprint,
               candidate_provider_ids, proof_state
        FROM person_cleanup_alias_proof_items
        WHERE proof_id = %s
        ORDER BY person_id ASC
        """,
        (str(source_proof_id),),
    )
    source_rows = [dict(row) for row in cursor.fetchall()]
    source_hash = hashlib.sha256(json.dumps(
        source_rows,
        ensure_ascii=False,
        sort_keys=True,
        separators=(',', ':'),
    ).encode('utf-8')).hexdigest()
    return {
        'candidate_total': expected,
        'source_total': int(counts['source_total'] or 0),
        'source_proof_hash': source_hash,
    }


def get_alias_proof_source_diagnostic(source_proof_id: str) -> Dict[str, Any]:
    """Read the current source hash and completeness without mutating the proof."""
    with get_db_connection() as conn:
        with conn.cursor() as cursor:
            cursor.execute(
                "SELECT proof_id FROM person_cleanup_alias_proof_runs WHERE proof_id = %s",
                (str(source_proof_id),),
            )
            if not cursor.fetchone():
                return {
                    'complete': False,
                    'source_proof_hash': None,
                    'error': 'Alias Proof source 不存在',
                }
            cursor.execute(
                """
                SELECT person_id, person_name, candidate_fingerprint,
                       candidate_provider_ids, proof_state
                FROM person_cleanup_alias_proof_items
                WHERE proof_id = %s
                ORDER BY person_id ASC
                """,
                (str(source_proof_id),),
            )
            source_rows = [dict(row) for row in cursor.fetchall()]
            source_hash = hashlib.sha256(json.dumps(
                source_rows,
                ensure_ascii=False,
                sort_keys=True,
                separators=(',', ':'),
            ).encode('utf-8')).hexdigest()
            try:
                complete = _require_complete_alias_proof_source(
                    cursor, source_proof_id,
                )
            except RuntimeError as exc:
                return {
                    'complete': False,
                    'source_proof_hash': source_hash,
                    'error': str(exc),
                }
            return {
                'complete': True,
                'source_proof_hash': complete['source_proof_hash'],
                'error': None,
            }


def create_stale_index_run(
    source_proof_id: str,
    snapshot_generation: int,
    protection_hash: str,
    normal_people_relationship_hash: str,
    person_snapshot_hash: str,
) -> Dict[str, Any]:
    """Create a new GET-only run from completed identity_not_found evidence."""
    run_id = str(uuid.uuid4())
    with get_db_connection() as conn:
        with conn.cursor() as cursor:
            source = _require_complete_alias_proof_source(cursor, source_proof_id)
            source_total = source['source_total']
            cursor.execute(
                """
                UPDATE person_cleanup_stale_index_runs
                SET state = 'superseded', completed_at = COALESCE(completed_at, NOW()),
                    updated_at = NOW()
                WHERE state IN ('running', 'stopped', 'interrupted', 'stop_requested')
                """
            )
            cursor.execute(
                """
                INSERT INTO person_cleanup_stale_index_runs (
                    run_id, source_proof_id, source_proof_hash, state, snapshot_generation,
                    protection_hash, normal_people_relationship_hash,
                    person_snapshot_hash, candidate_total
                ) VALUES (%s, %s, %s, 'running', %s, %s, %s, %s, %s)
                """,
                (
                    run_id, str(source_proof_id), source['source_proof_hash'],
                    int(snapshot_generation),
                    str(protection_hash), str(normal_people_relationship_hash),
                    str(person_snapshot_hash), source_total,
                ),
            )
            cursor.execute(
                """
                INSERT INTO person_cleanup_stale_index_items (
                    run_id, person_id, person_name, candidate_fingerprint,
                    provider_ids, source_proof_state, forensic_state
                )
                SELECT %s, person_id, person_name, candidate_fingerprint,
                       candidate_provider_ids, proof_state, 'pending'
                FROM person_cleanup_alias_proof_items
                WHERE proof_id = %s AND proof_state = 'identity_not_found'
                ORDER BY person_id ASC
                """,
                (run_id, str(source_proof_id)),
            )
            if cursor.rowcount != source_total:
                raise RuntimeError('identity_not_found source 项目复制不完整')
    return get_stale_index_run(run_id)


def get_stale_index_run(run_id: Optional[str] = None) -> Optional[Dict[str, Any]]:
    with get_db_connection() as conn:
        with conn.cursor() as cursor:
            if run_id:
                cursor.execute(
                    "SELECT * FROM person_cleanup_stale_index_runs WHERE run_id = %s",
                    (str(run_id),),
                )
            else:
                cursor.execute(
                    """
                    SELECT * FROM person_cleanup_stale_index_runs
                    ORDER BY forensic_generation DESC, run_id DESC LIMIT 1
                    """
                )
            row = cursor.fetchone()
            return dict(row) if row else None


def resume_stale_index_run(
    run_id: str,
    snapshot_generation: int,
    protection_hash: str,
    normal_people_relationship_hash: str,
    person_snapshot_hash: str,
) -> Dict[str, Any]:
    with get_db_connection() as conn:
        with conn.cursor() as cursor:
            cursor.execute(
                """
                SELECT source_proof_id, source_proof_hash
                FROM person_cleanup_stale_index_runs
                WHERE run_id = %s AND state IN ('stopped', 'interrupted')
                FOR UPDATE
                """,
                (str(run_id),),
            )
            existing = cursor.fetchone()
            if not existing:
                raise RuntimeError('取证 run 当前不可继续')
            source = _require_complete_alias_proof_source(
                cursor, existing['source_proof_id'],
            )
            if source['source_proof_hash'] != existing['source_proof_hash']:
                raise RuntimeError('Alias Proof source 已变化，不能继续旧 run')
            cursor.execute(
                """
                UPDATE person_cleanup_stale_index_runs
                SET state = 'running', stop_requested = FALSE, last_error = NULL,
                    updated_at = NOW()
                WHERE run_id = %s
                  AND state IN ('stopped', 'interrupted')
                  AND snapshot_generation = %s
                  AND protection_hash = %s
                  AND normal_people_relationship_hash = %s
                  AND person_snapshot_hash = %s
                RETURNING run_id
                """,
                (
                    str(run_id), int(snapshot_generation), str(protection_hash),
                    str(normal_people_relationship_hash), str(person_snapshot_hash),
                ),
            )
            if not cursor.fetchone():
                raise RuntimeError('取证 snapshot 已变化，不能继续旧 run')
            cursor.execute(
                """
                UPDATE person_cleanup_stale_index_items
                SET forensic_state = 'pending', error = NULL
                WHERE run_id = %s AND forensic_state = 'checking'
                """,
                (str(run_id),),
            )
    return get_stale_index_run(run_id)


def requeue_changed_stale_index_items(
    run_id: str,
    current_candidates: Iterable[Dict[str, Any]],
) -> int:
    current = {
        str(item.get('person_id') or ''): item
        for item in current_candidates if str(item.get('person_id') or '')
    }
    changed = 0
    with get_db_connection() as conn:
        with conn.cursor() as cursor:
            cursor.execute(
                """
                SELECT person_id, candidate_fingerprint, forensic_state
                FROM person_cleanup_stale_index_items
                WHERE run_id = %s AND forensic_state NOT IN ('pending', 'checking')
                """,
                (str(run_id),),
            )
            for row in cursor.fetchall():
                candidate = current.get(str(row['person_id']))
                matches = bool(
                    candidate
                    and candidate_fingerprint(candidate) == row['candidate_fingerprint']
                )
                should_requeue = (
                    (not matches and row['forensic_state'] != 'candidate_changed')
                    or (matches and row['forensic_state'] == 'candidate_changed')
                )
                if not should_requeue:
                    continue
                cursor.execute(
                    """
                    UPDATE person_cleanup_stale_index_items
                    SET forensic_state = 'pending', identity_signal = NULL,
                        people_signal = NULL, query_count = 0,
                        actual_people_count = 0, same_name_other_count = 0,
                        different_name_people_count = 0, identity_owner_count = 0,
                        stable_pass_count = 0, error = NULL, checked_at = NULL
                    WHERE run_id = %s AND person_id = %s
                    """,
                    (str(run_id), str(row['person_id'])),
                )
                changed += cursor.rowcount
            _refresh_stale_index_run_counts(cursor, run_id)
    return changed


def claim_stale_index_items(run_id: str, limit: int = 4) -> List[Dict[str, Any]]:
    safe_limit = max(1, min(int(limit), 4))
    with get_db_connection() as conn:
        with conn.cursor() as cursor:
            cursor.execute(
                """
                SELECT state, stop_requested FROM person_cleanup_stale_index_runs
                WHERE run_id = %s FOR UPDATE
                """,
                (str(run_id),),
            )
            run = cursor.fetchone()
            if not run or run['state'] != 'running' or run['stop_requested']:
                return []
            cursor.execute(
                """
                WITH claimed AS (
                    SELECT person_id FROM person_cleanup_stale_index_items
                    WHERE run_id = %s AND forensic_state = 'pending'
                    ORDER BY person_id ASC
                    FOR UPDATE SKIP LOCKED LIMIT %s
                )
                UPDATE person_cleanup_stale_index_items items
                SET forensic_state = 'checking', error = NULL
                FROM claimed
                WHERE items.run_id = %s AND items.person_id = claimed.person_id
                RETURNING items.*
                """,
                (str(run_id), safe_limit, str(run_id)),
            )
            return [dict(row) for row in cursor.fetchall()]


def _refresh_stale_index_run_counts(cursor, run_id: str) -> None:
    cursor.execute(
        """
        UPDATE person_cleanup_stale_index_runs runs
        SET checked_count = counts.checked_count,
            verified_signature_count = counts.verified_count,
            stable_signature_count = counts.stable_count,
            updated_at = NOW()
        FROM (
            SELECT
                COUNT(*) FILTER (WHERE forensic_state NOT IN ('pending', 'checking'))::INTEGER AS checked_count,
                COUNT(*) FILTER (WHERE forensic_state = ANY(%s))::INTEGER AS verified_count,
                COUNT(*) FILTER (WHERE forensic_state = 'stable_stale_index_signature')::INTEGER AS stable_count
            FROM person_cleanup_stale_index_items WHERE run_id = %s
        ) counts
        WHERE runs.run_id = %s
        """,
        (sorted(STALE_INDEX_SIGNATURE_STATES), str(run_id), str(run_id)),
    )


def finish_stale_index_item(
    run_id: str,
    person_id: str,
    outcome: Dict[str, Any],
) -> bool:
    state = str(outcome.get('forensic_state') or '')
    if state not in STALE_INDEX_FORENSIC_STATES:
        raise ValueError(f'不支持的 stale-index forensic 状态: {state}')
    stable_pass_count = int(state == 'verified_stale_index_signature')
    with get_db_connection() as conn:
        with conn.cursor() as cursor:
            cursor.execute(
                """
                SELECT forensic_generation FROM person_cleanup_stale_index_runs
                WHERE run_id = %s AND state IN ('running', 'stop_requested')
                FOR UPDATE
                """,
                (str(run_id),),
            )
            current_run = cursor.fetchone()
            if not current_run:
                return False
            cursor.execute(
                """
                UPDATE person_cleanup_stale_index_items
                SET forensic_state = %s, identity_signal = %s, people_signal = %s,
                    query_count = %s, actual_people_count = %s,
                    same_name_other_count = %s,
                    different_name_people_count = %s, identity_owner_count = %s,
                    stable_pass_count = %s, error = %s, checked_at = NOW()
                WHERE run_id = %s AND person_id = %s AND forensic_state = 'checking'
                RETURNING person_id
                """,
                (
                    state, outcome.get('identity_signal'), outcome.get('people_signal'),
                    max(0, int(outcome.get('query_count') or 0)),
                    max(0, int(outcome.get('actual_people_count') or 0)),
                    max(0, int(outcome.get('same_name_other_count') or 0)),
                    max(0, int(outcome.get('different_name_people_count') or 0)),
                    max(0, int(outcome.get('identity_owner_count') or 0)),
                    stable_pass_count,
                    str(outcome.get('error'))[:4000] if outcome.get('error') else None,
                    str(run_id), str(person_id),
                ),
            )
            if not cursor.fetchone():
                return False
            _refresh_stale_index_run_counts(cursor, run_id)
    return True


def request_stale_index_stop(run_id: str) -> bool:
    with get_db_connection() as conn:
        with conn.cursor() as cursor:
            cursor.execute(
                """
                UPDATE person_cleanup_stale_index_runs
                SET stop_requested = TRUE, state = 'stop_requested', updated_at = NOW()
                WHERE run_id = %s AND state = 'running' RETURNING run_id
                """,
                (str(run_id),),
            )
            return bool(cursor.fetchone())


def stale_index_stop_requested(run_id: str) -> bool:
    run = get_stale_index_run(run_id)
    return bool(not run or run.get('stop_requested') or run.get('state') != 'running')


def stop_stale_index_run(run_id: str) -> None:
    with get_db_connection() as conn:
        with conn.cursor() as cursor:
            cursor.execute(
                """
                UPDATE person_cleanup_stale_index_items SET forensic_state = 'pending', error = NULL
                WHERE run_id = %s AND forensic_state = 'checking'
                """,
                (str(run_id),),
            )
            cursor.execute(
                """
                UPDATE person_cleanup_stale_index_runs
                SET state = 'stopped', stop_requested = FALSE, updated_at = NOW()
                WHERE run_id = %s AND state IN ('running', 'stop_requested')
                """,
                (str(run_id),),
            )


def _persist_stale_index_diagnostics(cursor, run_id: str, diagnostics: Dict[str, Any]) -> None:
    cursor.execute(
        """
        UPDATE person_cleanup_stale_index_runs
        SET final_snapshot_generation = %s,
            final_protection_hash = %s,
            final_normal_people_relationship_hash = %s,
            final_person_snapshot_hash = %s,
            final_source_proof_hash = %s,
            drift_generation = %s,
            drift_protection = %s,
            drift_normal_relationship = %s,
            drift_person = %s,
            drift_source_proof = %s,
            normal_relationship_drift_summary = %s::jsonb,
            person_drift_summary = %s::jsonb,
            protection_drift_summary = %s::jsonb,
            source_proof_drift_summary = %s::jsonb,
            updated_at = NOW()
        WHERE run_id = %s
        """,
        (
            diagnostics.get('final_snapshot_generation'),
            diagnostics.get('final_protection_hash'),
            diagnostics.get('final_normal_people_relationship_hash'),
            diagnostics.get('final_person_snapshot_hash'),
            diagnostics.get('final_source_proof_hash'),
            bool(diagnostics.get('drift_generation')),
            bool(diagnostics.get('drift_protection')),
            bool(diagnostics.get('drift_normal_relationship')),
            bool(diagnostics.get('drift_person')),
            bool(diagnostics.get('drift_source_proof')),
            json.dumps(
                diagnostics.get('normal_relationship_drift_summary') or {},
                ensure_ascii=False,
            ),
            json.dumps(
                diagnostics.get('person_drift_summary') or {},
                ensure_ascii=False,
            ),
            json.dumps(
                diagnostics.get('protection_drift_summary') or {},
                ensure_ascii=False,
            ),
            json.dumps(
                diagnostics.get('source_proof_drift_summary') or {},
                ensure_ascii=False,
            ),
            str(run_id),
        ),
    )


def record_stale_index_final_diagnostics(
    run_id: str,
    diagnostics: Dict[str, Any],
) -> None:
    with get_db_connection() as conn:
        with conn.cursor() as cursor:
            cursor.execute(
                """
                SELECT state FROM person_cleanup_stale_index_runs
                WHERE run_id = %s FOR UPDATE
                """,
                (str(run_id),),
            )
            run = cursor.fetchone()
            if not run or run['state'] != 'running':
                raise RuntimeError('stale-index forensic run 状态已变化')
            _persist_stale_index_diagnostics(cursor, run_id, diagnostics)


def fail_stale_index_run(
    run_id: str,
    error: str,
    stale: bool = False,
    diagnostics: Optional[Dict[str, Any]] = None,
) -> None:
    # The fail-closed transition is authoritative. Diagnostic persistence is
    # deliberately attempted only after that transition commits, so a JSON or
    # schema/write problem in observability can never leave a drifted run live
    # or preserve signature eligibility.
    with get_db_connection() as conn:
        with conn.cursor() as cursor:
            cursor.execute(
                """
                UPDATE person_cleanup_stale_index_items
                SET forensic_state = 'failed_safe', stable_pass_count = 0,
                    error = %s, checked_at = NOW()
                WHERE run_id = %s
                  AND forensic_state IN ('pending', 'checking',
                    'verified_stale_index_signature', 'stable_stale_index_signature')
                """,
                (str(error)[:4000], str(run_id)),
            )
            _refresh_stale_index_run_counts(cursor, run_id)
            cursor.execute(
                """
                UPDATE person_cleanup_stale_index_runs
                SET state = %s, completed_at = NOW(), updated_at = NOW(),
                    last_error = %s, verified_signature_count = 0,
                    stable_signature_count = 0
                WHERE run_id = %s
                """,
                ('stale' if stale else 'failed', str(error)[:4000], str(run_id)),
            )
    if diagnostics is not None:
        try:
            with get_db_connection() as conn:
                with conn.cursor() as cursor:
                    _persist_stale_index_diagnostics(cursor, run_id, diagnostics)
        except Exception:
            logger.error(
                'Stale Index 已失败关闭，但持久化 drift diagnostics 失败 run_id=%s',
                str(run_id),
                exc_info=True,
            )


def complete_stale_index_run(
    run_id: str,
    snapshot_generation: int,
    protection_hash: str,
    normal_people_relationship_hash: str,
    person_snapshot_hash: str,
) -> Dict[str, Any]:
    with get_db_connection() as conn:
        with conn.cursor() as cursor:
            cursor.execute(
                """
                SELECT forensic_generation, source_proof_id, source_proof_hash, state,
                       snapshot_generation, protection_hash,
                       normal_people_relationship_hash, person_snapshot_hash
                FROM person_cleanup_stale_index_runs
                WHERE run_id = %s
                FOR UPDATE
                """,
                (str(run_id),),
            )
            current_run = cursor.fetchone()
            if (
                not current_run or current_run['state'] != 'running'
                or int(current_run['snapshot_generation']) != int(snapshot_generation)
                or current_run['protection_hash'] != str(protection_hash)
                or current_run['normal_people_relationship_hash']
                   != str(normal_people_relationship_hash)
                or current_run['person_snapshot_hash'] != str(person_snapshot_hash)
            ):
                raise RuntimeError('stale-index forensic run 状态或 snapshot hash 已变化')
            source = _require_complete_alias_proof_source(
                cursor, current_run['source_proof_id'],
            )
            if source['source_proof_hash'] != current_run['source_proof_hash']:
                raise RuntimeError('Alias Proof source 已变化，禁止完成 stale-index run')
            cursor.execute(
                """
                SELECT generation, snapshot_state FROM person_cleanup_protection_state
                WHERE singleton = TRUE
                """
            )
            protection = cursor.fetchone()
            if (
                not protection or protection['snapshot_state'] != 'ready'
                or int(protection['generation']) != int(snapshot_generation)
            ):
                raise RuntimeError('保护快照 generation 已漂移')
            cursor.execute(
                """
                SELECT runs.candidate_total, runs.checked_count,
                       COUNT(items.person_id)::INTEGER AS items_total,
                       COUNT(items.person_id) FILTER (
                         WHERE items.forensic_state IN ('pending', 'checking')
                       )::INTEGER AS pending,
                       COUNT(items.person_id) FILTER (
                         WHERE NOT (items.forensic_state = ANY(%s))
                       )::INTEGER AS unknown
                FROM person_cleanup_stale_index_runs runs
                LEFT JOIN person_cleanup_stale_index_items items ON items.run_id = runs.run_id
                WHERE runs.run_id = %s
                GROUP BY runs.candidate_total, runs.checked_count
                """,
                (sorted(STALE_INDEX_FORENSIC_STATES | {'pending', 'checking'}), str(run_id)),
            )
            counts = cursor.fetchone()
            if not counts:
                raise RuntimeError('stale-index forensic run 不存在')
            expected = int(counts['candidate_total'] or 0)
            if (
                int(counts['pending'] or 0) or int(counts['unknown'] or 0)
                or int(counts['items_total'] or 0) != expected
                or int(counts['checked_count'] or 0) != expected
            ):
                raise RuntimeError('stale-index forensic 项目状态或计数不完整')
            cursor.execute(
                """
                SELECT run_id, source_proof_id, source_proof_hash
                FROM person_cleanup_stale_index_runs
                WHERE state = 'completed' AND forensic_generation < %s
                ORDER BY forensic_generation DESC
                LIMIT 1
                """,
                (int(current_run['forensic_generation']),),
            )
            previous_run = cursor.fetchone()
            if previous_run:
                try:
                    previous_source = _require_complete_alias_proof_source(
                        cursor, previous_run['source_proof_id'],
                    )
                except RuntimeError:
                    previous_run = None
                else:
                    if previous_source['source_proof_hash'] != previous_run['source_proof_hash']:
                        previous_run = None
            if previous_run:
                cursor.execute(
                    """
                    UPDATE person_cleanup_stale_index_items current_items
                    SET forensic_state = 'stable_stale_index_signature',
                        stable_pass_count = 2
                    FROM person_cleanup_stale_index_items previous_items
                    WHERE current_items.run_id = %s
                      AND current_items.forensic_state = 'verified_stale_index_signature'
                      AND current_items.stable_pass_count = 1
                      AND previous_items.run_id = %s
                      AND previous_items.person_id = current_items.person_id
                      AND previous_items.candidate_fingerprint = current_items.candidate_fingerprint
                      AND previous_items.forensic_state = ANY(%s)
                      AND previous_items.stable_pass_count >= 1
                    """,
                    (
                        str(run_id), str(previous_run['run_id']),
                        sorted(STALE_INDEX_SIGNATURE_STATES),
                    ),
                )
            _refresh_stale_index_run_counts(cursor, run_id)
            cursor.execute(
                """
                UPDATE person_cleanup_stale_index_runs
                SET state = 'completed', completed_at = NOW(), updated_at = NOW()
                WHERE run_id = %s AND state = 'running'
                  AND snapshot_generation = %s AND checked_count = candidate_total
                  AND protection_hash = %s
                  AND normal_people_relationship_hash = %s
                  AND person_snapshot_hash = %s
                RETURNING run_id
                """,
                (
                    str(run_id), int(snapshot_generation), str(protection_hash),
                    str(normal_people_relationship_hash), str(person_snapshot_hash),
                ),
            )
            if not cursor.fetchone():
                raise RuntimeError('stale-index forensic run 状态已变化')
    return get_stale_index_run(run_id)


def get_stale_index_summary(run_id: str) -> Dict[str, Any]:
    run = get_stale_index_run(run_id)
    if not run:
        raise KeyError('stale-index forensic run 不存在')
    with get_db_connection() as conn:
        with conn.cursor() as cursor:
            cursor.execute(
                """
                SELECT forensic_state, COUNT(*)::INTEGER AS count
                FROM person_cleanup_stale_index_items WHERE run_id = %s
                GROUP BY forensic_state ORDER BY count DESC, forensic_state ASC
                """,
                (str(run_id),),
            )
            states = [dict(row) for row in cursor.fetchall()]
            cursor.execute(
                """
                SELECT identity_signal AS signal, COUNT(*)::INTEGER AS count
                FROM person_cleanup_stale_index_items
                WHERE run_id = %s AND identity_signal IS NOT NULL
                GROUP BY identity_signal ORDER BY count DESC, identity_signal ASC
                """,
                (str(run_id),),
            )
            identity_signals = [dict(row) for row in cursor.fetchall()]
            cursor.execute(
                """
                SELECT people_signal AS signal, COUNT(*)::INTEGER AS count
                FROM person_cleanup_stale_index_items
                WHERE run_id = %s AND people_signal IS NOT NULL
                GROUP BY people_signal ORDER BY count DESC, people_signal ASC
                """,
                (str(run_id),),
            )
            people_signals = [dict(row) for row in cursor.fetchall()]
            cursor.execute(
                """
                SELECT query_count AS value, COUNT(*)::INTEGER AS count
                FROM person_cleanup_stale_index_items
                WHERE run_id = %s AND forensic_state NOT IN ('pending', 'checking')
                GROUP BY query_count ORDER BY query_count ASC
                """,
                (str(run_id),),
            )
            query_distribution = [dict(row) for row in cursor.fetchall()]
            cursor.execute(
                """
                SELECT actual_people_count AS value, COUNT(*)::INTEGER AS count
                FROM person_cleanup_stale_index_items
                WHERE run_id = %s AND forensic_state NOT IN ('pending', 'checking')
                GROUP BY actual_people_count ORDER BY actual_people_count ASC
                """,
                (str(run_id),),
            )
            people_distribution = [dict(row) for row in cursor.fetchall()]
    run.update({
        'states': states,
        'identity_signals': identity_signals,
        'people_signals': people_signals,
        'query_count_distribution': query_distribution,
        'actual_people_count_distribution': people_distribution,
        'items_total': sum(int(row['count']) for row in states),
    })
    run['consistent'] = run['items_total'] == int(run.get('candidate_total') or 0)
    return run


def list_stale_index_items(
    run_id: str,
    value: str,
    *,
    dimension: str = 'forensic_state',
    page: int = 1,
    page_size: int = 20,
) -> Dict[str, Any]:
    columns = {
        'forensic_state': 'forensic_state',
        'identity_signal': 'identity_signal',
        'people_signal': 'people_signal',
    }
    column = columns.get(str(dimension))
    if not column:
        raise ValueError('不支持的 stale-index sample dimension')
    safe_page = max(1, int(page))
    safe_size = max(1, min(100, int(page_size)))
    offset = (safe_page - 1) * safe_size
    with get_db_connection() as conn:
        with conn.cursor() as cursor:
            cursor.execute(
                f"SELECT COUNT(*)::INTEGER AS total FROM person_cleanup_stale_index_items "
                f"WHERE run_id = %s AND {column} = %s",
                (str(run_id), str(value)),
            )
            total = int(cursor.fetchone()['total'] or 0)
            cursor.execute(
                f"""
                SELECT person_id, person_name, provider_ids, source_proof_state,
                       forensic_state, identity_signal, people_signal,
                       query_count, actual_people_count, same_name_other_count,
                       different_name_people_count, identity_owner_count,
                       stable_pass_count, error, checked_at
                FROM person_cleanup_stale_index_items
                WHERE run_id = %s AND {column} = %s
                ORDER BY person_name ASC NULLS LAST, person_id ASC
                LIMIT %s OFFSET %s
                """,
                (str(run_id), str(value), safe_size, offset),
            )
            items = [dict(row) for row in cursor.fetchall()]
    return {'items': items, 'total': total, 'page': safe_page, 'page_size': safe_size}


def list_candidates() -> List[Dict[str, Any]]:
    with get_db_connection() as conn:
        with conn.cursor() as cursor:
            cursor.execute(
                """
                SELECT person_id, person_name, provider_ids_json,
                       discovered_at, last_checked_at, last_error,
                       verification_status, verification_snapshot_generation,
                       verification_fingerprint, alias_scan_id,
                       alias_scan_status, alias_scan_checked_at, alias_scan_error
                FROM person_cleanup_candidates
                ORDER BY person_name ASC, person_id ASC
                """
            )
            candidates = [dict(row) for row in cursor.fetchall()]
    return _exclude_protected_candidates(candidates)


def list_candidates_raw() -> List[Dict[str, Any]]:
    with get_db_connection() as conn:
        with conn.cursor() as cursor:
            cursor.execute(
                """
                SELECT person_id, person_name, provider_ids_json,
                       discovered_at, last_checked_at, last_error,
                       verification_status, verification_snapshot_generation,
                       verification_fingerprint, alias_scan_id,
                       alias_scan_status, alias_scan_checked_at, alias_scan_error
                FROM person_cleanup_candidates
                ORDER BY person_name ASC, person_id ASC
                """
            )
            return [dict(row) for row in cursor.fetchall()]


def list_alias_proof_candidates() -> List[Dict[str, Any]]:
    """Return current candidates with persisted identity-alias evidence.

    A phase-2 readonly scan may leave the candidate verification status as
    unverified while a later safe-preview persists identity_alias_only.  The
    historical evidence is accepted only when its immutable fingerprint still
    matches the current candidate.
    """
    with get_db_connection() as conn:
        with conn.cursor() as cursor:
            cursor.execute(
                """
                SELECT candidates.person_id, candidates.person_name,
                       candidates.provider_ids_json, candidates.discovered_at,
                       candidates.last_checked_at, candidates.last_error,
                       candidates.verification_status,
                       candidates.verification_snapshot_generation,
                       candidates.verification_fingerprint,
                       candidates.alias_scan_id, candidates.alias_scan_status,
                       candidates.alias_scan_checked_at, candidates.alias_scan_error,
                       ARRAY(
                           SELECT DISTINCT items.candidate_fingerprint
                           FROM person_cleanup_job_items items
                           WHERE items.person_id = candidates.person_id
                             AND items.preview_state = 'identity_alias_only'
                       ) AS alias_preview_fingerprints
                FROM person_cleanup_candidates candidates
                WHERE candidates.verification_status = 'identity_alias_only'
                   OR EXISTS (
                        SELECT 1
                        FROM person_cleanup_job_items items
                        WHERE items.person_id = candidates.person_id
                          AND items.preview_state = 'identity_alias_only'
                   )
                ORDER BY candidates.person_id ASC
                """
            )
            rows = [dict(row) for row in cursor.fetchall()]

    accepted = []
    for candidate in rows:
        preview_fingerprints = set(candidate.pop('alias_preview_fingerprints') or [])
        if candidate.get('verification_status') == 'identity_alias_only':
            accepted.append(candidate)
            continue
        if (
            candidate.get('verification_status') == 'unverified'
            and candidate_fingerprint(candidate) in preview_fingerprints
        ):
            candidate['verification_status'] = 'identity_alias_only'
            accepted.append(candidate)
    return accepted


def get_candidates_by_ids(
    person_ids: Iterable[str],
    include_protected: bool = False,
) -> List[Dict[str, Any]]:
    normalized = sorted({str(person_id).strip() for person_id in person_ids if str(person_id).strip()})
    if not normalized:
        return []
    with get_db_connection() as conn:
        with conn.cursor() as cursor:
            cursor.execute(
                """
                SELECT person_id, person_name, provider_ids_json,
                       discovered_at, last_checked_at, last_error,
                       verification_status, verification_snapshot_generation,
                       verification_fingerprint, alias_scan_id,
                       alias_scan_status, alias_scan_checked_at, alias_scan_error
                FROM person_cleanup_candidates
                WHERE person_id = ANY(%s)
                """,
                (normalized,),
            )
            candidates = [dict(row) for row in cursor.fetchall()]
    return candidates if include_protected else _exclude_protected_candidates(candidates)


def remove_candidate(person_id: str) -> None:
    with get_db_connection() as conn:
        with conn.cursor() as cursor:
            cursor.execute(
                "DELETE FROM person_cleanup_candidates WHERE person_id = %s",
                (str(person_id),),
            )


def persist_protected_alias_and_remove_candidate(
    candidate: Dict[str, Any],
    library_id: str,
    protection_status: str,
    evidence_item_id: str,
) -> bool:
    """Atomically persist protected ownership evidence and revoke candidacy."""
    allowed_statuses = {
        'protected_library_alias',
        'protected_library_unverifiable',
    }
    if protection_status not in allowed_statuses:
        raise ValueError(f'不支持的保护库 alias 状态: {protection_status}')
    person_id = str(candidate.get('person_id') or candidate.get('Id') or '').strip()
    normalized_library_id = str(library_id or '').strip()
    normalized_evidence_id = str(evidence_item_id or '').strip()
    if not person_id or not normalized_library_id or not normalized_evidence_id:
        raise ValueError('保护库 alias 证据缺少必要身份字段')

    with get_db_connection() as conn:
        with conn.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO person_cleanup_protected_aliases (
                    library_id, person_id, person_name, candidate_fingerprint,
                    protection_status, evidence_item_id, captured_at, updated_at
                )
                VALUES (%s, %s, %s, %s, %s, %s, NOW(), NOW())
                ON CONFLICT (library_id, person_id) DO UPDATE SET
                    person_name = EXCLUDED.person_name,
                    candidate_fingerprint = EXCLUDED.candidate_fingerprint,
                    protection_status = CASE
                        WHEN person_cleanup_protected_aliases.protection_status =
                             'protected_library_alias'
                        THEN person_cleanup_protected_aliases.protection_status
                        ELSE EXCLUDED.protection_status
                    END,
                    evidence_item_id = EXCLUDED.evidence_item_id,
                    updated_at = NOW()
                """,
                (
                    normalized_library_id,
                    person_id,
                    candidate.get('person_name') or candidate.get('Name'),
                    candidate_fingerprint(candidate),
                    protection_status,
                    normalized_evidence_id,
                ),
            )
            cursor.execute(
                "DELETE FROM person_cleanup_candidates WHERE person_id = %s",
                (person_id,),
            )
            return cursor.rowcount > 0


def mark_candidate_checked(
    person_id: str,
    status: str,
    snapshot_generation: Optional[int],
    error: Optional[str] = None,
) -> None:
    if status not in VERIFICATION_STATES:
        raise ValueError(f'不支持的人物核验状态: {status}')
    candidates = get_candidates_by_ids([person_id], include_protected=True)
    fingerprint = candidate_fingerprint(candidates[0]) if candidates else None
    with get_db_connection() as conn:
        with conn.cursor() as cursor:
            cursor.execute(
                """
                UPDATE person_cleanup_candidates
                SET last_checked_at = NOW(), last_error = %s,
                    verification_status = %s,
                    verification_snapshot_generation = %s,
                    verification_fingerprint = %s
                WHERE person_id = %s
                """,
                (
                    str(error)[:4000] if error else None,
                    status,
                    snapshot_generation,
                    fingerprint,
                    str(person_id),
                ),
            )


def list_protected_libraries() -> List[Dict[str, Any]]:
    with get_db_connection() as conn:
        with conn.cursor() as cursor:
            cursor.execute(
                """
                SELECT library_id, library_name, updated_at,
                       snapshot_state, snapshot_generation,
                       snapshot_completed_at, snapshot_error,
                       (SELECT COUNT(*)
                        FROM person_cleanup_protected_people people
                        WHERE people.library_id = libraries.library_id) AS protected_person_count,
                       (SELECT COUNT(*)
                        FROM person_cleanup_protected_names names
                        WHERE names.library_id = libraries.library_id) AS protected_name_count,
                       (SELECT COUNT(*)
                        FROM person_cleanup_protected_identities identities
                        WHERE identities.library_id = libraries.library_id) AS protected_identity_count
                FROM person_cleanup_protected_libraries libraries
                ORDER BY library_name ASC, library_id ASC
                """
            )
            return [dict(row) for row in cursor.fetchall()]


def replace_protected_libraries(libraries: Iterable[Dict[str, Any]]) -> int:
    normalized = {}
    for library in libraries:
        library_id = str(library.get('library_id') or '').strip()
        if not library_id:
            continue
        normalized[library_id] = str(library.get('library_name') or library_id).strip() or library_id

    with get_db_connection() as conn:
        with conn.cursor() as cursor:
            cursor.execute("SELECT library_id FROM person_cleanup_protected_libraries")
            existing_ids = {str(row['library_id']) for row in cursor.fetchall()}
            selected_ids = sorted(normalized)
            selection_changed = existing_ids != set(selected_ids)
            if selected_ids:
                cursor.execute(
                    "DELETE FROM person_cleanup_protected_libraries WHERE NOT (library_id = ANY(%s))",
                    (selected_ids,),
                )
                cursor.executemany(
                    """
                    INSERT INTO person_cleanup_protected_libraries (library_id, library_name, updated_at)
                    VALUES (%s, %s, NOW())
                    ON CONFLICT (library_id) DO UPDATE SET
                        library_name = EXCLUDED.library_name,
                        updated_at = NOW()
                    """,
                    [(library_id, normalized[library_id]) for library_id in selected_ids],
                )
            else:
                cursor.execute("DELETE FROM person_cleanup_protected_libraries")
            if selection_changed:
                cursor.execute(
                    """
                    UPDATE person_cleanup_protection_state
                    SET generation = generation + 1,
                        snapshot_state = 'pending',
                        snapshot_completed_at = NULL,
                        snapshot_error = NULL,
                        updated_at = NOW()
                    WHERE singleton = TRUE
                    """
                )
                cursor.execute(
                    """
                    UPDATE person_cleanup_protected_libraries
                    SET snapshot_state = 'pending',
                        snapshot_completed_at = NULL,
                        snapshot_error = NULL,
                        updated_at = NOW()
                    """
                )
    return len(normalized)


def get_protection_state() -> Dict[str, Any]:
    with get_db_connection() as conn:
        with conn.cursor() as cursor:
            cursor.execute(
                """
                SELECT generation, snapshot_state, snapshot_completed_at,
                       snapshot_error, updated_at
                FROM person_cleanup_protection_state
                WHERE singleton = TRUE
                """
            )
            row = cursor.fetchone()
            if not row:
                return {
                    'generation': 0,
                    'snapshot_state': 'pending',
                    'snapshot_error': '保护快照状态不存在',
                }
            return dict(row)


def require_ready_protection_snapshot() -> int:
    state = get_protection_state()
    if state.get('snapshot_state') != 'ready':
        raise RuntimeError(
            f"受保护媒体库快照未就绪: {state.get('snapshot_state') or 'unknown'}"
        )
    try:
        return int(state.get('generation'))
    except (TypeError, ValueError) as exc:
        raise RuntimeError('受保护媒体库快照 generation 无效') from exc


def begin_protection_snapshot() -> int:
    with get_db_connection() as conn:
        with conn.cursor() as cursor:
            cursor.execute(
                """
                UPDATE person_cleanup_protection_state
                SET generation = generation + 1,
                    snapshot_state = 'building',
                    snapshot_completed_at = NULL,
                    snapshot_error = NULL,
                    updated_at = NOW()
                WHERE singleton = TRUE
                RETURNING generation
                """
            )
            generation = int(cursor.fetchone()['generation'])
            cursor.execute(
                """
                UPDATE person_cleanup_protected_libraries
                SET snapshot_state = 'building',
                    snapshot_generation = %s,
                    snapshot_completed_at = NULL,
                    snapshot_error = NULL,
                    updated_at = NOW()
                """,
                (generation,),
            )
            cursor.execute(
                """
                UPDATE person_cleanup_candidates
                SET verification_status = 'unverified',
                    verification_snapshot_generation = NULL,
                    verification_fingerprint = NULL,
                    last_checked_at = NULL,
                    last_error = NULL
                """
            )
    return generation


def fail_protection_snapshot(generation: int, error: str) -> None:
    safe_error = str(error or '保护快照构建失败')[:4000]
    with get_db_connection() as conn:
        with conn.cursor() as cursor:
            cursor.execute(
                """
                UPDATE person_cleanup_protection_state
                SET snapshot_state = 'failed', snapshot_error = %s,
                    updated_at = NOW()
                WHERE singleton = TRUE AND generation = %s
                """,
                (safe_error, int(generation)),
            )
            cursor.execute(
                """
                UPDATE person_cleanup_protected_libraries
                SET snapshot_state = 'failed', snapshot_error = %s,
                    updated_at = NOW()
                WHERE snapshot_generation = %s
                """,
                (safe_error, int(generation)),
            )


def complete_protection_snapshot(generation: int) -> None:
    with get_db_connection() as conn:
        with conn.cursor() as cursor:
            cursor.execute(
                """
                UPDATE person_cleanup_protection_state
                SET snapshot_state = 'ready', snapshot_completed_at = NOW(),
                    snapshot_error = NULL, updated_at = NOW()
                WHERE singleton = TRUE AND generation = %s
                RETURNING generation
                """,
                (int(generation),),
            )
            if not cursor.fetchone():
                raise RuntimeError('保护快照 generation 已变化，拒绝标记 ready')
            cursor.execute(
                """
                UPDATE person_cleanup_protected_libraries
                SET snapshot_state = 'ready', snapshot_generation = %s,
                    snapshot_completed_at = NOW(), snapshot_error = NULL,
                    updated_at = NOW()
                WHERE snapshot_generation = %s
                """,
                (int(generation), int(generation)),
            )


def merge_protected_people_for_library(
    library_id: str,
    people: Iterable[Dict[str, Any]],
) -> int:
    normalized = {}
    for person in people:
        person_id = str(person.get('person_id') or '').strip()
        if not person_id:
            continue
        normalized[person_id] = str(person.get('person_name') or '').strip() or None

    with get_db_connection() as conn:
        with conn.cursor() as cursor:
            if normalized:
                cursor.executemany(
                    """
                    INSERT INTO person_cleanup_protected_people (
                        library_id, person_id, person_name, captured_at
                    )
                    VALUES (%s, %s, %s, NOW())
                    ON CONFLICT (library_id, person_id) DO UPDATE SET
                        person_name = COALESCE(EXCLUDED.person_name, person_cleanup_protected_people.person_name)
                    """,
                    [
                        (str(library_id), person_id, normalized[person_id])
                        for person_id in sorted(normalized)
                    ],
                )
    return len(normalized)


def merge_protected_names_for_library(
    library_id: str,
    person_names: Iterable[str],
) -> int:
    normalized = {}
    for person_name in person_names:
        raw_name = str(person_name or '').strip()
        if not raw_name:
            continue
        for protection_key in person_name_protection_keys(raw_name):
            if protection_key:
                normalized[protection_key] = raw_name

    with get_db_connection() as conn:
        with conn.cursor() as cursor:
            if normalized:
                cursor.executemany(
                    """
                    INSERT INTO person_cleanup_protected_names (
                        library_id, normalized_name, person_name, captured_at
                    )
                    VALUES (%s, %s, %s, NOW())
                    ON CONFLICT (library_id, normalized_name) DO UPDATE SET
                        person_name = EXCLUDED.person_name,
                        captured_at = NOW()
                    """,
                    [
                        (str(library_id), protection_key, normalized[protection_key])
                        for protection_key in sorted(normalized)
                    ],
                )
    return len(normalized)


def merge_protected_identities_for_library(
    library_id: str,
    people: Iterable[Dict[str, Any]],
) -> int:
    normalized = {}
    for person in people:
        person_id = str(person.get('person_id') or '').strip()
        person_name = str(person.get('person_name') or '').strip() or None
        if not person_id:
            continue
        identities = canonical_person_provider_identities(
            person.get('provider_ids'),
            strict=True,
        )
        for provider, provider_id in identities:
            normalized[(provider, provider_id, person_id)] = person_name

    with get_db_connection() as conn:
        with conn.cursor() as cursor:
            if normalized:
                cursor.executemany(
                    """
                    INSERT INTO person_cleanup_protected_identities (
                        library_id, provider, provider_id, person_id,
                        person_name, captured_at
                    )
                    VALUES (%s, %s, %s, %s, %s, NOW())
                    ON CONFLICT (library_id, provider, provider_id, person_id)
                    DO UPDATE SET
                        person_name = COALESCE(
                            EXCLUDED.person_name,
                            person_cleanup_protected_identities.person_name
                        ),
                        captured_at = NOW()
                    """,
                    [
                        (
                            str(library_id), provider, provider_id, person_id,
                            normalized[(provider, provider_id, person_id)],
                        )
                        for provider, provider_id, person_id in sorted(normalized)
                    ],
                )
    return len(normalized)


def get_protected_person_ids() -> set[str]:
    with get_db_connection() as conn:
        with conn.cursor() as cursor:
            cursor.execute("SELECT DISTINCT person_id FROM person_cleanup_protected_people")
            return {str(row['person_id']) for row in cursor.fetchall() if row.get('person_id')}


def get_protected_person_names() -> set[str]:
    with get_db_connection() as conn:
        with conn.cursor() as cursor:
            cursor.execute(
                """
                SELECT person_name
                FROM person_cleanup_protected_people
                WHERE NULLIF(BTRIM(person_name), '') IS NOT NULL
                UNION
                SELECT person_name
                FROM person_cleanup_protected_names
                WHERE NULLIF(BTRIM(person_name), '') IS NOT NULL
                """
            )
            return {str(row['person_name']) for row in cursor.fetchall() if row.get('person_name')}


def get_protected_provider_identities() -> set[tuple[str, str]]:
    with get_db_connection() as conn:
        with conn.cursor() as cursor:
            cursor.execute(
                """
                SELECT DISTINCT provider, provider_id
                FROM person_cleanup_protected_identities
                """
            )
            return {
                (str(row['provider']), str(row['provider_id']))
                for row in cursor.fetchall()
                if row.get('provider') and row.get('provider_id')
            }


def get_protected_alias_statuses() -> Dict[str, str]:
    """Return exact protected alias Person IDs; fingerprint drift is irrelevant."""
    with get_db_connection() as conn:
        with conn.cursor() as cursor:
            cursor.execute(
                """
                SELECT aliases.person_id, aliases.protection_status
                FROM person_cleanup_protected_aliases aliases
                JOIN person_cleanup_protected_libraries libraries
                  ON libraries.library_id = aliases.library_id
                ORDER BY aliases.person_id ASC, aliases.protection_status ASC
                """
            )
            statuses = {}
            for row in cursor.fetchall():
                person_id = str(row.get('person_id') or '').strip()
                if person_id:
                    statuses.setdefault(person_id, str(row['protection_status']))
            return statuses


def list_protected_aliases() -> List[Dict[str, Any]]:
    with get_db_connection() as conn:
        with conn.cursor() as cursor:
            cursor.execute(
                """
                SELECT library_id, person_id, person_name,
                       candidate_fingerprint, protection_status,
                       evidence_item_id, captured_at, updated_at
                FROM person_cleanup_protected_aliases
                ORDER BY library_id ASC, person_id ASC
                """
            )
            return [dict(row) for row in cursor.fetchall()]


def get_protection_contract() -> Dict[str, Any]:
    generation = require_ready_protection_snapshot()
    return {
        'generation': generation,
        'person_ids': get_protected_person_ids(),
        'name_keys': build_person_name_protection_keys(get_protected_person_names()),
        'provider_identities': get_protected_provider_identities(),
        'alias_statuses': get_protected_alias_statuses(),
    }


def candidate_protection_reason(
    candidate: Dict[str, Any],
    contract: Optional[Dict[str, Any]] = None,
) -> Optional[str]:
    contract = contract or get_protection_contract()
    person_id = str(candidate.get('person_id') or candidate.get('Id') or '').strip()
    if person_id in contract['person_ids']:
        return 'protected_id'
    alias_status = contract.get('alias_statuses', {}).get(person_id)
    if alias_status:
        return alias_status
    if not person_name_protection_keys(
        candidate.get('person_name') or candidate.get('Name')
    ).isdisjoint(contract['name_keys']):
        return 'protected_name'
    try:
        identities = canonical_person_provider_identities(
            candidate.get('provider_ids_json') or candidate.get('ProviderIds'),
            strict=True,
        )
    except ValueError:
        return 'protected_provider_invalid'
    if not identities.isdisjoint(contract['provider_identities']):
        return 'protected_provider_identity'
    return None


def list_explicit_verified_orphans(person_ids: Iterable[str]) -> List[Dict[str, Any]]:
    generation = require_ready_protection_snapshot()
    candidates = get_candidates_by_ids(person_ids)
    contract = get_protection_contract()
    return [
        candidate
        for candidate in candidates
        if not candidate_protection_reason(candidate, contract)
        and is_explicit_verified_orphan(candidate, generation)
    ]


def create_cleanup_job() -> str:
    job_id = str(uuid.uuid4())
    with get_db_connection() as conn:
        with conn.cursor() as cursor:
            cursor.execute(
                """
                UPDATE person_cleanup_jobs
                SET state = 'superseded', completed_at = NOW(), updated_at = NOW()
                WHERE state = 'preview_ready'
                """
            )
            cursor.execute(
                """
                INSERT INTO person_cleanup_jobs (job_id, state)
                VALUES (%s, 'previewing')
                """,
                (job_id,),
            )
    return job_id


def initialize_cleanup_job_candidate_total(job_id: str, candidate_total: int) -> int:
    """Persist the immutable candidate count before preview item processing starts."""
    normalized_total = max(0, int(candidate_total))
    with get_db_connection() as conn:
        with conn.cursor() as cursor:
            cursor.execute(
                """
                UPDATE person_cleanup_jobs
                SET candidate_total = %s, updated_at = NOW()
                WHERE job_id = %s AND state = 'previewing'
                  AND candidate_total = 0
                RETURNING candidate_total
                """,
                (normalized_total, str(job_id)),
            )
            updated = cursor.fetchone()
            if updated:
                return int(updated['candidate_total'] or 0)
            cursor.execute(
                """
                SELECT state, candidate_total
                FROM person_cleanup_jobs
                WHERE job_id = %s
                """,
                (str(job_id),),
            )
            existing = cursor.fetchone()
            if not existing:
                raise RuntimeError('未找到安全清理预览任务')
            persisted_total = int(existing['candidate_total'] or 0)
            if existing['state'] != 'previewing':
                raise RuntimeError('安全清理预览任务状态已变化')
            if persisted_total != normalized_total:
                raise RuntimeError(
                    '安全清理预览候选总数已固定，禁止按变化后的候选表重算'
                )
            return persisted_total


def add_cleanup_job_item(
    job_id: str,
    candidate: Dict[str, Any],
    preview_state: str,
    error: Optional[str] = None,
) -> None:
    with get_db_connection() as conn:
        with conn.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO person_cleanup_job_items (
                    job_id, person_id, person_name, provider_ids_json,
                    candidate_fingerprint, preview_state, execute_state,
                    last_error
                )
                VALUES (%s, %s, %s, %s::jsonb, %s, %s, 'pending', %s)
                ON CONFLICT (job_id, person_id) DO UPDATE SET
                    preview_state = EXCLUDED.preview_state,
                    last_error = EXCLUDED.last_error
                """,
                (
                    str(job_id),
                    str(candidate.get('person_id') or ''),
                    candidate.get('person_name'),
                    json.dumps(candidate.get('provider_ids_json') or {}, ensure_ascii=False),
                    candidate_fingerprint(candidate),
                    str(preview_state),
                    str(error)[:4000] if error else None,
                ),
            )


def _refresh_cleanup_job_counts(cursor, job_id: str) -> None:
    cursor.execute(
        """
        UPDATE person_cleanup_jobs jobs
        SET protected_count = counts.protected_count,
            linked_count = counts.linked_count,
            verification_failed_count = counts.verification_failed_count,
            verified_orphan_count = counts.verified_orphan_count,
            deleted_count = counts.deleted_count,
            skipped_count = counts.skipped_count,
            failed_count = counts.failed_count,
            updated_at = NOW()
        FROM (
            SELECT
                COUNT(*) FILTER (WHERE preview_state LIKE 'protected%%')::INTEGER AS protected_count,
                COUNT(*) FILTER (WHERE preview_state = 'linked')::INTEGER AS linked_count,
                COUNT(*) FILTER (WHERE preview_state IN (
                    'identity_alias_only', 'people_unavailable',
                    'connection_failed', 'invalid_response'
                ))::INTEGER AS verification_failed_count,
                COUNT(*) FILTER (WHERE preview_state = 'verified_orphan')::INTEGER AS verified_orphan_count,
                COUNT(*) FILTER (WHERE execute_state = 'deleted')::INTEGER AS deleted_count,
                COUNT(*) FILTER (WHERE execute_state LIKE 'skipped%%')::INTEGER AS skipped_count,
                COUNT(*) FILTER (WHERE execute_state IN (
                    'delete_failed', 'delete_ambiguous'
                ))::INTEGER AS failed_count
            FROM person_cleanup_job_items
            WHERE job_id = %s
        ) counts
        WHERE jobs.job_id = %s
        """,
        (str(job_id), str(job_id)),
    )


def finish_cleanup_preview(
    job_id: str,
    snapshot_generation: int,
    error: Optional[str] = None,
) -> None:
    with get_db_connection() as conn:
        with conn.cursor() as cursor:
            _refresh_cleanup_job_counts(cursor, job_id)
            cursor.execute(
                """
                UPDATE person_cleanup_jobs
                SET state = %s, snapshot_generation = %s,
                    preview_completed_at = NOW(),
                    completed_at = CASE WHEN %s IS NULL THEN NULL ELSE NOW() END,
                    updated_at = NOW(), last_error = %s
                WHERE job_id = %s AND state = 'previewing'
                """,
                (
                    'failed' if error else 'preview_ready',
                    int(snapshot_generation),
                    error,
                    str(error)[:4000] if error else None,
                    str(job_id),
                ),
            )


def fail_cleanup_job(job_id: str, error: str) -> None:
    with get_db_connection() as conn:
        with conn.cursor() as cursor:
            _refresh_cleanup_job_counts(cursor, job_id)
            cursor.execute(
                """
                UPDATE person_cleanup_jobs
                SET state = 'failed', completed_at = NOW(), updated_at = NOW(),
                    last_error = %s
                WHERE job_id = %s
                """,
                (str(error)[:4000], str(job_id)),
            )


def get_cleanup_job(job_id: str, include_items: bool = False) -> Optional[Dict[str, Any]]:
    with get_db_connection() as conn:
        with conn.cursor() as cursor:
            cursor.execute(
                """
                SELECT * FROM person_cleanup_jobs WHERE job_id = %s
                """,
                (str(job_id),),
            )
            row = cursor.fetchone()
            if not row:
                return None
            result = dict(row)
            if include_items:
                cursor.execute(
                    """
                    SELECT person_id, person_name, provider_ids_json,
                           preview_state, execute_state, post_attempts,
                           submitted_at, completed_at, last_error
                    FROM person_cleanup_job_items
                    WHERE job_id = %s
                    ORDER BY person_name ASC, person_id ASC
                    """,
                    (str(job_id),),
                )
                result['items'] = [dict(item) for item in cursor.fetchall()]
            return result


def get_latest_cleanup_job(include_items: bool = False) -> Optional[Dict[str, Any]]:
    """Return the newest persisted cleanup job without changing its state."""
    with get_db_connection() as conn:
        with conn.cursor() as cursor:
            cursor.execute(
                """
                SELECT job_id
                FROM person_cleanup_jobs
                ORDER BY created_at DESC, job_id DESC
                LIMIT 1
                """
            )
            row = cursor.fetchone()
    if not row:
        return None
    return get_cleanup_job(str(row['job_id']), include_items=include_items)


def list_cleanup_jobs(limit: int = 20) -> List[Dict[str, Any]]:
    """Return recent persisted cleanup jobs without changing any job state."""
    normalized_limit = max(1, min(100, int(limit)))
    with get_db_connection() as conn:
        with conn.cursor() as cursor:
            cursor.execute(
                """
                SELECT
                    job_id, state, candidate_total,
                    verified_orphan_count, verification_failed_count,
                    protected_count, linked_count, deleted_count,
                    skipped_count, failed_count,
                    created_at, preview_completed_at, completed_at, last_error
                FROM person_cleanup_jobs
                ORDER BY created_at DESC, job_id DESC
                LIMIT %s
                """,
                (normalized_limit,),
            )
            return [dict(row) for row in cursor.fetchall()]


def get_cleanup_job_preview_summary(job_id: str) -> Optional[Dict[str, Any]]:
    """Read the complete persisted preview-state distribution for one job."""
    with get_db_connection() as conn:
        with conn.cursor() as cursor:
            cursor.execute(
                """
                SELECT state, candidate_total
                FROM person_cleanup_jobs
                WHERE job_id = %s
                """,
                (str(job_id),),
            )
            job = cursor.fetchone()
            if not job:
                return None
            cursor.execute(
                """
                SELECT preview_state, COUNT(*)::INTEGER AS count
                FROM person_cleanup_job_items
                WHERE job_id = %s
                GROUP BY preview_state
                ORDER BY count DESC, preview_state ASC
                """,
                (str(job_id),),
            )
            grouped = [dict(row) for row in cursor.fetchall()]

    job_state = str(job.get('state') or 'unknown')
    candidate_total = int(job.get('candidate_total') or 0)
    actual_counts = {
        str(row.get('preview_state') or 'unknown'): int(row.get('count') or 0)
        for row in grouped
    }
    items_total = sum(actual_counts.values())
    expected_states = (
        'verified_orphan',
        'identity_alias_only',
        'people_unavailable',
        'invalid_response',
        'connection_failed',
        'linked',
        'protected_library_alias',
        'protected_library_unverifiable',
        'protected_id',
        'protected_name',
        'protected_provider_identity',
    )
    ordered_statuses = list(expected_states)
    ordered_statuses.extend(sorted(set(actual_counts) - set(expected_states)))
    states = []
    for status in ordered_statuses:
        count = int(actual_counts.get(status) or 0)
        states.append({
            'status': status,
            'count': count,
            'percentage': round((count * 100.0 / candidate_total), 2)
            if candidate_total else 0.0,
        })

    counts = {row['status']: row['count'] for row in states}
    verified_orphan = int(counts.get('verified_orphan') or 0)
    warning = None
    partial_preview_states = {'previewing', 'stopped', 'failed'}
    if items_total > candidate_total:
        warning = (
            '持久化预览明细数量超过任务启动时的候选总数：'
            f'job_items={items_total}, candidate_total={candidate_total}'
        )
    elif job_state not in partial_preview_states and items_total != candidate_total:
        warning = (
            '已完成预览的明细数量与任务启动时的候选总数不一致：'
            f'job_items={items_total}, candidate_total={candidate_total}'
        )
    preview_complete = items_total == candidate_total
    return {
        'total': candidate_total,
        'candidate_total': candidate_total,
        'items_total': items_total,
        'preview_progress_count': items_total,
        'preview_expected_count': candidate_total,
        'preview_complete': preview_complete,
        'verified_orphan': verified_orphan,
        'non_verified_orphan': max(0, items_total - verified_orphan),
        'states': states,
        'counts': counts,
        'consistent': warning is None,
        'consistency_warning': warning,
    }


def list_cleanup_job_preview_items(
    job_id: str,
    preview_state: str,
    page: int = 1,
    page_size: int = 5,
) -> Dict[str, Any]:
    """Page persisted preview items for an exact state; never re-verify them."""
    normalized_page = max(1, int(page))
    normalized_page_size = max(1, min(50, int(page_size)))
    offset = (normalized_page - 1) * normalized_page_size
    with get_db_connection() as conn:
        with conn.cursor() as cursor:
            cursor.execute(
                """
                SELECT COUNT(*)::INTEGER AS total
                FROM person_cleanup_job_items
                WHERE job_id = %s AND preview_state = %s
                """,
                (str(job_id), str(preview_state)),
            )
            total = int(cursor.fetchone()['total'] or 0)
            cursor.execute(
                """
                SELECT person_id, person_name, provider_ids_json,
                       preview_state, execute_state, last_error
                FROM person_cleanup_job_items
                WHERE job_id = %s AND preview_state = %s
                ORDER BY person_name ASC NULLS LAST, person_id ASC
                LIMIT %s OFFSET %s
                """,
                (
                    str(job_id),
                    str(preview_state),
                    normalized_page_size,
                    offset,
                ),
            )
            items = [dict(row) for row in cursor.fetchall()]
    return {
        'status': str(preview_state),
        'items': items,
        'total': total,
        'page': normalized_page,
        'page_size': normalized_page_size,
    }


def issue_cleanup_confirmation_token(job_id: str) -> str:
    token = secrets.token_urlsafe(32)
    token_hash = hashlib.sha256(token.encode('utf-8')).hexdigest()
    with get_db_connection() as conn:
        with conn.cursor() as cursor:
            cursor.execute(
                """
                UPDATE person_cleanup_jobs
                SET confirmation_token_hash = %s, updated_at = NOW()
                WHERE job_id = %s AND state = 'preview_ready'
                RETURNING job_id
                """,
                (token_hash, str(job_id)),
            )
            if not cursor.fetchone():
                raise RuntimeError('清理预览尚未就绪或已失效')
    return token


def confirm_cleanup_job(job_id: str, token: str) -> None:
    token_hash = hashlib.sha256(str(token).encode('utf-8')).hexdigest()
    with get_db_connection() as conn:
        with conn.cursor() as cursor:
            cursor.execute(
                """
                UPDATE person_cleanup_jobs
                SET state = 'confirmed', confirmed_at = NOW(), updated_at = NOW(),
                    confirmation_token_hash = NULL
                WHERE job_id = %s AND state = 'preview_ready'
                  AND confirmation_token_hash = %s
                RETURNING job_id
                """,
                (str(job_id), token_hash),
            )
            if not cursor.fetchone():
                raise RuntimeError('确认令牌无效、预览已失效或任务状态已变化')


def revert_confirmed_cleanup_job(job_id: str, error: str) -> None:
    with get_db_connection() as conn:
        with conn.cursor() as cursor:
            cursor.execute(
                """
                UPDATE person_cleanup_jobs
                SET state = 'preview_ready', confirmed_at = NULL,
                    updated_at = NOW(), last_error = %s
                WHERE job_id = %s AND state = 'confirmed'
                """,
                (str(error)[:4000], str(job_id)),
            )


def start_cleanup_job(job_id: str) -> None:
    with get_db_connection() as conn:
        with conn.cursor() as cursor:
            cursor.execute(
                """
                UPDATE person_cleanup_jobs
                SET state = 'running', started_at = NOW(), updated_at = NOW(),
                    last_error = NULL
                WHERE job_id = %s AND state = 'confirmed'
                RETURNING job_id
                """,
                (str(job_id),),
            )
            if not cursor.fetchone():
                raise RuntimeError('清理任务未确认或状态已变化')


def request_cleanup_job_stop(job_id: str) -> bool:
    with get_db_connection() as conn:
        with conn.cursor() as cursor:
            cursor.execute(
                """
                UPDATE person_cleanup_jobs
                SET stop_requested = TRUE,
                    state = CASE WHEN state = 'running' THEN 'stop_requested' ELSE state END,
                    updated_at = NOW()
                WHERE job_id = %s
                  AND state IN ('previewing', 'confirmed', 'running', 'stop_requested')
                RETURNING job_id
                """,
                (str(job_id),),
            )
            return bool(cursor.fetchone())


def cleanup_job_stop_requested(job_id: str) -> bool:
    job = get_cleanup_job(job_id)
    return bool(job and job.get('stop_requested'))


def list_cleanup_job_orphans(job_id: str) -> List[Dict[str, Any]]:
    with get_db_connection() as conn:
        with conn.cursor() as cursor:
            cursor.execute(
                """
                SELECT person_id, person_name, provider_ids_json,
                       candidate_fingerprint, preview_state, execute_state,
                       post_attempts
                FROM person_cleanup_job_items
                WHERE job_id = %s AND preview_state = 'verified_orphan'
                  AND execute_state = 'pending'
                ORDER BY person_id ASC
                """,
                (str(job_id),),
            )
            return [dict(row) for row in cursor.fetchall()]


def reserve_person_delete_attempt(person_id: str, operation_id: Optional[str] = None) -> bool:
    """Persist the irreversible at-most-once boundary before any delete POST."""
    operation_id = str(operation_id or f'manual:{uuid.uuid4()}')
    with get_db_connection() as conn:
        with conn.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO person_cleanup_delete_attempts (
                    person_id, operation_id, state, post_attempts
                )
                VALUES (%s, %s, 'submitting', 1)
                ON CONFLICT (person_id) DO NOTHING
                RETURNING person_id
                """,
                (str(person_id), operation_id),
            )
            return cursor.fetchone() is not None


def finish_person_delete_attempt(
    person_id: str,
    state: str,
    error: Optional[str] = None,
) -> None:
    if state not in {'confirmed', 'failed', 'ambiguous'}:
        raise ValueError(f'不支持的删除尝试状态: {state}')
    with get_db_connection() as conn:
        with conn.cursor() as cursor:
            cursor.execute(
                """
                UPDATE person_cleanup_delete_attempts
                SET state = %s, completed_at = NOW(), last_error = %s
                WHERE person_id = %s AND post_attempts = 1
                """,
                (state, str(error)[:4000] if error else None, str(person_id)),
            )


def mark_cleanup_job_item(
    job_id: str,
    person_id: str,
    execute_state: str,
    error: Optional[str] = None,
    *,
    submitted: bool = False,
    completed: bool = False,
) -> bool:
    with get_db_connection() as conn:
        with conn.cursor() as cursor:
            if submitted:
                cursor.execute(
                    """
                    INSERT INTO person_cleanup_delete_attempts (
                        person_id, operation_id, state, post_attempts
                    )
                    VALUES (%s, %s, 'submitting', 1)
                    ON CONFLICT (person_id) DO NOTHING
                    RETURNING person_id
                    """,
                    (str(person_id), f'job:{job_id}'),
                )
                if cursor.fetchone() is None:
                    return False
            submitted_guard = " AND execute_state = 'pending' AND post_attempts = 0" if submitted else ""
            cursor.execute(
                f"""
                UPDATE person_cleanup_job_items
                SET execute_state = %s,
                    last_error = %s,
                    post_attempts = post_attempts + CASE WHEN %s THEN 1 ELSE 0 END,
                    submitted_at = CASE WHEN %s THEN NOW() ELSE submitted_at END,
                    completed_at = CASE WHEN %s THEN NOW() ELSE completed_at END
                WHERE job_id = %s AND person_id = %s{submitted_guard}
                RETURNING person_id
                """,
                (
                    str(execute_state),
                    str(error)[:4000] if error else None,
                    bool(submitted), bool(submitted), bool(completed),
                    str(job_id), str(person_id),
                ),
            )
            changed = cursor.fetchone() is not None
            if submitted and not changed:
                cursor.execute(
                    """
                    DELETE FROM person_cleanup_delete_attempts
                    WHERE person_id = %s AND operation_id = %s AND state = 'submitting'
                    """,
                    (str(person_id), f'job:{job_id}'),
                )
            if changed:
                attempt_state = {
                    'deleted': 'confirmed',
                    'delete_failed': 'failed',
                    'delete_ambiguous': 'ambiguous',
                }.get(str(execute_state))
                if attempt_state:
                    cursor.execute(
                        """
                        UPDATE person_cleanup_delete_attempts
                        SET state = %s, completed_at = NOW(), last_error = %s
                        WHERE person_id = %s AND operation_id = %s
                        """,
                        (
                            attempt_state,
                            str(error)[:4000] if error else None,
                            str(person_id),
                            f'job:{job_id}',
                        ),
                    )
                _refresh_cleanup_job_counts(cursor, job_id)
            return changed


def finish_cleanup_job(job_id: str, stopped: bool = False, error: Optional[str] = None) -> None:
    with get_db_connection() as conn:
        with conn.cursor() as cursor:
            _refresh_cleanup_job_counts(cursor, job_id)
            cursor.execute(
                """
                UPDATE person_cleanup_jobs
                SET state = %s, completed_at = NOW(), updated_at = NOW(),
                    last_error = %s
                WHERE job_id = %s
                """,
                (
                    'failed' if error else ('stopped' if stopped else 'completed'),
                    str(error)[:4000] if error else None,
                    str(job_id),
                ),
            )
