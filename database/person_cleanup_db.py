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


def list_protected_libraries() -> List[Dict[str, Any]]:
    with get_db_connection() as conn:
        with conn.cursor() as cursor:
            cursor.execute(
                """
                SELECT library_id, library_name, updated_at,
                       (SELECT COUNT(*)
                        FROM person_cleanup_protected_people people
                        WHERE people.library_id = libraries.library_id) AS protected_person_count
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
            selected_ids = sorted(normalized)
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
    return len(normalized)


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


def get_protected_person_ids() -> set[str]:
    with get_db_connection() as conn:
        with conn.cursor() as cursor:
            cursor.execute("SELECT DISTINCT person_id FROM person_cleanup_protected_people")
            return {str(row['person_id']) for row in cursor.fetchall() if row.get('person_id')}
