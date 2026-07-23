import sys
import types
import unittest
from unittest import mock


class _UndefinedTable(Exception):
    pass


class _OperationalError(Exception):
    pass


psycopg2_stub = types.ModuleType("psycopg2")
psycopg2_stub.Error = Exception
psycopg2_stub.OperationalError = _OperationalError
psycopg2_stub.errors = types.SimpleNamespace(UndefinedTable=_UndefinedTable)
psycopg2_stub.extensions = types.SimpleNamespace(connection=object)
psycopg2_extras_stub = types.ModuleType("psycopg2.extras")
psycopg2_extras_stub.RealDictCursor = object
sys.modules.setdefault("psycopg2", psycopg2_stub)
sys.modules.setdefault("psycopg2.extras", psycopg2_extras_stub)
sys.modules.setdefault("pytz", types.ModuleType("pytz"))

from database import settings_db


class _FailingCursor:
    def __init__(self, error):
        self.error = error

    def execute(self, *_args, **_kwargs):
        raise self.error


class _FakeConnection:
    def __init__(self, error):
        self.error = error

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def cursor(self):
        return _FailingCursor(self.error)


class SettingsDbTests(unittest.TestCase):
    def test_missing_app_settings_table_is_first_run_empty_state(self):
        # Other test modules may have imported the real psycopg2 package before
        # this module is collected.  Always raise the exception class currently
        # used by settings_db so the test is independent of discovery order.
        connection = _FakeConnection(settings_db.psycopg2.errors.UndefinedTable())
        with mock.patch.object(settings_db, "get_db_connection", return_value=connection):
            self.assertIsNone(settings_db.get_setting("dynamic_app_config"))

    def test_other_database_errors_are_not_hidden(self):
        connection = _FakeConnection(_OperationalError("database unavailable"))
        with mock.patch.object(settings_db, "get_db_connection", return_value=connection):
            with self.assertRaises(_OperationalError):
                settings_db.get_setting("dynamic_app_config")


if __name__ == "__main__":
    unittest.main()
