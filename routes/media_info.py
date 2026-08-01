"""Minimal administrator API for Shenyi single-Item MediaInfo orchestration."""

from __future__ import annotations

from flask import Blueprint, jsonify, request

from extensions import admin_required
from services.mediainfo_repair_queue import get_media_info_coordinator


media_info_bp = Blueprint("media_info", __name__, url_prefix="/api/media-info")


def _coordinator():
    return get_media_info_coordinator()


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
    payload = request.get_json(silent=True) or {}
    try:
        result = _coordinator().submit_batch(payload.get("item_ids"))
        return jsonify(result), 202 if result["accepted"] else 200
    except ValueError as exc:
        return jsonify({"error": str(exc), "reason_code": "repair_not_eligible"}), 400
    except Exception:
        return jsonify(
            {"error": "批量任务提交失败", "reason_code": "unknown_failure"}
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
