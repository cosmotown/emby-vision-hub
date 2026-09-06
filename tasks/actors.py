# tasks/actors.py
# 演员相关任务模块

import hashlib
import json
import time
import logging
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed

# 导入需要的底层模块和共享实例
from database.connection import get_db_connection
from database import actor_db, person_cleanup_db
import constants
import handler.emby as emby
import task_manager
import utils
from actor_utils import enrich_all_actor_aliases_task
from handler.actor_sync import UnifiedSyncHandler
from services.person_cleanup_safety import (
    build_protected_library_root_contract,
    build_person_name_protection_keys,
    candidate_fingerprint,
    canonical_person_provider_identities,
    classify_alias_orphan_proof,
    classify_reference_check,
    classify_stale_index_forensic,
    find_ghost_candidates,
    is_explicit_verified_orphan,
    match_item_to_protected_library,
    person_name_protection_keys,
    reference_check_failure_message,
)

logger = logging.getLogger(__name__)

PERSON_ALIAS_SCAN_WORKERS = 4
PERSON_ALIAS_SCAN_CLAIM_LIMIT = 4
PERSON_ALIAS_PROOF_WORKERS = 4
PERSON_ALIAS_PROOF_CLAIM_LIMIT = 4
PERSON_STALE_INDEX_WORKERS = 4
PERSON_STALE_INDEX_CLAIM_LIMIT = 4
PERSON_STALE_INDEX_DRIFT_SAMPLE_LIMIT = 20
PERSON_STALE_DELETE_CANARY_LIMIT = 100

def _scan_protected_library_people(processor, protected_libraries, batch_size: int = 500):
    """Build a complete protected snapshot or raise without marking it ready."""
    snapshots = {}
    api_url = f"{processor.emby_url.rstrip('/')}/Items"
    safe_batch_size = max(1, int(batch_size))
    all_person_ids = set()

    for protected_library in protected_libraries:
        library_id = str(protected_library.get('library_id') or '').strip()
        library_name = str(protected_library.get('library_name') or library_id).strip() or library_id
        if not library_id:
            continue

        people_by_id = {}
        all_person_names = set()
        media_count = 0
        start_index = 0
        expected_total = None
        seen_item_ids = set()

        while True:
            params = {
                'ParentId': library_id,
                'Recursive': 'true',
                'IncludeItemTypes': 'Movie,Series,Episode,Video,MusicVideo',
                'Fields': 'People',
                'StartIndex': start_index,
                'Limit': safe_batch_size,
                'EnableTotalRecordCount': 'true',
            }
            try:
                response = emby.emby_client.get(
                    api_url,
                    headers={'X-Emby-Token': processor.emby_api_key},
                    params=params,
                    timeout=60,
                )
                response.raise_for_status()
                payload = response.json()
                if not isinstance(payload, dict) or not isinstance(payload.get('Items'), list):
                    raise ValueError('Emby 响应缺少有效 Items')
                total = payload.get('TotalRecordCount')
                if not isinstance(total, int) or isinstance(total, bool) or total < 0:
                    raise ValueError('Emby 响应缺少有效 TotalRecordCount')
                if expected_total is None:
                    expected_total = total
                elif total != expected_total:
                    raise ValueError('分页期间 TotalRecordCount 发生变化')
                items = payload['Items']
            except Exception as exc:
                raise RuntimeError(
                    f"受保护媒体库 '{library_name}' 读取失败，已拒绝继续人物清理: {exc}"
                ) from exc

            if not items:
                if start_index < int(expected_total or 0):
                    raise RuntimeError(
                        f"受保护媒体库 '{library_name}' 分页提前结束"
                    )
                break

            media_count += len(items)
            for item in items:
                if not isinstance(item, dict):
                    raise RuntimeError(f"受保护媒体库 '{library_name}' 返回非法媒体项")
                item_id = str(item.get('Id') or '').strip()
                if not item_id or item_id in seen_item_ids:
                    raise RuntimeError(
                        f"受保护媒体库 '{library_name}' 返回缺失或重复媒体 ID"
                    )
                seen_item_ids.add(item_id)
                if 'People' not in item or not isinstance(item.get('People'), list):
                    raise RuntimeError(
                        f"受保护媒体库 '{library_name}' 项目 {item_id} 的 People 不可核验"
                    )
                for person in item['People']:
                    if not isinstance(person, dict):
                        raise RuntimeError(
                            f"受保护媒体库 '{library_name}' 项目 {item_id} 含非法人物项"
                        )
                    person_id = str(person.get('Id') or '').strip()
                    person_name = str(person.get('Name') or '').strip()
                    if not person_id or not person_name:
                        raise RuntimeError(
                            f"受保护媒体库 '{library_name}' 项目 {item_id} 的人物身份不完整"
                        )
                    all_person_names.add(person_name)
                    people_by_id[person_id] = person_name
                    all_person_ids.add(person_id)

            start_index += len(items)
            if start_index >= int(expected_total or 0):
                break
            if len(items) < safe_batch_size:
                raise RuntimeError(f"受保护媒体库 '{library_name}' 分页长度不足")

        if len(seen_item_ids) != int(expected_total or 0):
            raise RuntimeError(
                f"受保护媒体库 '{library_name}' 分页数量不一致"
            )

        snapshots[library_id] = {
            'people_by_id': people_by_id,
            'all_person_names': all_person_names,
            'media_count': media_count,
        }

    person_details = emby.get_person_details_strict(
        processor.emby_url,
        processor.emby_api_key,
        all_person_ids,
    )
    if person_details is None or set(person_details) != all_person_ids:
        raise RuntimeError('受保护人物 exact detail 读取不完整')

    for snapshot in snapshots.values():
        protected_people = []
        for person_id, people_name in snapshot['people_by_id'].items():
            detail = person_details.get(person_id)
            if not detail:
                raise RuntimeError(f'受保护人物 {person_id} detail 缺失')
            provider_ids = detail.get('ProviderIds')
            if not isinstance(provider_ids, dict):
                raise RuntimeError(f'受保护人物 {person_id} ProviderIds 不可核验')
            canonical_person_provider_identities(provider_ids, strict=True)
            detail_name = str(detail.get('Name') or '').strip()
            if not detail_name:
                raise RuntimeError(f'受保护人物 {person_id} Name 不可核验')
            snapshot['all_person_names'].add(detail_name)
            protected_people.append({
                'person_id': person_id,
                'person_name': people_name or detail_name,
                'provider_ids': provider_ids,
            })
        snapshot['people'] = protected_people

    return snapshots


def _refresh_protected_snapshot(processor):
    generation = person_cleanup_db.begin_protection_snapshot()
    try:
        protected_libraries = person_cleanup_db.list_protected_libraries()
        snapshots = _scan_protected_library_people(processor, protected_libraries)
        for protected_library in protected_libraries:
            library_id = str(protected_library.get('library_id') or '')
            snapshot = snapshots.get(library_id)
            if snapshot is None:
                raise RuntimeError(f'保护库 {library_id} 未生成快照')
            people = snapshot.get('people') or []
            person_cleanup_db.merge_protected_people_for_library(library_id, people)
            person_cleanup_db.merge_protected_names_for_library(
                library_id,
                snapshot.get('all_person_names') or set(),
            )
            person_cleanup_db.merge_protected_identities_for_library(library_id, people)
        person_cleanup_db.complete_protection_snapshot(generation)
        return generation, snapshots
    except Exception as exc:
        person_cleanup_db.fail_protection_snapshot(generation, str(exc))
        raise


def _build_protected_root_contract(processor, protected_libraries=None):
    """Load VirtualFolders once for one verify/preview/delete operation."""
    selected = (
        list(protected_libraries)
        if protected_libraries is not None
        else person_cleanup_db.list_protected_libraries()
    )
    if not selected:
        return build_protected_library_root_contract([], [])
    libraries = emby.get_all_libraries_with_paths(
        processor.emby_url,
        processor.emby_api_key,
    )
    return build_protected_library_root_contract(libraries, selected)


def _check_readonly_alias_candidate(processor, candidate, protected_root_contract):
    """Perform the phase-2 GET-only check without granting delete eligibility."""
    references = emby.get_person_media_references(
        processor.emby_url,
        processor.emby_api_key,
        str(candidate.get('person_id') or ''),
        limit=1,
        person_name=candidate.get('person_name'),
        protected_root_contract=protected_root_contract,
        user_id=getattr(processor, 'emby_user_id', None),
        detail_workers=1,
    )
    return references, classify_reference_check(references)


def _run_readonly_alias_scan(
    processor,
    scan_id: str,
    protected_root_contract,
):
    """Drain persistent candidate work with four bounded GET-only workers."""
    last_reported_checked = -1
    with ThreadPoolExecutor(
        max_workers=PERSON_ALIAS_SCAN_WORKERS,
        thread_name_prefix='person-alias-readonly',
    ) as executor:
        while True:
            if processor.is_stop_requested():
                person_cleanup_db.stop_readonly_scan(scan_id)
                scan = person_cleanup_db.get_readonly_scan(scan_id) or {}
                checked = int(scan.get('checked_count') or 0)
                total = int(scan.get('candidate_total') or 0)
                task_manager.update_status_from_thread(
                    50 + int((checked / max(1, total)) * 49),
                    '只读扫描已中止；未核验候选已持久保留，可再次点击继续。',
                )
                return scan

            claimed = person_cleanup_db.claim_readonly_alias_candidates(
                scan_id,
                limit=PERSON_ALIAS_SCAN_CLAIM_LIMIT,
            )
            if not claimed:
                return person_cleanup_db.complete_readonly_scan(scan_id)

            futures = {
                executor.submit(
                    _check_readonly_alias_candidate,
                    processor,
                    candidate,
                    protected_root_contract,
                ): candidate
                for candidate in claimed
            }
            for future in as_completed(futures):
                candidate = futures[future]
                try:
                    references, status = future.result()
                except Exception as exc:
                    logger.error(
                        "只读保护别名核验异常 person_id=%s error=%s",
                        candidate.get('person_id'),
                        type(exc).__name__,
                    )
                    references = {}
                    status = 'invalid_response'

                if status in {
                    'protected_library_alias',
                    'protected_library_unverifiable',
                }:
                    error = None
                elif status == 'linked':
                    error = None
                elif status == 'orphan':
                    # Phase 2 only excludes protected/linked people. A GET-only
                    # scan never grants verified-orphan delete eligibility.
                    error = None
                elif status == 'identity_alias_only':
                    error = '仅命中普通库或归属不明的同身份人物；保持失败关闭'
                else:
                    error = reference_check_failure_message(
                        status,
                        context='保护别名核验',
                    )

                scan = person_cleanup_db.finish_readonly_alias_candidate(
                    scan_id,
                    candidate,
                    status,
                    library_id=(references or {}).get('protected_library_id'),
                    evidence_item_id=(references or {}).get('evidence_item_id'),
                    error=error,
                )
                if not scan:
                    continue
                checked = int(scan.get('checked_count') or 0)
                total = int(scan.get('candidate_total') or 0)
                protected = int(scan.get('protected_count') or 0)
                progress = 50 + int((checked / max(1, total)) * 49)
                if checked == total or checked - last_reported_checked >= 25:
                    task_manager.update_status_from_thread(
                        progress,
                        f"阶段 2：核验保护库别名人物 {checked}/{total}；"
                        f"本轮新增保护 {protected}。",
                    )
                    last_reported_checked = checked

            if processor.is_stop_requested():
                person_cleanup_db.stop_readonly_scan(scan_id)
                return person_cleanup_db.get_readonly_scan(scan_id) or {}


def task_scan_ghost_actor_candidates(processor):
    task_name = "扫描幽灵人物"
    logger.info(f"--- 开始只读任务: '{task_name}' ---")
    task_manager.update_status_from_thread(0, "阶段 1：建立幽灵人物候选...")

    scan_id = None
    try:
        protection_state = person_cleanup_db.get_protection_state()
        resumable = None
        if protection_state.get('snapshot_state') == 'ready':
            resumable = person_cleanup_db.get_resumable_readonly_scan(
                int(protection_state.get('generation') or 0),
            )

        if resumable:
            scan_id = str(resumable['scan_id'])
            protected_libraries = person_cleanup_db.list_protected_libraries()
            protected_root_contract = _build_protected_root_contract(
                processor,
                protected_libraries,
            )
            if protected_libraries and not protected_root_contract.get('complete'):
                raise RuntimeError('受保护媒体库路径归属合同不完整，已暂停只读核验')
            task_manager.update_status_from_thread(
                50,
                f"阶段 2：继续核验保护库别名人物 "
                f"{resumable.get('checked_count', 0)}/{resumable.get('candidate_total', 0)}。",
            )
            final_scan = _run_readonly_alias_scan(
                processor,
                scan_id,
                protected_root_contract,
            )
            if final_scan.get('state') != 'completed':
                return
            remaining = len(person_cleanup_db.list_candidates())
            message = (
                f"只读扫描完成：保护别名核验 "
                f"{final_scan.get('checked_count', 0)}/{final_scan.get('candidate_total', 0)}；"
                f"本轮新增保护 {final_scan.get('protected_count', 0)}；"
                f"待人工复核 {remaining}。"
            )
            logger.info(f"  ➜ {message}")
            task_manager.update_status_from_thread(100, message)
            return

        libraries = emby.get_all_libraries_with_paths(
            processor.emby_url,
            processor.emby_api_key,
        )
        library_ids = {
            str(lib.get('info', {}).get('Id'))
            for lib in libraries or []
            if lib.get('info', {}).get('Id')
        }
        if not library_ids:
            raise RuntimeError("无法获取任何有效媒体库，已终止扫描")

        protected_libraries = person_cleanup_db.list_protected_libraries()
        protected_library_ids = {
            str(item.get('library_id'))
            for item in protected_libraries
            if item.get('library_id')
        }
        missing_protected_ids = protected_library_ids - library_ids
        if missing_protected_ids:
            raise RuntimeError('至少一个受保护媒体库已无法从 Emby 精确读取')

        generation, _ = _refresh_protected_snapshot(processor)
        protected_root_contract = build_protected_library_root_contract(
            libraries,
            protected_libraries,
        )
        if protected_libraries and not protected_root_contract.get('complete'):
            raise RuntimeError('受保护媒体库路径归属合同不完整，候选列表保持不变')
        normal_library_ids = sorted(library_ids - protected_library_ids)
        reference_scan = emby.get_referenced_person_ids_strict(
            processor.emby_url,
            processor.emby_api_key,
            normal_library_ids,
        )
        if reference_scan is None:
            raise RuntimeError("至少一个媒体库读取失败，候选列表保持不变")
        if reference_scan['media_count'] > 0 and not reference_scan['person_ids']:
            raise RuntimeError("媒体存在但未返回人物关联，已拒绝生成候选")

        referenced_person_ids = reference_scan['person_ids']
        logger.info(f"  ➜ 已建立 {len(referenced_person_ids)} 位在用人物的安全白名单。")

        contract = person_cleanup_db.get_protection_contract()
        if contract['generation'] != generation:
            raise RuntimeError('保护快照 generation 在候选生成前发生变化')
        protected_person_ids = contract['person_ids']
        protected_alias_person_ids = set(contract.get('alias_statuses') or {})
        protected_person_names = person_cleanup_db.get_protected_person_names()
        protected_provider_identities = contract['provider_identities']
        if protected_person_ids or protected_person_names:
            referenced_person_ids = referenced_person_ids | protected_person_ids
            logger.info(
                f"  ➜ 受保护媒体库快照额外保护 {len(protected_person_ids)} 个人物 ID、"
                f"{len(protected_person_names)} 个姓名。"
            )

        all_people = []
        person_generator = emby.get_all_persons_from_emby(
            base_url=processor.emby_url,
            api_key=processor.emby_api_key,
            user_id=processor.emby_user_id,
            stop_event=processor.get_stop_event(),
            force_full_scan=True,
            update_status_callback=lambda progress, message: (
                task_manager.update_status_from_thread(
                    min(49, 10 + int(max(0, progress) * 0.39)),
                    f"阶段 1：建立幽灵人物候选；{message}",
                )
            ),
        )
        for person_batch in person_generator:
            if processor.is_stop_requested():
                task_manager.update_status_from_thread(100, "扫描已中止，候选列表保持不变")
                return
            all_people.extend(person_batch)

        if not all_people:
            raise RuntimeError("未能读取 Emby 人物列表，候选列表保持不变")

        candidates = find_ghost_candidates(
            all_people,
            referenced_person_ids,
            protected_person_names=protected_person_names,
            protected_provider_identities=protected_provider_identities,
            protected_alias_person_ids=protected_alias_person_ids,
        )
        scan = person_cleanup_db.start_readonly_alias_scan(candidates, generation)
        scan_id = str(scan['scan_id'])
        saved_count = int(scan.get('candidate_total') or 0)
        task_manager.update_status_from_thread(
            50,
            f"阶段 2：核验保护库别名人物 0/{saved_count}；本轮新增保护 0。",
        )
        final_scan = _run_readonly_alias_scan(
            processor,
            scan_id,
            protected_root_contract,
        )
        if final_scan.get('state') != 'completed':
            return
        remaining = len(person_cleanup_db.list_candidates())
        message = (
            f"只读扫描完成：阶段 1 发现 {saved_count} 位候选；"
            f"保护别名核验 {final_scan.get('checked_count', 0)}/{saved_count}；"
            f"本轮新增保护 {final_scan.get('protected_count', 0)}；"
            f"待人工复核 {remaining}；"
            f"保护库快照覆盖 {len(protected_person_ids)} 个 ID、"
            f"{len(protected_person_names)} 个姓名、"
            f"{len(protected_provider_identities)} 个外部身份、"
            f"{len(protected_alias_person_ids)} 个持久 alias ID。"
        )
        logger.info(f"  ➜ {message}")
        task_manager.update_status_from_thread(100, message)
    except Exception as exc:
        if scan_id:
            try:
                person_cleanup_db.stop_readonly_scan(scan_id, error=str(exc))
            except Exception:
                logger.error('持久化只读 alias scan 暂停状态失败', exc_info=True)
        logger.error(f"执行 '{task_name}' 失败: {exc}", exc_info=True)
        task_manager.update_status_from_thread(-1, f"扫描失败: {exc}")
        raise


def _alias_proof_snapshot_hash(value) -> str:
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode('utf-8')
    ).hexdigest()


def _alias_proof_protection_hash(contract, root_contract) -> str:
    """Bind every protection fact that can reject alias-orphan evidence."""
    return _alias_proof_snapshot_hash(
        _stale_index_protection_components(contract, root_contract)
    )


def _stale_index_protection_components(contract, root_contract):
    """Return the exact protection facts covered by the global hash."""
    roots = sorted(
        (
            str(root.get('library_id') or ''),
            str(root.get('library_name') or ''),
            str(root.get('style') or ''),
            str(root.get('path') or ''),
        )
        for root in (root_contract.get('roots') or ())
    )
    return {
        'generation': int(contract['generation']),
        'person_ids': sorted(str(value) for value in contract.get('person_ids') or ()),
        'name_keys': sorted(str(value) for value in contract.get('name_keys') or ()),
        'provider_identities': sorted(
            (str(provider), str(provider_id))
            for provider, provider_id in (contract.get('provider_identities') or ())
        ),
        'alias_statuses': sorted(
            (str(person_id), str(status))
            for person_id, status in (contract.get('alias_statuses') or {}).items()
        ),
        'selected_library_ids': sorted(
            str(value) for value in (root_contract.get('selected_library_ids') or ())
        ),
        'root_contract_complete': bool(root_contract.get('complete')),
        'roots': roots,
    }


def _stale_index_relationship_hash(item_people) -> str:
    """Hash exact item ownership and People facts independent of traversal order."""
    return _alias_proof_snapshot_hash([
        (
            str(item_id),
            str(relationship.get('item_type') or ''),
            str(relationship.get('library_id') or ''),
            sorted(
                (str(person_id), str(person_name))
                for person_id, person_name in (relationship.get('people') or ())
            ),
        )
        for item_id, relationship in sorted(item_people.items())
    ])


def _stale_index_relationship_drift_summary(start_snapshots, final_snapshots):
    """Summarize exact item/People drift without retaining paths or full snapshots."""
    start_items = start_snapshots.get('item_people') or {}
    final_items = final_snapshots.get('item_people') or {}
    start_ids = set(start_items)
    final_ids = set(final_items)
    added_ids = sorted(final_ids - start_ids)
    removed_ids = sorted(start_ids - final_ids)
    changed_people = []
    changed_types = []
    changed_libraries = []
    people_added_count = 0
    people_removed_count = 0
    people_name_changed_count = 0

    def people_map(relationship):
        return {
            str(person_id): str(person_name)
            for person_id, person_name in (relationship.get('people') or ())
        }

    for item_id in sorted(start_ids & final_ids):
        before = start_items[item_id]
        after = final_items[item_id]
        if str(before.get('item_type') or '') != str(after.get('item_type') or ''):
            changed_types.append(item_id)
        if str(before.get('library_id') or '') != str(after.get('library_id') or ''):
            changed_libraries.append(item_id)
        before_people = tuple(before.get('people') or ())
        after_people = tuple(after.get('people') or ())
        if before_people != after_people:
            changed_people.append(item_id)
            before_map = people_map(before)
            after_map = people_map(after)
            before_person_ids = set(before_map)
            after_person_ids = set(after_map)
            people_added_count += len(after_person_ids - before_person_ids)
            people_removed_count += len(before_person_ids - after_person_ids)
            people_name_changed_count += sum(
                before_map[person_id] != after_map[person_id]
                for person_id in before_person_ids & after_person_ids
            )

    change_types = defaultdict(set)
    for item_id in added_ids:
        change_types[item_id].add('added')
    for item_id in removed_ids:
        change_types[item_id].add('removed')
    for item_id in changed_people:
        change_types[item_id].add('people_changed')
    for item_id in changed_types:
        change_types[item_id].add('type_changed')
    for item_id in changed_libraries:
        change_types[item_id].add('library_ownership_changed')
    samples = []
    for item_id in sorted(change_types)[:PERSON_STALE_INDEX_DRIFT_SAMPLE_LIMIT]:
        before = start_items.get(item_id) or {}
        after = final_items.get(item_id) or {}
        samples.append({
            'item_id': str(item_id),
            'change_type': ','.join(sorted(change_types[item_id])),
            'start_people_count': len(before.get('people') or ()),
            'final_people_count': len(after.get('people') or ()),
        })
    return {
        'start_media_count': int(start_snapshots.get('media_count') or len(start_items)),
        'final_media_count': int(final_snapshots.get('media_count') or len(final_items)),
        'added_item_count': len(added_ids),
        'removed_item_count': len(removed_ids),
        'changed_item_people_count': len(changed_people),
        'changed_item_type_count': len(changed_types),
        'changed_library_ownership_count': len(changed_libraries),
        'people_added_count': people_added_count,
        'people_removed_count': people_removed_count,
        'people_name_changed_count': people_name_changed_count,
        'samples': samples,
    }


def _stale_index_person_drift_summary(start_snapshots, final_snapshots):
    """Summarize full Person ID/Name/ProviderIds drift with canonical identity samples."""
    start_people = start_snapshots.get('person_details') or {}
    final_people = final_snapshots.get('person_details') or {}
    start_ids = set(start_people)
    final_ids = set(final_people)
    added_ids = sorted(final_ids - start_ids)
    removed_ids = sorted(start_ids - final_ids)
    name_changed = []
    provider_changed = []

    def raw_provider_ids(detail):
        return json.dumps(
            detail.get('ProviderIds') or {}, ensure_ascii=False,
            sort_keys=True, separators=(',', ':'),
        )

    def canonical_identities(detail):
        try:
            identities = canonical_person_provider_identities(
                detail.get('ProviderIds'), strict=True,
            )
        except ValueError:
            identities = set()
        return [f'{provider}:{provider_id}' for provider, provider_id in sorted(identities)]

    for person_id in sorted(start_ids & final_ids):
        before = start_people[person_id]
        after = final_people[person_id]
        if str(before.get('Name') or '') != str(after.get('Name') or ''):
            name_changed.append(person_id)
        if raw_provider_ids(before) != raw_provider_ids(after):
            provider_changed.append(person_id)

    change_types = defaultdict(set)
    for person_id in added_ids:
        change_types[person_id].add('added')
    for person_id in removed_ids:
        change_types[person_id].add('removed')
    for person_id in name_changed:
        change_types[person_id].add('name_changed')
    for person_id in provider_changed:
        change_types[person_id].add('provider_ids_changed')
    samples = []
    for person_id in sorted(change_types)[:PERSON_STALE_INDEX_DRIFT_SAMPLE_LIMIT]:
        before = start_people.get(person_id) or {}
        after = final_people.get(person_id) or {}
        samples.append({
            'person_id': str(person_id),
            'change_type': ','.join(sorted(change_types[person_id])),
            'old_provider_identities': canonical_identities(before),
            'new_provider_identities': canonical_identities(after),
        })
    return {
        'person_added_count': len(added_ids),
        'person_removed_count': len(removed_ids),
        'person_name_changed_count': len(name_changed),
        'person_provider_ids_changed_count': len(provider_changed),
        'samples': samples,
    }


def _stale_index_protection_drift_summary(start_snapshots, final_snapshots):
    before = _stale_index_protection_components(
        start_snapshots['contract'], start_snapshots['root_contract'],
    )
    after = _stale_index_protection_components(
        final_snapshots['contract'], final_snapshots['root_contract'],
    )
    return {
        'generation_changed': before['generation'] != after['generation'],
        'protected_ids_changed': before['person_ids'] != after['person_ids'],
        'protected_names_changed': before['name_keys'] != after['name_keys'],
        'protected_provider_identities_changed': (
            before['provider_identities'] != after['provider_identities']
        ),
        'persistent_aliases_changed': before['alias_statuses'] != after['alias_statuses'],
        'selected_protected_libraries_changed': (
            before['selected_library_ids'] != after['selected_library_ids']
        ),
        'root_contract_changed': (
            before['root_contract_complete'] != after['root_contract_complete']
            or before['roots'] != after['roots']
        ),
    }


def _build_stale_index_drift_diagnostics(
    start_snapshots,
    final_snapshots,
    start_source_proof_hash,
    final_source,
):
    final_source_hash = final_source.get('source_proof_hash')
    source_changed = (
        not final_source.get('complete')
        or final_source_hash != str(start_source_proof_hash or '')
    )
    diagnostics = {
        'final_snapshot_generation': int(final_snapshots['generation']),
        'final_protection_hash': str(final_snapshots['protection_hash']),
        'final_normal_people_relationship_hash': str(
            final_snapshots['normal_people_relationship_hash']
        ),
        'final_person_snapshot_hash': str(final_snapshots['person_hash']),
        'final_source_proof_hash': (
            str(final_source_hash) if final_source_hash is not None else None
        ),
        'drift_generation': (
            int(final_snapshots['generation']) != int(start_snapshots['generation'])
        ),
        'drift_protection': (
            final_snapshots['protection_hash'] != start_snapshots['protection_hash']
        ),
        'drift_normal_relationship': (
            final_snapshots['normal_people_relationship_hash']
            != start_snapshots['normal_people_relationship_hash']
        ),
        'drift_person': final_snapshots['person_hash'] != start_snapshots['person_hash'],
        'drift_source_proof': source_changed,
        'source_proof_drift_summary': {
            'source_proof_changed': source_changed,
            'source_proof_complete': bool(final_source.get('complete')),
            'reason': final_source.get('error'),
        },
    }
    diagnostics['has_drift'] = any(
        diagnostics[key]
        for key in (
            'drift_generation', 'drift_protection', 'drift_normal_relationship',
            'drift_person', 'drift_source_proof',
        )
    )

    def diagnostic_summary(builder, *args):
        try:
            summary = builder(*args)
            summary['available'] = True
            return summary
        except Exception as exc:
            logger.error(
                'Stale Index drift diagnostic summary 计算失败 kind=%s error=%s',
                getattr(builder, '__name__', type(builder).__name__),
                type(exc).__name__,
                exc_info=True,
            )
            return {
                'available': False,
                'error': type(exc).__name__,
            }

    # These summaries are display-only. Exact drift flags above are calculated
    # first from the original full hashes and remain authoritative even when a
    # summary cannot be produced.
    diagnostics['normal_relationship_drift_summary'] = diagnostic_summary(
        _stale_index_relationship_drift_summary,
        start_snapshots,
        final_snapshots,
    )
    diagnostics['person_drift_summary'] = diagnostic_summary(
        _stale_index_person_drift_summary,
        start_snapshots,
        final_snapshots,
    )
    diagnostics['protection_drift_summary'] = diagnostic_summary(
        _stale_index_protection_drift_summary,
        start_snapshots,
        final_snapshots,
    )
    return diagnostics


def _build_alias_proof_snapshots(processor):
    """Build all global GET-only evidence before any item can be verified."""
    generation = person_cleanup_db.require_ready_protection_snapshot()
    contract = person_cleanup_db.get_protection_contract()
    if int(contract['generation']) != int(generation):
        raise RuntimeError('保护快照 generation 不一致')

    protected_libraries = person_cleanup_db.list_protected_libraries()
    for protected_library in protected_libraries:
        if (
            protected_library.get('snapshot_state') != 'ready'
            or int(protected_library.get('snapshot_generation') or -1) != int(generation)
        ):
            raise RuntimeError('至少一个受保护媒体库 snapshot 未在当前 generation 就绪')
    protected_ids = {
        str(item.get('library_id') or '').strip()
        for item in protected_libraries
        if str(item.get('library_id') or '').strip()
    }
    try:
        libraries = emby.get_all_libraries_with_paths_strict(
            processor.emby_url,
            processor.emby_api_key,
            required_library_ids=protected_ids,
        )
    except emby.StrictVirtualFoldersError as exc:
        raise RuntimeError(f'Alias Proof 无法启动：VirtualFolder {exc}') from exc
    library_ids = {
        str(library.get('info', {}).get('Id') or '').strip()
        for library in libraries or []
        if str(library.get('info', {}).get('Id') or '').strip()
    }
    if not library_ids:
        raise RuntimeError('无法读取完整媒体库列表')
    if not protected_ids.issubset(library_ids):
        raise RuntimeError('至少一个受保护媒体库已无法精确读取')
    root_contract = build_protected_library_root_contract(libraries, protected_libraries)
    if protected_ids and not root_contract.get('complete'):
        raise RuntimeError('受保护媒体库 root contract 不完整')

    normal_scan = emby.get_referenced_person_ids_strict(
        processor.emby_url,
        processor.emby_api_key,
        sorted(library_ids - protected_ids),
        require_person_names=True,
    )
    if normal_scan is None:
        raise RuntimeError('普通媒体库 People snapshot 不完整')
    normal_ids = {str(value) for value in normal_scan['person_ids']}

    person_details = emby.get_all_person_details_snapshot_strict(
        processor.emby_url,
        processor.emby_api_key,
    )
    if person_details is None:
        raise RuntimeError('全量 Person identity snapshot 不完整')
    identity_index = defaultdict(set)
    identity_rows = []
    for person_id, detail in sorted(person_details.items()):
        try:
            identities = canonical_person_provider_identities(
                detail.get('ProviderIds'), strict=True,
            )
        except ValueError:
            identities = set()
        for provider, provider_id in sorted(identities):
            identity_index[(provider, provider_id)].add(person_id)
            identity_rows.append((provider, provider_id, person_id))

    normal_hash = _alias_proof_snapshot_hash(sorted(normal_ids))
    person_hash = _alias_proof_snapshot_hash([
        (
            person_id,
            str(detail.get('Name') or ''),
            detail.get('ProviderIds') or {},
        )
        for person_id, detail in sorted(person_details.items())
    ])
    if person_cleanup_db.require_ready_protection_snapshot() != generation:
        raise RuntimeError('保护快照 generation 在全局 snapshot 构建期间漂移')
    return {
        'generation': generation,
        'contract': contract,
        'root_contract': root_contract,
        'protection_hash': _alias_proof_protection_hash(contract, root_contract),
        'normal_ids': normal_ids,
        'person_details': person_details,
        'identity_index': dict(identity_index),
        'normal_hash': normal_hash,
        'person_hash': person_hash,
        'identity_count': len(identity_rows),
    }


def _check_alias_orphan_proof_candidate(processor, proof_item, snapshots):
    person_id = str(proof_item.get('person_id') or '')
    candidates = person_cleanup_db.get_candidates_by_ids(
        [person_id], include_protected=True,
    )
    current = candidates[0] if candidates else None
    candidate = {
        'person_id': person_id,
        'person_name': proof_item.get('person_name'),
        'provider_ids_json': proof_item.get('candidate_provider_ids') or {},
        'verification_status': 'identity_alias_only',
    }
    current_details = emby.get_person_details_strict(
        processor.emby_url,
        processor.emby_api_key,
        [person_id],
        batch_size=1,
    )
    detail = current_details.get(person_id) if current_details else None
    references = emby.get_person_media_references(
        processor.emby_url,
        processor.emby_api_key,
        person_id,
        limit=1,
        person_name=(detail or {}).get('Name') or candidate.get('person_name'),
        protected_root_contract=snapshots['root_contract'],
        user_id=getattr(processor, 'emby_user_id', None),
        detail_workers=1,
    )
    refreshed_candidates = person_cleanup_db.get_candidates_by_ids(
        [person_id], include_protected=True,
    )
    current = refreshed_candidates[0] if refreshed_candidates else None
    protection_reason = (
        person_cleanup_db.candidate_protection_reason(current, snapshots['contract'])
        if current else None
    )
    return classify_alias_orphan_proof(
        candidate,
        current,
        detail,
        snapshots['normal_ids'],
        snapshots['identity_index'],
        snapshots['person_details'],
        references,
        protection_reason=protection_reason,
    )


def task_alias_orphan_readonly_proof(processor, proof_id=None):
    """Run a persistent GET-only alias-orphan proof; never mutates Emby."""
    task_manager.update_status_from_thread(0, 'Alias Orphan 只读证明：构建完整快照...')
    active_proof_id = str(proof_id or '').strip() or None
    try:
        snapshots = _build_alias_proof_snapshots(processor)
        candidates = person_cleanup_db.list_alias_proof_candidates()
        if active_proof_id:
            run = person_cleanup_db.resume_alias_proof_run(
                active_proof_id,
                snapshots['generation'],
                snapshots['protection_hash'],
                snapshots['normal_hash'],
                snapshots['person_hash'],
            )
            person_cleanup_db.requeue_changed_alias_proof_items(
                active_proof_id,
                person_cleanup_db.list_candidates_raw(),
            )
            run = person_cleanup_db.get_alias_proof_run(active_proof_id) or run
        else:
            run = person_cleanup_db.create_alias_proof_run(
                snapshots['generation'],
                snapshots['protection_hash'],
                snapshots['normal_hash'],
                snapshots['person_hash'],
                candidates,
            )
            active_proof_id = str(run['proof_id'])

        total = int(run.get('candidate_total') or 0)
        with ThreadPoolExecutor(
            max_workers=PERSON_ALIAS_PROOF_WORKERS,
            thread_name_prefix='person-alias-proof',
        ) as executor:
            while True:
                if (
                    processor.is_stop_requested()
                    or person_cleanup_db.alias_proof_stop_requested(active_proof_id)
                ):
                    person_cleanup_db.stop_alias_proof_run(active_proof_id)
                    task_manager.update_status_from_thread(
                        100,
                        'Alias Orphan 只读证明已中止；已完成结果保留，未完成项可继续。',
                    )
                    return
                if person_cleanup_db.require_ready_protection_snapshot() != snapshots['generation']:
                    person_cleanup_db.fail_alias_proof_run(
                        active_proof_id,
                        '保护快照 generation 漂移，全部证明结果已失败关闭',
                        stale=True,
                    )
                    return

                claimed = person_cleanup_db.claim_alias_proof_items(
                    active_proof_id,
                    limit=PERSON_ALIAS_PROOF_CLAIM_LIMIT,
                )
                if not claimed:
                    final_snapshots = _build_alias_proof_snapshots(processor)
                    if (
                        final_snapshots['generation'] != snapshots['generation']
                        or final_snapshots['protection_hash'] != snapshots['protection_hash']
                        or final_snapshots['normal_hash'] != snapshots['normal_hash']
                        or final_snapshots['person_hash'] != snapshots['person_hash']
                    ):
                        person_cleanup_db.fail_alias_proof_run(
                            active_proof_id,
                            'Emby/保护快照在证明期间发生变化，全部证明结果已失败关闭',
                            stale=True,
                        )
                        return
                    final = person_cleanup_db.complete_alias_proof_run(
                        active_proof_id,
                        snapshots['generation'],
                    )
                    task_manager.update_status_from_thread(
                        100,
                        f"Alias Orphan 只读证明完成：{final.get('checked_count', 0)}/{total}；"
                        f"verified_alias_orphan {final.get('verified_alias_orphan_count', 0)}。"
                        '本版本不会删除人物。',
                    )
                    return

                futures = {
                    executor.submit(
                        _check_alias_orphan_proof_candidate,
                        processor,
                        item,
                        snapshots,
                    ): item
                    for item in claimed
                }
                for future in as_completed(futures):
                    item = futures[future]
                    try:
                        outcome = future.result()
                    except Exception as exc:
                        logger.error(
                            'Alias Orphan 只读证明失败 person_id=%s error=%s',
                            item.get('person_id'), type(exc).__name__,
                        )
                        outcome = {
                            'proof_state': 'failed_safe',
                            'error': f'只读证明异常: {type(exc).__name__}',
                        }
                    person_cleanup_db.finish_alias_proof_item(
                        active_proof_id,
                        item['person_id'],
                        outcome,
                    )
                current = person_cleanup_db.get_alias_proof_run(active_proof_id) or {}
                checked = int(current.get('checked_count') or 0)
                progress = int((checked / max(1, total)) * 100)
                task_manager.update_status_from_thread(
                    progress,
                    f"Alias Orphan 只读证明 {checked}/{total}；"
                    f"通过 {current.get('verified_alias_orphan_count', 0)}；"
                    '仅分类，不删除人物。',
                )
    except Exception as exc:
        if active_proof_id:
            try:
                person_cleanup_db.fail_alias_proof_run(
                    active_proof_id,
                    str(exc),
                    stale='generation' in str(exc).lower(),
                )
            except Exception:
                logger.error('持久化 Alias Orphan proof 失败状态异常', exc_info=True)
        task_manager.update_status_from_thread(-1, f'Alias Orphan 只读证明失败: {exc}')
        raise


def _build_stale_index_forensic_snapshots(processor):
    """Build immutable global GET-only evidence for one forensic run."""
    generation = person_cleanup_db.require_ready_protection_snapshot()
    contract = person_cleanup_db.get_protection_contract()
    if int(contract['generation']) != int(generation):
        raise RuntimeError('保护快照 generation 不一致')
    protected_libraries = person_cleanup_db.list_protected_libraries()
    for protected_library in protected_libraries:
        if (
            protected_library.get('snapshot_state') != 'ready'
            or int(protected_library.get('snapshot_generation') or -1) != int(generation)
        ):
            raise RuntimeError('至少一个受保护媒体库 snapshot 未在当前 generation 就绪')
    protected_ids = {
        str(item.get('library_id') or '').strip()
        for item in protected_libraries
        if str(item.get('library_id') or '').strip()
    }
    try:
        libraries = emby.get_all_libraries_with_paths_strict(
            processor.emby_url,
            processor.emby_api_key,
            required_library_ids=protected_ids,
        )
    except emby.StrictVirtualFoldersError as exc:
        raise RuntimeError(f'Stale Index 取证无法启动：VirtualFolder {exc}') from exc
    library_ids = {
        str(library.get('info', {}).get('Id') or '').strip()
        for library in libraries or []
        if str(library.get('info', {}).get('Id') or '').strip()
    }
    if not library_ids or not protected_ids.issubset(library_ids):
        raise RuntimeError('严格 physical library 列表不完整')
    root_contract = build_protected_library_root_contract(libraries, protected_libraries)
    if protected_ids and not root_contract.get('complete'):
        raise RuntimeError('受保护媒体库 root contract 不完整')

    normal_scan = emby.get_referenced_person_ids_strict(
        processor.emby_url,
        processor.emby_api_key,
        sorted(library_ids - protected_ids),
        require_person_names=True,
        capture_item_people=True,
    )
    if normal_scan is None:
        raise RuntimeError('normal People relationship snapshot 不完整')
    normal_ids = {str(value) for value in normal_scan['person_ids']}
    item_people = normal_scan.get('item_people')
    if not isinstance(item_people, dict):
        raise RuntimeError('normal item People relationship snapshot 缺失')

    person_details = emby.get_all_person_details_snapshot_strict(
        processor.emby_url,
        processor.emby_api_key,
    )
    if person_details is None:
        raise RuntimeError('全量 Person identity snapshot 不完整')
    identity_index = defaultdict(set)
    for person_id, detail in sorted(person_details.items()):
        try:
            identities = canonical_person_provider_identities(
                detail.get('ProviderIds'), strict=True,
            )
        except ValueError:
            identities = set()
        for identity in identities:
            identity_index[identity].add(person_id)

    normal_people_relationship_hash = _stale_index_relationship_hash(item_people)
    person_hash = _alias_proof_snapshot_hash([
        (person_id, str(detail.get('Name') or ''), detail.get('ProviderIds') or {})
        for person_id, detail in sorted(person_details.items())
    ])
    if person_cleanup_db.require_ready_protection_snapshot() != generation:
        raise RuntimeError('保护快照 generation 在全局 snapshot 构建期间漂移')
    return {
        'generation': generation,
        'contract': contract,
        'root_contract': root_contract,
        'protection_hash': _alias_proof_protection_hash(contract, root_contract),
        'normal_ids': normal_ids,
        'item_people': item_people,
        'normal_people_relationship_hash': normal_people_relationship_hash,
        'person_details': person_details,
        'person_hash': person_hash,
        'identity_index': dict(identity_index),
        'media_count': int(normal_scan.get('media_count') or 0),
        'libraries': libraries,
    }


def _check_stale_index_forensic_candidate(processor, source_item, snapshots):
    person_id = str(source_item.get('person_id') or '').strip()
    current_rows = person_cleanup_db.get_candidates_by_ids(
        [person_id], include_protected=True,
    )
    current = current_rows[0] if current_rows else None
    detail_result = emby.get_person_detail_forensic_strict(
        processor.emby_url,
        processor.emby_api_key,
        person_id,
    )
    query_items = emby.get_person_media_query_items_strict(
        processor.emby_url,
        processor.emby_api_key,
        person_id,
    )
    refreshed_rows = person_cleanup_db.get_candidates_by_ids(
        [person_id], include_protected=True,
    )
    current = refreshed_rows[0] if refreshed_rows else None
    protection_reason = (
        person_cleanup_db.candidate_protection_reason(current, snapshots['contract'])
        if current else None
    )
    return classify_stale_index_forensic(
        source_item,
        current,
        detail_result,
        snapshots['normal_ids'],
        snapshots['item_people'],
        snapshots['identity_index'],
        query_items,
        snapshots['root_contract'],
        protection_reason=protection_reason,
    )


def _build_stale_delete_canary_snapshot(processor):
    """Canary-only strengthening; never refresh/write protected snapshots."""
    snapshot = _build_stale_index_forensic_snapshots(processor)
    selected = person_cleanup_db.list_protected_libraries()
    realtime = _scan_protected_library_people(processor, selected)
    contract = snapshot['contract']
    for library in realtime.values():
        contract['name_keys'].update(build_person_name_protection_keys(library['all_person_names']))
        for person in library['people']:
            contract['person_ids'].add(person['person_id'])
            contract['provider_identities'].update(canonical_person_provider_identities(
                person['provider_ids'], strict=True,
            ))
    libraries = snapshot['libraries']
    all_roots = build_protected_library_root_contract(libraries, [
        {'library_id': library['info']['Id'], 'library_name': library['info']['Name']}
        for library in libraries
    ])
    if not all_roots['complete']:
        raise person_cleanup_db.CanarySafetyError('protection_drift', '完整媒体库 root contract 不可核验')
    snapshot['all_roots'] = all_roots
    # Bind all roots too: changing a normal root must not evade preview binding.
    snapshot['protection_hash'] = _alias_proof_snapshot_hash({
        'contract': _alias_proof_protection_hash(contract, all_roots),
        # Keep realtime evidence separate: union with persisted protection must
        # not hide a removal/change in the current protected library snapshot.
        'realtime_protected': [
            (library_id, int(facts['media_count']), sorted(facts['all_person_names']),
             sorted(facts['people'], key=lambda person: person['person_id']))
            for library_id, facts in sorted(realtime.items())
        ],
    })
    snapshot['person_details'] = {
        str(person_id): {key: detail.get(key) for key in ('Id', 'Type', 'Name', 'ProviderIds')}
        for person_id, detail in snapshot['person_details'].items()
    }
    if person_cleanup_db.require_ready_protection_snapshot() != snapshot['generation']:
        raise person_cleanup_db.CanarySafetyError('protection_drift', '保护 generation 已变化')
    return snapshot


def _check_stale_delete_canary_candidate(processor, source_item, snapshot):
    """Exact fresh media DTOs, not cached relationship facts, authorize a canary."""
    person_id = source_item['person_id']
    query = emby.get_person_media_query_items_strict(processor.emby_url, processor.emby_api_key, person_id)
    if query is None:
        raise person_cleanup_db.CanarySafetyError('failed_safe', 'PersonIds GET 不完整')
    if not query:
        raise person_cleanup_db.CanarySafetyError('query_disappeared', 'PersonIds query 已消失')
    seen = set()
    for hit in query:
        item_id = str(hit.get('Id') or '')
        relationship = snapshot['item_people'].get(item_id)
        if not item_id or item_id in seen:
            raise person_cleanup_db.CanarySafetyError('failed_safe', 'query ID 缺失或重复')
        seen.add(item_id)
        owner = match_item_to_protected_library(hit, snapshot['all_roots'])
        if not owner or not relationship or owner['protected_library_id'] != relationship['library_id']:
            raise person_cleanup_db.CanarySafetyError('protected', 'query 无法绑定 exact normal library')
        try:
            response = emby.emby_client.get(
                f"{processor.emby_url.rstrip('/')}/Items/{item_id}",
                headers={'X-Emby-Token': processor.emby_api_key},
                params={'Fields': 'People,Path'}, timeout=30, allow_redirects=False,
            )
            if response.status_code != 200:
                raise ValueError('HTTP failure')
            detail = response.json()
            people = detail.get('People')
            if (not isinstance(people, list) or not people or
                    any(not isinstance(p, dict) or not p.get('Id') or not p.get('Name') for p in people)):
                raise ValueError('People unavailable')
            actual = tuple(sorted((str(p['Id']).strip(), str(p['Name']).strip()) for p in people))
            if person_id in {p[0] for p in actual}:
                raise person_cleanup_db.CanarySafetyError('linked', 'fresh exact People 已引用 candidate')
            if (str(detail.get('Id')) != item_id or detail.get('Type') != relationship['item_type']
                    or match_item_to_protected_library(detail, snapshot['all_roots']) != owner
                    or actual != tuple(relationship['people'])):
                raise person_cleanup_db.CanarySafetyError('relationship_drift', 'exact query item relationship 已变化')
        except person_cleanup_db.CanarySafetyError:
            raise
        except Exception:
            raise person_cleanup_db.CanarySafetyError('people_unavailable', 'exact query item People 无法完整核验') from None
    # Refresh candidate and Person identity after media GETs. No second PersonIds
    # request: classify precisely the same query items whose current DTOs we read.
    rows = person_cleanup_db.get_candidates_by_ids([person_id], include_protected=True)
    current = rows[0] if rows else None
    detail = emby.get_person_detail_forensic_strict(processor.emby_url, processor.emby_api_key, person_id)
    protection = person_cleanup_db.candidate_protection_reason(current, snapshot['contract']) if current else None
    return classify_stale_index_forensic(
        source_item, current, detail, snapshot['normal_ids'], snapshot['item_people'],
        snapshot['identity_index'], query, snapshot['root_contract'], protection_reason=protection,
    )


def _stale_delete_canary_source_item(item):
    return {
        'person_id': str(item.get('person_id') or ''),
        'person_name': item.get('person_name'),
        'provider_ids': item.get('provider_ids') or {},
        'candidate_fingerprint': str(item.get('candidate_fingerprint') or ''),
        'source_proof_state': 'identity_not_found',
    }


def _stale_delete_canary_result_ready(result):
    return bool(
        isinstance(result, dict)
        and result.get('forensic_state') == 'verified_stale_index_signature'
        and result.get('identity_signal') == 'stale_index_no_identity_owner'
        and result.get('people_signal') == 'stale_index_different_people'
        and int(result.get('query_count') or 0) > 0
        and int(result.get('actual_people_count') or 0) > 0
        and int(result.get('same_name_other_count') or 0) == 0
        and int(result.get('identity_owner_count') or 0) == 0
    )


def _stale_delete_canary_snapshot_equal(left, right):
    return bool(
        int(left.get('generation') or -1) == int(right.get('generation') or -2)
        and left.get('protection_hash') == right.get('protection_hash')
        and left.get('normal_people_relationship_hash')
            == right.get('normal_people_relationship_hash')
        and left.get('person_hash') == right.get('person_hash')
        and left.get('item_people') == right.get('item_people')
        and left.get('person_details') == right.get('person_details')
    )


def task_preview_stale_delete_canary(processor, job_id):
    """Build a fixed GET-only Canary preview from two completed stable runs."""
    try:
        person_cleanup_db.validate_stale_delete_canary_chain(job_id)
        admin_context = emby.ensure_admin_delete_context(processor.emby_url, processor.emby_api_key, job_id)
        person_cleanup_db.bind_stale_delete_canary_admin_context(job_id, admin_context.binding_hash)
        task_manager.update_status_from_thread(0, 'Stale Index Canary：构建完整只读快照...')
        start_snapshot = _build_stale_delete_canary_snapshot(processor)
        person_cleanup_db.set_stale_delete_canary_preview_snapshot(job_id, start_snapshot)
        items = person_cleanup_db.list_stale_delete_canary_items(job_id)
        total = len(items)
        if total < 1 or total > PERSON_STALE_DELETE_CANARY_LIMIT:
            raise RuntimeError('Canary 样本必须为 1 到 100，禁止空任务或越过硬上限')
        for index, item in enumerate(items, start=1):
            if processor.is_stop_requested() or person_cleanup_db.stale_delete_canary_stop_requested(job_id):
                person_cleanup_db.fail_stale_delete_canary_job(
                    job_id, 'stopped', 'Canary preview 已在人物边界停止；不得继续旧任务',
                )
                return
            try:
                result = _check_stale_delete_canary_candidate(
                    processor, _stale_delete_canary_source_item(item), start_snapshot,
                )
            except person_cleanup_db.CanarySafetyError as exc:
                result = {'forensic_state': exc.state, 'error': str(exc)}
            ready = _stale_delete_canary_result_ready(result)
            person_cleanup_db.mark_stale_delete_canary_preview_item(
                job_id,
                item['person_id'],
                'canary_delete_ready' if ready else 'preflight_rejected',
                evidence=result,
                error=None if ready else (result.get('error') or '当前证据不满足 Canary 删除合同'),
            )
            task_manager.update_status_from_thread(
                int(index * 90 / max(1, total)),
                f'Stale Index Canary 预览 {index}/{total}',
            )
        person_cleanup_db.validate_stale_delete_canary_chain(job_id)
        final_snapshot = _build_stale_delete_canary_snapshot(processor)
        if not _stale_delete_canary_snapshot_equal(start_snapshot, final_snapshot):
            raise RuntimeError('Canary preview 全局 snapshot 在预览期间漂移')
        person_cleanup_db.finish_stale_delete_canary_preview(job_id)
        task_manager.update_status_from_thread(100, 'Stale Index Canary 预览完成，等待独立确认')
    except Exception as exc:
        message = str(exc) if isinstance(exc, (person_cleanup_db.CanarySafetyError, emby.AdminDeleteContextError)) else f'Canary 预览中止: {type(exc).__name__}'
        logger.error('Stale Index Canary 预览失败: %s', message)
        try:
            person_cleanup_db.fail_stale_delete_canary_job(
                job_id, 'preview_failed', message,
            )
        except Exception:
            logger.error('持久化 Canary preview 失败状态异常', exc_info=True)
        raise RuntimeError(message) from None


def _stop_stale_delete_canary_at_boundary(job_id, items, start_index, message):
    for item in items[start_index:]:
        person_cleanup_db.finish_stale_delete_canary_item(
            job_id, item['person_id'], 'stopped_before_start', error=message,
        )
    person_cleanup_db.fail_stale_delete_canary_job(job_id, 'stopped', message)


def task_execute_stale_delete_canary(processor, job_id):
    """Delete at most 100 fixed stable signatures, serially and at most once."""
    execution_snapshot = None
    # A losing execution request returns without reading Emby OR changing the
    # winner's persistent state. Startup interruption never permits takeover.
    if not person_cleanup_db.claim_stale_delete_canary_execution(job_id):
        return
    try:
        chain = person_cleanup_db.validate_stale_delete_canary_chain(job_id)
        job = chain['job']
        admin_context = emby.ensure_admin_delete_context(processor.emby_url, processor.emby_api_key, job_id)
        person_cleanup_db.bind_stale_delete_canary_admin_context(job_id, admin_context.binding_hash, execution=True)
        admin_context = emby.authenticate_canary_admin_once(admin_context,
            before_submit=lambda: person_cleanup_db.reserve_stale_delete_canary_admin_auth(job_id))
        person_cleanup_db.verify_stale_delete_canary_admin_auth(job_id, admin_context.user_id, admin_context.binding_hash)
        task_manager.update_status_from_thread(0, 'Stale Index Canary：执行前重建完整快照...')
        execution_snapshot = _build_stale_delete_canary_snapshot(processor)
        for current_key, preview_key, state in (
            ('generation', 'preview_snapshot_generation', 'protection_drift'),
            ('protection_hash', 'preview_protection_hash', 'protection_drift'),
            ('normal_people_relationship_hash', 'preview_relationship_hash', 'relationship_drift'),
            ('person_hash', 'preview_person_hash', 'person_snapshot_drift'),
        ):
            if execution_snapshot[current_key] != job[preview_key]:
                raise person_cleanup_db.CanarySafetyError(state, 'Canary execution preflight 与 preview snapshot 不一致')
        person_cleanup_db.start_stale_delete_canary_job(job_id, execution_snapshot)
        items = person_cleanup_db.list_stale_delete_canary_items(job_id, ready_only=True)
        if not items or len(items) > PERSON_STALE_DELETE_CANARY_LIMIT:
            raise RuntimeError('Canary ready 项目为空或超过后端硬上限')

        confirmed_ids = set()
        for index, item in enumerate(items):
            if processor.is_stop_requested() or person_cleanup_db.stale_delete_canary_stop_requested(job_id):
                _stop_stale_delete_canary_at_boundary(
                    job_id, items, index,
                    'Canary 已在人物边界停止；未开始项目不会继续，任务不可 resume',
                )
                return

            result = {}
            transport = {}
            def before_submit():
                nonlocal result
                if processor.is_stop_requested() or person_cleanup_db.stale_delete_canary_stop_requested(job_id):
                    raise person_cleanup_db.CanarySafetyError('stopped', '提交前收到停止请求')
                person_cleanup_db.validate_stale_delete_canary_chain(job_id)
                fresh = _build_stale_delete_canary_snapshot(processor)
                expected_people = {key: value for key, value in execution_snapshot['person_details'].items()
                                   if key not in confirmed_ids}
                if fresh['protection_hash'] != execution_snapshot['protection_hash']:
                    raise person_cleanup_db.CanarySafetyError('protection_drift', '删除前 protection 已变化')
                if fresh['normal_people_relationship_hash'] != execution_snapshot['normal_people_relationship_hash']:
                    raise person_cleanup_db.CanarySafetyError('relationship_drift', '删除前 relationship 已变化')
                if fresh['person_details'] != expected_people:
                    raise person_cleanup_db.CanarySafetyError('person_snapshot_drift', '删除前 Person snapshot 非预期变化')
                result = _check_stale_delete_canary_candidate(processor, _stale_delete_canary_source_item(item), fresh)
                if not _stale_delete_canary_result_ready(result):
                    state = result.get('forensic_state')
                    raise person_cleanup_db.CanarySafetyError(
                        state if state in person_cleanup_db.STALE_DELETE_CANARY_TERMINAL_STATES else 'failed_safe',
                        result.get('error') or '删除前证据失效',
                    )
                # The context manager commits BEFORE this returns. Commit
                # failure/uncertainty propagates and the transport sends no POST.
                emby.verify_canary_admin_session(admin_context, lost=True)
                if not person_cleanup_db.reserve_stale_delete_canary_attempt(job_id, item['person_id']):
                    raise person_cleanup_db.CanarySafetyError('delete_ambiguous', '已有尝试或状态改变，禁止提交')
                return True
            outcome = emby.delete_person_custom_api_outcome(
                processor.emby_url,
                processor.emby_api_key,
                item['person_id'],
                before_submit=before_submit, cached_auth_only=True,
                admin_context=admin_context, context_job_id=job_id,
                response_observer=lambda status: transport.update(http_status=status),
            )
            if outcome in ('auth_unavailable', 'admin_session_lost'):
                message = '当前执行的管理员上下文已失效；停止执行，不会自动登录或重放删除'
                person_cleanup_db.finish_stale_delete_canary_item(
                    job_id, item['person_id'], 'precheck_failed', error=message,
                    evidence=transport,
                )
                person_cleanup_db.fail_stale_delete_canary_job(job_id, 'admin_session_lost', message)
                return
            if outcome == 'ambiguous':
                person_cleanup_db.finish_stale_delete_canary_item(
                    job_id, item['person_id'], 'delete_ambiguous',
                    evidence=transport,
                    error='DeletePerson 结果不确定；禁止自动重放',
                )
                person_cleanup_db.fail_stale_delete_canary_job(
                    job_id, 'delete_ambiguous',
                    f"人物 {item['person_id']} DeletePerson 结果不确定；任务停止",
                )
                return
            if outcome != 'confirmed':
                person_cleanup_db.finish_stale_delete_canary_item(
                    job_id, item['person_id'], 'delete_failed',
                    evidence=transport,
                    error='DeletePerson 明确失败；禁止继续 Canary',
                )
                person_cleanup_db.fail_stale_delete_canary_job(
                    job_id, 'delete_failed',
                    f"人物 {item['person_id']} DeletePerson 明确失败；任务停止",
                )
                return

            readback = emby.get_person_detail_forensic_strict(
                processor.emby_url,
                processor.emby_api_key,
                item['person_id'],
            )
            if readback.get('status') == 'person_missing':
                person_cleanup_db.finish_stale_delete_canary_item(
                    job_id, item['person_id'], 'confirmed_deleted',
                    {**transport, 'readback': 'person_missing'},
                )
                person_cleanup_db.remove_candidate(item['person_id'])
                confirmed_ids.add(str(item['person_id']))
            elif readback.get('status') == 'ok':
                person_cleanup_db.finish_stale_delete_canary_item(
                    job_id, item['person_id'], 'delete_not_confirmed',
                    {**transport, 'readback': 'person_exists'},
                    'DeletePerson 返回成功但 exact GET 仍存在；禁止重放',
                )
                person_cleanup_db.fail_stale_delete_canary_job(
                    job_id, 'delete_not_confirmed',
                    f"人物 {item['person_id']} 删除未由 exact GET 确认；任务停止",
                )
                return
            else:
                person_cleanup_db.finish_stale_delete_canary_item(
                    job_id, item['person_id'], 'delete_ambiguous',
                    {**transport, 'readback': 'unavailable'},
                    'DeletePerson 后 exact GET 不确定；禁止重放',
                )
                person_cleanup_db.fail_stale_delete_canary_job(
                    job_id, 'delete_ambiguous',
                    f"人物 {item['person_id']} 删除回读不确定；任务停止",
                )
                return

            task_manager.update_status_from_thread(
                int((index + 1) * 95 / max(1, len(items))),
                f'Stale Index Canary 串行删除 {index + 1}/{len(items)}',
            )

        person_cleanup_db.validate_stale_delete_canary_chain(job_id)
        final_snapshot = _build_stale_delete_canary_snapshot(processor)
        expected_people = {
            person_id: detail
            for person_id, detail in execution_snapshot['person_details'].items()
            if str(person_id) not in confirmed_ids
        }
        verification = {
            'protection_unchanged': final_snapshot['generation'] == execution_snapshot['generation']
                and final_snapshot['protection_hash'] == execution_snapshot['protection_hash'],
            'relationship_unchanged': final_snapshot['normal_people_relationship_hash'] == execution_snapshot['normal_people_relationship_hash']
                and final_snapshot['item_people'] == execution_snapshot['item_people'],
            'person_delta_exact': final_snapshot['person_details'] == expected_people,
            'final_protection_hash': final_snapshot['protection_hash'],
            'final_relationship_hash': final_snapshot['normal_people_relationship_hash'],
            'final_person_hash': final_snapshot['person_hash'],
            'confirmed_deleted_count': len(confirmed_ids),
        }
        if not all(verification[key] for key in ('protection_unchanged', 'relationship_unchanged', 'person_delta_exact')):
            person_cleanup_db.fail_stale_delete_canary_job(
                job_id, 'environment_drift',
                'Canary 最终全局快照不是 execution start 减去 confirmed IDs',
                final_verification=verification,
            )
            return
        person_cleanup_db.complete_stale_delete_canary_job(job_id, verification)
        task_manager.update_status_from_thread(100, 'Stale Index Canary 已完成并通过全局验证')
    except Exception as exc:
        # Do not persist/log a raw HTTP exception (it may contain URLs/secrets).
        state = exc.state if isinstance(exc, (person_cleanup_db.CanarySafetyError, emby.AdminDeleteContextError)) else 'failed_safe'
        message = str(exc) if isinstance(exc, (person_cleanup_db.CanarySafetyError, emby.AdminDeleteContextError)) else f'Canary 执行中止: {type(exc).__name__}'
        logger.error('Stale Index Canary 执行失败: %s', message)
        current = person_cleanup_db.get_stale_delete_canary_job(job_id)
        if current and current.get('admin_auth_state') == 'post_reserved' and state == 'failed_safe':
            state = 'admin_auth_ambiguous'
        if current and current.get('state') not in person_cleanup_db.STALE_DELETE_CANARY_TERMINAL_STATES:
            persisted_items = person_cleanup_db.list_stale_delete_canary_items(job_id)
            if any(row.get('execute_state') == 'post_reserved' for row in persisted_items):
                state = 'delete_ambiguous'
            elif 'item' in locals():
                if current.get('stop_requested'):
                    state = 'stopped'
                person_cleanup_db.finish_stale_delete_canary_item(
                    job_id, item['person_id'], 'precheck_failed', error=message,
                )
            elif current.get('stop_requested'):
                state = 'stopped'
            person_cleanup_db.fail_stale_delete_canary_job(
                job_id, state, message,
            )
        raise RuntimeError(message) from None


def task_stale_index_readonly_forensic(processor, run_id=None):
    """Run persistent stale PersonIds forensic using Emby GET only."""
    task_manager.update_status_from_thread(0, 'Stale Index 只读取证：构建完整关系快照...')
    active_run_id = str(run_id or '').strip() or None
    snapshots = None
    final_snapshots = None
    run = None
    diagnostics = None
    try:
        existing_run = person_cleanup_db.get_stale_index_run(active_run_id) if active_run_id else None
        if active_run_id and not existing_run:
            raise RuntimeError('Stale Index 只读取证任务不存在')
        source = (
            {'proof_id': existing_run['source_proof_id']}
            if existing_run else person_cleanup_db.get_latest_completed_alias_proof_source()
        )
        if not source:
            raise RuntimeError('没有 completed Alias Proof 可作为 identity_not_found source')
        snapshots = _build_stale_index_forensic_snapshots(processor)
        if active_run_id:
            run = person_cleanup_db.resume_stale_index_run(
                active_run_id,
                snapshots['generation'],
                snapshots['protection_hash'],
                snapshots['normal_people_relationship_hash'],
                snapshots['person_hash'],
            )
            person_cleanup_db.requeue_changed_stale_index_items(
                active_run_id,
                person_cleanup_db.list_candidates_raw(),
            )
            run = person_cleanup_db.get_stale_index_run(active_run_id) or run
        else:
            run = person_cleanup_db.create_stale_index_run(
                source['proof_id'],
                snapshots['generation'],
                snapshots['protection_hash'],
                snapshots['normal_people_relationship_hash'],
                snapshots['person_hash'],
            )
            active_run_id = str(run['run_id'])
        total = int(run.get('candidate_total') or 0)

        with ThreadPoolExecutor(
            max_workers=PERSON_STALE_INDEX_WORKERS,
            thread_name_prefix='person-stale-index',
        ) as executor:
            while True:
                if (
                    processor.is_stop_requested()
                    or person_cleanup_db.stale_index_stop_requested(active_run_id)
                ):
                    person_cleanup_db.stop_stale_index_run(active_run_id)
                    task_manager.update_status_from_thread(
                        100,
                        'Stale Index 只读取证已中止；已完成结果保留，未完成项可继续。',
                    )
                    return
                if person_cleanup_db.require_ready_protection_snapshot() != snapshots['generation']:
                    final_snapshots = _build_stale_index_forensic_snapshots(processor)
                    final_source = person_cleanup_db.get_alias_proof_source_diagnostic(
                        run['source_proof_id'],
                    )
                    diagnostics = _build_stale_index_drift_diagnostics(
                        snapshots,
                        final_snapshots,
                        run['source_proof_hash'],
                        final_source,
                    )
                    person_cleanup_db.fail_stale_index_run(
                        active_run_id,
                        '保护快照 generation 漂移，全部 signature 已失败关闭',
                        stale=True,
                        diagnostics=diagnostics,
                    )
                    return
                claimed = person_cleanup_db.claim_stale_index_items(
                    active_run_id,
                    limit=PERSON_STALE_INDEX_CLAIM_LIMIT,
                )
                if not claimed:
                    if person_cleanup_db.stale_index_stop_requested(active_run_id):
                        person_cleanup_db.stop_stale_index_run(active_run_id)
                        return
                    final_snapshots = _build_stale_index_forensic_snapshots(processor)
                    final_source = person_cleanup_db.get_alias_proof_source_diagnostic(
                        run['source_proof_id'],
                    )
                    diagnostics = _build_stale_index_drift_diagnostics(
                        snapshots,
                        final_snapshots,
                        run['source_proof_hash'],
                        final_source,
                    )
                    if diagnostics['has_drift']:
                        changed = [
                            label
                            for key, label in (
                                ('drift_generation', 'generation'),
                                ('drift_protection', 'protection'),
                                ('drift_normal_relationship', 'normal_relationship'),
                                ('drift_person', 'person'),
                                ('drift_source_proof', 'source_proof'),
                            )
                            if diagnostics[key]
                        ]
                        person_cleanup_db.fail_stale_index_run(
                            active_run_id,
                            'Snapshot 漂移：' + ', '.join(changed)
                            + '；全部 signature 已失败关闭',
                            stale=True,
                            diagnostics=diagnostics,
                        )
                        return
                    person_cleanup_db.record_stale_index_final_diagnostics(
                        active_run_id, diagnostics,
                    )
                    final = person_cleanup_db.complete_stale_index_run(
                        active_run_id,
                        snapshots['generation'],
                        snapshots['protection_hash'],
                        snapshots['normal_people_relationship_hash'],
                        snapshots['person_hash'],
                    )
                    task_manager.update_status_from_thread(
                        100,
                        f"Stale Index 只读取证完成：{final.get('checked_count', 0)}/{total}；"
                        f"signature {final.get('verified_signature_count', 0)}；"
                        f"stable {final.get('stable_signature_count', 0)}。"
                        '仅取证，不删除人物。',
                    )
                    return

                futures = {
                    executor.submit(
                        _check_stale_index_forensic_candidate,
                        processor,
                        item,
                        snapshots,
                    ): item
                    for item in claimed
                }
                for future in as_completed(futures):
                    item = futures[future]
                    try:
                        outcome = future.result()
                    except Exception as exc:
                        logger.error(
                            'Stale Index 只读取证失败 person_id=%s error=%s',
                            item.get('person_id'), type(exc).__name__,
                        )
                        outcome = {
                            'forensic_state': 'failed_safe',
                            'error': f'只读取证异常: {type(exc).__name__}',
                        }
                    person_cleanup_db.finish_stale_index_item(
                        active_run_id,
                        item['person_id'],
                        outcome,
                    )
                current = person_cleanup_db.get_stale_index_run(active_run_id) or {}
                checked = int(current.get('checked_count') or 0)
                progress = int((checked / max(1, total)) * 100)
                task_manager.update_status_from_thread(
                    progress,
                    f"Stale Index 只读取证 {checked}/{total}；"
                    f"signature {current.get('verified_signature_count', 0)}；"
                    f"stable {current.get('stable_signature_count', 0)}；"
                    'GET-only，不删除人物。',
                )
    except Exception as exc:
        if active_run_id:
            try:
                error_text = str(exc)
                error_lower = error_text.lower()
                stale_failure = any(
                    marker in error_lower
                    for marker in ('snapshot', 'generation', 'source')
                )
                if stale_failure and snapshots and run:
                    try:
                        final_snapshots = final_snapshots or _build_stale_index_forensic_snapshots(
                            processor,
                        )
                        final_source = person_cleanup_db.get_alias_proof_source_diagnostic(
                            run['source_proof_id'],
                        )
                        diagnostics = _build_stale_index_drift_diagnostics(
                            snapshots,
                            final_snapshots,
                            run['source_proof_hash'],
                            final_source,
                        )
                        if 'source' in error_lower:
                            diagnostics['drift_source_proof'] = True
                            diagnostics['has_drift'] = True
                            diagnostics['source_proof_drift_summary'].update({
                                'source_proof_changed': True,
                                'reason': error_text,
                            })
                    except Exception:
                        logger.error(
                            'Stale Index 失败后的 drift diagnostics 重建失败',
                            exc_info=True,
                        )
                person_cleanup_db.fail_stale_index_run(
                    active_run_id,
                    error_text,
                    stale=stale_failure,
                    diagnostics=diagnostics,
                )
            except Exception:
                logger.error('持久化 Stale Index forensic 失败状态异常', exc_info=True)
        task_manager.update_status_from_thread(-1, f'Stale Index 只读取证失败: {exc}')
        raise


def task_delete_selected_ghost_actors(processor, person_ids):
    task_name = "删除选中幽灵人物"
    requested_ids = sorted({str(person_id) for person_id in person_ids if person_id})
    candidates = person_cleanup_db.get_candidates_by_ids(
        requested_ids,
        include_protected=True,
    )
    candidate_map = {str(item['person_id']): item for item in candidates}

    if not requested_ids or len(candidate_map) != len(requested_ids):
        task_manager.update_status_from_thread(-1, "删除已取消：包含不在候选列表中的人物")
        return
    try:
        initial_generation = person_cleanup_db.require_ready_protection_snapshot()
    except RuntimeError as exc:
        task_manager.update_status_from_thread(-1, f"删除已取消：{exc}")
        return
    if any(
        not is_explicit_verified_orphan(candidate_map[person_id], initial_generation)
        for person_id in requested_ids
    ):
        task_manager.update_status_from_thread(
            -1,
            "删除已取消：只有当前保护快照下显式核验为 orphan 的人物可以删除",
        )
        return
    try:
        generation, _ = _refresh_protected_snapshot(processor)
        contract = person_cleanup_db.get_protection_contract()
        if contract['generation'] != generation:
            raise RuntimeError('删除前保护快照 generation 已变化')
        protected_root_contract = _build_protected_root_contract(processor)
    except Exception as exc:
        logger.error(f"删除前保护库复核失败: {exc}", exc_info=True)
        task_manager.update_status_from_thread(-1, f"删除已取消：{exc}")
        return

    deleted_count = 0
    linked_count = 0
    failed_count = 0
    protected_count = 0
    total = len(requested_ids)

    for index, person_id in enumerate(requested_ids, start=1):
        if processor.is_stop_requested():
            break
        candidate = candidate_map[person_id]
        person_name = candidate.get('person_name') or person_id
        protection_reason = person_cleanup_db.candidate_protection_reason(
            candidate,
            contract,
        )
        if protection_reason:
            protected_count += 1
            person_cleanup_db.remove_candidate(person_id)
            logger.warning(
                "  ➜ 跳过 '%s'：命中保护合同 %s。",
                person_name,
                protection_reason,
            )
            continue
        progress = int(((index - 1) / total) * 100)
        task_manager.update_status_from_thread(
            progress,
            f"({index}/{total}) 删除前复核: {person_name}",
        )

        references = emby.get_person_media_references(
            processor.emby_url,
            processor.emby_api_key,
            person_id,
            limit=1,
            person_name=person_name,
            protected_root_contract=protected_root_contract,
            user_id=getattr(processor, 'emby_user_id', None),
        )
        reference_status = classify_reference_check(references)
        if reference_status in {
            'protected_library_alias',
            'protected_library_unverifiable',
        }:
            protected_count += 1
            person_cleanup_db.persist_protected_alias_and_remove_candidate(
                candidate,
                references.get('protected_library_id'),
                reference_status,
                references.get('evidence_item_id'),
            )
            logger.warning(
                "  ➜ 跳过 '%s'：已按受保护媒体库 alias 合同撤销候选。",
                person_name,
            )
            continue
        if reference_status in {'connection_failed', 'invalid_response', 'people_unavailable'}:
            failed_count += 1
            error = reference_check_failure_message(reference_status, context='删除前复核')
            person_cleanup_db.mark_candidate_checked(
                person_id,
                reference_status,
                generation,
                error,
            )
            logger.warning(f"  ➜ 跳过 '{person_name}'：{error}")
            continue
        if reference_status == 'linked':
            linked_count += 1
            person_cleanup_db.remove_candidate(person_id)
            logger.warning(f"  ➜ 跳过 '{person_name}'：删除前发现新的媒体关联。")
            continue
        if reference_status == 'identity_alias_only':
            failed_count += 1
            error = '仅命中同身份其他 Person 的作品；不是显式 orphan，禁止删除'
            person_cleanup_db.mark_candidate_checked(
                person_id,
                'identity_alias_only',
                generation,
                error,
            )
            logger.warning(f"  ➜ 跳过 '{person_name}'：{error}")
            continue
        if reference_status != 'orphan':
            failed_count += 1
            error = reference_check_failure_message('invalid_response', context='删除前复核')
            person_cleanup_db.mark_candidate_checked(
                person_id,
                'invalid_response',
                generation,
                error,
            )
            continue

        if not person_cleanup_db.reserve_person_delete_attempt(person_id):
            failed_count += 1
            person_cleanup_db.mark_candidate_checked(
                person_id,
                'invalid_response',
                generation,
                '该人物已有删除 POST 尝试记录，禁止自动重放',
            )
            continue

        outcome = emby.delete_person_custom_api_outcome(
            processor.emby_url,
            processor.emby_api_key,
            person_id,
        )
        if outcome != 'confirmed':
            person_cleanup_db.finish_person_delete_attempt(
                person_id,
                'ambiguous' if outcome == 'ambiguous' else 'failed',
                '删除 POST 结果不确定' if outcome == 'ambiguous' else '删除 POST 明确失败',
            )
            failed_count += 1
            person_cleanup_db.mark_candidate_checked(
                person_id,
                'invalid_response',
                generation,
                'Emby 删除结果不确定，禁止自动重试'
                if outcome == 'ambiguous'
                else 'Emby 删除明确失败，请检查神医接口和管理员配置',
            )
            continue

        person_cleanup_db.finish_person_delete_attempt(person_id, 'confirmed')

        try:
            with get_db_connection() as conn:
                with conn.cursor() as cursor:
                    cursor.execute(
                        "DELETE FROM person_identity_map WHERE emby_person_id = %s",
                        (person_id,),
                    )
            person_cleanup_db.remove_candidate(person_id)
            deleted_count += 1
        except Exception as exc:
            failed_count += 1
            person_cleanup_db.mark_candidate_checked(
                person_id,
                'invalid_response',
                generation,
                f"Emby 已删除，但 Toolkit 映射清理失败: {exc}",
            )
        time.sleep(0.2)

    message = (
        f"人物清理完成：删除 {deleted_count}，保护跳过 {protected_count}，"
        f"因重新发现关联跳过 {linked_count}，"
        f"失败 {failed_count}。"
    )
    logger.info(f"  ➜ {message}")
    task_manager.update_status_from_thread(100, message)


def task_preview_safe_person_cleanup(processor, job_id):
    """Build a persistent preview; this task never calls the Person delete API."""
    try:
        generation, _ = _refresh_protected_snapshot(processor)
        contract = person_cleanup_db.get_protection_contract()
        if contract['generation'] != generation:
            raise RuntimeError('预览保护快照 generation 已变化')
        protected_root_contract = _build_protected_root_contract(processor)

        candidates = person_cleanup_db.list_candidates_raw()
        total = len(candidates)
        person_cleanup_db.initialize_cleanup_job_candidate_total(job_id, total)
        for index, candidate in enumerate(candidates, start=1):
            if processor.is_stop_requested() or person_cleanup_db.cleanup_job_stop_requested(job_id):
                person_cleanup_db.finish_cleanup_job(job_id, stopped=True)
                task_manager.update_status_from_thread(100, '一键安全清理预览已中止')
                return

            person_id = str(candidate.get('person_id') or '')
            person_name = candidate.get('person_name') or person_id
            protection_reason = person_cleanup_db.candidate_protection_reason(
                candidate,
                contract,
            )
            if protection_reason:
                person_cleanup_db.add_cleanup_job_item(
                    job_id,
                    candidate,
                    protection_reason,
                )
                person_cleanup_db.remove_candidate(person_id)
                continue

            references = emby.get_person_media_references(
                processor.emby_url,
                processor.emby_api_key,
                person_id,
                limit=1,
                person_name=person_name,
                protected_root_contract=protected_root_contract,
                user_id=getattr(processor, 'emby_user_id', None),
            )
            status = classify_reference_check(references)
            if status in {
                'protected_library_alias',
                'protected_library_unverifiable',
            }:
                person_cleanup_db.persist_protected_alias_and_remove_candidate(
                    candidate,
                    references.get('protected_library_id'),
                    status,
                    references.get('evidence_item_id'),
                )
                person_cleanup_db.add_cleanup_job_item(
                    job_id,
                    candidate,
                    status,
                    '已按受保护媒体库人物处理并撤销候选',
                )
            elif status == 'linked':
                person_cleanup_db.add_cleanup_job_item(job_id, candidate, 'linked')
                person_cleanup_db.remove_candidate(person_id)
            elif status == 'orphan':
                person_cleanup_db.mark_candidate_checked(
                    person_id,
                    'orphan',
                    generation,
                )
                person_cleanup_db.add_cleanup_job_item(
                    job_id,
                    candidate,
                    'verified_orphan',
                )
            elif status == 'identity_alias_only':
                error = '仅命中同身份其他 Person；不属于 verified orphan'
                person_cleanup_db.mark_candidate_checked(
                    person_id,
                    status,
                    generation,
                    error,
                )
                person_cleanup_db.add_cleanup_job_item(
                    job_id,
                    candidate,
                    status,
                    error,
                )
            else:
                safe_status = status if status in {
                    'people_unavailable', 'connection_failed', 'invalid_response'
                } else 'invalid_response'
                error = reference_check_failure_message(safe_status, context='安全清理预览')
                person_cleanup_db.mark_candidate_checked(
                    person_id,
                    safe_status,
                    generation,
                    error,
                )
                person_cleanup_db.add_cleanup_job_item(
                    job_id,
                    candidate,
                    safe_status,
                    error,
                )

            progress = int((index / max(1, total)) * 100)
            task_manager.update_status_from_thread(
                progress,
                f'一键安全清理预览 {index}/{total}',
            )

        person_cleanup_db.finish_cleanup_preview(job_id, generation)
        task_manager.update_status_from_thread(100, '一键安全清理预览已完成，等待管理员确认')
    except Exception as exc:
        logger.error('一键安全清理预览失败: %s', exc, exc_info=True)
        person_cleanup_db.fail_cleanup_job(job_id, str(exc))
        raise


def task_execute_safe_person_cleanup(processor, job_id):
    """Serially delete only previewed orphans after a fresh full precheck."""
    person_cleanup_db.start_cleanup_job(job_id)
    try:
        generation, _ = _refresh_protected_snapshot(processor)
        contract = person_cleanup_db.get_protection_contract()
        if contract['generation'] != generation:
            raise RuntimeError('执行保护快照 generation 已变化')
        protected_root_contract = _build_protected_root_contract(processor)

        items = person_cleanup_db.list_cleanup_job_orphans(job_id)
        total = len(items)
        for index, item in enumerate(items, start=1):
            if processor.is_stop_requested() or person_cleanup_db.cleanup_job_stop_requested(job_id):
                person_cleanup_db.finish_cleanup_job(job_id, stopped=True)
                task_manager.update_status_from_thread(100, '一键安全清理已中止')
                return

            person_id = str(item.get('person_id') or '')
            current_rows = person_cleanup_db.get_candidates_by_ids(
                [person_id],
                include_protected=True,
            )
            if not current_rows:
                person_cleanup_db.mark_cleanup_job_item(
                    job_id, person_id, 'skipped_candidate_changed',
                    '候选已不存在', completed=True,
                )
                continue
            candidate = current_rows[0]
            if candidate_fingerprint(candidate) != item.get('candidate_fingerprint'):
                person_cleanup_db.mark_cleanup_job_item(
                    job_id, person_id, 'skipped_candidate_changed',
                    '候选身份已变化', completed=True,
                )
                continue

            protection_reason = person_cleanup_db.candidate_protection_reason(
                candidate,
                contract,
            )
            if protection_reason:
                person_cleanup_db.remove_candidate(person_id)
                person_cleanup_db.mark_cleanup_job_item(
                    job_id, person_id, f'skipped_{protection_reason}',
                    '删除前命中保护合同', completed=True,
                )
                continue

            references = emby.get_person_media_references(
                processor.emby_url,
                processor.emby_api_key,
                person_id,
                limit=1,
                person_name=candidate.get('person_name'),
                protected_root_contract=protected_root_contract,
                user_id=getattr(processor, 'emby_user_id', None),
            )
            status = classify_reference_check(references)
            if status != 'orphan':
                if status in {
                    'protected_library_alias',
                    'protected_library_unverifiable',
                }:
                    person_cleanup_db.persist_protected_alias_and_remove_candidate(
                        candidate,
                        references.get('protected_library_id'),
                        status,
                        references.get('evidence_item_id'),
                    )
                elif status == 'linked':
                    person_cleanup_db.remove_candidate(person_id)
                person_cleanup_db.mark_cleanup_job_item(
                    job_id, person_id, f'skipped_{status}',
                    '删除前实时核验不再是显式 orphan', completed=True,
                )
                continue

            if int(item.get('post_attempts') or 0) != 0:
                person_cleanup_db.mark_cleanup_job_item(
                    job_id, person_id, 'delete_ambiguous',
                    '该 job item 已有 POST 尝试，禁止自动重放', completed=True,
                )
                continue

            # Irreversible boundary: commit deleting + post_attempts=1 first.
            submission_recorded = person_cleanup_db.mark_cleanup_job_item(
                job_id,
                person_id,
                'deleting',
                submitted=True,
            )
            if not submission_recorded:
                logger.warning(
                    '人物 %s 的删除提交边界未能原子持久化，禁止发送 POST',
                    person_id,
                )
                person_cleanup_db.mark_cleanup_job_item(
                    job_id, person_id, 'delete_ambiguous',
                    '已存在全局删除尝试记录，禁止自动重放', completed=True,
                )
                continue
            outcome = emby.delete_person_custom_api_outcome(
                processor.emby_url,
                processor.emby_api_key,
                person_id,
            )
            if outcome == 'ambiguous':
                person_cleanup_db.mark_cleanup_job_item(
                    job_id, person_id, 'delete_ambiguous',
                    '删除 POST 结果不确定，禁止自动重放', completed=True,
                )
                continue
            if outcome != 'confirmed':
                person_cleanup_db.mark_cleanup_job_item(
                    job_id, person_id, 'delete_failed',
                    '删除 POST 明确失败', completed=True,
                )
                continue

            try:
                with get_db_connection() as conn:
                    with conn.cursor() as cursor:
                        cursor.execute(
                            'DELETE FROM person_identity_map WHERE emby_person_id = %s',
                            (person_id,),
                        )
                person_cleanup_db.remove_candidate(person_id)
                person_cleanup_db.mark_cleanup_job_item(
                    job_id, person_id, 'deleted', completed=True,
                )
            except Exception as exc:
                person_cleanup_db.mark_cleanup_job_item(
                    job_id, person_id, 'delete_ambiguous',
                    f'Emby 已确认提交，但本地清理失败: {exc}', completed=True,
                )

            task_manager.update_status_from_thread(
                int((index / max(1, total)) * 100),
                f'一键安全清理 {index}/{total}',
            )
            time.sleep(0.2)

        person_cleanup_db.finish_cleanup_job(job_id)
        task_manager.update_status_from_thread(100, '一键安全清理完成')
    except Exception as exc:
        logger.error('一键安全清理执行失败: %s', exc, exc_info=True)
        person_cleanup_db.fail_cleanup_job(job_id, str(exc))
        raise

# --- 同步演员映射表 ---
def task_sync_person_map(processor):
    """
    【V2 - 支持进度反馈】任务：同步演员映射表。
    """
    task_name = "同步演员映射"
    logger.trace(f"开始执行 '{task_name}'...")
    
    try:
        config = processor.config
        
        sync_handler = UnifiedSyncHandler(
            emby_url=config.get("emby_server_url"),
            emby_api_key=config.get("emby_api_key"),
            emby_user_id=config.get("emby_user_id"),
            tmdb_api_key=config.get("tmdb_api_key", "")
        )
        
        # ### 修改点：将任务管理器的回调函数传递给处理器 ###
        sync_handler.sync_emby_person_map_to_db(
            update_status_callback=task_manager.update_status_from_thread
        )
        
        logger.trace(f"'{task_name}' 成功完成。")

    except Exception as e:
        logger.error(f"'{task_name}' 执行过程中发生严重错误: {e}", exc_info=True)
        task_manager.update_status_from_thread(-1, f"错误：同步失败 ({str(e)[:50]}...)")

# ✨✨✨ 演员数据补充函数 ✨✨✨
def task_enrich_aliases(processor, force_full_update: bool = False):
    """
    【V4 - 支持深度模式】演员数据补充任务的入口点。
    - 标准模式 (force_full_update=False): 使用30天冷却期，只处理过期或不完整的演员。
    - 深度模式 (force_full_update=True): 无视冷却期 (设置为0)，全量处理所有需要补充数据的演员。
    """
    # 根据模式确定任务名和冷却时间
    if force_full_update:
        task_name = "演员数据补充 (全量)"
        cooldown_days = 0  # 深度模式：冷却时间为0，即无视冷却期
        logger.info(f"后台任务 '{task_name}' 开始执行，将全量处理所有演员...")
    else:
        task_name = "演员数据补充 (增量)"
        cooldown_days = 30 # 标准模式：使用固定的30天冷却期
        logger.info(f"后台任务 '{task_name}' 开始执行...")

    try:
        # 从传入的 processor 对象中获取配置字典
        config = processor.config
        
        # 获取必要的配置项
        tmdb_api_key = config.get(constants.CONFIG_OPTION_TMDB_API_KEY)

        if not tmdb_api_key:
            logger.error(f"  🚫 任务 '{task_name}' 中止：未在配置中找到 TMDb API Key。")
            task_manager.update_status_from_thread(-1, "错误：缺少TMDb API Key")
            return

        # 运行时长硬编码为0，代表“不限制时长”
        duration_minutes = 0
        
        logger.trace(f"演员数据补充任务将使用 {cooldown_days} 天作为同步冷却期。")

        # 调用核心函数，并传递计算好的冷却时间
        enrich_all_actor_aliases_task(
            tmdb_api_key=tmdb_api_key,
            run_duration_minutes=duration_minutes,
            sync_interval_days=cooldown_days, # <--- 核心修改点
            stop_event=processor.get_stop_event(),
            update_status_callback=task_manager.update_status_from_thread,
            force_full_update=force_full_update
        )
        
        logger.info(f"--- '{task_name}' 任务执行完毕。 ---")
        task_manager.update_status_from_thread(100, f"{task_name}完成。")

    except Exception as e:
        logger.error(f"'{task_name}' 执行过程中发生严重错误: {e}", exc_info=True)
        task_manager.update_status_from_thread(-1, f"错误：任务失败 ({str(e)[:50]}...)")

# --- 扫描单个演员订阅的所有作品 ---
def task_scan_actor_media(processor, subscription_id: int):
    """
    手动触发对单个演员订阅进行全量作品扫描的任务。
    """
     # --- 步骤 1: 获取演员名，用于日志和前端状态显示 ---
    actor_name_for_log = f"订阅ID {subscription_id}"
    try:
        with get_db_connection() as conn:
            with conn.cursor() as cursor:
                cursor.execute("SELECT actor_name FROM actor_subscriptions WHERE id = %s", (subscription_id,))
                result = cursor.fetchone()
                if result:
                    actor_name_for_log = result['actor_name']
    except Exception as e:
        logger.warning(f"获取订阅 {subscription_id} 的演员名失败: {e}")

    logger.info(f"--- 开始为演员 '{actor_name_for_log}' 执行手动刷新任务 ---")
    
    try:
        # --- 步骤 2: 从 processor 中获取 ActorSubscriptionProcessor 实例 ---
        # processor 参数实际上是 extensions.actor_subscription_processor_instance
        sub_processor = processor
        if not sub_processor:
            raise RuntimeError("ActorSubscriptionProcessor 实例未初始化。")

        # --- 步骤 3: 从本地数据库（数据中台）一次性加载所有需要的 Emby 媒体信息 ---
        task_manager.update_status_from_thread(10, "正在从本地数据库缓存媒体信息...")
        logger.info("  ➜ 正在从 media_metadata 表一次性获取全量在库媒体及剧集结构数据...")
        
        try:
            (emby_media_map, 
             emby_series_seasons_map, 
             emby_series_name_to_tmdb_id_map) = actor_db.get_all_in_library_media_for_actor_sync()
            logger.info(f"  ➜ 从数据库成功加载 {len(emby_media_map)} 个媒体映射，{len(emby_series_seasons_map)} 个剧集季结构。")
        except Exception as e:
            logger.error(f"  ➜ 手动刷新任务：从 media_metadata 获取媒体库信息时发生严重错误: {e}", exc_info=True)
            task_manager.update_status_from_thread(-1, "错误：读取本地数据库失败。")
            return

        # --- 步骤 4: 调用核心扫描函数，传入所有必需的参数 ---
        # 这和 run_scheduled_task 中的调用逻辑完全一致。
        task_manager.update_status_from_thread(30, f"正在扫描演员 '{actor_name_for_log}' 的作品...")
        
        sub_processor.run_full_scan_for_actor(
            subscription_id=subscription_id,
            emby_media_map=emby_media_map
        )
        
        task_manager.update_status_from_thread(100, "扫描完成。")
        logger.info(f"--- 演员 '{actor_name_for_log}' 的手动刷新任务执行完毕 ---")

    except Exception as e:
        logger.error(f"手动刷新任务 '{actor_name_for_log}' 在执行时失败: {e}", exc_info=True)
        task_manager.update_status_from_thread(-1, f"错误: {e}")

# --- 演员订阅 ---
def task_process_actor_subscriptions(processor):
    """【新】后台任务：执行所有启用的刷新演员订阅。"""
    processor.run_scheduled_task(update_status_callback=task_manager.update_status_from_thread)

# --- 翻译演员任务 ---
def task_actor_translation(processor):
    """
    【V4.1 - 详细日志版】
    - 增加详细日志：明确打印翻译跳过原因（结果为空/结果相同）以及 Emby API 更新失败的原因。
    """
    task_name = "中文化演员名 (智能版)"
    logger.trace(f"--- 开始执行 '{task_name}' 任务 ---")

    actor = processor.config.get(constants.CONFIG_OPTION_AI_TRANSLATE_ACTOR_ROLE)

    if not actor:
        logger.info("  🚫 AI翻译功能未启用，跳过任务。")
        return
    
    try:
        # ======================================================================
        # 阶段 1: 扫描并聚合所有需要翻译的演员 (智能数据采集)
        # ======================================================================
        task_manager.update_status_from_thread(0, "阶段 1/3: 正在扫描 Emby，收集所有待翻译演员...")
        
        name_to_persons_map = {}
        actors_to_enrich = []

        person_generator = emby.get_all_persons_from_emby(
            base_url=processor.emby_url,
            api_key=processor.emby_api_key,
            user_id=processor.emby_user_id,
            stop_event=processor.get_stop_event(),
            batch_size=500
        )

        total_scanned = 0
        for person_batch in person_generator:
            if processor.is_stop_requested():
                logger.info("任务在扫描阶段被用户中断。")
                task_manager.update_status_from_thread(100, "任务已中止。")
                return

            for person in person_batch:
                name = person.get("Name")
                if name and not utils.contains_chinese(name):
                    tmdb_id = person.get("ProviderIds", {}).get("Tmdb")
                    if tmdb_id:
                        actors_to_enrich.append({"name": name, "tmdb_id": tmdb_id})
                    
                    if name not in name_to_persons_map:
                        name_to_persons_map[name] = []
                    name_to_persons_map[name].append(person)
            
            total_scanned += len(person_batch)
            task_manager.update_status_from_thread(5, f"阶段 1/3: 已扫描 {total_scanned} 名演员...")

        if not name_to_persons_map:
            logger.info("  ➜ 扫描完成，没有发现需要翻译的演员名。")
            task_manager.update_status_from_thread(100, "任务完成，所有演员名都无需翻译。")
            return

        logger.info(f"  ➜ 扫描完成！共发现 {len(name_to_persons_map)} 个外文名需要翻译。")

        # ======================================================================
        # 阶段 2: 从本地数据库获取 Original Name
        # ======================================================================
        task_manager.update_status_from_thread(10, "阶段 2/3: 正在从本地缓存获取演员原始名...")
        
        original_to_emby_name_map = {}
        texts_to_translate = set()
        
        tmdb_ids_to_query = list(set([int(actor['tmdb_id']) for actor in actors_to_enrich if actor.get('tmdb_id')]))

        if tmdb_ids_to_query:
            tmdb_id_to_original_name = {}
            with get_db_connection() as conn:
                with conn.cursor() as cursor:
                    query = "SELECT tmdb_id, original_name FROM actor_metadata WHERE tmdb_id = ANY(%s)"
                    cursor.execute(query, (tmdb_ids_to_query,))
                    for row in cursor.fetchall():
                        tmdb_id_to_original_name[str(row['tmdb_id'])] = row['original_name']
            
            logger.trace(f"  ➜ 成功从本地数据库为 {len(tmdb_id_to_original_name)} 个TMDb ID找到了original_name。")

            for actor in actors_to_enrich:
                emby_name = actor['name']
                tmdb_id = actor['tmdb_id']
                original_name = tmdb_id_to_original_name.get(str(tmdb_id))
                
                text_for_translation = original_name if original_name and not utils.contains_chinese(original_name) else emby_name
                
                texts_to_translate.add(text_for_translation)
                original_to_emby_name_map[text_for_translation] = emby_name

        emby_names_with_tmdb_id = {actor['name'] for actor in actors_to_enrich}
        for emby_name in name_to_persons_map.keys():
            if emby_name not in emby_names_with_tmdb_id:
                texts_to_translate.add(emby_name)
                original_to_emby_name_map[emby_name] = emby_name

        # ======================================================================
        # 阶段 3: 分批翻译并并发写回
        # ======================================================================
        all_names_list = list(texts_to_translate)
        TRANSLATION_BATCH_SIZE = 20
        total_names_to_process = len(all_names_list)
        total_batches = (total_names_to_process + TRANSLATION_BATCH_SIZE - 1) // TRANSLATION_BATCH_SIZE
        
        total_updated_count = 0

        for i in range(0, total_names_to_process, TRANSLATION_BATCH_SIZE):
            if processor.is_stop_requested():
                logger.info("任务在翻译阶段被用户中断。")
                break

            current_batch_names = all_names_list[i:i + TRANSLATION_BATCH_SIZE]
            batch_num = (i // TRANSLATION_BATCH_SIZE) + 1
            
            progress = int(20 + (i / total_names_to_process) * 80)
            task_manager.update_status_from_thread(
                progress, 
                f"阶段 3/3: 正在翻译批次 {batch_num}/{total_batches} (已成功 {total_updated_count} 个)"
            )
            
            try:
                translation_map = processor.ai_translator.batch_translate(
                    texts=current_batch_names, mode="transliterate"
                )
            except Exception as e_trans:
                logger.error(f"翻译批次 {batch_num} 时发生错误: {e_trans}，将跳过此批次。")
                continue

            if not translation_map:
                logger.warning(f"翻译批次 {batch_num} 未能返回任何结果。")
                continue

            batch_updated_count = 0
            
            # 1. 准备好所有需要更新的任务
            update_tasks = []
            for original_name, translated_name in translation_map.items():
                # --- [新增日志] 详细记录跳过原因 ---
                if not translated_name:
                    logger.warning(f"    - ⚠️ [跳过] 原名: '{original_name}' -> 翻译结果为空")
                    continue
                
                if original_name == translated_name:
                    # 如果翻译结果和原文一样，说明AI认为不需要翻译，或者翻译失败
                    logger.info(f"    - ℹ️ [跳过] 原名: '{original_name}' -> 结果与原文相同 (未变)")
                    continue
                # -----------------------------------

                emby_name = original_to_emby_name_map.get(original_name, original_name)
                persons_to_update = name_to_persons_map.get(emby_name, [])
                for person in persons_to_update:
                    update_tasks.append((person.get("Id"), translated_name))

            if not update_tasks:
                logger.info(f"  ➜ 批次 {batch_num}: 翻译结果经比对后无有效变更，跳过写入。")
                continue

            logger.info(f"  ➜ 批次 {batch_num}/{total_batches}: 准备并发写入 {len(update_tasks)} 个更新...")
            
            # 2. 使用 ThreadPoolExecutor 执行并发更新
            with ThreadPoolExecutor(max_workers=10) as executor:
                future_to_task = {
                    executor.submit(
                        emby.update_person_details,
                        person_id=task[0],
                        new_data={"Name": task[1]},
                        emby_server_url=processor.emby_url,
                        emby_api_key=processor.emby_api_key,
                        user_id=processor.emby_user_id
                    ): task for task in update_tasks
                }

                for future in as_completed(future_to_task):
                    if processor.is_stop_requested():
                        break
                    
                    task_info = future_to_task[future] # (person_id, new_name)
                    try:
                        success = future.result()
                        if success:
                            batch_updated_count += 1
                        else:
                            # --- [新增日志] 记录API调用失败 ---
                            logger.warning(f"    - ❌ [更新失败] Emby API 拒绝更新演员 ID: {task_info[0]} -> '{task_info[1]}'")
                    except Exception as exc:
                        logger.error(f"    - ❌ [异常] 更新演员 (ID: {task_info[0]}) 时发生错误: {exc}")

            total_updated_count += batch_updated_count
            
            if batch_updated_count > 0:
                logger.info(f"  ➜ 批次 {batch_num}/{total_batches} 完成，成功更新 {batch_updated_count} 个演员名。")
        
        # ======================================================================
        # 阶段 3: 任务结束
        # ======================================================================
        final_message = f"  ✅ 任务完成！共成功翻译并更新了 {total_updated_count} 个演员名。"
        if processor.is_stop_requested():
            final_message = f"任务已中断。本次运行成功翻译并更新了 {total_updated_count} 个演员名。"
        
        logger.info(final_message)
        task_manager.update_status_from_thread(100, final_message)

    except Exception as e:
        logger.error(f"执行演员翻译任务时发生严重错误: {e}", exc_info=True)
        task_manager.update_status_from_thread(-1, f"任务失败: {e}")

def task_merge_duplicate_actors(processor):
    raise RuntimeError(
        "分身演员合并任务已禁用：其删除链尚未接入人物安全闭环"
    )


def _disabled_legacy_task_merge_duplicate_actors(processor):
    raise RuntimeError(
        "旧版分身演员合并实现已移除：该删除链已永久禁用"
    )

def _disabled_legacy_task_purge_ghost_actors(processor):
    raise RuntimeError("旧版直接删除幽灵人物任务已永久停用，请使用人物清理页面")
def _disabled_legacy_task_purge_unregistered_actors(processor):
    raise RuntimeError("删除黑户人物任务已永久停用")
