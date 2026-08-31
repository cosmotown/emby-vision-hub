import json
import re
import hashlib
import ntpath
import posixpath
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


def _normalize_emby_path(value: Any) -> Optional[tuple[str, str]]:
    """Normalize an absolute Emby path without consulting the local filesystem."""
    raw = str(value or '').strip()
    if not raw or '\x00' in raw:
        return None

    is_windows = bool(
        re.match(r'^[A-Za-z]:[\\/]', raw)
        or raw.startswith('\\\\')
        or raw.startswith('//')
        or '\\' in raw
    )
    path_module = ntpath if is_windows else posixpath
    try:
        normalized = path_module.normpath(raw)
    except (TypeError, ValueError):
        return None
    if not normalized or normalized == '.' or not path_module.isabs(normalized):
        return None
    if is_windows:
        normalized = normalized.casefold()
    return ('windows' if is_windows else 'posix', normalized)


def build_protected_library_root_contract(
    libraries: Iterable[Dict[str, Any]],
    selected_libraries: Iterable[Dict[str, Any]],
) -> Dict[str, Any]:
    """Build an immutable, fail-closed root contract for selected libraries."""
    selected = {
        str(item.get('library_id') or '').strip():
        str(item.get('library_name') or item.get('library_id') or '').strip()
        for item in selected_libraries or []
        if str(item.get('library_id') or '').strip()
    }
    found = set()
    roots = []
    invalid = False
    for library in libraries or []:
        if not isinstance(library, dict):
            continue
        info = library.get('info')
        if not isinstance(info, dict):
            continue
        library_id = str(info.get('Id') or '').strip()
        if library_id not in selected:
            continue
        found.add(library_id)
        paths = library.get('paths')
        if not isinstance(paths, list) or not paths:
            invalid = True
            continue
        for root_path in paths:
            normalized = _normalize_emby_path(root_path)
            if normalized is None:
                invalid = True
                continue
            style, path = normalized
            roots.append({
                'library_id': library_id,
                'library_name': selected[library_id] or library_id,
                'style': style,
                'path': path,
            })

    complete = not invalid and found == set(selected) and (
        not selected or bool(roots)
    )
    return {
        'complete': complete,
        'selected_library_ids': frozenset(selected),
        'roots': tuple(roots),
    }


def match_item_to_protected_library(
    item: Dict[str, Any],
    root_contract: Optional[Dict[str, Any]],
) -> Optional[Dict[str, str]]:
    """Return the longest unique protected-library match, otherwise unknown."""
    if not isinstance(item, dict) or not isinstance(root_contract, dict):
        return None
    if not root_contract.get('complete'):
        return None
    normalized_item = _normalize_emby_path(item.get('Path'))
    if normalized_item is None:
        return None
    item_style, item_path = normalized_item

    matches = []
    for root in root_contract.get('roots') or ():
        if root.get('style') != item_style:
            continue
        root_path = str(root.get('path') or '')
        separator = '\\' if item_style == 'windows' else '/'
        root_boundary = root_path.rstrip(separator)
        if item_path != root_boundary and not item_path.startswith(
            root_boundary + separator
        ):
            continue
        matches.append((len(root_boundary), root))

    if not matches:
        return None
    longest = max(length for length, _ in matches)
    best = [root for length, root in matches if length == longest]
    if len({str(root.get('library_id') or '') for root in best}) != 1:
        return None
    selected = best[0]
    item_id = str(item.get('Id') or '').strip()
    if not item_id:
        return None
    return {
        'protected_library_id': str(selected.get('library_id') or ''),
        'protected_library_name': str(selected.get('library_name') or ''),
        'evidence_item_id': item_id,
    }


def find_protected_library_item_match(
    items: Iterable[Dict[str, Any]],
    root_contract: Optional[Dict[str, Any]],
) -> Optional[Dict[str, str]]:
    """Return the first exact protected-library ownership proof in query order."""
    for item in items or []:
        match = match_item_to_protected_library(item, root_contract)
        if match:
            return match
    return None


def find_ghost_candidates(
    all_people: Iterable[Dict[str, Any]],
    referenced_person_ids: Iterable[str],
    protected_person_names: Iterable[str] = (),
    protected_provider_identities: Iterable[tuple[str, str]] = (),
    protected_alias_person_ids: Iterable[str] = (),
) -> List[Dict[str, Any]]:
    referenced = {str(person_id) for person_id in referenced_person_ids if person_id}
    referenced.update(
        str(person_id) for person_id in protected_alias_person_ids if person_id
    )
    protected_names = build_person_name_protection_keys(protected_person_names)
    protected_identities = {
        (str(provider).strip().lower(), str(provider_id).strip())
        for provider, provider_id in protected_provider_identities
        if str(provider).strip() and str(provider_id).strip()
    }
    candidates = []
    for person in all_people:
        if not person.get('Id') or str(person['Id']) in referenced:
            continue
        if not person_name_protection_keys(person.get('Name')).isdisjoint(protected_names):
            continue
        try:
            person_identities = canonical_person_provider_identities(
                person.get('ProviderIds'),
                strict=True,
            )
        except ValueError:
            # Malformed identity data cannot make a Person eligible for deletion.
            continue
        if not person_identities.isdisjoint(protected_identities):
            continue
        candidates.append(person)
    return candidates


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
    """Classify a reference check without turning malformed data into an orphan."""
    if not isinstance(result, dict):
        return 'invalid_response'

    explicit_status = result.get('status')
    protected_statuses = {
        'protected_library_alias',
        'protected_library_unverifiable',
    }
    if explicit_status in protected_statuses:
        query_count = result.get('query_count')
        if (
            isinstance(query_count, int)
            and not isinstance(query_count, bool)
            and query_count > 0
            and str(result.get('protected_library_id') or '').strip()
            and str(result.get('evidence_item_id') or '').strip()
        ):
            return explicit_status
        return 'invalid_response'
    failure_statuses = {
        'connection_failed',
        'invalid_response',
        'people_unavailable',
    }
    if explicit_status in failure_statuses:
        return explicit_status

    count = result.get('count')
    if not isinstance(count, int) or isinstance(count, bool) or count < 0:
        return 'invalid_response'

    if explicit_status == 'linked':
        return 'linked' if count > 0 else 'invalid_response'
    if explicit_status == 'orphan':
        return 'orphan' if count == 0 else 'invalid_response'
    if explicit_status == 'identity_alias_only':
        query_count = result.get('query_count')
        if count == 0 and isinstance(query_count, int) and query_count > 0:
            return 'identity_alias_only'
        return 'invalid_response'
    if explicit_status is not None:
        return 'invalid_response'

    # Backward-compatible handling for callers/tests that only return a count.
    return 'orphan' if count == 0 else 'linked'


def reference_check_failure_message(status: str, context: str = '人物关联核对') -> str:
    """Return a precise, fail-closed message for an unsuccessful verification."""
    if status == 'connection_failed':
        return f'无法连接 Emby 完成{context}；该人物已受保护，不允许删除'
    if status == 'people_unavailable':
        return (
            'Emby 已返回可能关联作品，但作品人物明细不可核验；'
            '该人物已受保护，不允许删除'
        )
    return f'Emby 返回异常，无法完成{context}；该人物已受保护，不允许删除'


def canonical_person_provider_identities(
    provider_ids: Any,
    *,
    strict: bool = False,
) -> set[tuple[str, str]]:
    """Return exact canonical TMDb/IMDb/Douban identities for a Person."""
    if isinstance(provider_ids, str):
        try:
            provider_ids = json.loads(provider_ids)
        except (TypeError, ValueError):
            if strict:
                raise ValueError('ProviderIds JSON 格式无效')
            return set()
    if not isinstance(provider_ids, dict):
        if strict and provider_ids is not None:
            raise ValueError('ProviderIds 必须为对象')
        return set()

    supported = {'tmdb', 'imdb', 'douban'}
    collected: Dict[str, set[str]] = {}
    for key, value in provider_ids.items():
        provider = str(key).strip().lower()
        if provider not in supported:
            continue
        if isinstance(value, (dict, list, tuple, set)):
            if strict:
                raise ValueError(f'{provider} 身份为多值或复合结构')
            continue
        normalized_value = str(value or '').strip()
        if not normalized_value:
            continue
        if ',' in normalized_value or ';' in normalized_value:
            if strict:
                raise ValueError(f'{provider} 身份包含多值分隔符')
            continue
        if provider in {'tmdb', 'douban'}:
            if not normalized_value.isdigit() or int(normalized_value) <= 0:
                if strict:
                    raise ValueError(f'{provider} 身份格式无效')
                continue
            normalized_value = str(int(normalized_value))
        else:
            normalized_value = normalized_value.lower()
            if not re.fullmatch(r'nm\d+', normalized_value):
                if strict:
                    raise ValueError('imdb 身份格式无效')
                continue
        collected.setdefault(provider, set()).add(normalized_value)

    identities = set()
    for provider, values in collected.items():
        if len(values) != 1:
            if strict:
                raise ValueError(f'{provider} 身份冲突')
            continue
        identities.add((provider, next(iter(values))))
    return identities


def build_identity_provider_pairs(provider_ids: Any) -> List[str]:
    """Build Emby's exact provider filter for person identity comparison."""
    return sorted(
        f'{provider}.{provider_id}'
        for provider, provider_id in canonical_person_provider_identities(provider_ids)
    )


def candidate_fingerprint(candidate: Dict[str, Any]) -> str:
    """Bind a verification/preview result to the candidate identity that was checked."""
    payload = {
        'person_id': str(candidate.get('person_id') or candidate.get('Id') or '').strip(),
        'person_name': str(candidate.get('person_name') or candidate.get('Name') or '').strip(),
        'provider_ids': candidate.get('provider_ids_json') or candidate.get('ProviderIds') or {},
    }
    return hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode('utf-8')
    ).hexdigest()


def classify_alias_orphan_proof(
    candidate: Dict[str, Any],
    current_candidate: Optional[Dict[str, Any]],
    candidate_detail: Optional[Dict[str, Any]],
    normal_referenced_person_ids: Iterable[str],
    identity_index: Dict[tuple[str, str], Iterable[str]],
    person_details: Dict[str, Dict[str, Any]],
    reference_result: Optional[Dict[str, Any]],
    protection_reason: Optional[str] = None,
) -> Dict[str, Any]:
    """Classify one alias proof without granting any deletion permission.

    The caller must build complete normal-library and Person snapshots first.
    This function deliberately treats every malformed or changing input as a
    terminal fail-closed result.
    """
    person_id = str(candidate.get('person_id') or candidate.get('Id') or '').strip()
    base = {
        'proof_state': 'failed_safe',
        'matched_live_person_id': None,
        'matched_live_provider_ids': {},
        'query_count': 0,
        'exact_reference_count': 0,
        'error': None,
    }
    if not person_id or candidate.get('verification_status') != 'identity_alias_only':
        return {**base, 'error': '候选不是 identity_alias_only，拒绝只读证明'}
    if not current_candidate or candidate_fingerprint(current_candidate) != candidate_fingerprint(candidate):
        return {**base, 'proof_state': 'candidate_changed', 'error': '候选身份在证明期间发生变化'}
    if person_id in {str(value) for value in normal_referenced_person_ids}:
        return {**base, 'proof_state': 'linked', 'error': '普通媒体库 People 仍引用当前 Person ID'}
    if protection_reason:
        return {**base, 'proof_state': 'protected', 'error': str(protection_reason)}
    if not isinstance(candidate_detail, dict):
        return {**base, 'proof_state': 'invalid_response', 'error': '当前 Person detail 不可核验'}
    if (
        str(candidate_detail.get('Id') or '').strip() != person_id
        or candidate_detail.get('Type') != 'Person'
        or not str(candidate_detail.get('Name') or '').strip()
        or not isinstance(candidate_detail.get('ProviderIds'), dict)
    ):
        return {**base, 'proof_state': 'invalid_response', 'error': '当前 Person detail 身份不完整'}
    detail_candidate = {
        'person_id': person_id,
        'person_name': candidate_detail.get('Name'),
        'provider_ids_json': candidate_detail.get('ProviderIds'),
    }
    if candidate_fingerprint(detail_candidate) != candidate_fingerprint(candidate):
        return {**base, 'proof_state': 'candidate_changed', 'error': 'Emby Person detail 与候选指纹不一致'}

    reference_status = classify_reference_check(reference_result)
    if reference_status != 'identity_alias_only':
        mapped = {
            'linked': 'linked',
            'people_unavailable': 'people_unavailable',
            'connection_failed': 'connection_failed',
            'invalid_response': 'invalid_response',
            'protected_library_alias': 'protected',
            'protected_library_unverifiable': 'protected',
        }.get(reference_status, 'failed_safe')
        return {**base, 'proof_state': mapped, 'error': f'当前关联核验状态为 {reference_status}'}
    query_count = reference_result.get('query_count')
    exact_count = reference_result.get('count')
    unverified_items = reference_result.get('unverified_items') or []
    if (
        not isinstance(query_count, int) or isinstance(query_count, bool) or query_count <= 0
        or exact_count != 0
        or not isinstance(unverified_items, list) or unverified_items
    ):
        return {
            **base,
            'proof_state': 'people_unavailable',
            'query_count': query_count if isinstance(query_count, int) else 0,
            'exact_reference_count': exact_count if isinstance(exact_count, int) else 0,
            'error': 'PersonIds 命中作品未能全部完成精确 People 核验',
        }

    try:
        identities = canonical_person_provider_identities(
            candidate_detail.get('ProviderIds'), strict=True,
        )
    except ValueError as exc:
        return {
            **base, 'proof_state': 'identity_unavailable',
            'query_count': query_count, 'exact_reference_count': exact_count,
            'error': str(exc),
        }
    if not identities:
        return {
            **base, 'proof_state': 'identity_unavailable',
            'query_count': query_count, 'exact_reference_count': exact_count,
            'error': '缺少可用的 TMDb/IMDb/豆瓣身份',
        }

    matching_ids = set()
    for identity in identities:
        matching_ids.update(str(value) for value in identity_index.get(identity, ()) if value)
    matching_ids.discard(person_id)
    if not matching_ids:
        return {
            **base, 'proof_state': 'identity_not_found',
            'query_count': query_count, 'exact_reference_count': exact_count,
            'error': '未找到其他同 canonical provider identity 的 Person',
        }
    if len(matching_ids) != 1:
        return {
            **base, 'proof_state': 'identity_ambiguous',
            'query_count': query_count, 'exact_reference_count': exact_count,
            'error': f'找到 {len(matching_ids)} 个其他同身份 Person',
        }
    matched_id = next(iter(matching_ids))
    matched_detail = person_details.get(matched_id)
    if not isinstance(matched_detail, dict):
        return {
            **base, 'proof_state': 'identity_not_found',
            'query_count': query_count, 'exact_reference_count': exact_count,
            'error': '同身份 Person detail 不完整',
        }
    if matched_id not in {str(value) for value in normal_referenced_person_ids}:
        return {
            **base, 'proof_state': 'identity_not_found',
            'matched_live_person_id': matched_id,
            'matched_live_provider_ids': matched_detail.get('ProviderIds') or {},
            'query_count': query_count, 'exact_reference_count': exact_count,
            'error': '唯一同身份 Person 未被普通媒体库真实 People 引用',
        }
    return {
        **base,
        'proof_state': 'verified_alias_orphan',
        'matched_live_person_id': matched_id,
        'matched_live_provider_ids': matched_detail.get('ProviderIds') or {},
        'query_count': query_count,
        'exact_reference_count': exact_count,
        'error': None,
    }


def is_explicit_verified_orphan(candidate: Dict[str, Any], snapshot_generation: int) -> bool:
    """Only an explicit orphan result from the current protection snapshot is selectable."""
    try:
        checked_generation = int(candidate.get('verification_snapshot_generation'))
    except (TypeError, ValueError):
        return False
    return (
        candidate.get('verification_status') == 'orphan'
        and checked_generation == int(snapshot_generation)
        and not candidate.get('last_error')
        and candidate.get('verification_fingerprint') == candidate_fingerprint(candidate)
    )
