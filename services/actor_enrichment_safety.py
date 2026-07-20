import re
import unicodedata
from typing import Any, Dict, List
from urllib.parse import urlparse, urlunparse


_LEADING_TRANSLATION_INDEX_RE = re.compile(
    r'^\s*(?:(?:\d+\s*)?[①②③④⑤⑥⑦⑧⑨⑩⑪⑫⑬⑭⑮⑯⑰⑱⑲⑳]+|'
    r'\d+\s*[.)、:：]|'
    r'\d+(?=[\u3040-\u30ff\u3400-\u9fff\uac00-\ud7af]))'
)

_TRUSTED_DOUBAN_AVATAR_HOST_SUFFIXES = (
    'douban.com',
    'doubanio.com',
    'bdstatic.com',
)

_IDENTITY_ALIAS_LABELS = {
    '更多中文名',
    '更多外文名',
    '中文名',
    '外文名',
    '英文名',
}


def _normalize_identity_name(value: Any) -> str:
    """Normalize a name for candidate retrieval, never for identity confirmation."""
    if not isinstance(value, str):
        return ''
    normalized = unicodedata.normalize('NFKC', value).strip().casefold()
    return ''.join(character for character in normalized if character.isalnum())


def _split_identity_aliases(value: Any) -> List[str]:
    if isinstance(value, list):
        aliases = []
        for item in value:
            aliases.extend(_split_identity_aliases(item))
        return aliases
    if not isinstance(value, str):
        return []
    return [
        alias.strip()
        for alias in re.split(r'[/／、|;；,，]+', value)
        if alias.strip()
    ]


def _normalize_birthdate(value: Any) -> str:
    if not isinstance(value, str):
        return ''
    match = re.search(
        r'(?<!\d)(\d{4})\s*(?:[-/.年])\s*(\d{1,2})\s*(?:[-/.月])\s*(\d{1,2})(?:\s*日)?(?!\d)',
        unicodedata.normalize('NFKC', value),
    )
    if not match:
        return ''
    year, month, day = (int(part) for part in match.groups())
    if not (1 <= month <= 12 and 1 <= day <= 31):
        return ''
    return f'{year:04d}-{month:02d}-{day:02d}'


def build_douban_identity_profile(
    actor: Dict[str, Any],
    details: Dict[str, Any],
) -> Dict[str, Any]:
    """Extract Douban aliases and birthday without treating a display name as an ID."""
    alias_values = [
        actor.get('Name'),
        actor.get('OriginalName'),
        details.get('title'),
        details.get('latin_title'),
        details.get('name'),
        details.get('name_en'),
    ]
    birthday = ''
    info_list = (details.get('extra') or {}).get('info') or []
    if isinstance(info_list, list):
        for item in info_list:
            if not isinstance(item, list) or len(item) != 2:
                continue
            label, value = item
            if label in _IDENTITY_ALIAS_LABELS:
                alias_values.append(value)
            elif label == '出生日期':
                birthday = _normalize_birthdate(str(value or ''))

    aliases = {
        normalized
        for value in alias_values
        for alias in _split_identity_aliases(value)
        if (normalized := _normalize_identity_name(alias))
    }
    return {
        'douban_id': str(actor.get('DoubanCelebrityId') or '').strip(),
        'aliases': aliases,
        'birthday': birthday,
    }


def build_tmdb_identity_profile(
    actor: Dict[str, Any],
    details: Dict[str, Any],
) -> Dict[str, Any]:
    """Extract the current title's TMDb person aliases and birthday."""
    alias_values = [
        actor.get('name'),
        actor.get('original_name'),
        details.get('name'),
        details.get('original_name'),
        details.get('also_known_as'),
        details.get('english_name_from_translations'),
        details.get('foreign_name_from_original'),
    ]
    translations = (details.get('translations') or {}).get('translations') or []
    if isinstance(translations, list):
        for translation in translations:
            if isinstance(translation, dict):
                alias_values.append((translation.get('data') or {}).get('name'))

    aliases = {
        normalized
        for value in alias_values
        for alias in _split_identity_aliases(value)
        if (normalized := _normalize_identity_name(alias))
    }
    return {
        'tmdb_id': str(actor.get('id') or details.get('id') or '').strip(),
        'aliases': aliases,
        'birthday': _normalize_birthdate(details.get('birthday')),
    }


def resolve_douban_actor_against_tmdb_cast(
    douban_profile: Dict[str, Any],
    tmdb_profiles: List[Dict[str, Any]],
) -> Dict[str, Any]:
    """
    Use names only to retrieve candidates; require an exact birthday to merge.

    A non-empty candidate set without a unique birthday match is deliberately
    deferred so a translated or incomplete name can never create a false merge.
    """
    douban_aliases = set(douban_profile.get('aliases') or ())
    candidates = [
        profile
        for profile in tmdb_profiles
        if douban_aliases.intersection(profile.get('aliases') or ())
    ]
    if not candidates:
        return {'status': 'no_candidate', 'tmdb_id': None, 'candidate_count': 0}

    birthday = str(douban_profile.get('birthday') or '').strip()
    birthday_matches = [
        profile
        for profile in candidates
        if birthday
        and profile.get('birthday')
        and profile.get('birthday') == birthday
    ]
    if len(birthday_matches) == 1:
        return {
            'status': 'confirmed',
            'tmdb_id': birthday_matches[0].get('tmdb_id'),
            'candidate_count': len(candidates),
        }
    return {
        'status': 'ambiguous',
        'tmdb_id': None,
        'candidate_count': len(candidates),
    }


def normalize_douban_avatar_url(value: Any) -> str:
    """Accept only HTTP(S) avatar URLs served by known Douban image hosts."""
    if not isinstance(value, str):
        return ''
    candidate = value.strip()
    if not candidate:
        return ''
    try:
        parsed = urlparse(candidate)
    except ValueError:
        return ''
    hostname = (parsed.hostname or '').lower().rstrip('.')
    if parsed.scheme not in {'http', 'https'} or not hostname:
        return ''
    if not any(
        hostname == suffix or hostname.endswith(f'.{suffix}')
        for suffix in _TRUSTED_DOUBAN_AVATAR_HOST_SUFFIXES
    ):
        return ''
    if parsed.username or parsed.password:
        return ''
    return urlunparse(parsed._replace(scheme='https'))


def apply_douban_avatar_fallbacks(
    cast_list: List[Dict[str, Any]],
) -> tuple[List[Dict[str, Any]], int]:
    """Use a trusted Douban avatar only when the matched TMDb person has no image."""
    normalized_cast = []
    adopted_count = 0
    for actor in cast_list:
        normalized_actor = actor.copy()
        douban_avatar = normalize_douban_avatar_url(
            normalized_actor.pop('douban_avatar_url', None)
            or normalized_actor.pop('DoubanAvatarUrl', None)
        )
        if not normalized_actor.get('profile_path') and douban_avatar:
            normalized_actor['profile_path'] = douban_avatar
            adopted_count += 1
        normalized_cast.append(normalized_actor)
    return normalized_cast, adopted_count


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
    """Keep one cast row per confirmed TMDb, Douban, and Emby identity."""
    deduplicated = []
    seen_tmdb_ids = set()
    seen_emby_ids = set()
    seen_douban_ids = set()
    for actor in cast_list:
        tmdb_id = str(actor.get('id') or '').strip()
        emby_id = str(actor.get('emby_person_id') or '').strip()
        douban_id = str(actor.get('douban_id') or '').strip()
        if (
            not (tmdb_id or douban_id)
            or (tmdb_id and tmdb_id in seen_tmdb_ids)
            or (emby_id and emby_id in seen_emby_ids)
            or (douban_id and douban_id in seen_douban_ids)
        ):
            continue
        if tmdb_id:
            seen_tmdb_ids.add(tmdb_id)
        if emby_id:
            seen_emby_ids.add(emby_id)
        if douban_id:
            seen_douban_ids.add(douban_id)
        deduplicated.append(actor)
    return deduplicated


def filter_unsafe_new_cast(
    cast_list: List[Dict[str, Any]],
    original_emby_person_ids=(),
) -> List[Dict[str, Any]]:
    """Require a stable external identity and a safe profile image for new cast."""
    original_ids = {
        str(person_id).strip()
        for person_id in original_emby_person_ids
        if str(person_id).strip()
    }
    return [
        actor
        for actor in cast_list
        if (actor.get('id') or actor.get('douban_id'))
        and (
            str(actor.get('emby_person_id') or '').strip() in original_ids
            or actor.get('profile_path')
        )
    ]


def select_cast_by_source_order(
    cast_list: List[Dict[str, Any]],
    limit: int,
) -> List[Dict[str, Any]]:
    """Apply the actor limit only after a stable source-order sort."""
    try:
        normalized_limit = max(1, int(limit))
    except (TypeError, ValueError):
        normalized_limit = 30

    def source_order(indexed_actor):
        index, actor = indexed_actor
        order = actor.get('order')
        if isinstance(order, (int, float)) and order >= 0:
            return order, index
        return 999, index

    ordered = [
        actor
        for _, actor in sorted(enumerate(cast_list), key=source_order)
    ]
    return ordered[:normalized_limit]
