import unittest

from handler import emby, tmdb


class SharedRetrySafetyTests(unittest.TestCase):
    def test_shared_retry_policies_only_allow_safe_methods(self):
        expected = frozenset({"HEAD", "GET", "OPTIONS", "TRACE"})

        emby_retry = emby.emby_client.session.get_adapter(
            "http://"
        ).max_retries
        tmdb_retry = tmdb.tmdb_session.get_adapter("http://").max_retries

        self.assertEqual(expected, frozenset(emby_retry.allowed_methods))
        self.assertEqual(expected, frozenset(tmdb_retry.allowed_methods))
        for method in {"POST", "PUT", "PATCH", "DELETE"}:
            self.assertNotIn(method, emby_retry.allowed_methods)
            self.assertNotIn(method, tmdb_retry.allowed_methods)


if __name__ == "__main__":
    unittest.main()
