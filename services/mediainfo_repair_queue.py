"""Bounded, generation-aware orchestration for Shenyi single-Item repair."""

from __future__ import annotations

import collections
import logging
import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor, wait
from typing import Any, Callable, Dict, Optional

import config_manager
import constants
from database import media_info_repair_db
from services.mediainfo_state import (
    ACTIVE_JOB_STATES,
    MediaInfoStateService,
    public_snapshot,
)
from services.shenyi_mediainfo import ShenyiMediaInfoAdapter, SyncResult


logger = logging.getLogger(__name__)


DEFAULT_WORKERS = 2
DEFAULT_PENDING_LIMIT = 128
DEFAULT_BATCH_LIMIT = 20
DEFAULT_READBACK_DELAY_SECONDS = 5
DEFAULT_FAILURE_COOLDOWN_MINUTES = 30
DEFAULT_AMBIGUOUS_COOLDOWN_MINUTES = 60
DEFAULT_SHUTDOWN_TIMEOUT_SECONDS = 90


def _snapshot_from_job(job: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    snapshot = (job or {}).get("snapshot_json")
    return snapshot if isinstance(snapshot, dict) else None


def _public_job(job: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    if not job:
        return None
    return {
        "id": job.get("id"),
        "exact_item_id": job.get("exact_item_id"),
        "item_type": job.get("item_type"),
        "redacted_path_hint": job.get("redacted_path_hint"),
        "state": job.get("state"),
        "reason_code": job.get("reason_code"),
        "generation": job.get("generation"),
        "post_attempts": job.get("post_attempts"),
        "response_kind": job.get("response_kind"),
        "submitted_at": job.get("submitted_at"),
        "started_at": job.get("started_at"),
        "completed_at": job.get("completed_at"),
        "retry_after": job.get("retry_after"),
    }


class MediaInfoRepairCoordinator:
    def __init__(
        self,
        *,
        state_service: Optional[MediaInfoStateService] = None,
        repository=media_info_repair_db,
        adapter_factory: Optional[Callable[[], ShenyiMediaInfoAdapter]] = None,
        worker_count: int = DEFAULT_WORKERS,
        pending_limit: int = DEFAULT_PENDING_LIMIT,
        batch_limit: int = DEFAULT_BATCH_LIMIT,
        readback_delay_seconds: float = DEFAULT_READBACK_DELAY_SECONDS,
        sleep: Callable[[float], None] = time.sleep,
        shutdown_timeout_seconds: float = DEFAULT_SHUTDOWN_TIMEOUT_SECONDS,
        executor_factory=ThreadPoolExecutor,
    ):
        self.state_service = state_service or MediaInfoStateService()
        self.repository = repository
        self._adapter_factory = adapter_factory or self._default_adapter
        self.worker_count = max(1, int(worker_count))
        self.pending_limit = max(1, int(pending_limit))
        self.batch_limit = max(1, int(batch_limit))
        self.readback_delay_seconds = max(0.0, float(readback_delay_seconds))
        self._sleep = sleep
        self.shutdown_timeout_seconds = max(0.1, float(shutdown_timeout_seconds))
        self._executor_factory = executor_factory

        self._condition = threading.Condition(threading.RLock())
        self._state = "stopped"
        self._generation = 0
        self._pending: collections.deque[Dict[str, Any]] = collections.deque()
        self._active_roots: set[str] = set()
        self._active_items: set[str] = set()
        self._futures: set[Future] = set()
        self._executor: Optional[ThreadPoolExecutor] = None
        self._dispatcher: Optional[threading.Thread] = None
        self._ever_started = False

    @property
    def state(self) -> str:
        with self._condition:
            return self._state

    @property
    def accepting(self) -> bool:
        return self.state == "accepting"

    @property
    def generation(self) -> int:
        with self._condition:
            return self._generation

    @property
    def pending_count(self) -> int:
        with self._condition:
            return len(self._pending)

    @property
    def active_count(self) -> int:
        with self._condition:
            return len(self._futures)

    def _default_adapter(self) -> ShenyiMediaInfoAdapter:
        config = config_manager.APP_CONFIG
        return ShenyiMediaInfoAdapter(
            str(config.get(constants.CONFIG_OPTION_EMBY_SERVER_URL) or ""),
            str(config.get(constants.CONFIG_OPTION_EMBY_API_KEY) or ""),
            timeout_seconds=75,
        )

    def start(self) -> int:
        with self._condition:
            if self._state == "accepting":
                return self._generation
            if self._state == "draining":
                raise RuntimeError("MediaInfo repair coordinator is draining")
            self.repository.recover_interrupted_jobs(
                DEFAULT_AMBIGUOUS_COOLDOWN_MINUTES
            )
            self._generation = int(self.repository.next_generation())
            self._ever_started = True
            self._executor = self._executor_factory(
                max_workers=self.worker_count,
                thread_name_prefix=f"evh-mediainfo-{self._generation}",
            )
            self._pending.clear()
            self._active_roots.clear()
            self._active_items.clear()
            self._futures.clear()
            self._state = "accepting"
            self._dispatcher = threading.Thread(
                target=self._dispatch_loop,
                name=f"evh-mediainfo-dispatch-{self._generation}",
                daemon=True,
            )
            self._dispatcher.start()
            return self._generation

    def _ensure_started(self) -> None:
        if self.state == "stopped" and not self._ever_started:
            self.start()

    def get_status(self, item_id: str, *, recheck: bool = False) -> Dict[str, Any]:
        self._ensure_started()
        normalized = str(item_id or "").strip()
        if not normalized:
            raise ValueError("exact item ID is required")
        job = self.repository.get_by_item_id(normalized)
        snapshot = _snapshot_from_job(job)
        if recheck or not snapshot:
            snapshot = self.state_service.observe(
                normalized,
                include_media=bool(recheck),
                previous_snapshot=snapshot,
            )
            job = self.repository.upsert_observation(snapshot)
        conflict = self.repository.get_active_conflict(snapshot)
        eligibility_job = (
            job
            if job and job.get("state") in ACTIVE_JOB_STATES
            else conflict or job
        )
        eligible, reason = self.state_service.repair_eligibility(
            snapshot,
            active_job=eligibility_job,
        )
        return public_snapshot(
            snapshot,
            active_job=job,
            repair_eligible=eligible,
            eligibility_reason=reason,
        )

    def submit(self, item_id: str) -> Dict[str, Any]:
        self._ensure_started()
        with self._condition:
            if self._state != "accepting":
                return {"result": "rejected", "reason_code": "shutdown_before_start"}
        normalized = str(item_id or "").strip()
        existing = self.repository.get_by_item_id(normalized)
        existing_snapshot = _snapshot_from_job(existing)
        if existing and existing.get("state") in ACTIVE_JOB_STATES:
            return {
                "result": "existing",
                "reason_code": "repair_already_active",
                "job": _public_job(existing),
            }

        snapshot = self.state_service.observe(
            normalized,
            include_media=True,
            previous_snapshot=existing_snapshot,
        )
        conflict = self.repository.get_active_conflict(snapshot)
        eligibility_job = (
            existing
            if existing and existing.get("state") in ACTIVE_JOB_STATES
            else conflict or existing
        )
        eligible, reason = self.state_service.repair_eligibility(
            snapshot,
            active_job=eligibility_job,
        )
        if not eligible:
            self.repository.upsert_observation(snapshot)
            return {"result": "rejected", "reason_code": reason or "repair_not_eligible"}

        create_result, job = self.repository.create_job(
            snapshot,
            self.generation,
            self.pending_limit,
        )
        if create_result == "existing":
            return {
                "result": "existing",
                "reason_code": "repair_already_active",
                "job": _public_job(job),
            }
        if create_result == "cooldown":
            return {
                "result": "rejected",
                "reason_code": "repair_cooldown",
                "job": _public_job(job),
            }
        if create_result == "full":
            return {"result": "rejected", "reason_code": "repair_queue_full"}

        pending = {
            "id": int(job["id"]),
            "exact_item_id": normalized,
            "root_series_key": str(job["root_series_key"]),
            "generation": int(job["generation"]),
        }
        with self._condition:
            if self._state != "accepting":
                self.repository.mark_pending_shutdown([pending["id"]])
                return {
                    "result": "rejected",
                    "reason_code": "shutdown_before_start",
                }
            self._pending.append(pending)
            self._condition.notify_all()
        return {
            "result": "accepted",
            "reason_code": None,
            "job": _public_job(job),
        }

    def submit_batch(self, item_ids: list[str]) -> Dict[str, Any]:
        if not isinstance(item_ids, list):
            raise ValueError("item_ids must be a list")
        normalized = list(
            dict.fromkeys(str(item_id or "").strip() for item_id in item_ids)
        )
        if any(not item_id for item_id in normalized):
            raise ValueError("item_ids contains an empty value")
        if len(normalized) > self.batch_limit:
            raise ValueError(f"at most {self.batch_limit} items may be submitted")
        result = {"accepted": [], "skipped": [], "rejected": []}
        for item_id in normalized:
            item_result = self.submit(item_id)
            entry = {
                "item_id": item_id,
                "reason_code": item_result.get("reason_code"),
                "job": item_result.get("job"),
            }
            if item_result["result"] == "accepted":
                result["accepted"].append(entry)
            elif item_result["result"] == "existing":
                result["skipped"].append(entry)
            else:
                result["rejected"].append(entry)
        return result

    def get_job(self, job_id: int) -> Optional[Dict[str, Any]]:
        return _public_job(self.repository.get_by_id(int(job_id)))

    def cancel(self, job_id: int) -> Optional[Dict[str, Any]]:
        cancelled = self.repository.cancel_pending(int(job_id))
        if not cancelled:
            return None
        with self._condition:
            self._pending = collections.deque(
                job for job in self._pending if int(job["id"]) != int(job_id)
            )
            self._condition.notify_all()
        return _public_job(cancelled)

    def _next_runnable_index(self) -> Optional[int]:
        if len(self._futures) >= self.worker_count:
            return None
        for index, job in enumerate(self._pending):
            if (
                job["root_series_key"] not in self._active_roots
                and job["exact_item_id"] not in self._active_items
            ):
                return index
        return None

    def _pop_index(self, index: int) -> Dict[str, Any]:
        self._pending.rotate(-index)
        job = self._pending.popleft()
        self._pending.rotate(index)
        return job

    def _dispatch_loop(self) -> None:
        while True:
            with self._condition:
                while True:
                    if self._state != "accepting":
                        return
                    index = self._next_runnable_index()
                    if index is not None:
                        break
                    self._condition.wait(timeout=0.25)
                job = self._pop_index(index)
                executor = self._executor
                if executor is None:
                    self._pending.appendleft(job)
                    return
                self._active_roots.add(job["root_series_key"])
                self._active_items.add(job["exact_item_id"])
                try:
                    future = executor.submit(self._run_job, job)
                except Exception:
                    self._active_roots.discard(job["root_series_key"])
                    self._active_items.discard(job["exact_item_id"])
                    pending_ids = [job["id"]] + [
                        pending["id"] for pending in self._pending
                    ]
                    self._pending.clear()
                    self._state = "stopped"
                    self._executor = None
                    self._condition.notify_all()
                    logger.exception("MediaInfo repair executor rejected a job")
                    self.repository.mark_pending_shutdown(pending_ids)
                    try:
                        executor.shutdown(wait=False, cancel_futures=True)
                    except Exception:
                        pass
                    return
                self._futures.add(future)
                future.add_done_callback(
                    lambda completed, job=job: self._job_done(completed, job)
                )

    def _job_done(self, future: Future, job: Dict[str, Any]) -> None:
        try:
            future.result()
        except Exception:
            logger.exception(
                "MediaInfo repair worker failed for Item %s",
                job["exact_item_id"],
            )
        finally:
            with self._condition:
                self._futures.discard(future)
                self._active_roots.discard(job["root_series_key"])
                self._active_items.discard(job["exact_item_id"])
                if self._state == "draining" and not self._futures:
                    self._state = "stopped"
                self._condition.notify_all()

    def _run_job(self, pending: Dict[str, Any]) -> None:
        job = self.repository.mark_running(
            pending["id"],
            pending["generation"],
        )
        if not job:
            return
        previous_snapshot = _snapshot_from_job(job) or {}
        try:
            adapter = self._adapter_factory()
            submitting = self.repository.mark_submitting(
                pending["id"],
                pending["generation"],
            )
            if not submitting:
                return
            result: SyncResult = adapter.sync_item(pending["exact_item_id"])
            snapshot = self.state_service.observe(
                pending["exact_item_id"],
                include_media=True,
                previous_snapshot=previous_snapshot,
            )
            ready = (
                (snapshot.get("emby_media_status") or {}).get("status") == "ready"
            )
            if not ready and self.readback_delay_seconds > 0:
                self._sleep(self.readback_delay_seconds)
                snapshot = self.state_service.observe(
                    pending["exact_item_id"],
                    include_media=True,
                    previous_snapshot=snapshot,
                )
                ready = (
                    (snapshot.get("emby_media_status") or {}).get("status")
                    == "ready"
                )

            if ready and result.outcome == "ambiguous":
                final_state = "succeeded_after_ambiguous"
                reason_code = "succeeded_after_ambiguous"
                cooldown = 0
            elif ready:
                final_state = "succeeded"
                reason_code = "succeeded"
                cooldown = 0
            elif result.outcome == "ambiguous":
                final_state = "ambiguous"
                reason_code = "post_result_ambiguous"
                cooldown = DEFAULT_AMBIGUOUS_COOLDOWN_MINUTES
            else:
                final_state = "failed"
                reason_code = (
                    result.reason_code
                    if result.reason_code != "readback_not_ready"
                    else "readback_not_ready"
                )
                cooldown = DEFAULT_FAILURE_COOLDOWN_MINUTES

            self.repository.finish_job(
                pending["id"],
                pending["generation"],
                state=final_state,
                reason_code=reason_code,
                response_kind=result.response_kind,
                snapshot=snapshot,
                cooldown_minutes=cooldown,
            )
        except Exception:
            logger.exception(
                "MediaInfo repair orchestration failed for Item %s",
                pending["exact_item_id"],
            )
            self.repository.finish_job(
                pending["id"],
                pending["generation"],
                state="failed",
                reason_code="unknown_failure",
                response_kind="internal_error",
                snapshot=previous_snapshot,
                cooldown_minutes=DEFAULT_FAILURE_COOLDOWN_MINUTES,
            )

    def shutdown(self) -> None:
        with self._condition:
            if self._state == "stopped":
                return
            self._state = "draining"
            pending_ids = [int(job["id"]) for job in self._pending]
            self._pending.clear()
            self._condition.notify_all()
            dispatcher = self._dispatcher
            futures = set(self._futures)
            executor = self._executor
        if pending_ids:
            self.repository.mark_pending_shutdown(pending_ids)
        if dispatcher and dispatcher.is_alive():
            dispatcher.join(timeout=2)
        if futures:
            wait(futures, timeout=self.shutdown_timeout_seconds)
        if executor:
            executor.shutdown(wait=False, cancel_futures=True)
        with self._condition:
            still_running = {future for future in self._futures if not future.done()}
            self._futures = still_running
            self._executor = None
            self._dispatcher = None
            self._state = "draining" if still_running else "stopped"
            self._condition.notify_all()


_COORDINATOR_LOCK = threading.Lock()
_COORDINATOR: Optional[MediaInfoRepairCoordinator] = None


def get_media_info_coordinator() -> MediaInfoRepairCoordinator:
    global _COORDINATOR
    with _COORDINATOR_LOCK:
        if _COORDINATOR is None:
            _COORDINATOR = MediaInfoRepairCoordinator()
        return _COORDINATOR


def shutdown_media_info_coordinator() -> None:
    global _COORDINATOR
    with _COORDINATOR_LOCK:
        coordinator = _COORDINATOR
    if coordinator is not None:
        coordinator.shutdown()
