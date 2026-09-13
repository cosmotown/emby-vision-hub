import json
import os
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import config_manager  # Initialize settings before importing the proxy.
import reverse_proxy


class VidHubTraceTests(unittest.TestCase):
    def setUp(self):
        self.trace_env = patch.dict(os.environ, {'VIDHUB_TRACE_ENABLED': '1'}, clear=False)
        self.trace_env.start()
        self.config = patch.dict(
            reverse_proxy.config_manager.APP_CONFIG,
            {
                'emby_server_url': 'http://isolated-emby:8096',
                'emby_api_key': 'server-secret-key',
                'proxy_merge_native_libraries': True,
                'proxy_native_view_selection': 'native-1',
                'proxy_native_view_order': 'before',
            },
            clear=False,
        )
        self.config.start()
        self.client = reverse_proxy.proxy_app.test_client()

    def tearDown(self):
        self.config.stop()
        self.trace_env.stop()

    def test_vidhub_family_detection_is_narrow_and_version_independent(self):
        for user_agent in ('VidHub/2.3.6', 'VidHub/2.x.x'):
            with self.subTest(user_agent=user_agent), reverse_proxy.proxy_app.test_request_context(
                headers={'User-Agent': user_agent},
            ):
                self.assertTrue(reverse_proxy.is_vidhub_client(reverse_proxy.request))

        for user_agent in ('Mozilla/5.0 EmbyWeb/4.9.5.0', 'OtherVidHub/2.3.6', ''):
            with self.subTest(user_agent=user_agent), reverse_proxy.proxy_app.test_request_context(
                headers={'User-Agent': user_agent},
            ):
                self.assertFalse(reverse_proxy.is_vidhub_client(reverse_proxy.request))

    def test_infuse_direct_family_detection_is_narrow_and_version_independent(self):
        for user_agent in ('Infuse-Direct/8.5.3', 'Infuse-Direct/9.x'):
            with self.subTest(user_agent=user_agent), reverse_proxy.proxy_app.test_request_context(
                headers={'User-Agent': user_agent},
            ):
                self.assertTrue(reverse_proxy.is_infuse_direct_client(reverse_proxy.request))

        for user_agent in ('Infuse/8.5.3', 'OtherInfuse-Direct/8.5.3', 'VidHub/2.3.6', ''):
            with self.subTest(user_agent=user_agent), reverse_proxy.proxy_app.test_request_context(
                headers={'User-Agent': user_agent},
            ):
                self.assertFalse(reverse_proxy.is_infuse_direct_client(reverse_proxy.request))

    def test_collection_type_isolated_by_client_and_content_type(self):
        recent_mixed = {
            'type': 'filter',
            'definition_json': {
                'item_type': ['Movie', 'Series'],
                'rules': [
                    {'field': 'date_added', 'operator': 'in_last_days', 'value': 30},
                ],
            },
        }
        cases = (
            ('VidHub/2.3.6', {'definition_json': {'item_type': ['Movie']}}, 'movies'),
            ('VidHub/2.x.x', {'definition_json': {'item_type': 'Movie'}}, 'movies'),
            ('Infuse-Direct/8.5.3', {'definition_json': {'item_type': ['Movie']}}, 'movies'),
            ('Infuse-Direct/9.x', {'definition_json': {'item_type': 'Movie'}}, 'movies'),
            ('Mozilla/5.0 EmbyWeb/4.9.5.0', {'definition_json': {'item_type': ['Movie']}}, 'mixed'),
            ('', {'definition_json': {'item_type': ['Movie']}}, 'mixed'),
            ('OtherClient/1.0', {'definition_json': {'item_type': ['Movie']}}, 'mixed'),
            ('VidHub/2.3.6', {'definition_json': {'item_type': ['Series', 'Episode']}}, 'tvshows'),
            ('Infuse-Direct/8.5.3', {'definition_json': {'item_type': ['Series', 'Episode']}}, 'tvshows'),
            ('OtherClient/1.0', {'definition_json': {'item_type': ['Series', 'Episode']}}, 'tvshows'),
            ('VidHub/2.3.6', recent_mixed, 'movies'),
            ('Mozilla/5.0 EmbyWeb/4.9.5.0', recent_mixed, 'mixed'),
            ('Infuse/8.1.7', recent_mixed, 'mixed'),
            ('Infuse-Direct/8.5.3', recent_mixed, 'movies'),
            ('', recent_mixed, 'mixed'),
            ('VidHub/2.3.6', {'type': 'filter', 'definition_json': {'item_type': ['Movie', 'Series'], 'rules': []}}, 'mixed'),
        )
        for user_agent, collection, expected in cases:
            with self.subTest(user_agent=user_agent, collection=collection), \
                 reverse_proxy.proxy_app.test_request_context(headers={'User-Agent': user_agent}):
                self.assertEqual(
                    reverse_proxy.get_virtual_collection_type(collection, reverse_proxy.request),
                    expected,
                )

    def test_infuse_movie_views_and_detail_use_same_movies_type(self):
        collection = {
            'id': 5,
            'name': 'Localized Movie View',
            'emby_collection_id': 'boxset-5',
            'definition_json': {'item_type': ['Movie']},
            'in_library_count': 2,
        }
        with patch.object(reverse_proxy.extensions, 'EMBY_SERVER_ID', 'server-1'), \
             patch.object(reverse_proxy.emby, 'get_emby_libraries', return_value=[]), \
             patch.object(
                 reverse_proxy.custom_collection_db,
                 'get_all_active_custom_collections',
                 return_value=[collection],
             ), patch.object(
                 reverse_proxy.custom_collection_db,
                 'get_custom_collection_by_id',
                 return_value=collection,
             ):
            for path in ('/emby/Users/abcdef/Views', '/Users/abcdef/Views'):
                with self.subTest(path=path):
                    response = self.client.get(path, headers={'User-Agent': 'Infuse-Direct/8.5.3'})
                    self.assertEqual(response.status_code, 200)
                    self.assertEqual(response.get_json()['Items'][0]['CollectionType'], 'movies')

            detail = self.client.get(
                '/emby/Users/abcdef/Items/-900005',
                headers={'User-Agent': 'Infuse-Direct/8.5.3'},
            )
            self.assertEqual(detail.status_code, 200)
            self.assertEqual(detail.get_json()['CollectionType'], 'movies')

            ordinary = self.client.get(
                '/Users/abcdef/Views',
                headers={'User-Agent': 'Mozilla/5.0 EmbyWeb/4.9.5.0'},
            )
            self.assertEqual(ordinary.get_json()['Items'][0]['CollectionType'], 'mixed')

    def test_infuse_unprefixed_virtual_catalogue_paths_reuse_existing_handlers(self):
        json_response = reverse_proxy.Response(
            json.dumps({'Items': [], 'TotalRecordCount': 0}),
            mimetype='application/json',
        )
        latest_response = reverse_proxy.Response(json.dumps([]), mimetype='application/json')
        detail_response = reverse_proxy.Response(
            json.dumps({'Id': '-900005', 'CollectionType': 'movies'}),
            mimetype='application/json',
        )
        with patch.object(
            reverse_proxy,
            'handle_get_latest_items',
            return_value=latest_response,
        ) as latest, patch.object(
            reverse_proxy,
            'handle_get_mimicked_library_items',
            return_value=json_response,
        ) as items, patch.object(
            reverse_proxy,
            'handle_get_mimicked_library_details',
            return_value=detail_response,
        ) as details:
            headers = {'User-Agent': 'Infuse-Direct/8.5.3'}
            self.assertEqual(self.client.get(
                '/Users/abcdef/Items/Latest',
                query_string={'ParentId': '-900005'},
                headers=headers,
            ).status_code, 200)
            self.assertEqual(self.client.get(
                '/Users/abcdef/Items',
                query_string={'ParentId': '-900005'},
                headers=headers,
            ).status_code, 200)
            self.assertEqual(self.client.get(
                '/Users/abcdef/Items/-900005',
                headers=headers,
            ).status_code, 200)

        latest.assert_called_once()
        self.assertEqual(latest.call_args.args[0], 'abcdef')
        items.assert_called_once()
        self.assertEqual(items.call_args.args[:2], ('abcdef', '-900005'))
        details.assert_called_once_with('abcdef', '-900005')

    def test_infuse_unprefixed_virtual_primary_image_uses_persisted_mapping_without_tag(self):
        collection = {
            'id': 5,
            'name': 'Localized Movie View',
            'emby_collection_id': 'real-boxset-5',
        }
        upstream = Mock()
        upstream.status_code = 200
        upstream.raw.headers = {
            'Content-Type': 'image/jpeg',
            'Content-Length': '4',
        }
        upstream.iter_content.return_value = [b'jpeg']

        with patch.object(
            reverse_proxy.custom_collection_db,
            'get_custom_collection_by_id',
            return_value=collection,
        ), patch.object(
            reverse_proxy,
            '_get_real_emby_url_and_key',
            return_value=('http://isolated-emby:8096', 'server-secret-key'),
        ), patch.object(reverse_proxy.requests, 'get', return_value=upstream) as get:
            response = self.client.get(
                '/Items/-900005/Images/Primary',
                headers={
                    'User-Agent': 'Infuse-Direct/8.5.3',
                    'X-Emby-Token': 'client-token',
                },
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data, b'jpeg')
        self.assertEqual(response.content_type, 'image/jpeg')
        self.assertEqual(
            get.call_args.args[0],
            'http://isolated-emby:8096/Items/real-boxset-5/Images/Primary',
        )
        self.assertEqual(get.call_args.kwargs['params'], {})
        self.assertEqual(get.call_args.kwargs['headers']['X-Emby-Token'], 'client-token')

    def test_virtual_primary_image_ignores_client_tag_and_unknown_view_fails_closed(self):
        collection = {'id': 5, 'emby_collection_id': 'real-boxset-5'}
        upstream = Mock()
        upstream.status_code = 200
        upstream.raw.headers = {'Content-Type': 'image/jpeg'}
        upstream.iter_content.return_value = [b'jpeg']
        with patch.object(
            reverse_proxy.custom_collection_db,
            'get_custom_collection_by_id',
            side_effect=lambda db_id: collection if db_id == 5 else None,
        ), patch.object(
            reverse_proxy,
            '_get_real_emby_url_and_key',
            return_value=('http://isolated-emby:8096', 'server-secret-key'),
        ), patch.object(reverse_proxy.requests, 'get', return_value=upstream) as get:
            tagged = self.client.get(
                '/emby/Items/-900005/Images/Primary',
                query_string={'tag': 'untrusted-other-item'},
                headers={'User-Agent': 'VidHub/2.3.6'},
            )
            missing = self.client.get('/Items/-900099/Images/Primary')

        self.assertEqual(tagged.status_code, 200)
        self.assertEqual(missing.status_code, 404)
        self.assertEqual(get.call_count, 1)
        self.assertEqual(
            get.call_args.args[0],
            'http://isolated-emby:8096/Items/real-boxset-5/Images/Primary',
        )
        self.assertEqual(get.call_args.kwargs['params'], {})

    def test_restricted_virtual_primary_image_requires_allowed_authenticated_user(self):
        collection = {
            'id': 5,
            'emby_collection_id': 'real-boxset-5',
            'allowed_user_ids': ['allowed-user'],
        }
        profile = Mock()
        profile.status_code = 200
        profile.json.return_value = {'Id': 'allowed-user'}
        image = Mock()
        image.status_code = 200
        image.raw.headers = {'Content-Type': 'image/jpeg'}
        image.iter_content.return_value = [b'jpeg']
        authorization = 'MediaBrowser Client="Infuse", UserId="allowed-user", DeviceId="device-1"'

        with patch.object(
            reverse_proxy.custom_collection_db,
            'get_custom_collection_by_id',
            return_value=collection,
        ), patch.object(
            reverse_proxy,
            '_get_real_emby_url_and_key',
            return_value=('http://isolated-emby:8096', 'server-secret-key'),
        ), patch.object(reverse_proxy.requests, 'get', side_effect=[profile, image]) as get:
            response = self.client.get(
                '/Items/-900005/Images/Primary',
                headers={
                    'User-Agent': 'Infuse-Direct/8.5.3',
                    'X-Emby-Authorization': authorization,
                    'X-Emby-Token': 'client-token',
                },
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data, b'jpeg')
        self.assertEqual(get.call_count, 2)
        self.assertEqual(get.call_args_list[0].args[0], 'http://isolated-emby:8096/Users/allowed-user')
        self.assertNotIn('api_key', get.call_args_list[0].kwargs['params'])

    def test_restricted_virtual_routes_fail_closed_for_disallowed_user(self):
        collection = {
            'id': 5,
            'emby_collection_id': 'real-boxset-5',
            'allowed_user_ids': ['allowed-user'],
            'definition_json': {'item_type': ['Movie']},
        }
        with patch.object(
            reverse_proxy.custom_collection_db,
            'get_custom_collection_by_id',
            return_value=collection,
        ), patch.object(reverse_proxy.requests, 'get') as get:
            detail = self.client.get('/Users/disallowed-user/Items/-900005')
            items = self.client.get(
                '/Users/disallowed-user/Items',
                query_string={'ParentId': '-900005'},
            )
            image = self.client.get(
                '/Items/-900005/Images/Primary',
                headers={
                    'X-Emby-Authorization': 'MediaBrowser UserId="disallowed-user"',
                    'X-Emby-Token': 'client-token',
                },
            )

        self.assertEqual(detail.status_code, 404)
        self.assertEqual(items.status_code, 200)
        self.assertEqual(items.get_json(), {'Items': [], 'TotalRecordCount': 0})
        self.assertEqual(image.status_code, 404)
        get.assert_not_called()

    def test_restricted_virtual_primary_image_rejects_failed_user_authentication(self):
        collection = {
            'id': 5,
            'emby_collection_id': 'real-boxset-5',
            'allowed_user_ids': '["allowed-user"]',
        }
        unauthorized = Mock()
        unauthorized.status_code = 401
        with patch.object(
            reverse_proxy.custom_collection_db,
            'get_custom_collection_by_id',
            return_value=collection,
        ), patch.object(
            reverse_proxy,
            '_get_real_emby_url_and_key',
            return_value=('http://isolated-emby:8096', 'server-secret-key'),
        ), patch.object(reverse_proxy.requests, 'get', return_value=unauthorized) as get:
            response = self.client.get(
                '/Items/-900005/Images/Primary',
                headers={
                    'X-Emby-Authorization': 'MediaBrowser UserId="allowed-user"',
                    'X-Emby-Token': 'revoked-client-token',
                },
            )

        self.assertEqual(response.status_code, 404)
        self.assertEqual(get.call_count, 1)

    def test_infuse_virtual_items_include_requested_media_fields(self):
        collection = {
            'id': 5,
            'type': 'filter',
            'definition_json': {
                'item_type': ['Movie'],
                'rules': [],
                'default_sort_by': 'DateCreated',
            },
        }
        indexed = [{'Id': 'movie-1'}]
        details = [{'Id': 'movie-1', 'Type': 'Movie', 'MediaSources': [{'Id': 'source-1'}]}]
        with patch.object(
            reverse_proxy.custom_collection_db,
            'get_custom_collection_by_id',
            return_value=collection,
        ), patch.object(
            reverse_proxy.queries_db,
            'query_virtual_library_items',
            return_value=(indexed, 1),
        ), patch.object(
            reverse_proxy,
            '_get_real_emby_url_and_key',
            return_value=('http://isolated-emby:8096', 'server-secret-key'),
        ), patch.object(
            reverse_proxy,
            '_fetch_items_in_chunks',
            return_value=details,
        ) as fetch:
            response = self.client.get(
                '/Users/abcdef/Items',
                query_string={
                    'ParentId': '-900005',
                    'Fields': 'MediaSources,Path,ParentId,Etag',
                },
                headers={'User-Agent': 'Infuse-Direct/8.5.3'},
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()['TotalRecordCount'], 1)
        requested_fields = set(fetch.call_args.args[4].split(','))
        self.assertTrue({'MediaSources', 'Path', 'ParentId', 'Etag'} <= requested_fields)

    def test_infuse_virtual_items_do_not_forward_unproven_requested_fields(self):
        base = 'PrimaryImageAspectRatio,ImageTags,Name'
        with reverse_proxy.proxy_app.test_request_context(headers={'User-Agent': 'Infuse-Direct/8.5.3'}):
            fields = reverse_proxy._get_virtual_item_fields(
                base,
                {'Fields': 'MediaSources,Path,UnprovenField'},
                reverse_proxy.request,
            )
        self.assertEqual(set(fields.split(',')), {
            'PrimaryImageAspectRatio', 'ImageTags', 'Name', 'MediaSources', 'Path',
        })

    def test_non_infuse_virtual_items_keep_existing_field_set(self):
        base = 'PrimaryImageAspectRatio,ImageTags,Name'
        with reverse_proxy.proxy_app.test_request_context(headers={'User-Agent': 'VidHub/2.3.6'}):
            self.assertEqual(
                reverse_proxy._get_virtual_item_fields(
                    base,
                    {'Fields': 'MediaSources,Path,Etag'},
                    reverse_proxy.request,
                ),
                base,
            )

    def test_recent_detection_uses_filter_semantics_not_display_name(self):
        semantic_recent = {
            'type': 'filter',
            'name': 'Any Localized Name',
            'definition_json': {
                'item_type': ['Series', 'Movie'],
                'rules': [
                    {'field': 'date_added', 'operator': 'in_last_days', 'value': 14},
                ],
            },
        }
        name_only = {
            'type': 'filter',
            'name': '近期入库',
            'definition_json': {'item_type': ['Movie', 'Series'], 'rules': []},
        }
        wrong_date_semantics = {
            'type': 'filter',
            'name': 'Recent',
            'definition_json': {
                'item_type': ['Movie', 'Series'],
                'rules': [
                    {'field': 'release_date', 'operator': 'in_last_days', 'value': 30},
                ],
            },
        }

        with reverse_proxy.proxy_app.test_request_context(headers={'User-Agent': 'VidHub/9.0'}):
            self.assertEqual(
                reverse_proxy.get_virtual_collection_type(semantic_recent, reverse_proxy.request),
                'movies',
            )
            self.assertEqual(
                reverse_proxy.get_virtual_collection_type(name_only, reverse_proxy.request),
                'mixed',
            )
            self.assertEqual(
                reverse_proxy.get_virtual_collection_type(wrong_date_semantics, reverse_proxy.request),
                'mixed',
            )

    def test_vidhub_views_paths_and_detail_use_same_movies_type(self):
        collection = {
            'id': 7,
            'name': '电影',
            'emby_collection_id': 'boxset-7',
            'definition_json': {'item_type': ['Movie']},
            'in_library_count': 12,
        }
        with patch.object(reverse_proxy.extensions, 'EMBY_SERVER_ID', 'server-1'), \
             patch.object(reverse_proxy.emby, 'get_emby_libraries', return_value=[]), \
             patch.object(
                 reverse_proxy.custom_collection_db,
                 'get_all_active_custom_collections',
                 return_value=[collection],
             ), patch.object(
                 reverse_proxy.custom_collection_db,
                 'get_custom_collection_by_id',
                 return_value=collection,
             ):
            for path in ('/emby/Users/abcdef/Views', '/Users/abcdef/Views'):
                with self.subTest(path=path):
                    response = self.client.get(path, headers={'User-Agent': 'VidHub/2.4.0'})
                    self.assertEqual(response.status_code, 200)
                    self.assertEqual(response.get_json()['Items'][0]['CollectionType'], 'movies')

            detail = self.client.get(
                '/emby/Users/abcdef/Items/-900007',
                headers={'User-Agent': 'VidHub/2.4.0'},
            )
            self.assertEqual(detail.status_code, 200)
            self.assertEqual(detail.get_json()['CollectionType'], 'movies')

            ordinary_view = self.client.get(
                '/emby/Users/abcdef/Views',
                headers={'User-Agent': 'Mozilla/5.0 EmbyWeb/4.9.5.0'},
            )
            self.assertEqual(ordinary_view.status_code, 200)
            self.assertEqual(ordinary_view.get_json()['Items'][0]['CollectionType'], 'mixed')

            ordinary_detail = self.client.get(
                '/emby/Users/abcdef/Items/-900007',
                headers={'User-Agent': 'Mozilla/5.0 EmbyWeb/4.9.5.0'},
            )
            self.assertEqual(ordinary_detail.status_code, 200)
            self.assertEqual(ordinary_detail.get_json()['CollectionType'], 'mixed')

    def test_recent_mixed_views_and_detail_are_client_isolated(self):
        collection = {
            'id': 7,
            'name': 'Localized Recent View',
            'type': 'filter',
            'emby_collection_id': 'boxset-7',
            'definition_json': {
                'item_type': ['Movie', 'Series'],
                'rules': [
                    {'field': 'date_added', 'operator': 'in_last_days', 'value': 30},
                ],
            },
            'in_library_count': 3,
        }
        with patch.object(reverse_proxy.extensions, 'EMBY_SERVER_ID', 'server-1'), \
             patch.object(reverse_proxy.emby, 'get_emby_libraries', return_value=[]), \
             patch.object(
                 reverse_proxy.custom_collection_db,
                 'get_all_active_custom_collections',
                 return_value=[collection],
             ), patch.object(
                 reverse_proxy.custom_collection_db,
                 'get_custom_collection_by_id',
                 return_value=collection,
             ):
            for path in ('/emby/Users/abcdef/Views', '/Users/abcdef/Views'):
                with self.subTest(path=path):
                    response = self.client.get(path, headers={'User-Agent': 'VidHub/2.3.6'})
                    self.assertEqual(response.status_code, 200)
                    self.assertEqual(response.get_json()['Items'][0]['CollectionType'], 'movies')

            detail = self.client.get(
                '/emby/Users/abcdef/Items/-900007',
                headers={'User-Agent': 'VidHub/2.3.6'},
            )
            self.assertEqual(detail.status_code, 200)
            self.assertEqual(detail.get_json()['CollectionType'], 'movies')

            infuse_view = self.client.get(
                '/Users/abcdef/Views',
                headers={'User-Agent': 'Infuse-Direct/8.5.3'},
            )
            self.assertEqual(infuse_view.get_json()['Items'][0]['CollectionType'], 'movies')
            infuse_detail = self.client.get(
                '/Users/abcdef/Items/-900007',
                headers={'User-Agent': 'Infuse-Direct/8.5.3'},
            )
            self.assertEqual(infuse_detail.get_json()['CollectionType'], 'movies')

            for user_agent in ('Mozilla/5.0 EmbyWeb/4.9.5.0', 'Infuse/8.1.7', ''):
                with self.subTest(user_agent=user_agent):
                    view = self.client.get(
                        '/emby/Users/abcdef/Views',
                        headers={'User-Agent': user_agent},
                    )
                    item = view.get_json()['Items'][0]
                    self.assertEqual(item['CollectionType'], 'mixed')
                    ordinary_detail = self.client.get(
                        '/emby/Users/abcdef/Items/-900007',
                        headers={'User-Agent': user_agent},
                    )
                    self.assertEqual(ordinary_detail.get_json()['CollectionType'], 'mixed')

    def test_recent_mixed_items_keep_movie_and_series_query_semantics(self):
        collection = {
            'id': 7,
            'name': 'Localized Recent View',
            'type': 'filter',
            'definition_json': {
                'item_type': ['Movie', 'Series'],
                'target_library_ids': ['movies-library', 'series-library'],
                'rules': [
                    {'field': 'date_added', 'operator': 'in_last_days', 'value': 30},
                ],
            },
        }
        indexed_items = [
            {'Id': 'movie-1', 'tmdb_id': '101'},
            {'Id': 'series-1', 'tmdb_id': '202'},
        ]
        emby_items = [
            {'Id': 'movie-1', 'Name': 'Recent Movie', 'Type': 'Movie'},
            {
                'Id': 'series-1',
                'Name': 'Recent Series',
                'Type': 'Series',
                'ChildCount': 1,
            },
        ]

        with patch.object(
            reverse_proxy.custom_collection_db,
            'get_custom_collection_by_id',
            return_value=collection,
        ), patch.object(
            reverse_proxy.queries_db,
            'query_virtual_library_items',
            return_value=(indexed_items, 2),
        ) as query, patch.object(
            reverse_proxy,
            '_get_real_emby_url_and_key',
            return_value=('http://isolated-emby:8096', 'server-secret-key'),
        ), patch.object(
            reverse_proxy,
            '_fetch_items_in_chunks',
            return_value=emby_items,
        ):
            response = self.client.get(
                '/emby/Users/abcdef/Items',
                query_string={
                    'ParentId': '-900007',
                    'Recursive': 'true',
                    # VidHub requests Movie after seeing CollectionType=movies.
                    # The virtual collection definition must remain authoritative.
                    'IncludeItemTypes': 'Movie',
                },
                headers={'User-Agent': 'VidHub/2.3.6'},
            )

        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertEqual(payload['TotalRecordCount'], 2)
        self.assertEqual([item['Type'] for item in payload['Items']], ['Movie', 'Series'])
        self.assertEqual(payload['Items'][1]['ChildCount'], 1)
        self.assertEqual(query.call_args.kwargs['item_types'], ['Movie', 'Series'])
        self.assertEqual(
            query.call_args.kwargs['target_library_ids'],
            ['movies-library', 'series-library'],
        )

    def test_views_trace_records_request_counts_and_view_fields_without_secrets(self):
        native = {
            'Id': 'native-1',
            'Name': 'Native Movies',
            'Type': 'CollectionFolder',
            'CollectionType': 'movies',
            'ServerId': 'server-1',
            'ParentId': '2',
            'IsFolder': True,
            'ImageTags': {},
        }
        collection = {
            'id': 7,
            'name': '电影',
            'emby_collection_id': 'boxset-7',
            'definition_json': {'item_type': ['Movie']},
            'in_library_count': 12,
        }

        with patch.object(reverse_proxy.extensions, 'EMBY_SERVER_ID', 'server-1'), \
             patch.object(reverse_proxy.emby, 'get_emby_libraries', return_value=[native]), \
             patch.object(
                 reverse_proxy.custom_collection_db,
                 'get_all_active_custom_collections',
                 return_value=[collection],
             ), self.assertLogs(reverse_proxy.logger, level='INFO') as captured:
            response = self.client.get(
                '/emby/Users/abcdef/Views',
                query_string={
                    'ParentId': '-900007',
                    'IncludeItemTypes': 'Movie,Series',
                    'Recursive': 'true',
                    'Fields': 'ImageTags,Path',
                    'SortBy': 'SortName',
                    'CollectionType': 'movies',
                    'api_key': 'query-secret-key',
                    'X-Emby-Token': 'query-secret-token',
                },
                headers={
                    'User-Agent': 'VidHub/Test',
                    'X-Emby-Token': 'header-secret-token',
                },
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()['TotalRecordCount'], 2)
        output = '\n'.join(captured.output)
        self.assertIn('[VIDHUB_TRACE]', output)
        self.assertIn('"client_hint":"vidhub"', output)
        self.assertIn('"items_count":2', output)
        self.assertIn('"total_record_count":2', output)
        self.assertIn('"event":"view_item"', output)
        self.assertIn('"Name":"Native Movies"', output)
        self.assertIn('"Name":"电影"', output)
        self.assertEqual(response.get_json()['Items'][1]['CollectionType'], 'movies')
        self.assertIn('api_key=%3Credacted%3E', output)
        self.assertIn('X-Emby-Token=%3Credacted%3E', output)
        self.assertNotIn('query-secret-key', output)
        self.assertNotIn('query-secret-token', output)
        self.assertNotIn('header-secret-token', output)
        self.assertNotIn('server-secret-key', output)

    def test_unknown_user_agent_remains_traceable_without_guessing_client(self):
        with patch.object(reverse_proxy.extensions, 'EMBY_SERVER_ID', 'server-1'), \
             patch.object(reverse_proxy.emby, 'get_emby_libraries', return_value=[]), \
             patch.object(
                 reverse_proxy.custom_collection_db,
                 'get_all_active_custom_collections',
                 return_value=[],
             ), self.assertLogs(reverse_proxy.logger, level='INFO') as captured:
            response = self.client.get(
                '/emby/Users/abcdef/Views',
                headers={'User-Agent': 'UnknownClient/1.0'},
            )

        self.assertEqual(response.status_code, 200)
        output = '\n'.join(captured.output)
        self.assertIn('"client_hint":"unclassified"', output)
        self.assertIn('"user_agent":"UnknownClient/1.0"', output)

    def test_nginx_trace_uses_allowlisted_query_fields_and_routes_catalogue_reads(self):
        template = (
            Path(__file__).resolve().parents[1]
            / 'templates'
            / 'nginx'
            / 'emby_proxy.conf.template'
        ).read_text(encoding='utf-8')

        self.assertIn('log_format vidhub_trace', template)
        self.assertIn('access_log /dev/stdout vidhub_trace', template)
        self.assertNotIn('log_format infuse_trace', template)
        self.assertIn('if ($arg_ParentId ~ ^-\\d+$)', template)
        self.assertIn('location ~ ^/(emby/)?Users/[^/]+/Items/-(\\d+)$', template)
        self.assertIn('location ~ ^/Items/-(\\d+)/Images/Primary$', template)
        self.assertNotIn('location ~ ^/(emby/)?Items/-(\\d+)', template)
        self.assertNotIn('location = /emby/Library/VirtualFolders', template)
        self.assertNotIn('location = /Library/VirtualFolders', template)
        self.assertNotIn('location = /emby/Items', template)
        self.assertNotIn('location = /Items', template)
        self.assertNotIn('$args', template)
        self.assertNotIn('$query_string', template)
        self.assertNotIn('$http_x_emby_token', template.lower())
        self.assertNotIn('$arg_api_key', template.lower())

    def test_native_views_upstream_auth_uses_header_and_failure_log_is_path_free(self):
        response = Mock()
        response.json.return_value = {'Items': []}
        with patch.object(reverse_proxy.emby.logger, 'trace', create=True), \
             patch.object(reverse_proxy.emby.emby_client, 'get', return_value=response) as get:
            self.assertEqual(
                reverse_proxy.emby.get_emby_libraries(
                    'http://isolated-emby:8096',
                    'upstream-secret-token',
                    'abcdef',
                ),
                [],
            )

        self.assertEqual(
            get.call_args.kwargs['headers'],
            {'X-Emby-Token': 'upstream-secret-token'},
        )
        self.assertNotIn('params', get.call_args.kwargs)

        request_error = reverse_proxy.requests.exceptions.RequestException(
            'https://example.invalid/Views?api_key=must-not-log'
        )
        with patch.object(reverse_proxy.emby.logger, 'trace', create=True), \
             patch.object(reverse_proxy.emby.emby_client, 'get', side_effect=request_error), \
             self.assertLogs(reverse_proxy.emby.logger, level='ERROR') as captured:
            result = reverse_proxy.emby.get_emby_libraries(
                'http://isolated-emby:8096',
                'upstream-secret-token',
                'abcdef',
            )

        self.assertIsNone(result)
        output = '\n'.join(captured.output)
        self.assertIn('error_type=RequestException', output)
        self.assertNotIn('must-not-log', output)
        self.assertNotIn('upstream-secret-token', output)


if __name__ == '__main__':
    unittest.main()
