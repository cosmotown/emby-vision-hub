import json
import re
from typing import Any, Dict, Iterable, List, Optional


_LEADING_INDEX_MARKER_RE = re.compile(
    r'^(?:(?:\d+\s*)?[①②③④⑤⑥⑦⑧⑨⑩⑪⑫⑬⑭⑮⑯⑰⑱⑲⑳]+\s*|'
    r'\d+\s*[.)、:：]\s*|'
    r'\d+(?=[\u3040-\u30ff\u3400-\u9fff\uac00-\ud7af]))'
)


def normalize_person_name(value: Any) -> str:
    """Normalize a person name for conservative protected-library matching."""
    return ' '.join(str(value or '').split()).casefold()


def person_name_protection_keys(value: Any) -> set[str]:
    """Return exact and conservative alias keys used only to expand protection."""
    normalized = normalize_person_name(value)
    if not normalized:
        return set()
    keys = {normalized}
    stripped = _LEADING_INDEX_MARKER_RE.sub('', normalized, count=1).strip()
    if len(stripped) >= 2:
        keys.add(stripped)
    return keys


def build_person_name_protection_keys(values: Iterable[Any]) -> set[str]:
    keys = set()
    for value in values:
        keys.update(person_name_protection_keys(value))
    return keys


def find_ghost_candidates(
    all_people: Iterable[Dict[str, Any]],
    referenced_person_ids: Iterable[str],
    protected_person_names: Iterable[str] = (),
) -> List[Dict[str, Any]]:
    referenced = {str(person_id) for person_id in referenced_person_ids if person_id}
    protected_names = build_person_name_protection_keys(protected_person_names)
    return [
        person
        for person in all_people
        if person.get('Id')
        and str(person['Id']) not in referenced
        and person_name_protection_keys(person.get('Name')).isdisjoint(protected_names)
    ]


def media_item_has_exact_person_reference(
    item: Dict[str, Any],
    person_id: str,
    person_name: Optional[str] = None,
) -> Optional[bool]:
    '''
    Verify the actual embedded People list instead of trusting Emby's PersonIds
    filter alone. Some duplicate Person records share provider identities and the
    filter can return media linked to a different Person row.

    Returns:
      True  -> the exact Person ID is embedded, or a name-only People row matches.
      False -> People is usable and the target is not embedded.
      None  -> People is missing/empty, so deletion must fail closed.
    '''
    people = item.get('People')
    if not isinstance(people, list) or not people:
        return None

    target_id = str(person_id or '').strip()
    target_name_keys = person_name_protection_keys(person_name)
    saw_usable_person = False

    for person in people:
        if not isinstance(person, dict):
            continue
        embedded_id = str(person.get('Id') or '').strip()
        embedded_name = str(person.get('Name') or '').strip()

        if embedded_id:
            saw_usable_person = True
            if embedded_id == target_id:
                return True
            # An ID-bearing row with the same name is still a different Person.
            continue

        if embedded_name:
            saw_usable_person = True
            if target_name_keys and not person_name_protection_keys(
                embedded_name
            ).isdisjoint(target_name_keys):
                return True

    return False if saw_usable_person else None


def classify_reference_check(result: Optional[Dict[str, Any]]) -> str:
    """Only an explicit zero count is safe enough to proceed with deletion."""
    if not isinstance(result, dict):
        return 'verification_failed'
    count = result.get('count')
    if not isinstance(count, int) or isinstance(count, bool) or count < 0:
        return 'verification_failed'
    return 'orphan' if count == 0 else 'linked'


def build_identity_provider_pairs(provider_ids: Any) -> List[str]:
    """Build Emby's exact provider filter for person identity comparison."""
    if isinstance(provider_ids, str):
        try:
            provider_ids = json.loads(provider_ids)
        except (TypeError, ValueError):
            return []
    if not isinstance(provider_ids, dict):
        return []
    supported = {'tmdb': 'tmdb', 'imdb': 'imdb', 'douban': 'douban'}
    pairs = []
    for key, value in provider_ids.items():
        provider = supported.get(str(key).strip().lower())
        normalized_value = str(value or '').strip()
        if provider and normalized_value and ',' not in normalized_value:
            pairs.append(f'{provider}.{normalized_value}')
    return sorted(set(pairs))
