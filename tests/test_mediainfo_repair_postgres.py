import os
import logging
import threading
import time
import unittest
from datetime import datetime, timedelta, timezone

import config_manager
import constants
from database import media_info_repair_db as repository
from database.connection import get_db_connection, init_db
from services.mediainfo_repair_queue import MediaInfoRepairCoordinator
from services.shenyi_mediainfo import SyncResult
from tests.test_mediainfo_repair_queue import FakeStateService, wait_until


POSTGRES_HOST = os.environ.get("EVH_TEST_POSTGRES_HOST")


def snapshot(item_id, path_hash=None, root=None):
    return {
        "identity": {
            "exact_item_id": item_id,
            "item_type": "Episode",
            "exact_strm_path_hash": path_hash or f"path-{item_id}",
            "root_series_key": root or f"root-{item_id}",
            "redacted_path_hint": f"{item_id}.strm",
        },
        "strm_status": {"status": "present"},
        "emby_index_status": {"status": "indexed"},
        "shenyi_persist_status": {"status": "missing"},
        "emby_media_status": {"status": "media_streams_empty"},
        "snapshot_fingerprint": f"fp-{item_id}",
    }


class InspectingAdapter:
    def __init__(self):
        self.calls = []
        self.ledger_at_post = []
        self.lock = threading.Lock()

    def sync_item(self, item_id):
        row = repository.get_by_item_id(item_id)
        with self.lock:
            self.calls.append(item_id)
            self.ledger_at_post.append((row["state"], row["post_attempts"]))
        return SyncResult("failed", "sync_empty_result", "empty_array", 200, 2, "x")


class RepositoryFault:
    def __init__(self, *, fail_submitting=False, fail_finish=False, fail_ambiguous=False):
        self.fail_submitting = fail_submitting
        self.fail_finish = fail_finish
        self.fail_ambiguous = fail_ambiguous

    def __getattr__(self, name):
        return getattr(repository, name)

    def mark_submitting(self, *args, **kwargs):
        if self.fail_submitting:
            raise RuntimeError("injected submitting persistence failure")
        return repository.mark_submitting(*args, **kwargs)

    def finish_job(self, *args, **kwargs):
        if self.fail_finish:
            raise RuntimeError("injected terminal persistence failure")
        return repository.finish_job(*args, **kwargs)

    def mark_post_ambiguous(self, *args, **kwargs):
        if self.fail_ambiguous:
            raise RuntimeError("injected ambiguous persistence failure")
        return repository.mark_post_ambiguous(*args, **kwargs)


@unittest.skipUnless(POSTGRES_HOST, "isolated PostgreSQL is not configured")
class MediaInfoRepairPostgresTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not hasattr(logging.Logger, "trace"):
            logging.Logger.trace = logging.Logger.debug
        cls.old_config = dict(config_manager.APP_CONFIG)
        config_manager.APP_CONFIG.update(
            {
                constants.CONFIG_OPTION_DB_HOST: POSTGRES_HOST,
                constants.CONFIG_OPTION_DB_PORT: int(
                    os.environ.get("EVH_TEST_POSTGRES_PORT", "5432")
                ),
                constants.CONFIG_OPTION_DB_USER: os.environ.get(
                    "EVH_TEST_POSTGRES_USER", "evh_test"
                ),
                constants.CONFIG_OPTION_DB_PASSWORD: os.environ.get(
                    "EVH_TEST_POSTGRES_PASSWORD", "evh_test"
                ),
                constants.CONFIG_OPTION_DB_NAME: os.environ.get(
                    "EVH_TEST_POSTGRES_DB", "evh_test"
                ),
            }
        )
        init_db()

    @classmethod
    def tearDownClass(cls):
        config_manager.APP_CONFIG.clear()
        config_manager.APP_CONFIG.update(cls.old_config)

    def setUp(self):
        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("DELETE FROM media_info_repair_jobs")
            conn.commit()

    def _race(self, snapshots):
        barrier = threading.Barrier(len(snapshots))
        results = []
        lock = threading.Lock()

        def create(index, value):
            generation = repository.next_generation()
            barrier.wait(timeout=5)
            result = repository.create_job(
                value,
                generation,
                128,
                f"instance-{index}",
                90,
            )
            with lock:
                results.append(result)

        threads = [
            threading.Thread(target=create, args=(index, value))
            for index, value in enumerate(snapshots)
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=10)
        self.assertTrue(all(not thread.is_alive() for thread in threads))
        return results

    def test_fifty_way_same_item_admission_is_one_active_row(self):
        results = self._race([snapshot("same-item") for _ in range(50)])
        self.assertEqual(1, sum(result == "created" for result, _ in results))
        self.assertEqual(
            49,
            sum(result == "same_item_active" for result, _ in results),
        )
        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT COUNT(*) AS total FROM media_info_repair_jobs "
                "WHERE state IN ('pending','running','submitting')"
            )
            self.assertEqual(1, cursor.fetchone()["total"])

    def test_same_path_and_same_root_are_database_atomic(self):
        path_results = self._race(
            [
                snapshot("path-a", "shared-path", "root-a"),
                snapshot("path-b", "shared-path", "root-b"),
            ]
        )
        self.assertEqual(1, sum(result == "created" for result, _ in path_results))
        self.assertEqual(
            1, sum(result == "same_path_active" for result, _ in path_results)
        )
        self.setUp()
        root_results = self._race(
            [
                snapshot("root-a", "path-a", "shared-root"),
                snapshot("root-b", "path-b", "shared-root"),
            ]
        )
        self.assertEqual(1, sum(result == "created" for result, _ in root_results))
        self.assertEqual(
            1, sum(result == "same_root_active" for result, _ in root_results)
        )

    def _two_coordinator_post_race(self, item_ids, state):
        adapter = InspectingAdapter()
        coordinators = [
            MediaInfoRepairCoordinator(
                state_service=state,
                repository=repository,
                adapter_factory=lambda: adapter,
                instance_id=f"coordinator-{index}",
                readback_delay_seconds=0,
                heartbeat_interval_seconds=0.05,
                lease_seconds=1,
            )
            for index in range(2)
        ]
        for coordinator in coordinators:
            coordinator.start()
        barrier = threading.Barrier(2)
        results = []

        def submit(index):
            barrier.wait(timeout=5)
            results.append(coordinators[index].submit(item_ids[index]))

        threads = [threading.Thread(target=submit, args=(index,)) for index in range(2)]
        try:
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=10)
            self.assertTrue(all(not thread.is_alive() for thread in threads))
            self.assertTrue(wait_until(lambda: len(adapter.calls) == 1, timeout=5))
            self.assertTrue(
                wait_until(
                    lambda: not any(
                        (repository.get_by_item_id(item_id) or {}).get("state")
                        in {"pending", "running", "submitting"}
                        for item_id in set(item_ids)
                    ),
                    timeout=5,
                )
            )
        finally:
            for coordinator in coordinators:
                coordinator.shutdown()
        self.assertEqual(1, sum(result["result"] == "accepted" for result in results))
        self.assertEqual(1, sum(result["result"] == "existing" for result in results))
        self.assertEqual(1, len(adapter.calls))
        self.assertEqual([("submitting", 1)], adapter.ledger_at_post)

    def test_two_coordinators_post_once_for_item_path_and_root_conflicts(self):
        scenarios = [
            (
                ["same-item", "same-item"],
                FakeStateService(
                    readback_states={"same-item": ["media_streams_empty"] * 8}
                ),
            ),
            (
                ["path-a", "path-b"],
                FakeStateService(
                    roots={"path-a": "root-a", "path-b": "root-b"},
                    path_hashes={"path-a": "shared-path", "path-b": "shared-path"},
                    readback_states={
                        "path-a": ["media_streams_empty"] * 4,
                        "path-b": ["media_streams_empty"] * 4,
                    },
                ),
            ),
            (
                ["root-a", "root-b"],
                FakeStateService(
                    roots={"root-a": "shared-root", "root-b": "shared-root"},
                    readback_states={
                        "root-a": ["media_streams_empty"] * 4,
                        "root-b": ["media_streams_empty"] * 4,
                    },
                ),
            ),
        ]
        for item_ids, state in scenarios:
            with self.subTest(item_ids=item_ids):
                self.setUp()
                self._two_coordinator_post_race(item_ids, state)

    def test_active_partial_unique_indexes_exist(self):
        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT indexname, indexdef FROM pg_indexes "
                "WHERE tablename = 'media_info_repair_jobs'"
            )
            indexes = {row["indexname"]: row["indexdef"] for row in cursor.fetchall()}
        for name in (
            "uq_media_info_repair_active_item",
            "uq_media_info_repair_active_path",
            "uq_media_info_repair_active_root",
        ):
            self.assertIn(name, indexes)
            self.assertIn("pending", indexes[name])
            self.assertIn("running", indexes[name])
            self.assertIn("submitting", indexes[name])

    def test_sequence_generations_are_unique_across_connections(self):
        values = []
        lock = threading.Lock()
        threads = []

        def allocate():
            value = repository.next_generation()
            with lock:
                values.append(value)

        for _ in range(20):
            threads.append(threading.Thread(target=allocate))
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=5)
        self.assertEqual(20, len(values))
        self.assertEqual(20, len(set(values)))

    def test_live_lease_is_untouched_and_expired_attempt_is_ambiguous(self):
        generation = repository.next_generation()
        _, row = repository.create_job(
            snapshot("leased"), generation, 128, "owner-a", 90
        )
        repository.mark_running(row["id"], generation, "owner-a", 90)
        live = repository.recover_expired_jobs("owner-b", 60)
        self.assertEqual(0, live["safe_terminalized"])
        self.assertEqual("running", repository.get_by_id(row["id"])["state"])
        preflight = snapshot("leased")
        repository.mark_submitting(
            row["id"], generation, "owner-a", preflight, 90
        )
        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "UPDATE media_info_repair_jobs SET lease_expires_at = %s WHERE id = %s",
                (datetime.now(timezone.utc) - timedelta(seconds=1), row["id"]),
            )
            conn.commit()
        recovered = repository.recover_expired_jobs("owner-b", 60)
        self.assertEqual(1, recovered["ambiguous"])
        final = repository.get_by_id(row["id"])
        self.assertEqual("ambiguous", final["state"])
        self.assertEqual(1, final["post_attempts"])

    def test_post_observes_committed_attempt_and_terminal_failure_is_ambiguous(self):
        state = FakeStateService(
            readback_states={"one": ["media_streams_empty"] * 4}
        )
        adapter = InspectingAdapter()
        coordinator = MediaInfoRepairCoordinator(
            state_service=state,
            repository=RepositoryFault(fail_finish=True),
            adapter_factory=lambda: adapter,
            readback_delay_seconds=0,
            heartbeat_interval_seconds=0.05,
            lease_seconds=1,
        )
        try:
            result = coordinator.submit("one")
            self.assertEqual("accepted", result["result"])
            job_id = result["job"]["id"]
            self.assertTrue(
                wait_until(
                    lambda: repository.get_by_id(job_id)["state"] == "ambiguous",
                    timeout=5,
                )
            )
        finally:
            coordinator.shutdown()
        self.assertEqual([("submitting", 1)], adapter.ledger_at_post)
        self.assertEqual(["one"], adapter.calls)

    def test_submitting_write_failure_produces_zero_post(self):
        state = FakeStateService(
            readback_states={"one": ["media_streams_empty"] * 4}
        )
        adapter = InspectingAdapter()
        coordinator = MediaInfoRepairCoordinator(
            state_service=state,
            repository=RepositoryFault(fail_submitting=True),
            adapter_factory=lambda: adapter,
            readback_delay_seconds=0,
            heartbeat_interval_seconds=0.05,
            lease_seconds=1,
        )
        try:
            result = coordinator.submit("one")
            job_id = result["job"]["id"]
            self.assertTrue(
                wait_until(
                    lambda: repository.get_by_id(job_id)["state"] == "skipped",
                    timeout=5,
                )
            )
        finally:
            coordinator.shutdown()
        row = repository.get_by_id(job_id)
        self.assertEqual(0, row["post_attempts"])
        self.assertEqual([], adapter.calls)

    def test_double_persistence_failure_leaves_submitting_and_recovery_never_posts(self):
        state = FakeStateService(
            readback_states={"one": ["media_streams_empty"] * 4}
        )
        adapter = InspectingAdapter()
        coordinator = MediaInfoRepairCoordinator(
            state_service=state,
            repository=RepositoryFault(fail_finish=True, fail_ambiguous=True),
            adapter_factory=lambda: adapter,
            readback_delay_seconds=0,
            heartbeat_interval_seconds=0.05,
            lease_seconds=1,
        )
        try:
            result = coordinator.submit("one")
            job_id = result["job"]["id"]
            self.assertTrue(
                wait_until(
                    lambda: repository.get_by_id(job_id)["state"] == "submitting",
                    timeout=5,
                )
            )
        finally:
            coordinator.shutdown()
        self.assertEqual(["one"], adapter.calls)
        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "UPDATE media_info_repair_jobs SET lease_expires_at = NOW() - INTERVAL '1 second' WHERE id = %s",
                (job_id,),
            )
            conn.commit()
        repository.recover_expired_jobs("new-instance", 60)
        self.assertEqual("ambiguous", repository.get_by_id(job_id)["state"])
        self.assertEqual(["one"], adapter.calls)

    def test_cleanup_removes_only_old_terminal_rows(self):
        now = datetime.now(timezone.utc)
        with get_db_connection() as conn:
            cursor = conn.cursor()
            rows = [
                ("old-terminal", "failed", now - timedelta(days=100), None),
                ("active", "pending", now - timedelta(days=100), None),
                (
                    "cooldown-ambiguous",
                    "ambiguous",
                    now - timedelta(days=100),
                    now + timedelta(hours=1),
                ),
                ("recent-terminal", "failed", now - timedelta(days=1), None),
            ]
            for item_id, state, updated_at, retry_after in rows:
                cursor.execute(
                    """
                    INSERT INTO media_info_repair_jobs (
                        exact_item_id, item_type, exact_strm_path_hash,
                        root_series_key, state, retry_after, completed_at,
                        updated_at
                    ) VALUES (%s, 'Episode', %s, %s, %s, %s, %s, %s)
                    """,
                    (
                        item_id,
                        f"path-{item_id}",
                        f"root-{item_id}",
                        state,
                        retry_after,
                        updated_at,
                        updated_at,
                    ),
                )
            conn.commit()
        self.assertEqual(1, repository.cleanup_terminal_jobs(90, 10))
        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT exact_item_id FROM media_info_repair_jobs")
            remaining = {row["exact_item_id"] for row in cursor.fetchall()}
        self.assertNotIn("old-terminal", remaining)
        self.assertIn("active", remaining)
        self.assertIn("cooldown-ambiguous", remaining)
        self.assertIn("recent-terminal", remaining)


if __name__ == "__main__":
    unittest.main()
