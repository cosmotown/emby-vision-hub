"""4.10 Web home trace regression: Views is correct, Sections bypasses it."""
import json
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import reverse_proxy as proxy


def upstream(data, status=200):
    result = Mock(status_code=status, content=json.dumps(data).encode())
    result.json.return_value = data
    return result


class OfficialHomeDiscoveryTests(unittest.TestCase):
    def setUp(self):
        self.client = proxy.proxy_app.test_client()
        self.native = [
            {'Id': '20', 'Name': 'Movie', 'CollectionType': 'movies'},
            {'Id': '3', 'Name': 'TV', 'CollectionType': 'tvshows'},
            {'Id': '22', 'Name': 'Visible', 'CollectionType': 'movies'},
        ]
        self.coll = {'id': 5, 'name': 'Movie', 'emby_collection_id': '100',
                     'definition_json': {'item_type': ['Movie']}, 'allowed_user_ids': ['u']}
        self.sections = [
            {'Id': 'smalllibrarytiles', 'SectionType': 'userviews'},
            {'Id': 'resume', 'SectionType': 'resume', 'Monitor': ['videoplayback']},
            *[{'Id': 'latestmedia_' + x['Id'], 'SectionType': 'latestmedia',
               'ParentId': x['Id'], 'ParentItem': x} for x in self.native],
        ]
        for p in (
            patch.dict(proxy.config_manager.APP_CONFIG, {
                'emby_server_url': 'http://isolated-emby:8096', 'emby_api_key': 'SERVER-SECRET',
                'proxy_merge_native_libraries': True, 'proxy_native_view_selection': ['22'],
                'proxy_native_view_order': 'before',
            }),
            patch.object(proxy.extensions, 'EMBY_SERVER_ID', 'server'),
            patch.object(proxy.emby, 'get_emby_libraries', return_value=self.native),
            patch.object(proxy.custom_collection_db, 'get_all_active_custom_collections',
                         side_effect=lambda: [self.coll]),
            patch.object(proxy.custom_collection_db, 'get_custom_collection_by_id',
                         side_effect=lambda i: self.coll if i == 5 else None),
        ):
            p.start()
            self.addCleanup(p.stop)

    def get(self, suffix, prefix='/emby', **kwargs):
        return self.client.get(prefix + '/Users/u/' + suffix, **kwargs)

    def test_tiles_match_views_and_keep_same_name_virtual_for_both_prefixes(self):
        with patch.object(proxy.requests, 'get', return_value=upstream({'Id': 'u'})):
            for prefix in ('', '/emby'):
                tiles = self.get('Sections/smalllibrarytiles/Items', prefix).get_json()
                views = self.get('Views', prefix).get_json()
                self.assertEqual(tiles['TotalRecordCount'], 2)
                self.assertEqual([x['Id'] for x in tiles['Items']], ['22', '-900005'])
                self.assertEqual([(x['Id'], x['CollectionType']) for x in tiles['Items']],
                                 [(x['Id'], x['CollectionType']) for x in views['Items']])

    def test_client_type_matrix_remains_unchanged(self):
        for ua, movie in [('VidHub/2.3.6', 'movies'), ('Infuse-Direct/8', 'movies'),
                          ('Hills/1', 'mixed'), ('Mozilla/5.0', 'mixed'), ('', 'mixed')]:
            for kinds, expected in [(['Movie'], movie), (['Series'], 'tvshows'),
                                    (['Movie', 'Series'], 'mixed')]:
                self.coll['definition_json']['item_type'] = kinds
                with self.subTest(ua=ua, kinds=kinds), patch.object(
                    proxy.requests, 'get', return_value=upstream({'Id': 'u'})
                ):
                    r = self.get('Sections/smalllibrarytiles/Items', headers={'User-Agent': ua})
                    self.assertEqual(r.get_json()['Items'][1]['CollectionType'], expected)

    def test_hidden_latest_removed_visible_native_preserved_and_virtual_added(self):
        with patch.object(proxy.requests, 'get', side_effect=[upstream({'Id': 'u'}), upstream(self.sections)]):
            data = self.get('HomeSections').get_json()
        self.assertEqual([s['Id'] for s in data],
                         ['smalllibrarytiles', 'resume', 'latestmedia_22', 'latestmedia_-900005'])
        self.assertEqual(data[1], self.sections[1])
        self.assertEqual(data[2], self.sections[-1])
        self.assertEqual(data[3]['ParentItem']['Id'], '-900005')
        self.assertEqual(data[3]['CollectionType'], 'mixed')

    def test_no_latest_preference_does_not_add_sections(self):
        with patch.object(proxy.requests, 'get', side_effect=[upstream({'Id': 'u'}), upstream(self.sections[:2])]):
            self.assertEqual(self.get('HomeSections').get_json(), self.sections[:2])

    def test_show_in_latest_false(self):
        self.coll['definition_json']['show_in_latest'] = False
        with patch.object(proxy.requests, 'get', side_effect=[upstream({'Id': 'u'}), upstream(self.sections)]):
            self.assertNotIn('latestmedia_-900005', [s['Id'] for s in self.get('HomeSections').get_json()])

    def test_unpermitted_virtual_absent(self):
        self.coll['allowed_user_ids'] = ['other']
        with patch.object(proxy.requests, 'get', return_value=upstream({'Id': 'u'})):
            self.assertEqual([v['Id'] for v in self.get('Sections/smalllibrarytiles/Items').get_json()['Items']], ['22'])
            self.assertEqual(self.get('Sections/latestmedia_-900005/Items').status_code, 404)

    def test_hidden_and_nonexistent_latest_fail_closed(self):
        with patch.object(proxy.requests, 'get', return_value=upstream({'Id': 'u'})), \
                patch.object(proxy, 'handle_get_latest_items') as latest:
            for target in ('20', '3', '-900099'):
                self.assertEqual(self.get('Sections/latestmedia_' + target + '/Items').status_code, 404)
            latest.assert_not_called()

    def test_client_auth_not_server_key_and_mismatched_user_denied(self):
        with patch.object(proxy.requests, 'get', return_value=upstream({'Id': 'other'})) as call, \
                patch.object(proxy, 'handle_get_views') as views:
            r = self.get('HomeSections?X-Emby-Token=CLIENT-TOKEN')
            self.assertEqual(r.status_code, 403)
            views.assert_not_called()
            self.assertEqual(call.call_args.kwargs['params']['X-Emby-Token'], 'CLIENT-TOKEN')
            self.assertNotIn('SERVER-SECRET', str(call.call_args))

    def test_auth_failure_and_timeout_fail_closed(self):
        for reply in (upstream({}, 401), upstream({}, 403)):
            with patch.object(proxy.requests, 'get', return_value=reply):
                self.assertEqual(self.get('HomeSections').status_code, 403)
        with patch.object(proxy.requests, 'get', side_effect=proxy.requests.Timeout()):
            self.assertEqual(self.get('HomeSections').status_code, 502)

    def test_tiles_pagination_after_filtering(self):
        with patch.object(proxy.requests, 'get', return_value=upstream({'Id': 'u'})):
            data = self.get('Sections/smalllibrarytiles/Items?StartIndex=1&Limit=1').get_json()
            self.assertEqual(data['TotalRecordCount'], 2)
            self.assertEqual([x['Id'] for x in data['Items']], ['-900005'])

    def test_virtual_latest_reuses_existing_query_and_preserves_movie_series(self):
        payload = [{'Id': 'm', 'Type': 'Movie'}, {'Id': 's', 'Type': 'Series'}]
        with patch.object(proxy.requests, 'get', return_value=upstream({'Id': 'u'})), \
                patch.object(proxy, 'handle_get_latest_items', return_value=proxy.Response(
                    json.dumps(payload), mimetype='application/json')) as latest:
            data = self.get('Sections/latestmedia_-900005/Items?Limit=12&ParentId=20').get_json()
            self.assertEqual(data, {'Items': payload, 'TotalRecordCount': 2})
            self.assertEqual(latest.call_args.args, ('u', {'Limit': '12', 'ParentId': '-900005'}))

    def test_visible_native_latest_keeps_response(self):
        payload = {'Items': [{'Id': 'm', 'Type': 'Movie'}], 'TotalRecordCount': 1}
        with patch.object(proxy.requests, 'get', side_effect=[upstream({'Id': 'u'}), upstream(payload)]):
            self.assertEqual(self.get('Sections/latestmedia_22/Items').get_json(), payload)

    def test_malformed_sections_fail_closed(self):
        with patch.object(proxy.requests, 'get', side_effect=[upstream({'Id': 'u'}), upstream({'Items': []})]):
            self.assertEqual(self.get('HomeSections').status_code, 502)

    def test_only_proven_nginx_paths_and_get_are_intercepted(self):
        template = (Path(__file__).resolve().parents[1] / 'templates/nginx/emby_proxy.conf.template').read_text()
        self.assertIn('HomeSections|Sections/(smalllibrarytiles|latestmedia_-?[0-9]+)/Items', template)
        self.assertIn('if ($request_method = GET)', template)
        for path in ('Sections/resume/Items', 'Sections/resumeaudio/Items', 'Sections/onnow/Items',
                     'Items/123/PlaybackInfo', 'Items/123', 'Sections/unknown/Items'):
            with patch.object(proxy, 'handle_get_home_discovery') as home, \
                 patch.object(proxy.requests, 'request') as passthrough:
                passthrough.return_value.status_code = 200
                passthrough.return_value.raw.headers = {}
                passthrough.return_value.iter_content.return_value = [b'{}']
                self.get(path)
                home.assert_not_called()
                passthrough.assert_called_once()


if __name__ == '__main__':
    unittest.main()
