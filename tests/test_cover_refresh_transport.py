import unittest
from unittest import mock
import base64

import requests

import config_manager
from services.cover_generator import CoverGeneratorService
import services.cover_generator as cover_generator


class CoverRefreshTransportTests(unittest.TestCase):
    def setUp(self):
        self.service = CoverGeneratorService.__new__(CoverGeneratorService)
        self.service._covers_output = None
        self.config = {
            "emby_server_url": "http://emby.example",
            "emby_api_key": "secret-token",
        }

    def _refresh(self):
        with mock.patch.object(
            cover_generator.config_manager,
            "APP_CONFIG",
            self.config,
        ):
            self.service._CoverGeneratorService__refresh_emby_image_cache(
                "library-1"
            )

    def _upload(self):
        return self.service._CoverGeneratorService__upload_primary_image(
            "http://emby.example/Items/library-1/Images/Primary",
            "secret-token",
            {"Name": "Movies"},
            {
                "content_type": "image/png",
                "data": b"\x89PNG\r\n\x1a\nbinary-image",
                "label": "original",
            },
        )

    @mock.patch("handler.emby.requests.post")
    def test_redirects_are_not_followed_or_replayed(self, post):
        for status_code in (307, 308):
            with self.subTest(status_code=status_code):
                post.reset_mock()
                post.return_value = mock.Mock(status_code=status_code)

                self._refresh()

                post.assert_called_once()
                self.assertFalse(post.call_args.kwargs["allow_redirects"])

    @mock.patch("handler.emby.requests.post")
    def test_timeout_is_not_replayed(self, post):
        post.side_effect = requests.exceptions.Timeout("secret-token")

        with self.assertLogs(cover_generator.logger, level="WARNING") as logs:
            self._refresh()

        post.assert_called_once()
        self.assertNotIn("secret-token", "\n".join(logs.output))

    @mock.patch("handler.emby.requests.post")
    def test_server_error_is_not_replayed(self, post):
        post.return_value = mock.Mock(status_code=500)

        self._refresh()

        post.assert_called_once()

    @mock.patch("handler.emby.requests.post")
    def test_success_submits_exactly_once_with_header_token(self, post):
        post.return_value = mock.Mock(status_code=204)

        self._refresh()

        post.assert_called_once()
        request_url = post.call_args.args[0]
        request_params = post.call_args.kwargs["params"]
        request_headers = post.call_args.kwargs["headers"]
        self.assertEqual(
            "http://emby.example/Items/library-1/Refresh",
            request_url,
        )
        self.assertNotIn("secret-token", request_url)
        self.assertNotIn("api_key", request_params)
        self.assertEqual("secret-token", request_headers["X-Emby-Token"])
        self.assertEqual(15, post.call_args.kwargs["timeout"])
        self.assertFalse(post.call_args.kwargs["allow_redirects"])

    @mock.patch("handler.emby.requests.post")
    def test_error_log_and_url_do_not_expose_token(self, post):
        post.return_value = mock.Mock(status_code=401)

        with self.assertLogs(cover_generator.logger, level="WARNING") as logs:
            self._refresh()

        post.assert_called_once()
        self.assertNotIn("secret-token", post.call_args.args[0])
        self.assertNotIn("secret-token", "\n".join(logs.output))

    @mock.patch("services.cover_generator.requests.post")
    def test_image_upload_redirects_are_not_followed_or_replayed(self, post):
        for status_code in (307, 308):
            with self.subTest(status_code=status_code):
                post.reset_mock()
                post.return_value = mock.Mock(
                    status_code=status_code,
                    text="sensitive-response-body",
                )

                with self.assertLogs(
                    cover_generator.logger,
                    level="WARNING",
                ) as logs:
                    self.assertFalse(self._upload())

                post.assert_called_once()
                self.assertFalse(post.call_args.kwargs["allow_redirects"])
                self.assertNotIn(
                    "sensitive-response-body",
                    "\n".join(logs.output),
                )

    @mock.patch("services.cover_generator.requests.post")
    def test_image_upload_server_error_is_not_replayed(self, post):
        post.return_value = mock.Mock(
            status_code=500,
            text="sensitive-response-body",
        )

        with self.assertLogs(cover_generator.logger, level="WARNING") as logs:
            self.assertFalse(self._upload())

        post.assert_called_once()
        self.assertNotIn("sensitive-response-body", "\n".join(logs.output))

    @mock.patch("services.cover_generator.requests.post")
    def test_image_upload_timeout_is_not_replayed(self, post):
        post.side_effect = requests.exceptions.Timeout(
            "secret-token binary-image"
        )

        with self.assertLogs(cover_generator.logger, level="WARNING") as logs:
            self.assertFalse(self._upload())

        post.assert_called_once()
        log_text = "\n".join(logs.output)
        self.assertNotIn("secret-token", log_text)
        self.assertNotIn("binary-image", log_text)

    @mock.patch("services.cover_generator.requests.post")
    def test_image_upload_success_preserves_body_headers_and_timeout(self, post):
        post.return_value = mock.Mock(status_code=204)

        self.assertTrue(self._upload())

        post.assert_called_once()
        request_url = post.call_args.args[0]
        request_kwargs = post.call_args.kwargs
        self.assertEqual(
            "http://emby.example/Items/library-1/Images/Primary",
            request_url,
        )
        self.assertNotIn("secret-token", request_url)
        self.assertNotIn("params", request_kwargs)
        self.assertEqual(
            "secret-token",
            request_kwargs["headers"]["X-Emby-Token"],
        )
        self.assertEqual(
            "image/png",
            request_kwargs["headers"]["Content-Type"],
        )
        self.assertEqual(
            base64.b64encode(b"\x89PNG\r\n\x1a\nbinary-image").decode("ascii"),
            request_kwargs["data"],
        )
        self.assertEqual(60, request_kwargs["timeout"])
        self.assertFalse(request_kwargs["allow_redirects"])

    @mock.patch("services.cover_generator.requests.post")
    def test_image_upload_error_log_and_url_do_not_expose_token(self, post):
        post.return_value = mock.Mock(
            status_code=401,
            text="secret-token sensitive-response-body",
        )

        with self.assertLogs(cover_generator.logger, level="WARNING") as logs:
            self.assertFalse(self._upload())

        post.assert_called_once()
        self.assertNotIn("secret-token", post.call_args.args[0])
        log_text = "\n".join(logs.output)
        self.assertNotIn("secret-token", log_text)
        self.assertNotIn("sensitive-response-body", log_text)

    @mock.patch("services.cover_generator.spawn_later")
    @mock.patch("services.cover_generator.gevent_sleep")
    @mock.patch("services.cover_generator.requests.post")
    def test_library_image_url_is_constructed_without_token(
        self,
        post,
        _sleep,
        _spawn_later,
    ):
        post.return_value = mock.Mock(status_code=204)
        candidate = {
            "content_type": "image/png",
            "data": b"\x89PNG\r\n\x1a\nbinary-image",
            "label": "original",
        }
        with (
            mock.patch.object(
                cover_generator.config_manager,
                "APP_CONFIG",
                self.config,
            ),
            mock.patch.object(
                self.service,
                "_CoverGeneratorService__get_image_upload_type",
                return_value=("image/png", ".png"),
            ),
            mock.patch.object(
                self.service,
                "_CoverGeneratorService__build_emby_upload_candidates",
                return_value=[candidate],
            ),
            mock.patch.object(
                self.service,
                "_CoverGeneratorService__is_animated_image",
                return_value=False,
            ),
            mock.patch.object(
                self.service,
                "_CoverGeneratorService__get_primary_image_tag",
                return_value=None,
            ),
            mock.patch.object(
                self.service,
                "_CoverGeneratorService__verify_primary_image_upload",
                return_value=True,
            ),
            mock.patch.object(
                self.service,
                "_CoverGeneratorService__refresh_emby_image_cache",
            ),
        ):
            self.assertTrue(
                self.service._CoverGeneratorService__set_library_image(
                    "server-1",
                    {"Id": "library-1", "Name": "Movies"},
                    candidate["data"],
                )
            )

        post.assert_called_once()
        request_url = post.call_args.args[0]
        self.assertEqual(
            "http://emby.example/Items/library-1/Images/Primary",
            request_url,
        )
        self.assertNotIn("secret-token", request_url)
        self.assertNotIn("api_key", request_url)
        self.assertEqual(
            "secret-token",
            post.call_args.kwargs["headers"]["X-Emby-Token"],
        )


if __name__ == "__main__":
    unittest.main()
