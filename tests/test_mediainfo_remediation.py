import copy
import threading
import unittest

from services.mediainfo_repair_queue import MediaInfoRepairCoordinator
from tests.test_mediainfo_repair_queue import (
    EMPTY,
    FakeRepository,
    FakeStateService,
    RecordingAdapter,
    wait_until,
)


class LedgerCheckingAdapter(RecordingAdapter):
    def __init__(self, repository):
        super().__init__(EMPTY)
        self.repository = repository
        self.ledger_at_call = []

    def sync_item(self, item_id):
        row = self.repository.get_by_item_id(item_id)
        self.ledger_at_call.append((row["state"], row["post_attempts"]))
        return super().sync_item(item_id)


class SubmittingFailureRepository(FakeRepository):
    def mark_submitting(self, *_args, **_kwargs):
        raise RuntimeError("injected submitting persistence failure")


class TerminalFailureRepository(FakeRepository):
    def __init__(self, *, ambiguous_also=False):
        super().__init__()
        self.ambiguous_also = ambiguous_also

    def finish_job(self, *_args, **_kwargs):
        raise RuntimeError("injected terminal persistence failure")

    def mark_post_ambiguous(self, *args, **kwargs):
        if self.ambiguous_also:
            raise RuntimeError("injected ambiguous persistence failure")
        return FakeRepository.finish_job(
            self,
            *args,
            state="ambiguous",
            reason_code="post_result_ambiguous",
            response_kind=kwargs.get("response_kind", "persistence_uncertain"),
            snapshot=kwargs["snapshot"],
            cooldown_minutes=kwargs.get("cooldown_minutes", 60),
        )


class PathChangingState(FakeStateService):
    def observe(self, item_id, *, include_media, previous_snapshot=None):
        result = super().observe(
            item_id,
            include_media=include_media,
            previous_snapshot=previous_snapshot,
        )
        if len([call for call in self.calls if call[0] == item_id]) >= 2:
            result = copy.deepcopy(result)
            result["identity"]["exact_strm_path_hash"] = "changed-path"
        return result


class DisabledAtPreflightState(FakeStateService):
    def repair_eligibility(self, snapshot, *, active_job=None, **kwargs):
        item_calls = [call for call in self.calls if call[0] == snapshot["identity"]["exact_item_id"]]
        if len(item_calls) >= 2:
            return False, "repair_disabled"
        return super().repair_eligibility(snapshot, active_job=active_job, **kwargs)


class MediaInfoRemediationTests(unittest.TestCase):
    def make(self, state, repo, adapter, **kwargs):
        return MediaInfoRepairCoordinator(
            state_service=state,
            repository=repo,
            adapter_factory=lambda: adapter,
            readback_delay_seconds=0,
            heartbeat_interval_seconds=0.01,
            lease_seconds=1,
            shutdown_timeout_seconds=1,
            **kwargs,
        )

    def test_adapter_is_called_only_after_committed_submitting_attempt_one(self):
        repo = FakeRepository()
        adapter = LedgerCheckingAdapter(repo)
        coordinator = self.make(
            FakeStateService(readback_states={"a": ["media_streams_empty"] * 4}),
            repo,
            adapter,
        )
        try:
            job = coordinator.submit("a")["job"]
            self.assertTrue(wait_until(lambda: repo.get_by_id(job["id"])["state"] == "failed"))
        finally:
            coordinator.shutdown()
        self.assertEqual([("submitting", 1)], adapter.ledger_at_call)
        self.assertEqual(["a"], adapter.calls)

    def test_submitting_persistence_failure_means_zero_post(self):
        repo = SubmittingFailureRepository()
        adapter = RecordingAdapter(EMPTY)
        coordinator = self.make(
            FakeStateService(readback_states={"a": ["media_streams_empty"] * 3}),
            repo,
            adapter,
        )
        try:
            job = coordinator.submit("a")["job"]
            self.assertTrue(wait_until(lambda: repo.get_by_id(job["id"])["state"] == "skipped"))
        finally:
            coordinator.shutdown()
        self.assertEqual(0, repo.get_by_id(job["id"])["post_attempts"])
        self.assertEqual([], adapter.calls)

    def test_terminal_write_failure_becomes_ambiguous_without_second_post(self):
        repo = TerminalFailureRepository()
        adapter = RecordingAdapter(EMPTY)
        coordinator = self.make(
            FakeStateService(readback_states={"a": ["media_streams_empty"] * 4}),
            repo,
            adapter,
        )
        try:
            job = coordinator.submit("a")["job"]
            self.assertTrue(wait_until(lambda: repo.get_by_id(job["id"])["state"] == "ambiguous"))
        finally:
            coordinator.shutdown()
        self.assertEqual(1, repo.get_by_id(job["id"])["post_attempts"])
        self.assertEqual(["a"], adapter.calls)

    def test_double_terminal_write_failure_leaves_submitting_attempt_one(self):
        repo = TerminalFailureRepository(ambiguous_also=True)
        adapter = RecordingAdapter(EMPTY)
        coordinator = self.make(
            FakeStateService(readback_states={"a": ["media_streams_empty"] * 4}),
            repo,
            adapter,
        )
        try:
            job = coordinator.submit("a")["job"]
            self.assertTrue(wait_until(lambda: repo.get_by_id(job["id"])["state"] == "submitting"))
        finally:
            coordinator.shutdown()
        row = repo.get_by_id(job["id"])
        self.assertEqual(1, row["post_attempts"])
        self.assertEqual(["a"], adapter.calls)

    def test_ready_while_pending_is_noop_before_submit(self):
        repo = FakeRepository()
        state = FakeStateService(
            roots={"blocker": "root-a", "target": "root-b"},
            readback_states={
                "blocker": ["media_streams_empty"] * 5,
                "target": ["media_streams_empty", "ready"],
            },
        )
        started = threading.Event()
        release = threading.Event()
        adapter = RecordingAdapter(EMPTY, started, release)
        coordinator = self.make(state, repo, adapter, worker_count=1)
        try:
            blocker = coordinator.submit("blocker")["job"]
            self.assertTrue(started.wait(timeout=1))
            target = coordinator.submit("target")["job"]
            release.set()
            self.assertTrue(wait_until(lambda: repo.get_by_id(target["id"])["state"] == "skipped"))
        finally:
            release.set()
            coordinator.shutdown()
        self.assertEqual("no_op_before_submit", repo.get_by_id(target["id"])["reason_code"])
        self.assertEqual(["blocker"], adapter.calls)
        self.assertEqual(0, repo.get_by_id(target["id"])["post_attempts"])

    def test_path_change_and_disabled_flag_fail_before_post(self):
        for state, expected in (
            (PathChangingState(readback_states={"a": ["media_streams_empty"] * 3}), "path_identity_changed"),
            (DisabledAtPreflightState(readback_states={"a": ["media_streams_empty"] * 3}), "repair_disabled_before_submit"),
        ):
            with self.subTest(expected=expected):
                repo = FakeRepository()
                adapter = RecordingAdapter(EMPTY)
                coordinator = self.make(state, repo, adapter)
                try:
                    job = coordinator.submit("a")["job"]
                    self.assertTrue(wait_until(lambda: repo.get_by_id(job["id"])["state"] == "skipped"))
                finally:
                    coordinator.shutdown()
                self.assertEqual(expected, repo.get_by_id(job["id"])["reason_code"])
                self.assertEqual([], adapter.calls)

    def test_second_instance_does_not_recover_live_lease(self):
        repo = FakeRepository()
        old = FakeStateService().observe("old", include_media=True)
        _, row = repo.create_job(old, 1, 128, "live-owner", 90)
        repo.rows_by_id[row["id"]]["lease_expired"] = False
        coordinator = self.make(FakeStateService(), repo, RecordingAdapter(EMPTY), instance_id="peer")
        coordinator.start()
        try:
            self.assertEqual("pending", repo.get_by_id(row["id"])["state"])
        finally:
            coordinator.shutdown()


if __name__ == "__main__":
    unittest.main()
