"""Fresh, read-only classification for explicit ReviewList cleanup actions."""

from __future__ import annotations

from typing import Any, Dict, Optional

from database import log_db
from services.mediainfo_state import MediaInfoStateService


REVIEW_CLEANUP_CATEGORIES = {"ready", "historical_item_missing"}


class ReviewCleanupService:
    def __init__(self, state_service: Optional[MediaInfoStateService] = None):
        self.state_service = state_service or MediaInfoStateService()

    @staticmethod
    def _validate_category(category: str) -> str:
        normalized = str(category or "").strip()
        if normalized not in REVIEW_CLEANUP_CATEGORIES:
            raise ValueError("unsupported review cleanup category")
        return normalized

    def collect(self, category: str) -> Dict[str, Any]:
        """Re-read every row and return only currently confirmed candidates."""
        normalized = self._validate_category(category)
        rows = log_db.get_all_review_items()
        candidate_ids: list[str] = []
        unavailable = 0

        for row in rows:
            source_id = str(row.get("item_id") or "").strip()
            if not source_id:
                unavailable += 1
                continue
            try:
                target = self.state_service.resolve_review_target(
                    source_id,
                    row.get("item_type"),
                    row.get("reason"),
                )
                target_reason = target.get("target_reason_code")
                if target_reason == "historical_item_missing":
                    current_status = "historical_item_missing"
                elif target.get("target_item_id"):
                    snapshot = self.state_service.observe(
                        str(target["target_item_id"]),
                        include_media=True,
                    )
                    current_status = snapshot.get("summary_status")
                else:
                    current_status = target_reason or "target_unresolved"
            except Exception:
                unavailable += 1
                continue

            if current_status == normalized and source_id not in candidate_ids:
                candidate_ids.append(source_id)

        return {
            "category": normalized,
            "scanned": len(rows),
            "candidate_count": len(candidate_ids),
            "candidate_ids": candidate_ids,
            "unavailable_count": unavailable,
        }

    def preview(self, category: str) -> Dict[str, Any]:
        result = self.collect(category)
        result.pop("candidate_ids", None)
        return result

    def execute(self, category: str) -> Dict[str, Any]:
        """Repeat the fresh read, then delete only that exact candidate set."""
        result = self.collect(category)
        candidate_ids = result.pop("candidate_ids")
        removed = log_db.remove_review_items(candidate_ids)
        result.update(
            {
                "removed_count": removed,
                "skipped_count": result["scanned"] - removed,
            }
        )
        return result
