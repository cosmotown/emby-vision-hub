import unittest
from unittest import mock

from flask import Flask

import config_manager
from routes.database_admin import db_admin_bp


class ReviewCleanupRouteSafetyTests(unittest.TestCase):
    def setUp(self):
        self.old_config = dict(config_manager.APP_CONFIG)
        config_manager.APP_CONFIG.clear()
        config_manager.APP_CONFIG["auth_enabled"] = False
        self.app = Flask(__name__)
        self.app.secret_key = "test"
        self.app.register_blueprint(db_admin_bp)
        self.client = self.app.test_client()

    def tearDown(self):
        config_manager.APP_CONFIG.clear()
        config_manager.APP_CONFIG.update(self.old_config)

    @mock.patch("routes.database_admin.log_db.clear_all_review_items")
    def test_legacy_unconditional_clear_is_fail_closed(self, clear_all):
        response = self.client.post("/api/actions/clear_review_items")
        self.assertEqual(409, response.status_code)
        self.assertEqual(
            "unsafe_bulk_clear_disabled",
            response.get_json()["reason_code"],
        )
        clear_all.assert_not_called()


if __name__ == "__main__":
    unittest.main()
