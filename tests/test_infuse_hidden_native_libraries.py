import json
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import config_manager
import reverse_proxy


AUTH = 'MediaBrowser Client="Infuse", UserId="user-a", DeviceId="device-1"'


def response(payload, status=200):
    result = Mock()
    result.status_code = status
    result.json.return_value = payload
    result.raise_for_status.return_value = None
    result.raw.headers = {'Content-Type': 'application/json'}
    result.content = json.dumps(payload).encode()
    result.iter_content.return_value = [result.content]
    return result


class InfuseHiddenNativeLibraryTests(unittest.TestCase):
    def setUp(self):
        self.config = patch.dict(
            reverse_proxy.config_manager.APP_CONFIG,
            {
                'emby_server_url': 'http://isolated-emby:8096',
                'emby_api_key': 'server-secret',
                'proxy_merge_native_libraries': True,
                'proxy_native_view_selection': ['visible-native'],
                'proxy_native_view_order': 'after',
            },
            clear=False,
        )
        self.config.start()
        self.client = reverse_proxy.proxy_app.test_client()

    def tearDown(self):
        self.config.stop()

    @staticmethod
    def request_headers(user_agent='Infuse-Direct/8.5.3', user_id='user-a'):
        return {
            'User-Agent': user_agent,
            'X-Emby-Token': 'client-token',
            'X-Emby-Authorization': AUTH.replace('user-a', user_id),
        }

    @staticmethod
    def native_views():
        return [
            {'Id': 'hidden-movie', 'Name': 'Same Display Name', 'CollectionType': 'movies'},
            {'Id': 'hidden-tv', 'Name': 'Hidden TV', 'CollectionType': 'tvshows'},
            {'Id': 'visible-native', 'Name': 'Visible Native', 'CollectionType': 'movies'},
        ]

    @staticmethod
    def virtual_folders():
        return [
            {'ItemId': 'hidden-movie', 'Name': 'Same Display Name', 'CollectionType': 'movies'},
            {'ItemId': 'hidden-tv', 'Name': 'Hidden TV', 'CollectionType': 'tvshows'},
            {'ItemId': 'visible-native', 'Name': 'Visible Native', 'CollectionType': 'movies'},
        ]

    def test_infuse_virtual_folders_reuses_exact_id_native_visibility_for_both_prefixes(self):
        profile = response({'Id': 'user-a'})
        folders = response(self.virtual_folders())
        with patch.object(reverse_proxy.emby, 'get_emby_libraries', return_value=self.native_views()), \
             patch.object(reverse_proxy.requests, 'get', side_effect=[profile, folders, profile, folders]):
            for path in ('/Library/VirtualFolders', '/emby/Library/VirtualFolders'):
                with self.subTest(path=path):
                    result = self.client.get(path, headers=self.request_headers())
                    self.assertEqual(result.status_code, 200)
                    self.assertEqual(
                        [item['ItemId'] for item in result.get_json()],
                        ['visible-native'],
                    )

    def test_views_keep_same_name_virtual_while_hiding_unselected_native_ids(self):
        virtual = {
            'id': 5,
            'name': 'Same Display Name',
            'emby_collection_id': 'boxset-5',
            'definition_json': {'item_type': ['Movie']},
            'in_library_count': 2,
        }
        with patch.object(reverse_proxy.extensions, 'EMBY_SERVER_ID', 'server-1'), \
             patch.object(reverse_proxy.emby, 'get_emby_libraries', return_value=self.native_views()), \
             patch.object(reverse_proxy.custom_collection_db, 'get_all_active_custom_collections', return_value=[virtual]):
            result = self.client.get('/Users/user-a/Views', headers=self.request_headers())

        self.assertEqual(result.status_code, 200)
        items = result.get_json()['Items']
        self.assertEqual([item['Id'] for item in items], ['-900005', 'visible-native'])
        self.assertEqual(items[0]['Name'], 'Same Display Name')
        self.assertEqual(items[0]['CollectionType'], 'movies')

    def test_different_user_visibility_is_intersected_before_configuration(self):
        profile = response({'Id': 'user-b'})
        folders = response(self.virtual_folders())
        with patch.object(
            reverse_proxy.emby,
            'get_emby_libraries',
            return_value=[{'Id': 'hidden-tv', 'Name': 'Hidden TV'}],
        ), patch.object(reverse_proxy.requests, 'get', side_effect=[profile, folders]):
            result = self.client.get(
                '/Library/VirtualFolders',
                headers=self.request_headers(user_id='user-b'),
            )

        self.assertEqual(result.status_code, 200)
        self.assertEqual(result.get_json(), [])

    def test_missing_or_unverified_user_fails_closed(self):
        missing = self.client.get(
            '/Library/VirtualFolders',
            headers={'User-Agent': 'Infuse-Direct/8.5.3'},
        )
        profile = response({'Id': 'different-user'})
        with patch.object(reverse_proxy.requests, 'get', return_value=profile), \
             patch.object(reverse_proxy.emby, 'get_emby_libraries') as get_libraries:
            unverified = self.client.get(
                '/Library/VirtualFolders',
                headers=self.request_headers(),
            )

        self.assertEqual(missing.status_code, 403)
        self.assertEqual(unverified.status_code, 403)
        get_libraries.assert_not_called()

    def test_vidhub_emby_web_and_unknown_clients_keep_native_passthrough(self):
        upstream_payload = self.virtual_folders()
        for user_agent in ('VidHub/2.3.6', 'Mozilla/5.0 EmbyWeb/4.9.5.0', 'Other/1.0', ''):
            with self.subTest(user_agent=user_agent), patch.object(
                reverse_proxy.requests,
                'request',
                return_value=response(upstream_payload),
            ) as request_call, patch.object(reverse_proxy.emby, 'get_emby_libraries') as get_libraries:
                result = self.client.get(
                    '/Library/VirtualFolders',
                    headers={'User-Agent': user_agent},
                )
                self.assertEqual(result.status_code, 200)
                self.assertEqual(result.get_json(), upstream_payload)
                request_call.assert_called_once()
                get_libraries.assert_not_called()

    def test_nginx_routes_only_infuse_virtualfolders_to_python(self):
        template = (
            Path(__file__).resolve().parents[1]
            / 'templates/nginx/emby_proxy.conf.template'
        ).read_text(encoding='utf-8')
        self.assertIn('location ~ ^/(emby/)?Library/VirtualFolders$', template)
        self.assertIn('if ($http_user_agent ~* "^Infuse-Direct/")', template)
        self.assertIn('proxy_pass http://virtual_library_proxy;', template)
        self.assertIn('proxy_pass http://emby_server;', template)


if __name__ == '__main__':
    unittest.main()
