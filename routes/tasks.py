# routes/tasks.py

import logging
import uuid
from flask import Blueprint, request, jsonify

# 导入您项目中用于管理和执行任务的核心模块
import task_manager 
from extensions import admin_required, processor_ready_required, task_lock_required
from database import strm_ingest_db, webhook_event_db
# ★★★ 导入任务注册表，这是“翻译”的关键 ★★★
from tasks.core import get_task_registry

logger = logging.getLogger(__name__)

# 创建一个新的蓝图
tasks_bp = Blueprint('tasks', __name__, url_prefix='/api/tasks')

# --- 获取所有可供选择的任务 ---
@tasks_bp.route('/available', methods=['GET'])
@admin_required
def get_available_tasks():
    """
    返回一个可用于任务链配置的、有序的、人类可读的任务列表。
    它现在只返回那些被标记为适合在任务链中运行的任务。
    """
    try:
        # 调用 get_task_registry 时，明确告诉它我们需要用于“任务链”的上下文
        registry = get_task_registry(context='chain')
        
        available_tasks = [
            {"key": key, "name": info[1]} 
            for key, info in registry.items()
        ]
        return jsonify(available_tasks), 200
    except Exception as e:
        logger.error(f"获取可用任务列表时出错: {e}", exc_info=True)
        return jsonify({"error": "无法获取可用任务列表"}), 500

@tasks_bp.route('/run', methods=['POST'])
@admin_required
@task_lock_required
@processor_ready_required
def run_task():
    """
    【V2 - 精确调度版】
    一个通用的、用于从前端触发后台任务的API端点。
    它会从任务注册表中查找任务所需处理器的类型，并精确地提交给任务管理器。
    """
    data = request.get_json()
    if not data or 'task_name' not in data:
        return jsonify({"error": "请求体中缺少 'task_name' 参数"}), 400

    task_key = data.pop('task_name')
    logger.trace(f"收到来自前端的通用任务执行请求: {task_key}, 参数: {data}")

    try:
        task_registry = get_task_registry()
        task_info = task_registry.get(task_key)
        if not task_info:
            return jsonify({"error": f"未知的任务名称: {task_key}"}), 404

        task_function_obj, task_description, processor_type = task_info
        
        success = task_manager.submit_task(
            task_function=task_function_obj, 
            task_name=task_description,
            processor_type=processor_type, 
            **data
        )
        
        if success:
            return jsonify({"message": f"任务 '{task_description}' 已成功提交。"}), 202
        else:
            return jsonify({"error": "任务提交失败，未知错误。"}), 500

    except Exception as e:
        logger.error(f"提交任务 '{task_key}' 时出错: {e}", exc_info=True)
        return jsonify({"error": f"服务器内部错误: {e}"}), 500


@tasks_bp.route('/webhook-events', methods=['GET'])
@admin_required
def get_webhook_events():
    try:
        limit = request.args.get('limit', 50, type=int)
        events = webhook_event_db.list_recent_events(limit=limit)
        summary = webhook_event_db.get_queue_summary()
        return jsonify({"events": events, "summary": summary}), 200
    except Exception as exc:
        logger.error(f"获取 Webhook 事件队列失败: {exc}", exc_info=True)
        return jsonify({"error": "无法获取 Webhook 事件队列"}), 500


@tasks_bp.route('/webhook-events/diagnostic', methods=['POST'])
@admin_required
def enqueue_webhook_diagnostic():
    try:
        diagnostic_id = uuid.uuid4().hex
        event_id, _ = webhook_event_db.enqueue_event(
            dedupe_key=f"diagnostic:{diagnostic_id}",
            event_source='diagnostic',
            task_kind='diagnostic',
            task_name='Webhook 队列只读自检',
            item_id=diagnostic_id,
            item_name='队列诊断',
            item_type='Diagnostic',
            payload={'read_only': True},
            max_attempts=1,
        )

        from routes.webhook import _schedule_persistent_webhook_drain
        _schedule_persistent_webhook_drain(delay=0)
        return jsonify({
            "message": "只读诊断事件已进入队列",
            "event_id": event_id,
        }), 202
    except Exception as exc:
        logger.error(f"创建 Webhook 队列诊断事件失败: {exc}", exc_info=True)
        return jsonify({"error": "无法创建队列诊断事件"}), 500


@tasks_bp.route('/webhook-events/<int:event_id>/retry', methods=['POST'])
@admin_required
def retry_webhook_event(event_id):
    try:
        if not webhook_event_db.retry_event(event_id):
            return jsonify({"error": "事件不存在或当前状态不可重试"}), 409

        from routes.webhook import _schedule_persistent_webhook_drain
        _schedule_persistent_webhook_drain(delay=0)
        return jsonify({"message": "事件已重新加入处理队列", "event_id": event_id}), 202
    except Exception as exc:
        logger.error(f"重试 Webhook 事件 {event_id} 失败: {exc}", exc_info=True)
        return jsonify({"error": "无法重试 Webhook 事件"}), 500


@tasks_bp.route('/strm-ingest-events', methods=['GET'])
@admin_required
def get_strm_ingest_events():
    try:
        limit = request.args.get('limit', 100, type=int)
        return jsonify({
            'events': strm_ingest_db.list_recent(limit=limit),
            'summary': strm_ingest_db.get_summary(),
        }), 200
    except Exception as exc:
        logger.error(f"获取 STRM 入库诊断失败: {exc}", exc_info=True)
        return jsonify({'error': '无法获取 STRM 入库诊断'}), 500


@tasks_bp.route('/strm-ingest-events/<int:event_id>/retry', methods=['POST'])
@admin_required
def retry_strm_ingest_event(event_id):
    try:
        if not strm_ingest_db.retry_path(event_id):
            return jsonify({'error': '记录不存在或当前状态不可重试'}), 409
        return jsonify({'message': 'STRM 已重新加入入库队列', 'event_id': event_id}), 202
    except Exception as exc:
        logger.error(f"重试 STRM 入库记录 {event_id} 失败: {exc}", exc_info=True)
        return jsonify({'error': '无法重试 STRM 入库记录'}), 500


@tasks_bp.route('/strm-ingest-events/<int:event_id>/ignore', methods=['POST'])
@admin_required
def ignore_strm_ingest_event(event_id):
    try:
        if not strm_ingest_db.ignore_path(event_id):
            return jsonify({'error': '记录不存在或当前状态不可忽略'}), 409
        return jsonify({'message': 'STRM 入库异常已忽略', 'event_id': event_id}), 200
    except Exception as exc:
        logger.error(f"忽略 STRM 入库记录 {event_id} 失败: {exc}", exc_info=True)
        return jsonify({'error': '无法忽略 STRM 入库记录'}), 500
