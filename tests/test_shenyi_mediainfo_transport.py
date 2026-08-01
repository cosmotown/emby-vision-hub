import json
import unittest
from unittest import mock

import requests

from services.shenyi_mediainfo import ShenyiMediaInfoAdapter


class FakeStreamResponse:
    def __init__(self, status_code=200, body=b"[]", chunks=None):
        self.status_code = status_code
        self._body = body
        self._chunks = chunks
        self.closed = False

    def iter_content(self, chunk_size=1):
        if self._chunks is not None:
            if isinstance(self._chunks, BaseException):
                raise self._chunks
            yield from self._chunks
        else:
            for index in range(0, len(self._body), chunk_size):
                yield self._body[index:index + chunk_size]

    def close(self):
        self.closed = True


class ShenyiMediaInfoTransportTests(unittest.TestCase):
    def adapter(self, post, **kwargs):
        return ShenyiMediaInfoAdapter(
            "https://emby.example",
            "super-secret-token",
            post=post,
            **kwargs,
        )

    def test_nonempty_success_uses_exact_id_empty_body_and_header_token(self):
        post = mock.Mock(
            return_value=FakeStreamResponse(
                body=json.dumps([{"MediaSourceInfo": {}}]).encode()
            )
        )
        result = self.adapter(post).sync_item("episode_123")
        self.assertEqual("submitted", result.outcome)
        self.assertEqual(1, result.post_attempts)
        post.assert_called_once()
        args, kwargs = post.call_args
        self.assertEqual("https://emby.example/Items/SyncMediaInfo", args[0])
        self.assertEqual({"Id": "episode_123"}, kwargs["params"])
        self.assertEqual(b"", kwargs["data"])
        self.assertEqual("super-secret-token", kwargs["headers"]["X-Emby-Token"])
        self.assertNotIn("super-secret-token", args[0])
        self.assertNotIn("super-secret-token", kwargs["params"])
        self.assertFalse(kwargs["allow_redirects"])
        self.assertTrue(kwargs["stream"])
        self.assertEqual(75, kwargs["timeout"])

    def test_http_400_is_rejected_once_without_reading_body(self):
        response = FakeStreamResponse(status_code=400, body=b"sensitive response")
        post = mock.Mock(return_value=response)
        result = self.adapter(post).sync_item("7")
        self.assertEqual("rejected", result.outcome)
        self.assertEqual("sync_item_rejected", result.reason_code)
        self.assertEqual(1, post.call_count)
        self.assertTrue(response.closed)

    def test_http_200_empty_array_is_not_success(self):
        post = mock.Mock(return_value=FakeStreamResponse(body=b"[]"))
        result = self.adapter(post).sync_item("7")
        self.assertEqual("failed", result.outcome)
        self.assertEqual("sync_empty_result", result.reason_code)
        self.assertEqual("empty_array", result.response_kind)
        self.assertEqual(1, post.call_count)

    def test_redirects_are_never_followed_or_replayed(self):
        for status in (301, 302, 307, 308):
            with self.subTest(status=status):
                post = mock.Mock(return_value=FakeStreamResponse(status_code=status))
                result = self.adapter(post).sync_item("7")
                self.assertEqual("rejected", result.outcome)
                self.assertEqual("sync_redirect_rejected", result.reason_code)
                self.assertEqual(1, post.call_count)
                self.assertFalse(post.call_args.kwargs["allow_redirects"])

    def test_http_5xx_is_ambiguous_and_not_replayed(self):
        for status in (500, 503):
            with self.subTest(status=status):
                post = mock.Mock(return_value=FakeStreamResponse(status_code=status))
                result = self.adapter(post).sync_item("7")
                self.assertEqual("ambiguous", result.outcome)
                self.assertEqual("sync_http_5xx", result.reason_code)
                self.assertEqual(1, post.call_count)

    def test_transport_exceptions_are_ambiguous_and_not_replayed(self):
        cases = [
            (requests.exceptions.Timeout(), "sync_timeout"),
            (requests.exceptions.ConnectionError(), "sync_connection_error"),
            (requests.exceptions.SSLError(), "sync_tls_error"),
        ]
        for exception, reason in cases:
            with self.subTest(reason=reason):
                post = mock.Mock(side_effect=exception)
                result = self.adapter(post).sync_item("7")
                self.assertEqual("ambiguous", result.outcome)
                self.assertEqual(reason, result.reason_code)
                self.assertEqual(1, post.call_count)

    def test_response_stream_disconnect_is_ambiguous_and_not_replayed(self):
        cases = [
            (requests.exceptions.Timeout(), "sync_timeout"),
            (requests.exceptions.ConnectionError(), "sync_connection_error"),
            (requests.exceptions.SSLError(), "sync_tls_error"),
        ]
        for exception, reason in cases:
            with self.subTest(reason=reason):
                response = FakeStreamResponse(chunks=exception)
                post = mock.Mock(return_value=response)
                result = self.adapter(post).sync_item("7")
                self.assertEqual("ambiguous", result.outcome)
                self.assertEqual(reason, result.reason_code)
                self.assertEqual(1, post.call_count)
                self.assertTrue(response.closed)

    def test_response_size_is_bounded(self):
        post = mock.Mock(
            return_value=FakeStreamResponse(chunks=[b"a" * 700, b"b" * 700])
        )
        result = self.adapter(post, max_response_bytes=1024).sync_item("7")
        self.assertEqual("failed", result.outcome)
        self.assertEqual("sync_response_too_large", result.reason_code)
        self.assertIsNone(result.response_fingerprint)
        self.assertEqual(1, post.call_count)

    def test_invalid_item_id_is_rejected_before_post(self):
        post = mock.Mock()
        with self.assertRaises(ValueError):
            self.adapter(post).sync_item("../Series/1")
        post.assert_not_called()


if __name__ == "__main__":
    unittest.main()
