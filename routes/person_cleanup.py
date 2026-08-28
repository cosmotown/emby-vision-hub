import logging

from flask import Blueprint, jsonify, request

import config_manager
import constants
import extensions
import handler.emby as emby
import task_manager
from database import person_cleanup_db
from extensions import admin_required, processor_ready_required, task_lock_required
from services.person_cleanup_safety import (
    build_protected_library_root_contract,
    build_identity_provider_pairs,
    classify_reference_check,
    reference_check_failure_message,
)
from tasks.actors import (
    task_delete_selected_ghost_actors,
    task_execute_safe_person_cleanup,
    task_preview_safe_person_cleanup,
    task_scan_ghost_actor_candidates,
)


logger = logging.getLogger(__name__)
person_cleanup_bp = Blueprint('person_cleanup_bp', __name__, url_prefix='/api/person-cleanup')


def _serialize_reference_items(items):
    return [
        {
            'id': str(item.get('Id') or ''),
            'name': item.get('Name') or '未命名作品',
            'type': item.get('Type') or '',
            'series_name': item.get('SeriesName') or '',
            'production_year': item.get('ProductionYear'),
        }
        for item in items or []
        if isinstance(item, dict)
    ]


def _refreshed_candidate(person_id, fallback):
    refreshed = person_cleanup_db.get_candidates_by_ids([person_id])
    return refreshed[0] if refreshed else fallback


def _build_protected_root_contract():
    selected = person_cleanup_db.list_protected_libraries()
    if not selected:
        return build_protected_library_root_contract([], [])
    libraries = emby.get_all_libraries_with_paths(
        extensions.media_processor_instance.emby_url,
        extensions.media_processor_instance.emby_api_key,
    )
    return build_protected_library_root_contract(libraries, selected)


@person_cleanup_bp.route('/candidates', methods=['GET'])
@admin_required
def get_person_cleanup_candidates():
    try:
        generation = person_cleanup_db.require_ready_protection_snapshot()
        candidates = person_cleanup_db.list_candidates()
        return jsonify({
            'candidates': candidates,
            'total': len(candidates),
            'snapshot_generation': generation,
            'snapshot_state': 'ready',
        })
    except RuntimeError as exc:
        return jsonify({
            'error': str(exc),
            'snapshot_state': person_cleanup_db.get_protection_state(),
        }), 409
    except Exception as exc:
        logger.error(f"读取人物清理候选失败: {exc}", exc_info=True)
        return jsonify({'error': '无法读取人物清理候选'}), 500


@person_cleanup_bp.route('/scan', methods=['POST'])
@admin_required
@task_lock_required
@processor_ready_required
def scan_person_cleanup_candidates():
    submitted = task_manager.submit_task(
        task_scan_ghost_actor_candidates,
        '扫描幽灵人物',
        processor_type='media',
    )
    if not submitted:
        return jsonify({'error': '扫描任务提交失败，可能已有后台任务运行'}), 409
    return jsonify({'message': '只读扫描任务已提交，不会删除任何人物'}), 202


@person_cleanup_bp.route('/protected-libraries', methods=['GET'])
@admin_required
@processor_ready_required
def get_person_cleanup_protected_libraries():
    libraries = emby.get_all_libraries_with_paths(
        extensions.media_processor_instance.emby_url,
        extensions.media_processor_instance.emby_api_key,
    )
    if not libraries:
        return jsonify({'error': '无法读取 Emby 媒体库，保护设置保持不变'}), 502
    protected = {
        str(item['library_id']): item
        for item in person_cleanup_db.list_protected_libraries()
    }
    result = []
    available_ids = set()
    for library in libraries or []:
        info = library.get('info') or {}
        library_id = str(info.get('Id') or '').strip()
        if not library_id:
            continue
        available_ids.add(library_id)
        protected_info = protected.get(library_id) or {}
        result.append({
            'library_id': library_id,
            'library_name': info.get('Name') or library_id,
            'collection_type': info.get('CollectionType') or '',
            'selected': library_id in protected,
            'protected_person_count': int(protected_info.get('protected_person_count') or 0),
            'protected_name_count': int(protected_info.get('protected_name_count') or 0),
            'protected_identity_count': int(protected_info.get('protected_identity_count') or 0),
            'missing': False,
        })
    for library_id, protected_info in protected.items():
        if library_id in available_ids:
            continue
        result.append({
            'library_id': library_id,
            'library_name': protected_info.get('library_name') or library_id,
            'collection_type': '',
            'selected': True,
            'protected_person_count': int(protected_info.get('protected_person_count') or 0),
            'protected_name_count': int(protected_info.get('protected_name_count') or 0),
            'protected_identity_count': int(protected_info.get('protected_identity_count') or 0),
            'missing': True,
        })
    return jsonify({
        'libraries': result,
        'snapshot': person_cleanup_db.get_protection_state(),
    })


@person_cleanup_bp.route('/protected-libraries', methods=['POST'])
@admin_required
@task_lock_required
@processor_ready_required
def save_person_cleanup_protected_libraries():
    payload = request.get_json(silent=True) or {}
    selected_ids = payload.get('library_ids')
    if not isinstance(selected_ids, list):
        return jsonify({'error': 'library_ids 必须为数组'}), 400
    normalized_ids = {str(library_id).strip() for library_id in selected_ids if str(library_id).strip()}
    if len(normalized_ids) > 100:
        return jsonify({'error': '受保护媒体库数量不能超过 100'}), 400

    libraries = emby.get_all_libraries_with_paths(
        extensions.media_processor_instance.emby_url,
        extensions.media_processor_instance.emby_api_key,
    )
    if not libraries:
        return jsonify({'error': '无法读取 Emby 媒体库，保护设置未修改'}), 502
    available = {}
    for library in libraries or []:
        info = library.get('info') or {}
        library_id = str(info.get('Id') or '').strip()
        if library_id:
            available[library_id] = info.get('Name') or library_id

    existing_protected = {
        str(item['library_id']): item.get('library_name') or str(item['library_id'])
        for item in person_cleanup_db.list_protected_libraries()
    }
    allowed = {**existing_protected, **available}
    unknown_ids = sorted(normalized_ids - set(allowed))
    if unknown_ids:
        return jsonify({'error': '选择中包含已不存在的媒体库，请刷新后重试'}), 409

    saved_count = person_cleanup_db.replace_protected_libraries([
        {'library_id': library_id, 'library_name': allowed[library_id]}
        for library_id in sorted(normalized_ids)
    ])
    return jsonify({
        'message': f'已保存 {saved_count} 个受保护媒体库；保护快照重新就绪前人物清理保持禁用',
        'count': saved_count,
        'snapshot': person_cleanup_db.get_protection_state(),
    })


@person_cleanup_bp.route('/candidates/<person_id>/verify', methods=['POST'])
@admin_required
@processor_ready_required
def verify_person_cleanup_candidate(person_id):
    try:
        snapshot_generation = person_cleanup_db.require_ready_protection_snapshot()
        contract = person_cleanup_db.get_protection_contract()
    except RuntimeError as exc:
        return jsonify({'error': str(exc)}), 409
    normalized_id = str(person_id or '').strip()
    candidates = person_cleanup_db.get_candidates_by_ids([normalized_id])
    if not candidates:
        return jsonify({'error': '该人物已不在候选列表中，请刷新页面'}), 404

    candidate = candidates[0]
    protection_reason = person_cleanup_db.candidate_protection_reason(candidate, contract)
    if protection_reason:
        person_cleanup_db.remove_candidate(normalized_id)
        return jsonify({
            'status': protection_reason,
            'message': '该人物命中受保护媒体库合同，已撤销候选',
            'candidate_removed': True,
            'verification_complete': False,
        }), 409
    protected_root_contract = _build_protected_root_contract()
    references = emby.get_person_media_references(
        extensions.media_processor_instance.emby_url,
        extensions.media_processor_instance.emby_api_key,
        normalized_id,
        limit=50,
        person_name=candidate.get('person_name'),
        protected_root_contract=protected_root_contract,
        user_id=getattr(extensions.media_processor_instance, 'emby_user_id', None),
    )
    reference_status = classify_reference_check(references)
    safe_references = references if isinstance(references, dict) else {}
    reference_count = safe_references.get('count')
    if not isinstance(reference_count, int) or isinstance(reference_count, bool):
        reference_count = 0
    query_reference_count = safe_references.get('query_count')
    if not isinstance(query_reference_count, int) or isinstance(query_reference_count, bool):
        query_reference_count = reference_count

    response = {
        'person_id': normalized_id,
        'person_name': candidate.get('person_name') or '未知人物',
        'provider_ids': candidate.get('provider_ids_json') or {},
        'status': reference_status,
        'reference_count': reference_count,
        'query_reference_count': query_reference_count,
        'identity_alias_only': reference_status == 'identity_alias_only',
        'protected_library_id': safe_references.get('protected_library_id'),
        'protected_library_name': safe_references.get('protected_library_name'),
        'evidence_item_id': safe_references.get('evidence_item_id'),
        'items': _serialize_reference_items(safe_references.get('items')),
        'unverified_items': _serialize_reference_items(safe_references.get('unverified_items')),
        'emby_url': (
            config_manager.APP_CONFIG.get(constants.CONFIG_OPTION_EMBY_PUBLIC_URL)
            or config_manager.APP_CONFIG.get(constants.CONFIG_OPTION_EMBY_SERVER_URL)
            or ''
        ).rstrip('/'),
        'emby_server_id': extensions.EMBY_SERVER_ID or '',
    }

    if reference_status in {
        'protected_library_alias',
        'protected_library_unverifiable',
    }:
        person_cleanup_db.persist_protected_alias_and_remove_candidate(
            candidate,
            safe_references.get('protected_library_id'),
            reference_status,
            safe_references.get('evidence_item_id'),
        )
        response.update({
            'candidate_removed': True,
            'verification_complete': True,
            'message': (
                '该人物仅以其他 Person 身份关联受保护媒体库作品，'
                '已按保护库人物处理并移出待复核。'
                if reference_status == 'protected_library_alias'
                else
                '该人物关联受保护媒体库作品，但 People 明细无法完整核验；'
                '已按保护处理并移出待复核。'
            ),
        })
        return jsonify(response)

    if reference_status in {'connection_failed', 'invalid_response', 'people_unavailable'}:
        error = reference_check_failure_message(reference_status)
        person_cleanup_db.mark_candidate_checked(
            normalized_id,
            reference_status,
            snapshot_generation,
            error,
        )
        response.update({
            'error': error,
            'message': error,
            'candidate': _refreshed_candidate(normalized_id, candidate),
            'candidate_removed': False,
            'verification_complete': False,
        })
        return jsonify(response), 409 if reference_status == 'people_unavailable' else 502

    response['verification_complete'] = True

    if reference_status == 'linked':
        person_cleanup_db.remove_candidate(normalized_id)
        response['candidate_removed'] = True
        response['message'] = '发现当前关联作品，已从清理候选中撤销'
        return jsonify(response)

    if reference_status == 'identity_alias_only':
        error = '仅命中同身份其他 Person 的作品；不是显式 orphan，禁止删除'
        person_cleanup_db.mark_candidate_checked(
            normalized_id,
            'identity_alias_only',
            snapshot_generation,
            error,
        )
        response.update({
            'error': error,
            'message': error,
            'candidate': _refreshed_candidate(normalized_id, candidate),
            'candidate_removed': False,
            'verification_complete': True,
        })
        return jsonify(response), 409

    provider_pairs = build_identity_provider_pairs(response['provider_ids'])
    identity_matches = []
    if provider_pairs:
        matching_people = emby.get_people_by_provider_ids(
            extensions.media_processor_instance.emby_url,
            extensions.media_processor_instance.emby_api_key,
            provider_pairs,
        )
        if matching_people is None:
            error = reference_check_failure_message(
                'invalid_response',
                context='TMDb/IMDb/豆瓣同身份人物对照',
            )
            person_cleanup_db.mark_candidate_checked(
                normalized_id,
                'invalid_response',
                snapshot_generation,
                error,
            )
            response.update({
                'status': 'invalid_response',
                'candidate_reference_status': reference_status,
                'error': error,
                'message': error,
                'candidate': _refreshed_candidate(normalized_id, candidate),
                'verification_complete': False,
            })
            return jsonify(response), 502

        for matching_person in matching_people:
            matching_id = str(matching_person.get('Id') or '').strip()
            if not matching_id or matching_id == normalized_id:
                continue
            matching_references = emby.get_person_media_references(
                extensions.media_processor_instance.emby_url,
                extensions.media_processor_instance.emby_api_key,
                matching_id,
                limit=50,
                person_name=matching_person.get('Name'),
                user_id=getattr(extensions.media_processor_instance, 'emby_user_id', None),
            )
            matching_status = classify_reference_check(matching_references)
            if matching_status in {'connection_failed', 'invalid_response', 'people_unavailable'}:
                context = f'同身份人物 {matching_person.get("Name") or matching_id} 的关联作品核对'
                error = reference_check_failure_message(matching_status, context=context)
                person_cleanup_db.mark_candidate_checked(
                    normalized_id,
                    matching_status,
                    snapshot_generation,
                    error,
                )
                response.update({
                    'status': matching_status,
                    'candidate_reference_status': reference_status,
                    'error': error,
                    'message': error,
                    'candidate': _refreshed_candidate(normalized_id, candidate),
                    'verification_complete': False,
                })
                return jsonify(response), 409 if matching_status == 'people_unavailable' else 502
            identity_matches.append({
                'person_id': matching_id,
                'person_name': matching_person.get('Name') or '未知人物',
                'provider_ids': matching_person.get('ProviderIds') or {},
                'status': matching_status,
                'reference_count': matching_references['count'],
                'items': _serialize_reference_items(matching_references.get('items')),
            })

    person_cleanup_db.mark_candidate_checked(
        normalized_id,
        'orphan',
        snapshot_generation,
    )
    refreshed = person_cleanup_db.get_candidates_by_ids([normalized_id])
    response['candidate'] = refreshed[0] if refreshed else candidate
    response['candidate_removed'] = False
    response['identity_matches'] = identity_matches
    response['identity_comparison'] = 'matched' if identity_matches else ('no_match' if provider_pairs else 'unavailable')
    if reference_status == 'identity_alias_only':
        base_message = (
            f'PersonIds 查询返回 {query_reference_count} 部作品，但完整 People 明细均未引用当前 Person ID；'
            '当前精确关联为 0'
        )
    else:
        base_message = '候选本身精确关联为 0'
    if identity_matches:
        response['message'] = f'{base_message}；找到 {len(identity_matches)} 位同身份人物，请结合其作品人工判断'
    elif provider_pairs:
        response['message'] = f'{base_message}；未在 Emby 中找到其他同 TMDb/IMDb/豆瓣人物'
    else:
        response['message'] = f'{base_message}；缺少 TMDb/IMDb/豆瓣，无法进行同身份对照'
    return jsonify(response)


@person_cleanup_bp.route('/delete', methods=['POST'])
@admin_required
@task_lock_required
@processor_ready_required
def delete_person_cleanup_candidates():
    try:
        person_cleanup_db.require_ready_protection_snapshot()
    except RuntimeError as exc:
        return jsonify({'error': str(exc)}), 409
    payload = request.get_json(silent=True) or {}
    person_ids = payload.get('person_ids')
    if not isinstance(person_ids, list) or not person_ids:
        return jsonify({'error': '请选择要删除的人物'}), 400

    normalized_ids = sorted({str(person_id).strip() for person_id in person_ids if str(person_id).strip()})
    if not normalized_ids:
        return jsonify({'error': '未提供有效人物 ID'}), 400
    if len(normalized_ids) > 500:
        return jsonify({'error': '单次最多删除 500 位人物'}), 400

    candidates = person_cleanup_db.list_explicit_verified_orphans(normalized_ids)
    if len(candidates) != len(normalized_ids):
        return jsonify({
            'error': '只有当前保护快照下显式核验为 orphan 的人物可以删除'
        }), 409

    submitted = task_manager.submit_task(
        task_delete_selected_ghost_actors,
        f'删除 {len(normalized_ids)} 位幽灵人物',
        processor_type='media',
        person_ids=normalized_ids,
    )
    if not submitted:
        return jsonify({'error': '删除任务提交失败，可能已有后台任务运行'}), 409
    return jsonify({
        'message': '删除任务已提交，每位人物都会在删除前重新核验媒体关联',
        'count': len(normalized_ids),
    }), 202


@person_cleanup_bp.route('/cleanup-jobs/preview', methods=['POST'])
@admin_required
@task_lock_required
@processor_ready_required
def create_safe_cleanup_preview():
    try:
        person_cleanup_db.require_ready_protection_snapshot()
    except RuntimeError as exc:
        return jsonify({'error': str(exc)}), 409
    try:
        job_id = person_cleanup_db.create_cleanup_job()
    except Exception as exc:
        return jsonify({'error': f'无法创建安全清理预览: {exc}'}), 409
    submitted = task_manager.submit_task(
        task_preview_safe_person_cleanup,
        '人物一键安全清理预览',
        processor_type='media',
        job_id=job_id,
    )
    if not submitted:
        person_cleanup_db.fail_cleanup_job(job_id, '后台任务繁忙，预览未启动')
        return jsonify({'error': '预览任务提交失败，可能已有后台任务运行'}), 409
    return jsonify({
        'job_id': job_id,
        'state': 'previewing',
        'message': '安全清理预览已提交；此阶段不会删除人物',
    }), 202


@person_cleanup_bp.route('/cleanup-jobs/<job_id>', methods=['GET'])
@admin_required
def get_safe_cleanup_job(job_id):
    include_items = request.args.get('include_items', 'false').lower() == 'true'
    job = person_cleanup_db.get_cleanup_job(job_id, include_items=include_items)
    if not job:
        return jsonify({'error': '未找到清理任务'}), 404
    return jsonify({'job': job})


@person_cleanup_bp.route('/cleanup-jobs/<job_id>/confirmation-token', methods=['POST'])
@admin_required
@processor_ready_required
def issue_safe_cleanup_confirmation_token(job_id):
    try:
        token = person_cleanup_db.issue_cleanup_confirmation_token(job_id)
    except RuntimeError as exc:
        return jsonify({'error': str(exc)}), 409
    return jsonify({
        'job_id': job_id,
        'confirmation_token': token,
        'message': '确认令牌仅适用于当前预览',
    })


@person_cleanup_bp.route('/cleanup-jobs/<job_id>/confirm', methods=['POST'])
@admin_required
@task_lock_required
@processor_ready_required
def confirm_safe_cleanup_job(job_id):
    payload = request.get_json(silent=True) or {}
    if payload.get('confirmation') != '确认删除已核验孤儿人物':
        return jsonify({'error': '缺少明确的安全清理确认文本'}), 400
    token = str(payload.get('confirmation_token') or '')
    if not token:
        return jsonify({'error': '缺少当前预览确认令牌'}), 400
    try:
        person_cleanup_db.confirm_cleanup_job(job_id, token)
    except RuntimeError as exc:
        return jsonify({'error': str(exc)}), 409
    submitted = task_manager.submit_task(
        task_execute_safe_person_cleanup,
        '人物一键安全清理',
        processor_type='media',
        job_id=job_id,
    )
    if not submitted:
        person_cleanup_db.revert_confirmed_cleanup_job(
            job_id,
            '后台任务繁忙，尚未开始删除',
        )
        return jsonify({'error': '清理任务提交失败，未执行删除'}), 409
    return jsonify({'job_id': job_id, 'state': 'confirmed'}), 202


@person_cleanup_bp.route('/cleanup-jobs/<job_id>/stop', methods=['POST'])
@admin_required
@processor_ready_required
def stop_safe_cleanup_job(job_id):
    if not person_cleanup_db.request_cleanup_job_stop(job_id):
        return jsonify({'error': '任务不存在或当前状态不可中止'}), 409
    extensions.media_processor_instance.signal_stop()
    return jsonify({'job_id': job_id, 'state': 'stop_requested'}), 202
