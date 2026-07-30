import logging
import threading
import unittest
from unittest import mock

import constants
import handler.moviepilot as moviepilot


class Resp:
    def __init__(self, code=200, data=None, text=""):
        self.status_code = code
        self._data = data
        self.text = text

    def json(self):
        return self._data

    def raise_for_status(self):
        if self.status_code >= 400:
            raise moviepilot.requests.HTTPError(str(self.status_code))


class MoviePilotTransportSafetyTests(unittest.TestCase):
    def setUp(self):
        self.cfg = {
            constants.CONFIG_OPTION_MOVIEPILOT_URL: "http://moviepilot",
            constants.CONFIG_OPTION_MOVIEPILOT_USERNAME: "user",
            constants.CONFIG_OPTION_MOVIEPILOT_PASSWORD: "secret",
        }
        moviepilot._SUBSCRIPTION_LOCKS.clear()
        self.trace_patcher = mock.patch.object(
            moviepilot.logger, "trace", create=True
        )
        self.trace_patcher.start()

    def tearDown(self):
        self.trace_patcher.stop()
        moviepilot._SUBSCRIPTION_LOCKS.clear()

    @staticmethod
    def details(state="P", total=12):
        return {
            "id": 312,
            "state": state,
            "total_episode": total,
            "lack_episode": 0,
            "_detail_payload": {
                "id": 312,
                "state": state,
                "total_episode": total,
                "lack_episode": 0,
            },
        }

    def test_all_moviepilot_mutations_disable_redirects(self):
        with mock.patch.object(
            moviepilot, "_get_access_token", return_value="token"
        ), mock.patch.object(
            moviepilot.requests, "post", return_value=Resp(201)
        ) as post:
            self.assertTrue(
                moviepilot.subscribe_with_custom_payload(
                    {"tmdbid": 100, "season": 1, "name": "x"}, self.cfg
                )
            )
        self.assertIs(False, post.call_args.kwargs["allow_redirects"])

        with mock.patch.object(
            moviepilot, "_get_access_token", return_value="token"
        ), mock.patch.object(
            moviepilot.requests, "delete", return_value=Resp(204)
        ) as delete:
            self.assertTrue(
                moviepilot.cancel_subscription("100", "Series", self.cfg, season=1)
            )
        self.assertIs(False, delete.call_args.kwargs["allow_redirects"])

        with mock.patch.object(
            moviepilot,
            "get_subscription_details",
            return_value=self.details("P", 12),
        ), mock.patch.object(
            moviepilot, "_get_access_token", return_value="token"
        ), mock.patch.object(
            moviepilot.requests, "put", return_value=Resp(204)
        ) as put:
            self.assertTrue(
                moviepilot.update_subscription_status(100, 1, "R", self.cfg, 12)
            )
        self.assertIs(False, put.call_args.kwargs["allow_redirects"])

    def test_login_post_disables_redirects(self):
        with mock.patch.object(
            moviepilot.requests,
            "post",
            return_value=Resp(200, {"access_token": "token"}),
        ) as post:
            self.assertEqual("token", moviepilot._get_access_token(self.cfg))
        self.assertIs(False, post.call_args.kwargs["allow_redirects"])

    def test_error_body_is_not_written_to_logs(self):
        secret_body = "authorization=Bearer leaked-secret"
        with mock.patch.object(
            moviepilot, "_get_access_token", return_value="token"
        ), mock.patch.object(
            moviepilot.requests, "post", return_value=Resp(500, text=secret_body)
        ), self.assertLogs(moviepilot.logger, level=logging.ERROR) as captured:
            self.assertFalse(
                moviepilot.subscribe_with_custom_payload(
                    {"tmdbid": 100, "name": "x"}, self.cfg
                )
            )
        self.assertNotIn(secret_body, "\n".join(captured.output))

        with mock.patch.object(
            moviepilot, "_get_access_token", return_value="token"
        ), mock.patch.object(
            moviepilot.requests,
            "get",
            return_value=Resp(500, text=secret_body),
        ):
            with self.assertRaises(moviepilot.MoviePilotSubscriptionLookupError) as caught:
                moviepilot.get_subscription_details("100", "Series", self.cfg, season=1)
        self.assertNotIn(secret_body, str(caught.exception))

    def test_timeout_and_5xx_mutations_are_not_replayed(self):
        with mock.patch.object(
            moviepilot, "_get_access_token", return_value="token"
        ), mock.patch.object(
            moviepilot.requests,
            "post",
            side_effect=moviepilot.requests.Timeout("unknown delivery"),
        ) as post:
            self.assertFalse(
                moviepilot.subscribe_with_custom_payload(
                    {"tmdbid": 100, "season": 1, "name": "x"},
                    self.cfg,
                )
            )
        self.assertEqual(1, post.call_count)

        with mock.patch.object(
            moviepilot,
            "get_subscription_details",
            return_value=self.details("P", 12),
        ), mock.patch.object(
            moviepilot, "_get_access_token", return_value="token"
        ), mock.patch.object(
            moviepilot.requests, "put", return_value=Resp(503)
        ) as put:
            self.assertFalse(
                moviepilot.update_subscription_status(
                    100, 1, "R", self.cfg, 12
                )
            )
        self.assertEqual(1, put.call_count)

    def test_stale_caller_details_are_ignored_under_lock(self):
        stale = self.details("R", 12)
        fresh = self.details("P", 12)
        with mock.patch.object(
            moviepilot, "get_subscription_details", return_value=fresh
        ) as lookup, mock.patch.object(
            moviepilot, "_get_access_token", return_value="token"
        ), mock.patch.object(
            moviepilot.requests, "put", return_value=Resp(204)
        ) as put:
            self.assertTrue(
                moviepilot.update_subscription_status(
                    100,
                    1,
                    "R",
                    self.cfg,
                    12,
                    subscription_details=stale,
                )
            )
        lookup.assert_called_once()
        put.assert_called_once()

    def test_concurrent_same_subscription_performs_one_put(self):
        state = {"value": "P"}
        first_put_entered = threading.Event()
        release_first_put = threading.Event()
        results = []
        lookup_calls = []

        def lookup(*_args, **_kwargs):
            lookup_calls.append(state["value"])
            return self.details(state["value"], 12)

        def put(url, **kwargs):
            first_put_entered.set()
            release_first_put.wait(2)
            state["value"] = kwargs["params"]["state"]
            return Resp(204)

        def run():
            results.append(
                moviepilot.update_subscription_status(100, 1, "R", self.cfg, 12)
            )

        with mock.patch.object(
            moviepilot, "get_subscription_details", side_effect=lookup
        ), mock.patch.object(
            moviepilot, "_get_access_token", return_value="token"
        ), mock.patch.object(
            moviepilot.requests, "put", side_effect=put
        ) as put_mock:
            first = threading.Thread(target=run)
            second = threading.Thread(target=run)
            first.start()
            self.assertTrue(first_put_entered.wait(1))
            second.start()
            release_first_put.set()
            first.join(2)
            second.join(2)

        self.assertEqual([True, True], sorted(results))
        self.assertEqual(1, put_mock.call_count)
        self.assertEqual(["P", "R"], lookup_calls)
        self.assertEqual({}, moviepilot._SUBSCRIPTION_LOCKS)


if __name__ == "__main__":
    unittest.main()
