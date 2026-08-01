import copy
import threading
import time
import unittest
from datetime import datetime, timezone

from services.mediainfo_repair_queue import MediaInfoRepairCoordinator
from services.shenyi_mediainfo import SyncResult


def wait_until(predicate, timeout=2.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return bool(predicate())


class FakeRepository:
    ACTIVE = {"pending", "running", "submitting", "submitted"}

    def __init__(self):
        self.lock = threading.RLock()
        self.rows_by_item = {}
        self.rows_by_id = {}
        self.next_id = 1
        self.generation = 0
        self.recover_calls = 0

    def recover_interrupted_jobs(self, _minutes):
        with self.lock:
            self.recover_calls += 1
            for row in self.rows_by_id.values():
                if row["state"] == "pending":
                    row.update(state="shutdown_before_start", reason_code="shutdown_before_start")
                elif row["state"] in {"running", "submitting", "submitted"}:
                    row.update(state="ambiguous", reason_code="post_result_ambiguous")
        return {}

    def next_generation(self):
        with self.lock:
            self.generation += 1
            return self.generation

    def get_by_item_id(self, item_id):
        with self.lock:
            return copy.deepcopy(self.rows_by_item.get(str(item_id)))

    def get_by_id(self, job_id):
        with self.lock:
            return copy.deepcopy(self.rows_by_id.get(int(job_id)))

    def get_active_conflict(self, snapshot):
        identity = snapshot["identity"]
        with self.lock:
            for row in self.rows_by_id.values():
                if row.get("exact_item_id") == identity["exact_item_id"]:
                    continue
                same_path_active = (
                    row.get("exact_strm_path_hash")
                    == identity["exact_strm_path_hash"]
                    and row.get("state") in self.ACTIVE
                )
                same_root_running = (
                    row.get("root_series_key") == identity["root_series_key"]
                    and row.get("state") in {"running", "submitting", "submitted"}
                )
                if same_path_active or same_root_running:
                    return copy.deepcopy(row)
        return None

    def upsert_observation(self, snapshot):
        item_id = snapshot["identity"]["exact_item_id"]
        with self.lock:
            row = self.rows_by_item.get(item_id)
            if row is None:
                row = {
                    "id": self.next_id,
                    "exact_item_id": item_id,
                    "state": "idle",
                    "reason_code": None,
                    "generation": 0,
                    "post_attempts": 0,
                }
                self.next_id += 1
                self.rows_by_item[item_id] = row
                self.rows_by_id[row["id"]] = row
            row.update(
                item_type=snapshot["identity"]["item_type"],
                exact_strm_path_hash=snapshot["identity"]["exact_strm_path_hash"],
                redacted_path_hint=snapshot["identity"]["redacted_path_hint"],
                root_series_key=snapshot["identity"]["root_series_key"],
                snapshot_json=copy.deepcopy(snapshot),
            )
            return copy.deepcopy(row)

    def create_job(self, snapshot, generation, pending_limit):
        item_id = snapshot["identity"]["exact_item_id"]
        with self.lock:
            existing = self.rows_by_item.get(item_id)
            if existing and existing["state"] in self.ACTIVE:
                return "existing", copy.deepcopy(existing)
            pending = sum(1 for row in self.rows_by_id.values() if row["state"] == "pending")
            if pending >= pending_limit:
                return "full", {}
            if existing is None:
                existing = {"id": self.next_id, "exact_item_id": item_id}
                self.next_id += 1
                self.rows_by_item[item_id] = existing
                self.rows_by_id[existing["id"]] = existing
            existing.update(
                item_type=snapshot["identity"]["item_type"],
                exact_strm_path_hash=snapshot["identity"]["exact_strm_path_hash"],
                redacted_path_hint=snapshot["identity"]["redacted_path_hint"],
                root_series_key=snapshot["identity"]["root_series_key"],
                state="pending",
                reason_code=None,
                generation=generation,
                post_attempts=0,
                retry_after=None,
                snapshot_json=copy.deepcopy(snapshot),
            )
            return "created", copy.deepcopy(existing)

    def mark_running(self, job_id, generation):
        with self.lock:
            row = self.rows_by_id[job_id]
            if row["state"] != "pending" or row["generation"] != generation:
                return None
            row["state"] = "running"
            return copy.deepcopy(row)

    def mark_submitting(self, job_id, generation):
        with self.lock:
            row = self.rows_by_id[job_id]
            if row["state"] != "running" or row["generation"] != generation:
                return None
            row["state"] = "submitting"
            row["post_attempts"] += 1
            return copy.deepcopy(row)

    def finish_job(
        self,
        job_id,
        generation,
        *,
        state,
        reason_code,
        response_kind,
        snapshot,
        cooldown_minutes=0,
    ):
        with self.lock:
            row = self.rows_by_id[job_id]
            if row["generation"] != generation:
                return None
            row.update(
                state=state,
                reason_code=reason_code,
                response_kind=response_kind,
                snapshot_json=copy.deepcopy(snapshot),
                retry_after=(
                    datetime.now(timezone.utc).isoformat()
                    if cooldown_minutes
                    else None
                ),
            )
            return copy.deepcopy(row)

    def cancel_pending(self, job_id):
        with self.lock:
            row = self.rows_by_id.get(job_id)
            if not row or row["state"] != "pending":
                return None
            row.update(state="cancelled", reason_code="cancelled_before_start")
            return copy.deepcopy(row)

    def mark_pending_shutdown(self, job_ids):
        with self.lock:
            count = 0
            for job_id in job_ids:
                row = self.rows_by_id[job_id]
                if row["state"] == "pending":
                    row.update(state="shutdown_before_start", reason_code="shutdown_before_start")
                    count += 1
            return count


class FakeStateService:
    def __init__(self, roots=None, path_hashes=None, readback_states=None):
        self.roots = roots or {}
        self.path_hashes = path_hashes or {}
        self.readback_states = {
            item: list(states) for item, states in (readback_states or {}).items()
        }
        self.calls = []

    def observe(self, item_id, *, include_media, previous_snapshot=None):
        self.calls.append((item_id, include_media))
        states = self.readback_states.setdefault(item_id, ["media_streams_empty"])
        state = states.pop(0) if len(states) > 1 else states[0]
        root = self.roots.get(item_id, item_id)
        return {
            "identity": {
                "exact_item_id": item_id,
                "item_type": "Episode",
                "root_series_key": root,
                "exact_strm_path_hash": self.path_hashes.get(
                    item_id,
                    f"hash-{item_id}",
                ),
                "redacted_path_hint": f"{item_id}.strm",
            },
            "strm_status": {"status": "present"},
            "emby_index_status": {"status": "indexed"},
            "shenyi_persist_status": {"status": "missing"},
            "emby_media_status": {"status": state},
            "summary_status": "ready" if state == "ready" else "media_info_incomplete",
            "suggested_action": "none" if state == "ready" else "manual_recheck",
            "snapshot_fingerprint": f"fp-{item_id}-{len(self.calls)}",
        }

    def repair_eligibility(self, snapshot, *, active_job=None, feature_enabled=None, now=None):
        if snapshot["emby_media_status"]["status"] == "ready":
            return False, "repair_not_eligible"
        if active_job and active_job.get("state") in FakeRepository.ACTIVE:
            return False, "repair_already_active"
        return True, None


class RecordingAdapter:
    def __init__(self, result, started=None, release=None):
        self.result = result
        self.started = started
        self.release = release
        self.calls = []
        self.lock = threading.Lock()
        self.active = 0
        self.max_active = 0

    def sync_item(self, item_id):
        with self.lock:
            self.calls.append(item_id)
            self.active += 1
            self.max_active = max(self.max_active, self.active)
        if self.started:
            self.started.set()
        if self.release:
            self.release.wait(timeout=2)
        with self.lock:
            self.active -= 1
        return self.result


class RejectingExecutor:
    def __init__(self, **_kwargs):
        self.shutdown_calls = 0

    def submit(self, *_args, **_kwargs):
        raise RuntimeError("executor closed")

    def shutdown(self, **_kwargs):
        self.shutdown_calls += 1


SUBMITTED = SyncResult("submitted", "readback_not_ready", "nonempty_json", 200, 2, "x")
AMBIGUOUS = SyncResult("ambiguous", "sync_timeout", "transport_timeout", None, 0, None)
EMPTY = SyncResult("failed", "sync_empty_result", "empty_array", 200, 2, "x")


class MediaInfoRepairQueueTests(unittest.TestCase):
    def make_coordinator(self, state, repo, adapter, **kwargs):
        return MediaInfoRepairCoordinator(
            state_service=state,
            repository=repo,
            adapter_factory=lambda: adapter,
            readback_delay_seconds=0.001,
            sleep=lambda _seconds: None,
            shutdown_timeout_seconds=1,
            **kwargs,
        )

    def test_two_different_works_run_in_parallel_and_third_is_pending(self):
        repo = FakeRepository()
        state = FakeStateService(
            roots={"a": "root-a", "b": "root-b", "c": "root-c"},
            readback_states={key: ["media_streams_empty"] * 3 for key in "abc"},
        )
        started = threading.Event()
        release = threading.Event()
        adapter = RecordingAdapter(EMPTY, started, release)
        coordinator = self.make_coordinator(state, repo, adapter, worker_count=2)
        try:
            self.assertEqual("accepted", coordinator.submit("a")["result"])
            self.assertEqual("accepted", coordinator.submit("b")["result"])
            self.assertEqual("accepted", coordinator.submit("c")["result"])
            self.assertTrue(wait_until(lambda: coordinator.active_count == 2))
            self.assertEqual(1, coordinator.pending_count)
            self.assertEqual(2, adapter.max_active)
        finally:
            release.set()
            coordinator.shutdown()

    def test_default_limits_are_two_workers_128_pending_and_twenty_per_batch(self):
        coordinator = self.make_coordinator(
            FakeStateService(),
            FakeRepository(),
            RecordingAdapter(EMPTY),
        )
        self.assertEqual(2, coordinator.worker_count)
        self.assertEqual(128, coordinator.pending_limit)
        self.assertEqual(20, coordinator.batch_limit)

    def test_scheduler_skips_active_root_and_runs_another_work(self):
        coordinator = self.make_coordinator(
            FakeStateService(),
            FakeRepository(),
            RecordingAdapter(EMPTY),
        )
        with coordinator._condition:
            coordinator._pending.extend(
                [
                    {
                        "id": 1,
                        "exact_item_id": "same-root",
                        "root_series_key": "root-a",
                        "generation": 1,
                    },
                    {
                        "id": 2,
                        "exact_item_id": "other-root",
                        "root_series_key": "root-b",
                        "generation": 1,
                    },
                ]
            )
            coordinator._active_roots.add("root-a")
            self.assertEqual(1, coordinator._next_runnable_index())

    def test_running_same_root_is_rejected_without_second_post(self):
        repo = FakeRepository()
        state = FakeStateService(
            roots={"a": "root-a", "b": "root-a"},
            readback_states={"a": ["media_streams_empty"] * 3, "b": ["media_streams_empty"] * 3},
        )
        started = threading.Event()
        release = threading.Event()
        adapter = RecordingAdapter(EMPTY, started, release)
        coordinator = self.make_coordinator(state, repo, adapter)
        try:
            self.assertEqual("accepted", coordinator.submit("a")["result"])
            self.assertTrue(started.wait(timeout=1))
            second = coordinator.submit("b")
            self.assertEqual("rejected", second["result"])
            self.assertEqual("repair_already_active", second["reason_code"])
        finally:
            release.set()
            coordinator.shutdown()
        self.assertEqual(["a"], adapter.calls)

    def test_same_normalized_path_is_deduplicated_while_pending(self):
        repo = FakeRepository()
        state = FakeStateService(
            roots={"a": "root-a", "b": "root-b"},
            path_hashes={"a": "same-path", "b": "same-path"},
            readback_states={"a": ["media_streams_empty"] * 3, "b": ["media_streams_empty"] * 3},
        )
        started = threading.Event()
        release = threading.Event()
        adapter = RecordingAdapter(EMPTY, started, release)
        coordinator = self.make_coordinator(state, repo, adapter, worker_count=1)
        try:
            first = coordinator.submit("a")
            self.assertEqual("accepted", first["result"])
            second = coordinator.submit("b")
            self.assertEqual("rejected", second["result"])
            self.assertEqual("repair_already_active", second["reason_code"])
            self.assertTrue(started.wait(timeout=1))
        finally:
            release.set()
            coordinator.shutdown()
        self.assertEqual(["a"], adapter.calls)

    def test_same_item_duplicate_returns_existing_without_second_post(self):
        repo = FakeRepository()
        state = FakeStateService(readback_states={"a": ["media_streams_empty"] * 3})
        release = threading.Event()
        started = threading.Event()
        adapter = RecordingAdapter(EMPTY, started, release)
        coordinator = self.make_coordinator(state, repo, adapter)
        try:
            first = coordinator.submit("a")
            second = coordinator.submit("a")
            self.assertEqual("accepted", first["result"])
            self.assertEqual("existing", second["result"])
            self.assertTrue(started.wait(timeout=1))
        finally:
            release.set()
            coordinator.shutdown()
        self.assertEqual(["a"], adapter.calls)

    def test_pending_limit_and_cancel_are_bounded(self):
        repo = FakeRepository()
        state = FakeStateService(
            roots={key: key for key in "abcde"},
            readback_states={key: ["media_streams_empty"] * 3 for key in "abcde"},
        )
        release = threading.Event()
        adapter = RecordingAdapter(EMPTY, threading.Event(), release)
        coordinator = self.make_coordinator(
            state,
            repo,
            adapter,
            worker_count=2,
            pending_limit=2,
        )
        try:
            self.assertEqual("accepted", coordinator.submit("a")["result"])
            self.assertEqual("accepted", coordinator.submit("b")["result"])
            self.assertTrue(wait_until(lambda: coordinator.active_count == 2))
            self.assertEqual("accepted", coordinator.submit("c")["result"])
            self.assertEqual("accepted", coordinator.submit("d")["result"])
            self.assertEqual(2, coordinator.pending_count)
            fifth = coordinator.submit("e")
            self.assertEqual("repair_queue_full", fifth["reason_code"])
            pending_jobs = [
                row for row in repo.rows_by_id.values() if row["state"] == "pending"
            ]
            self.assertEqual(2, len(pending_jobs))
            cancelled = coordinator.cancel(pending_jobs[0]["id"])
            self.assertEqual("cancelled", cancelled["state"])
        finally:
            release.set()
            coordinator.shutdown()

    def test_strict_readback_uses_at_most_two_gets_and_one_post(self):
        repo = FakeRepository()
        state = FakeStateService(
            readback_states={
                "a": ["media_streams_empty", "media_streams_empty", "ready"]
            }
        )
        adapter = RecordingAdapter(SUBMITTED)
        coordinator = self.make_coordinator(state, repo, adapter)
        try:
            job = coordinator.submit("a")["job"]
            self.assertTrue(
                wait_until(lambda: repo.get_by_id(job["id"])["state"] == "succeeded")
            )
        finally:
            coordinator.shutdown()
        self.assertEqual(["a"], adapter.calls)
        self.assertEqual(3, len(state.calls))  # precheck + two strict readbacks
        self.assertEqual(1, repo.get_by_id(job["id"])["post_attempts"])

    def test_ambiguous_can_upgrade_only_by_readback_and_never_reposts(self):
        repo = FakeRepository()
        state = FakeStateService(
            readback_states={"a": ["media_streams_empty", "ready"]}
        )
        adapter = RecordingAdapter(AMBIGUOUS)
        coordinator = self.make_coordinator(state, repo, adapter)
        try:
            job = coordinator.submit("a")["job"]
            self.assertTrue(
                wait_until(
                    lambda: repo.get_by_id(job["id"])["state"]
                    == "succeeded_after_ambiguous"
                )
            )
        finally:
            coordinator.shutdown()
        self.assertEqual(["a"], adapter.calls)

    def test_ambiguous_not_ready_stays_ambiguous_and_is_not_replayed(self):
        repo = FakeRepository()
        state = FakeStateService(
            readback_states={"a": ["media_streams_empty"] * 3}
        )
        adapter = RecordingAdapter(AMBIGUOUS)
        coordinator = self.make_coordinator(state, repo, adapter)
        try:
            job = coordinator.submit("a")["job"]
            self.assertTrue(
                wait_until(lambda: repo.get_by_id(job["id"])["state"] == "ambiguous")
            )
            self.assertEqual("post_result_ambiguous", repo.get_by_id(job["id"])["reason_code"])
        finally:
            coordinator.shutdown()
        self.assertEqual(["a"], adapter.calls)

    def test_http_200_empty_result_fails_and_cools_down(self):
        repo = FakeRepository()
        state = FakeStateService(
            readback_states={"a": ["media_streams_empty"] * 3}
        )
        adapter = RecordingAdapter(EMPTY)
        coordinator = self.make_coordinator(state, repo, adapter)
        try:
            job = coordinator.submit("a")["job"]
            self.assertTrue(
                wait_until(lambda: repo.get_by_id(job["id"])["state"] == "failed")
            )
            final = repo.get_by_id(job["id"])
            self.assertEqual("sync_empty_result", final["reason_code"])
            self.assertIsNotNone(final["retry_after"])
        finally:
            coordinator.shutdown()

    def test_stop_rejects_new_work_and_explicit_restart_uses_new_generation(self):
        repo = FakeRepository()
        state = FakeStateService(readback_states={"a": ["media_streams_empty"] * 3})
        adapter = RecordingAdapter(EMPTY)
        coordinator = self.make_coordinator(state, repo, adapter)
        coordinator.start()
        first_generation = coordinator.generation
        coordinator.shutdown()
        rejected = coordinator.submit("a")
        self.assertEqual("shutdown_before_start", rejected["reason_code"])
        coordinator.start()
        self.assertGreater(coordinator.generation, first_generation)
        coordinator.shutdown()

    def test_restart_marks_unknown_old_post_ambiguous_and_never_replays_it(self):
        repo = FakeRepository()
        old_snapshot = FakeStateService().observe("old", include_media=True)
        _, old = repo.create_job(old_snapshot, 1, 128)
        repo.mark_running(old["id"], 1)
        repo.mark_submitting(old["id"], 1)
        adapter = RecordingAdapter(EMPTY)
        coordinator = self.make_coordinator(FakeStateService(), repo, adapter)
        coordinator.start()
        try:
            recovered = repo.get_by_id(old["id"])
            self.assertEqual("ambiguous", recovered["state"])
            self.assertEqual("post_result_ambiguous", recovered["reason_code"])
            self.assertEqual([], adapter.calls)
        finally:
            coordinator.shutdown()

    def test_first_status_after_restart_recovers_unknown_post_without_replay(self):
        repo = FakeRepository()
        old_snapshot = FakeStateService().observe("old", include_media=True)
        _, old = repo.create_job(old_snapshot, 1, 128)
        repo.mark_running(old["id"], 1)
        repo.mark_submitting(old["id"], 1)
        adapter = RecordingAdapter(EMPTY)
        coordinator = self.make_coordinator(FakeStateService(), repo, adapter)
        try:
            status = coordinator.get_status("old")
            self.assertEqual("ambiguous", status["active_job"]["state"])
            self.assertEqual("post_result_ambiguous", status["active_job"]["reason_code"])
            self.assertEqual([], adapter.calls)
        finally:
            coordinator.shutdown()

    def test_batch_limit_is_twenty_and_results_are_independent(self):
        repo = FakeRepository()
        state = FakeStateService()
        adapter = RecordingAdapter(EMPTY)
        coordinator = self.make_coordinator(state, repo, adapter)
        try:
            with self.assertRaises(ValueError):
                coordinator.submit_batch([str(index) for index in range(21)])
        finally:
            coordinator.shutdown()

    def test_executor_submit_failure_marks_unaccepted_job_without_retry_loop(self):
        repo = FakeRepository()
        state = FakeStateService(readback_states={"a": ["media_streams_empty"]})
        adapter = RecordingAdapter(EMPTY)
        coordinator = self.make_coordinator(
            state,
            repo,
            adapter,
            executor_factory=RejectingExecutor,
        )
        result = coordinator.submit("a")
        job_id = result["job"]["id"]
        self.assertTrue(
            wait_until(
                lambda: repo.get_by_id(job_id)["state"] == "shutdown_before_start"
            )
        )
        self.assertEqual("stopped", coordinator.state)
        self.assertEqual([], adapter.calls)


if __name__ == "__main__":
    unittest.main()
