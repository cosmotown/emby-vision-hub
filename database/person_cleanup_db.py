import json
from typing import Any, Dict, Iterable, List, Optional

from .connection import get_db_connection


def replace_candidates(candidates: Iterable[Dict[str, Any]]) -> int:
    normalized = []
    for candidate in candidates:
        person_id = str(candidate.get('Id') or '').strip()
        if not person_id:
            continue
        normalized.append((
            person_id,
            candidate.get('Name') or '未知人物',
            json.dumps(candidate.get('ProviderIds') or {}, ensure_ascii=False),
        ))

    with get_db_connection() as conn:
        with conn.cursor() as cursor:
            cursor.execute("DELETE FROM person_cleanup_candidates")
            if normalized:
                cursor.executemany(
                    """
                    INSERT INTO person_cleanup_candidates (
                        person_id, person_name, provider_ids_json
                    )
                    VALUES (%s, %s, %s::jsonb)
                    """,
                    normalized,
                )
    return len(normalized)


def list_candidates() -> List[Dict[str, Any]]:
    with get_db_connection() as conn:
        with conn.cursor() as cursor:
            cursor.execute(
                """
                SELECT person_id, person_name, provider_ids_json,
                       discovered_at, last_checked_at, last_error
                FROM person_cleanup_candidates
                ORDER BY person_name ASC, person_id ASC
                """
            )
            return [dict(row) for row in cursor.fetchall()]


def get_candidates_by_ids(person_ids: Iterable[str]) -> List[Dict[str, Any]]:
    normalized = sorted({str(person_id).strip() for person_id in person_ids if str(person_id).strip()})
    if not normalized:
        return []
    with get_db_connection() as conn:
        with conn.cursor() as cursor:
            cursor.execute(
                """
                SELECT person_id, person_name, provider_ids_json,
                       discovered_at, last_checked_at, last_error
                FROM person_cleanup_candidates
                WHERE person_id = ANY(%s)
                """,
                (normalized,),
            )
            return [dict(row) for row in cursor.fetchall()]


def remove_candidate(person_id: str) -> None:
    with get_db_connection() as conn:
        with conn.cursor() as cursor:
            cursor.execute(
                "DELETE FROM person_cleanup_candidates WHERE person_id = %s",
                (str(person_id),),
            )


def mark_candidate_checked(person_id: str, error: Optional[str] = None) -> None:
    with get_db_connection() as conn:
        with conn.cursor() as cursor:
            cursor.execute(
                """
                UPDATE person_cleanup_candidates
                SET last_checked_at = NOW(), last_error = %s
                WHERE person_id = %s
                """,
                (str(error)[:4000] if error else None, str(person_id)),
            )
