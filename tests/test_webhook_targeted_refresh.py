import unittest
from unittest.mock import MagicMock, patch

from core_processor import MediaProcessor
from handler import emby


class WebhookTargetedRefreshTests(unittest.TestCase):
    @patch('handler.emby.wait_for_server_idle')
    @patch('handler.emby.get_emby_item_details')
    def test_refresh_override_uses_header_auth_and_disables_recursion(
        self,
        get_details,
        wait_for_idle,
    ):
        get_details.return_value = {
            'Id': 'series-1',
            'Name': '测试剧',
            'Type': 'Series',
            'LockData': False,
            'LockedFields': [],
        }
        response = MagicMock(status_code=204)

        with patch.object(emby.logger, 'trace', create=True), patch.object(
            emby.emby_client, 'post_once', return_value=response
        ) as post:
            submitted = emby.refresh_emby_item_metadata(
                'series-1',
                'http://emby.invalid',
                'secret-value',
                'user-1',
                replace_all_metadata_param=True,
                recursive_override=False,
            )

        self.assertTrue(submitted)
        wait_for_idle.assert_called_once()
        _, kwargs = post.call_args
        self.assertEqual(kwargs['params']['Recursive'], 'false')
        self.assertNotIn('api_key', kwargs['params'])
        self.assertEqual(kwargs['headers']['X-Emby-Token'], 'secret-value')

    @patch('core_processor.emby.refresh_emby_item_metadata', return_value=True)
    def test_followup_refresh_targets_only_new_episodes(self, refresh):
        processor = MediaProcessor.__new__(MediaProcessor)
        processor.emby_url = 'http://emby.invalid'
        processor.emby_api_key = 'secret-value'
        processor.emby_user_id = 'user-1'

        submitted = processor._refresh_emby_after_metadata_sync(
            'series-1',
            '测试剧',
            ['episode-2', 'episode-1', 'episode-2'],
        )

        self.assertTrue(submitted)
        self.assertEqual(refresh.call_count, 3)
        calls = refresh.call_args_list
        self.assertEqual(calls[0].kwargs['item_emby_id'], 'series-1')
        self.assertEqual(calls[0].kwargs['recursive_override'], False)
        self.assertEqual(calls[1].kwargs['item_emby_id'], 'episode-2')
        self.assertEqual(calls[2].kwargs['item_emby_id'], 'episode-1')
        self.assertTrue(all(call.kwargs['recursive_override'] is False for call in calls))
        self.assertNotIn('wait_for_idle', calls[0].kwargs)
        self.assertTrue(all(call.kwargs['wait_for_idle'] is False for call in calls[1:]))

    @patch('core_processor.emby.refresh_emby_item_metadata')
    def test_followup_refresh_stops_after_first_episode_failure(self, refresh):
        refresh.side_effect = [True, False, True]
        processor = MediaProcessor.__new__(MediaProcessor)
        processor.emby_url = 'http://emby.invalid'
        processor.emby_api_key = 'secret-value'
        processor.emby_user_id = 'user-1'

        submitted = processor._refresh_emby_after_metadata_sync(
            'series-1',
            '测试剧',
            ['episode-1', 'episode-2'],
        )

        self.assertFalse(submitted)
        self.assertEqual(refresh.call_count, 2)

    @patch('core_processor.emby.refresh_emby_item_metadata', return_value=True)
    def test_non_followup_keeps_existing_auto_recursive_policy(self, refresh):
        processor = MediaProcessor.__new__(MediaProcessor)
        processor.emby_url = 'http://emby.invalid'
        processor.emby_api_key = 'secret-value'
        processor.emby_user_id = 'user-1'

        submitted = processor._refresh_emby_after_metadata_sync(
            'series-1', '测试剧', None
        )

        self.assertTrue(submitted)
        self.assertEqual(refresh.call_count, 1)
        self.assertNotIn('recursive_override', refresh.call_args.kwargs)


if __name__ == '__main__':
    unittest.main()
