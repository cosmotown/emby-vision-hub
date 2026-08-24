import os
import logging
import unittest
import uuid

import config_manager
import constants
from database import log_db
from database.connection import get_db_connection, init_db


POSTGRES_HOST = os.environ.get("EVH_TEST_POSTGRES_HOST")


@unittest.skipUnless(POSTGRES_HOST, "isolated PostgreSQL is not configured")
class ReviewCleanupPostgresTests(unittest.TestCase):
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
                    "EVH_TEST_POSTGRES_USER", os.environ.get("EVH_DB_USER", "evh_test")
                ),
                constants.CONFIG_OPTION_DB_PASSWORD: os.environ.get(
                    "EVH_TEST_POSTGRES_PASSWORD",
                    os.environ.get("EVH_DB_PASSWORD", "evh_test"),
                ),
                constants.CONFIG_OPTION_DB_NAME: os.environ.get(
                    "EVH_TEST_POSTGRES_DB", os.environ.get("EVH_DB_NAME", "evh_test")
                ),
            }
        )
        init_db()

    @classmethod
    def tearDownClass(cls):
        config_manager.APP_CONFIG.clear()
        config_manager.APP_CONFIG.update(cls.old_config)

    def setUp(self):
        self.prefix = f"v7214-{uuid.uuid4().hex}-"
        self.item_ids = [f"{self.prefix}{suffix}" for suffix in ("ready", "gone", "keep")]
        with get_db_connection() as conn:
            with conn.cursor() as cursor:
                for item_id in self.item_ids:
                    cursor.execute(
                        """
                        INSERT INTO failed_log (item_id, item_name, reason, item_type)
                        VALUES (%s, %s, %s, %s)
                        """,
                        (item_id, item_id, "test", "Movie"),
                    )
            conn.commit()

    def tearDown(self):
        with get_db_connection() as conn:
            with conn.cursor() as cursor:
                cursor.execute("DELETE FROM failed_log WHERE item_id LIKE %s", (f"{self.prefix}%",))
            conn.commit()

    def test_atomic_filtered_remove_preserves_non_candidate_rows(self):
        rows = log_db.get_all_review_items()
        visible = {row["item_id"] for row in rows if row["item_id"].startswith(self.prefix)}
        self.assertEqual(set(self.item_ids), visible)

        removed = log_db.remove_review_items(self.item_ids[:2])

        self.assertEqual(2, removed)
        with get_db_connection() as conn:
            with conn.cursor() as cursor:
                cursor.execute(
                    "SELECT item_id FROM failed_log WHERE item_id LIKE %s ORDER BY item_id",
                    (f"{self.prefix}%",),
                )
                remaining = [row["item_id"] for row in cursor.fetchall()]
        self.assertEqual([self.item_ids[2]], remaining)


if __name__ == "__main__":
    unittest.main()
