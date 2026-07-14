import json
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


def is_verified_orphan_candidate(candidate: Optional[Dict[str, Any]]) -> bool:
    """A candidate must have a successful explicit verification before selection."""
    if not isinstance(candidate, dict):
        return False
    return bool(candidate.get('last_checked_at')) and not candidate.get('last_error')


def build_identity_provider_pairs(provider_ids: Any) -> List[str]:
    """Build Emby's exact provider filter for person identity comparison."""
    if isinstance(provider_ids, str):
        try:
            provider_ids = json.loads(provider_ids)
        except (TypeError, ValueError):
            return []
    if not isinstance(provider_ids, dict):
        return []
    supported = {'tmdb': 'tmdb', 'imdb': 'imdb'}
    pairs = []
    for key, value in provider_ids.items():
        provider = supported.get(str(key).strip().lower())
        normalized_value = str(value or '').strip()
        if provider and normalized_value and ',' not in normalized_value:
            pairs.append(f'{provider}.{normalized_value}')
    return sorted(set(pairs))
