import unittest
from unittest.mock import MagicMock, patch

import requests

from handler.douban import DoubanApi


class DoubanLogSecurityTests(unittest.TestCase):
    def setUp(self):
        self.original_session = DoubanApi._session
        DoubanApi._session = MagicMock()

    def tearDown(self):
        DoubanApi._session = self.original_session

    @patch.object(DoubanApi, '_apply_cooldown')
    def test_request_exception_does_not_echo_signed_url_or_api_key(self, _cooldown):
        secret_marker = 'must-not-appear-in-log'
        DoubanApi._session.get.side_effect = requests.exceptions.RequestException(
            f'https://example.invalid/path?apiKey={secret_marker}&_sig=hidden'
        )
        client = DoubanApi.__new__(DoubanApi)

        with self.assertLogs('handler.douban', level='ERROR') as captured:
            result = client._DoubanApi__invoke('/celebrity/123')

        rendered_logs = '\n'.join(captured.output)
        self.assertNotIn(secret_marker, rendered_logs)
        self.assertNotIn('apiKey=', rendered_logs)
        self.assertNotIn('_sig=', rendered_logs)
        self.assertEqual(result['message'], 'RequestException')


if __name__ == '__main__':
    unittest.main()
