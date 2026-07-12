import logging

from flask import Blueprint, jsonify, request

import task_manager
from database import person_cleanup_db
from extensions import admin_required, processor_ready_required, task_lock_required
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
