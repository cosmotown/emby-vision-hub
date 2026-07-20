import re
from typing import Any, Dict, List


_LEADING_TRANSLATION_INDEX_RE = re.compile(
    r'^\s*(?:(?:\d+\s*)?[①②③④⑤⑥⑦⑧⑨⑩⑪⑫⑬⑭⑮⑯⑰⑱⑲⑳]+|'
    r'\d+\s*[.)、:：]|'
    r'\d+(?=[\u3040-\u30ff\u3400-\u9fff\uac00-\ud7af]))'
)


def is_safe_actor_name_translation(original_name: Any, translated_name: Any) -> bool:
    """Reject empty, oversized, or newly numbered actor-name translations."""
    original = str(original_name or '').strip()
    if not isinstance(translated_name, str):
        return False
    translated = translated_name.strip()
    if not original or not translated or len(translated) > 120:
        return False
    translated_has_index = bool(_LEADING_TRANSLATION_INDEX_RE.match(translated))
    original_starts_with_digit = bool(original.lstrip()[:1].isdigit())
    if translated_has_index and not original_starts_with_digit:
        return False
    return True


def apply_safe_actor_name_translations(
    cast_list: List[Dict[str, Any]],
    translation_map: Dict[str, str],
) -> List[str]:
    """Apply actor-name translations without introducing numbering or collisions."""
    proposed_names = []
    rejected = []
    for actor in cast_list:
        original = str(actor.get('name') or '').strip()
        translated = translation_map.get(original)
        if translated is None:
            proposed_names.append(original)
            continue
        if not is_safe_actor_name_translation(original, translated):
            proposed_names.append(original)
            rejected.append(original)
            continue
        proposed_names.append(str(translated).strip())

    target_groups = {}
    for index, name in enumerate(proposed_names):
        normalized = name.casefold()
        if normalized:
            target_groups.setdefault(normalized, []).append(index)

    for indexes in target_groups.values():
        source_names = {
            str(cast_list[index].get('name') or '').strip().casefold()
            for index in indexes
        }
        if len(indexes) > 1 and len(source_names) > 1:
            for index in indexes:
                original = str(cast_list[index].get('name') or '').strip()
                proposed_names[index] = original
                rejected.append(original)

    for actor, safe_name in zip(cast_list, proposed_names):
        actor['name'] = safe_name
    return sorted(set(rejected))


def deduplicate_cast_by_identity(cast_list: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Keep one cast row per TMDb identity and per existing Emby person record."""
    deduplicated = []
    seen_tmdb_ids = set()
    seen_emby_ids = set()
    for actor in cast_list:
        tmdb_id = str(actor.get('id') or '').strip()
        emby_id = str(actor.get('emby_person_id') or '').strip()
        if not tmdb_id or tmdb_id in seen_tmdb_ids or (emby_id and emby_id in seen_emby_ids):
            continue
        seen_tmdb_ids.add(tmdb_id)
        if emby_id:
            seen_emby_ids.add(emby_id)
        deduplicated.append(actor)
    return deduplicated


def filter_unsafe_new_cast(
    cast_list: List[Dict[str, Any]],
    original_emby_person_ids=(),
) -> List[Dict[str, Any]]:
    """Require every supplemented cast member to have a TMDb identity and profile image."""
    original_ids = {
        str(person_id).strip()
        for person_id in original_emby_person_ids
        if str(person_id).strip()
    }
    return [
        actor
        for actor in cast_list
        if actor.get('id')
        and (
            str(actor.get('emby_person_id') or '').strip() in original_ids
            or actor.get('profile_path')
        )
    ]
