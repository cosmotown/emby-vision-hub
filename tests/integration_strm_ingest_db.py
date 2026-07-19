import os
import logging
import tempfile
import time
import unittest

import config_manager
import constants
from database import connection, strm_ingest_db


if not hasattr(logging.Logger, "trace"):
    logging.Logger.trace = logging.Logger.debug


@unittest.skipUnless(os.environ.get("TEST_STRM_DATABASE") == "1", "requires isolated PostgreSQL")
class StrmIngestDatabaseTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        config_manager.APP_CONFIG.update({
            constants.CONFIG_OPTION_DB_HOST: os.environ.get("DB_HOST", "127.0.0.1"),
            constants.CONFIG_OPTION_DB_PORT: int(os.environ.get("DB_PORT", "5432")),
            constants.CONFIG_OPTION_DB_USER: os.environ.get("DB_USER", "embytoolkit"),
            constants.CONFIG_OPTION_DB_PASSWORD: os.environ.get("DB_PASSWORD", "embytoolkit"),
            constants.CONFIG_OPTION_DB_NAME: os.environ.get("DB_NAME", "embytoolkit"),
        })
        connection.init_db()

    def setUp(self):
        with connection.get_db_connection() as conn:
            with conn.cursor() as cursor:
                cursor.execute("TRUNCATE strm_ingest_retry_queue, strm_ingest_inventory_roots RESTART IDENTITY")

    @staticmethod
    def _write(path, content):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(content)
        return path

    def test_inventory_detects_add_change_remove_and_same_fingerprint_revival(self):
        with tempfile.TemporaryDirectory() as root:
            first = self._write(os.path.join(root, "Movie", "A.strm"), "one")
            second = self._write(os.path.join(root, "Movie", "A-remux.strm"), "two")
            initial = {
                path: (os.path.getsize(path), os.path.getmtime(path))
                for path in (first, second)
            }
            baseline = strm_ingest_db.reconcile_inventory(root, initial)
            self.assertFalse(baseline["initialized"])
            self.assertEqual(2, baseline["seeded"])

            os.remove(first)
            current = {second: initial[second]}
            removed = strm_ingest_db.reconcile_inventory(root, current)
            self.assertEqual([first], removed["removed"])
            self.assertEqual([], removed["added"])

            strm_ingest_db.mark_deleted([first])
            self._write(first, "one")
            revived = strm_ingest_db.reconcile_inventory(root, {
                first: initial[first],
                second: initial[second],
            })
            self.assertEqual([first], revived["added"])

            time.sleep(0.01)
            self._write(second, "changed-content")
            changed = strm_ingest_db.reconcile_inventory(root, {
                first: (os.path.getsize(first), os.path.getmtime(first)),
                second: (os.path.getsize(second), os.path.getmtime(second)),
            })
            self.assertEqual([second], changed["changed"])

    def test_retry_stops_after_three_attempts_and_can_be_manually_restarted(self):
        with tempfile.TemporaryDirectory() as root:
            path = self._write(os.path.join(root, "Show", "S01E01.strm"), "url")
            result = strm_ingest_db.enqueue_paths(
                [path], source="test", last_error="not indexed", initial_delay_seconds=0
            )
            self.assertEqual(1, result["queued"])

            for expected_attempt in (1, 2, 3):
                with connection.get_db_connection() as conn:
                    with conn.cursor() as cursor:
                        cursor.execute(
                            "UPDATE strm_ingest_retry_queue SET next_attempt_at = NOW() WHERE file_path = %s",
                            (path,),
                        )
                claimed = strm_ingest_db.claim_due_paths()
                self.assertEqual([path], [item["file_path"] for item in claimed])
                strm_ingest_db.mark_failed_attempts([path], "still missing")
                row = strm_ingest_db.list_recent()[0]
                self.assertEqual(expected_attempt, row["attempt_count"])

            self.assertEqual("failed", row["status"])
            self.assertEqual([], strm_ingest_db.claim_due_paths())
            self.assertTrue(strm_ingest_db.retry_path(row["id"]))
            restarted = strm_ingest_db.claim_due_paths()
            self.assertEqual(0, restarted[0]["attempt_count"])


if __name__ == "__main__":
    unittest.main()
