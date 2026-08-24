"""Minimal administrator API for Shenyi single-Item MediaInfo orchestration."""

from __future__ import annotations

from flask import Blueprint, jsonify, request

from extensions import admin_required
from database import log_db
from services.mediainfo_repair_queue import get_media_info_coordinator
from services.mediainfo_state import MediaInfoStateService
from services.review_cleanup import ReviewCleanupService


media_info_bp = Blueprint("media_info", __name__, url_prefix="/api/media-info")


def _coordinator():
    return get_media_info_coordinator()


def _cleanup_service():
    return ReviewCleanupService()


@media_info_bp.route("/review-targets", methods=["GET"])
@admin_required
def get_review_targets():
    """Return a bounded, explicit target inventory for read-only global recheck."""
    requested_limit = request.args.get("limit", 1000, type=int)
    limit = max(1, min(requested_limit, 1000))
    rows, total = log_db.get_review_items_paginated(1, limit, "")
    resolver = MediaInfoStateService()
    targets = []
    for row in rows:
        resolved = resolver.resolve_review_target(
            row.get("item_id"), row.get("item_type"), row.get("reason")
        )
        targets.append(
            {
                "source_item_id": row.get("item_id"),
                "source_item_type": row.get("item_type"),
                "target": resolved,
            }
        )
    return jsonify(
        {
            "targets": targets,
            "returned": len(targets),
            "total": total,
            "truncated": total > limit,
            "limit": limit,
        }
    )


@media_info_bp.route("/items/<item_id>/status", methods=["GET"])
@admin_required
def get_item_status(item_id: str):
    try:
        return jsonify(_coordinator().get_status(item_id, recheck=False))
    except ValueError as exc:
        return jsonify({"error": str(exc), "reason_code": "repair_not_eligible"}), 400
    except Exception:
        return jsonify(
            {"error": "无法读取媒体信息状态", "reason_code": "emby_lookup_failed"}
        ), 503


@media_info_bp.route("/items/<item_id>/recheck", methods=["POST"])
@admin_required
def recheck_item(item_id: str):
    try:
        return jsonify(_coordinator().get_status(item_id, recheck=True))
    except ValueError as exc:
        return jsonify({"error": str(exc), "reason_code": "repair_not_eligible"}), 400
    except Exception:
        return jsonify(
            {"error": "重新核对失败", "reason_code": "emby_lookup_failed"}
        ), 503


@media_info_bp.route("/items/<item_id>/repair", methods=["POST"])
@admin_required
def repair_item(item_id: str):
    try:
        result = _coordinator().submit(item_id)
    except ValueError as exc:
        return jsonify({"error": str(exc), "reason_code": "repair_not_eligible"}), 400
    except Exception:
        return jsonify(
            {"error": "修复任务提交失败", "reason_code": "unknown_failure"}
        ), 503
    if result["result"] == "accepted":
        return jsonify(result), 202
    if result["result"] == "existing":
        return jsonify(result), 200
    reason = result.get("reason_code")
    status = 429 if reason == "repair_queue_full" else 409
    return jsonify(result), status


@media_info_bp.route("/repair-batch", methods=["POST"])
@admin_required
def repair_batch():
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        return jsonify(
            {
                "error": "请求正文必须是 JSON 对象",
                "reason_code": "repair_not_eligible",
            }
        ), 400
    item_ids = payload.get("item_ids")
    if not isinstance(item_ids, list) or any(
        isinstance(value, bool) or not isinstance(value, (str, int))
        for value in item_ids
    ):
        return jsonify(
            {
                "error": "item_ids 必须是标量 ID 数组",
                "reason_code": "repair_not_eligible",
            }
        ), 422
    try:
        result = _coordinator().submit_batch(item_ids)
        return jsonify(result), 202 if result["accepted"] else 200
    except ValueError as exc:
        return jsonify({"error": str(exc), "reason_code": "repair_not_eligible"}), 400
    except Exception:
        return jsonify(
            {"error": "批量任务提交失败", "reason_code": "unknown_failure"}
        ), 503


def _cleanup_category_from_request():
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        raise ValueError("请求正文必须是 JSON 对象")
    return payload.get("category")


@media_info_bp.route("/review-cleanup/preview", methods=["POST"])
@admin_required
def preview_review_cleanup():
    try:
        result = _cleanup_service().preview(_cleanup_category_from_request())
        return jsonify(result)
    except ValueError as exc:
        return jsonify({"error": str(exc), "reason_code": "invalid_cleanup_category"}), 400
    except Exception:
        return jsonify(
            {"error": "重新核验待复核记录失败", "reason_code": "emby_lookup_failed"}
        ), 503


@media_info_bp.route("/review-cleanup/execute", methods=["POST"])
@admin_required
def execute_review_cleanup():
    try:
        result = _cleanup_service().execute(_cleanup_category_from_request())
        return jsonify(result)
    except ValueError as exc:
        return jsonify({"error": str(exc), "reason_code": "invalid_cleanup_category"}), 400
    except Exception:
        return jsonify(
            {"error": "清理待复核记录失败", "reason_code": "emby_lookup_failed"}
        ), 503


@media_info_bp.route("/jobs/<int:job_id>", methods=["GET"])
@admin_required
def get_job(job_id: int):
    job = _coordinator().get_job(job_id)
    if not job:
        return jsonify({"error": "任务不存在"}), 404
    return jsonify(job)


@media_info_bp.route("/jobs/<int:job_id>/cancel", methods=["POST"])
@admin_required
def cancel_job(job_id: int):
    job = _coordinator().cancel(job_id)
    if not job:
        return jsonify(
            {
                "error": "只能取消尚未运行的任务",
                "reason_code": "repair_not_eligible",
            }
        ), 409
    return jsonify(job)
