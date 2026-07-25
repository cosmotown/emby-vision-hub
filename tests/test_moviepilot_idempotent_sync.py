import unittest
from unittest import mock
import constants
import handler.moviepilot as moviepilot
import watchlist_processor

class Resp:
    def __init__(self, code=200, data=None, text=""):
        self.status_code = code
        self._data = data
        self.text = text
    def json(self):
        return self._data

class UpdateTests(unittest.TestCase):
    def setUp(self):
        self.cfg = {
            constants.CONFIG_OPTION_MOVIEPILOT_URL:
                "http://moviepilot"
        }

    def details(self, state="R", total=12, lack=0):
        return {
            "id": 312,
            "state": state,
            "total_episode": total,
            "lack_episode": lack,
            "_detail_payload": {
                "id": 312,
                "state": state,
                "total_episode": total,
                "lack_episode": lack,
            },
        }

    def test_full_details(self):
        with mock.patch.object(
            moviepilot, "_get_access_token", return_value="t"
        ), mock.patch.object(
            moviepilot.requests, "get",
            side_effect=[
                Resp(200, {"id": 312}),
                Resp(200, {
                    "id": 312, "state": "r",
                    "total_episode": "12",
                    "lack_episode": "3",
                }),
            ],
        ):
            d = moviepilot.get_subscription_details(
                "100", "Series", self.cfg, season=1
            )
        self.assertEqual(
            (d["id"], d["state"], d["total_episode"],
             d["lack_episode"]),
            (312, "R", 12, 3),
        )

    def test_r_to_r_zero_put(self):
        with mock.patch.object(
            moviepilot, "_get_access_token"
        ) as token, mock.patch.object(
            moviepilot.requests, "put"
        ) as put:
            ok = moviepilot.update_subscription_status(
                100, 1, "R", self.cfg, 12, self.details()
            )
        self.assertTrue(ok)
        token.assert_not_called()
        put.assert_not_called()

    def test_p_to_r_one_status_put(self):
        with mock.patch.object(
            moviepilot, "_get_access_token", return_value="t"
        ), mock.patch.object(
            moviepilot.requests, "put",
            return_value=Resp(200),
        ) as put:
            ok = moviepilot.update_subscription_status(
                100, 1, "R", self.cfg, 12,
                self.details("P", 12),
            )
        self.assertTrue(ok)
        self.assertEqual(put.call_count, 1)
        self.assertIn(
            "/status/312", put.call_args.args[0]
        )

    def test_total_only_one_detail_put(self):
        with mock.patch.object(
            moviepilot, "_get_access_token", return_value="t"
        ), mock.patch.object(
            moviepilot.requests, "put",
            return_value=Resp(200),
        ) as put:
            ok = moviepilot.update_subscription_status(
                100, 1, "R", self.cfg, 12,
                self.details("R", 10, 2),
            )
        self.assertTrue(ok)
        self.assertEqual(put.call_count, 1)
        self.assertEqual(
            put.call_args.args[0],
            "http://moviepilot/api/v1/subscribe/",
        )

    def test_state_and_total_each_one_put(self):
        with mock.patch.object(
            moviepilot, "_get_access_token", return_value="t"
        ), mock.patch.object(
            moviepilot.requests, "put",
            side_effect=[Resp(200), Resp(200)],
        ) as put:
            ok = moviepilot.update_subscription_status(
                100, 1, "R", self.cfg, 12,
                self.details("P", 99, 90),
            )
        self.assertTrue(ok)
        self.assertEqual(put.call_count, 2)
        self.assertEqual(
            [c.args[0] for c in put.call_args_list],
            [
                "http://moviepilot/api/v1/subscribe/",
                "http://moviepilot/api/v1/subscribe/status/312",
            ],
        )

class SyncTests(unittest.TestCase):
    def processor(self):
        p = watchlist_processor.WatchlistProcessor.__new__(
            watchlist_processor.WatchlistProcessor
        )
        p.config = {}
        return p

    def cfg(self):
        return {
            "sync_mp_subscription": True,
            "auto_pause": 0,
            "auto_pending": {
                "default_total_episodes": 99
            },
        }

    def test_old_missing_season_not_subscribed(self):
        series = {"seasons": [
            {"season_number": 1, "episode_count": 10},
            {"season_number": 2, "episode_count": 8},
        ]}
        with mock.patch.object(
            watchlist_processor.settings_db,
            "get_setting", return_value=self.cfg()
        ), mock.patch.object(
            watchlist_processor.moviepilot,
            "get_subscription_details",
            side_effect=[
                None,
                {
                    "id": 2, "state": "R",
                    "total_episode": 8,
                    "lack_episode": 0,
                },
            ],
        ), mock.patch.object(
            watchlist_processor.moviepilot,
            "subscribe_series_to_moviepilot",
        ) as sub, mock.patch.object(
            watchlist_processor.moviepilot,
            "update_subscription_status",
        ) as update:
            self.processor()._sync_status_to_moviepilot(
                "100", "测试剧", series,
                watchlist_processor.STATUS_WATCHING
            )
        sub.assert_not_called()
        update.assert_not_called()

    def test_latest_missing_season_auto_subscribed(self):
        series = {"seasons": [
            {"season_number": 2, "episode_count": 8},
        ]}
        after = {
            "id": 2, "state": "P",
            "total_episode": 99,
            "lack_episode": 90,
            "_detail_payload": {
                "id": 2, "state": "P",
                "total_episode": 99,
                "lack_episode": 90,
            },
        }
        with mock.patch.object(
            watchlist_processor.settings_db,
            "get_setting", return_value=self.cfg()
        ), mock.patch.object(
            watchlist_processor.moviepilot,
            "get_subscription_details",
            side_effect=[None, after],
        ), mock.patch.object(
            watchlist_processor.moviepilot,
            "subscribe_series_to_moviepilot",
            return_value=True,
        ) as sub, mock.patch.object(
            watchlist_processor.moviepilot,
            "update_subscription_status",
            return_value=True,
        ) as update:
            self.processor()._sync_status_to_moviepilot(
                "100", "测试剧", series,
                watchlist_processor.STATUS_WATCHING
            )
        sub.assert_called_once()
        update.assert_called_once()

    def test_lookup_failure_never_auto_subscribes(self):
        series = {"seasons": [
            {"season_number": 2, "episode_count": 8},
        ]}
        with mock.patch.object(
            watchlist_processor.settings_db,
            "get_setting", return_value=self.cfg()
        ), mock.patch.object(
            watchlist_processor.moviepilot,
            "get_subscription_details",
            side_effect=moviepilot.MoviePilotSubscriptionLookupError(
                "temporary"
            ),
        ), mock.patch.object(
            watchlist_processor.moviepilot,
            "subscribe_series_to_moviepilot",
        ) as sub:
            self.processor()._sync_status_to_moviepilot(
                "100", "测试剧", series,
                watchlist_processor.STATUS_WATCHING
            )
        sub.assert_not_called()

if __name__ == "__main__":
    unittest.main()
