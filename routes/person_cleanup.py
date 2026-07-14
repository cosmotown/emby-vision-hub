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
    build_identity_provider_pairs,
    classify_reference_check,
    is_verified_orphan_candidate,
)
from tasks.actors import task_delete_selected_ghost_actors, task_scan_ghost_actor_candidates


logger = logging.getLogger(__name__)
person_cleanup_bp = Blueprint('person_cleanup_bp', __name__, url_prefix='/api/person-cleanup')


@person_cleanup_bp.route('/candidates', methods=['GET'])
@admin_required
def get_person_cleanup_candidates():
    try:
        candidates = person_cleanup_db.list_candidates()
        return jsonify({'candidates': candidates, 'total': len(candidates)})
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
            'missing': True,
        })
    return jsonify({'libraries': result})


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
        'message': f'已保存 {saved_count} 个受保护媒体库；请执行一次只读扫描以更新人物快照',
        'count': saved_count,
    })


@person_cleanup_bp.route('/candidates/<person_id>/verify', methods=['POST'])
@admin_required
@processor_ready_required
def verify_person_cleanup_candidate(person_id):
    normalized_id = str(person_id or '').strip()
    candidates = person_cleanup_db.get_candidates_by_ids([normalized_id])
    if not candidates:
        return jsonify({'error': '该人物已不在候选列表中，请刷新页面'}), 404

    candidate = candidates[0]
    references = emby.get_person_media_references(
        extensions.media_processor_instance.emby_url,
        extensions.media_processor_instance.emby_api_key,
        normalized_id,
        limit=50,
    )
    reference_status = classify_reference_check(references)
    if reference_status == 'verification_failed':
        error = '无法连接 Emby 完成人物关联核对'
        person_cleanup_db.mark_candidate_checked(normalized_id, error)
        return jsonify({'error': error}), 502

    items = []
    for item in references.get('items') or []:
        items.append({
            'id': str(item.get('Id') or ''),
            'name': item.get('Name') or '未命名作品',
            'type': item.get('Type') or '',
            'series_name': item.get('SeriesName') or '',
            'production_year': item.get('ProductionYear'),
        })

    response = {
        'person_id': normalized_id,
        'person_name': candidate.get('person_name') or '未知人物',
        'provider_ids': candidate.get('provider_ids_json') or {},
        'status': reference_status,
        'reference_count': references['count'],
        'items': items,
        'emby_url': (
            config_manager.APP_CONFIG.get(constants.CONFIG_OPTION_EMBY_PUBLIC_URL)
            or config_manager.APP_CONFIG.get(constants.CONFIG_OPTION_EMBY_SERVER_URL)
            or ''
        ).rstrip('/'),
        'emby_server_id': extensions.EMBY_SERVER_ID or '',
    }

    if reference_status == 'linked':
        person_cleanup_db.remove_candidate(normalized_id)
        response['candidate_removed'] = True
        response['message'] = '发现当前关联作品，已从清理候选中撤销'
        return jsonify(response)

    provider_pairs = build_identity_provider_pairs(response['provider_ids'])
    identity_matches = []
    if provider_pairs:
        matching_people = emby.get_people_by_provider_ids(
            extensions.media_processor_instance.emby_url,
            extensions.media_processor_instance.emby_api_key,
            provider_pairs,
        )
        if matching_people is None:
            error = '候选本身无关联，但无法完成 TMDb/IMDb 同身份人物对照'
            person_cleanup_db.mark_candidate_checked(normalized_id, error)
            return jsonify({'error': error}), 502

        for matching_person in matching_people:
            matching_id = str(matching_person.get('Id') or '').strip()
            if not matching_id or matching_id == normalized_id:
                continue
            matching_references = emby.get_person_media_references(
                extensions.media_processor_instance.emby_url,
                extensions.media_processor_instance.emby_api_key,
                matching_id,
                limit=50,
            )
            if classify_reference_check(matching_references) == 'verification_failed':
                error = f'无法核对同身份人物 {matching_person.get("Name") or matching_id} 的关联作品'
                person_cleanup_db.mark_candidate_checked(normalized_id, error)
                return jsonify({'error': error}), 502
            identity_matches.append({
                'person_id': matching_id,
                'person_name': matching_person.get('Name') or '未知人物',
                'provider_ids': matching_person.get('ProviderIds') or {},
                'reference_count': matching_references['count'],
                'items': [
                    {
                        'id': str(item.get('Id') or ''),
                        'name': item.get('Name') or '未命名作品',
                        'type': item.get('Type') or '',
                        'series_name': item.get('SeriesName') or '',
                        'production_year': item.get('ProductionYear'),
                    }
                    for item in matching_references.get('items') or []
                ],
            })

    person_cleanup_db.mark_candidate_checked(normalized_id)
    refreshed = person_cleanup_db.get_candidates_by_ids([normalized_id])
    response['candidate'] = refreshed[0] if refreshed else candidate
    response['candidate_removed'] = False
    response['identity_matches'] = identity_matches
    response['identity_comparison'] = 'matched' if identity_matches else ('no_match' if provider_pairs else 'unavailable')
    if identity_matches:
        response['message'] = f'候选本身关联为 0；找到 {len(identity_matches)} 位同身份人物，请结合其作品人工判断'
    elif provider_pairs:
        response['message'] = '候选本身关联为 0；未在 Emby 中找到其他同 TMDb/IMDb 人物'
    else:
        response['message'] = '候选本身关联为 0；缺少 TMDb/IMDb，无法进行同身份对照'
    return jsonify(response)


@person_cleanup_bp.route('/delete', methods=['POST'])
@admin_required
@task_lock_required
@processor_ready_required
def delete_person_cleanup_candidates():
    payload = request.get_json(silent=True) or {}
    person_ids = payload.get('person_ids')
    if not isinstance(person_ids, list) or not person_ids:
        return jsonify({'error': '请选择要删除的人物'}), 400

    normalized_ids = sorted({str(person_id).strip() for person_id in person_ids if str(person_id).strip()})
    if not normalized_ids:
        return jsonify({'error': '未提供有效人物 ID'}), 400
    if len(normalized_ids) > 500:
        return jsonify({'error': '单次最多删除 500 位人物'}), 400

    candidates = person_cleanup_db.get_candidates_by_ids(normalized_ids)
    if len(candidates) != len(normalized_ids):
        return jsonify({'error': '选择中包含已失效或不在候选列表的人物，请刷新后重试'}), 409
    unverified = [
        item.get('person_name') or item.get('person_id')
        for item in candidates
        if not is_verified_orphan_candidate(item)
    ]
    if unverified:
        preview = '、'.join(unverified[:5])
        suffix = ' 等' if len(unverified) > 5 else ''
        return jsonify({
            'error': f'请先逐一实时核对并确认关联作品为 0：{preview}{suffix}',
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
