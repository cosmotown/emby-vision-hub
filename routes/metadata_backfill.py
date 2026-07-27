import copy
import logging
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from typing import Dict, List

from flask import Blueprint, jsonify, request

import config_manager
import constants
import extensions
from extensions import admin_required, processor_ready_required
from services.shenyi_metadata_backfill import ShenyiMetadataBackfillService


logger = logging.getLogger(__name__)
metadata_backfill_bp = Blueprint(
    "metadata_backfill", __name__, url_prefix="/api/metadata-backfill"
)


class MetadataBackfillTaskStore:
    def __init__(self, max_workers: int = 2):
        self._lock = threading.Lock()
        self._tasks: Dict[str, dict] = {}
        self._active_keys = set()
        self._executor = ThreadPoolExecutor(
            max_workers=max(1, int(max_workers)),
            thread_name_prefix="metadata-backfill",
        )
        self._futures = set()

    def _trim(self):
        if len(self._tasks) <= 100:
            return
        completed = [
            key
            for key, value in self._tasks.items()
            if value.get("status") in {"completed", "failed"}
        ]
        for key in completed[: len(self._tasks) - 100]:
            self._tasks.pop(key, None)

    def start(
        self,
        service: ShenyiMetadataBackfillService,
        item_ids: List[str],
        *,
        explicit_retry: bool = False,
    ) -> dict:
        active_keys = {
            service.execution_key(item_id)
            for item_id in item_ids
        }
        with self._lock:
            duplicates = sorted(active_keys & self._active_keys)
            if duplicates:
                raise ValueError("同一根媒体项目已有补齐任务运行")
            task_id = uuid.uuid4().hex
            self._active_keys.update(active_keys)
            self._tasks[task_id] = {
                "task_id": task_id,
                "status": "pending",
                "total": len(item_ids),
                "completed": 0,
                "results": [],
                "explicit_retry": bool(explicit_retry),
                "created_at": datetime.now(timezone.utc).isoformat(),
            }
            self._trim()

        future = self._executor.submit(
            self._run,
            task_id,
            service,
            item_ids,
            active_keys,
            bool(explicit_retry),
        )
        with self._lock:
            self._futures.add(future)
        future.add_done_callback(self._discard_future)
        return self.get(task_id)

    def _discard_future(self, future):
        with self._lock:
            self._futures.discard(future)

    def _run(self, task_id, service, item_ids, active_keys, explicit_retry):
        with self._lock:
            self._tasks[task_id]["status"] = "running"
        try:
            for item_id in item_ids:
                try:
                    result = service.execute(
                        item_id, explicit_retry=explicit_retry
                    )
                except Exception as exc:
                    logger.warning(
                        "神医元数据补齐项目失败 (ItemID=%s, %s)",
                        item_id,
                        type(exc).__name__,
                    )
                    result = {
                        "item_id": item_id,
                        "status": "failed",
                        "error": _safe_error(exc),
                    }
                with self._lock:
                    task = self._tasks[task_id]
                    task["results"].append(result)
                    task["completed"] += 1
            with self._lock:
                self._tasks[task_id]["status"] = "completed"
                self._tasks[task_id]["finished_at"] = datetime.now(
                    timezone.utc
                ).isoformat()
        except Exception as exc:
            logger.exception("神医元数据补齐批任务失败")
            with self._lock:
                self._tasks[task_id]["status"] = "failed"
                self._tasks[task_id]["error"] = type(exc).__name__
        finally:
            with self._lock:
                self._active_keys.difference_update(active_keys)

    def get(self, task_id):
        with self._lock:
            task = self._tasks.get(str(task_id or "").strip())
            return copy.deepcopy(task) if task else None

    def shutdown(self, wait=True):
        self._executor.shutdown(wait=wait)


metadata_backfill_tasks = MetadataBackfillTaskStore()


def _safe_error(exc):
    if isinstance(exc, ValueError):
        return str(exc)[:200]
    return f"处理失败 ({type(exc).__name__})"


def _service():
    processor = extensions.media_processor_instance
    root = str(
        config_manager.APP_CONFIG.get(constants.CONFIG_OPTION_LOCAL_DATA_PATH) or ""
    ).strip()
    if not root:
        raise ValueError("LocalDataSource.local_data_path 未配置")
    return ShenyiMetadataBackfillService(
        root,
        processor.emby_url,
        processor.emby_api_key,
        processor.emby_user_id,
    )


def _item_ids():
    payload = request.get_json(silent=True) or {}
    values = payload.get("item_ids")
    if values is None and payload.get("item_id") is not None:
        values = [payload.get("item_id")]
    if not isinstance(values, list):
        raise ValueError("item_ids 必须为数组")
    normalized = list(
        dict.fromkeys(str(value or "").strip() for value in values if str(value or "").strip())
    )
    if not normalized:
        raise ValueError("至少提供一个 item_id")
    if len(normalized) > 100:
        raise ValueError("单批最多处理 100 个根项目")
    return normalized


@metadata_backfill_bp.route("/preview", methods=["POST"])
@admin_required
@processor_ready_required
def preview_metadata_backfill():
    try:
        service = _service()
        results = []
        for item_id in _item_ids():
            try:
                results.append(service.preview(item_id))
            except Exception as exc:
                results.append(
                    {
                        "item_id": item_id,
                        "status": "failed",
                        "error": _safe_error(exc),
                    }
                )
        return jsonify({"dry_run": True, "results": results})
    except ValueError as exc:
        return jsonify({"error": _safe_error(exc)}), 400


@metadata_backfill_bp.route("/tasks", methods=["POST"])
@admin_required
@processor_ready_required
def start_metadata_backfill():
    try:
        payload = request.get_json(silent=True) or {}
        task = metadata_backfill_tasks.start(
            _service(),
            _item_ids(),
            explicit_retry=payload.get("explicit_retry") is True,
        )
        return jsonify(task), 202
    except ValueError as exc:
        status = 409 if "已有" in str(exc) else 400
        return jsonify({"error": _safe_error(exc)}), status


@metadata_backfill_bp.route("/tasks/<task_id>", methods=["GET"])
@admin_required
def get_metadata_backfill_task(task_id):
    task = metadata_backfill_tasks.get(task_id)
    if not task:
        return jsonify({"error": "任务不存在"}), 404
    return jsonify(task)
