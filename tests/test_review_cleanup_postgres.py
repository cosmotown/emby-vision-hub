import os
import logging
import unittest
import uuid
from unittest import mock

import config_manager
import constants
from database import log_db
from database.connection import get_db_connection, init_db
from services.mediainfo_state import MediaInfoStateService
from services.review_cleanup import ReviewCleanupService


POSTGRES_HOST = os.environ.get("EVH_TEST_POSTGRES_HOST")


class FakeResponse:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


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
                constants.CONFIG_OPTION_EMBY_SERVER_URL: "http://emby.invalid",
                constants.CONFIG_OPTION_EMBY_API_KEY: "secret-token",
            }
        )
        init_db()

    @classmethod
    def tearDownClass(cls):
        config_manager.APP_CONFIG.clear()
        config_manager.APP_CONFIG.update(cls.old_config)

    def setUp(self):
        self.prefix = f"v7215-{uuid.uuid4().hex}-"
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

    @mock.patch("services.mediainfo_state.emby.emby_client.get")
    def test_stale_series_without_coordinate_is_removed_but_lookup_failure_is_preserved(
        self, emby_get
    ):
        gone_id = self.item_ids[1]
        unavailable_id = self.item_ids[2]
        with get_db_connection() as conn:
            with conn.cursor() as cursor:
                cursor.execute(
                    """
                    UPDATE failed_log
                    SET item_type = 'Series', reason = '处理评分 (4.00) 低于阈值'
                    WHERE item_id = ANY(%s)
                    """,
                    ([gone_id, unavailable_id],),
                )
                cursor.execute(
                    """
                    SELECT item_id, item_name, failed_at, reason, item_type, score
                    FROM failed_log
                    WHERE item_id = ANY(%s)
                    ORDER BY item_id
                    """,
                    ([gone_id, unavailable_id],),
                )
                rows = [dict(row) for row in cursor.fetchall()]
            conn.commit()

        def exact_lookup(_url, *, params, headers):
            self.assertEqual("secret-token", headers["X-Emby-Token"])
            if params["Ids"] == gone_id:
                return FakeResponse({"Items": []})
            raise RuntimeError("temporary Emby outage")

        emby_get.side_effect = exact_lookup
        service = ReviewCleanupService(MediaInfoStateService())
        with mock.patch(
            "services.review_cleanup.log_db.get_all_review_items",
            return_value=rows,
        ):
            preview = service.preview("historical_item_missing")
            result = service.execute("historical_item_missing")

        self.assertEqual(1, preview["candidate_count"])
        self.assertEqual(1, result["candidate_count"])
        self.assertEqual(1, result["removed_count"])
        with get_db_connection() as conn:
            with conn.cursor() as cursor:
                cursor.execute(
                    "SELECT item_id FROM failed_log WHERE item_id = ANY(%s) ORDER BY item_id",
                    ([gone_id, unavailable_id],),
                )
                remaining = [row["item_id"] for row in cursor.fetchall()]
        self.assertEqual([unavailable_id], remaining)


if __name__ == "__main__":
    unittest.main()
