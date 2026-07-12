from typing import Any, Dict, Iterable, List, Optional


def find_ghost_candidates(
    all_people: Iterable[Dict[str, Any]],
    referenced_person_ids: Iterable[str],
) -> List[Dict[str, Any]]:
    referenced = {str(person_id) for person_id in referenced_person_ids if person_id}
    return [
        person
        for person in all_people
        if person.get('Id') and str(person['Id']) not in referenced
    ]


def classify_reference_check(result: Optional[Dict[str, Any]]) -> str:
    """Only an explicit zero count is safe enough to proceed with deletion."""
    if not isinstance(result, dict):
        return 'verification_failed'
    count = result.get('count')
    if not isinstance(count, int) or isinstance(count, bool) or count < 0:
        return 'verification_failed'
    return 'orphan' if count == 0 else 'linked'
