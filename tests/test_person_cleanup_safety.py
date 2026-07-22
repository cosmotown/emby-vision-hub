import unittest
import ast
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, List, Optional
from unittest.mock import MagicMock, patch

import requests

from services.person_cleanup_safety import (
    build_identity_provider_pairs,
    classify_reference_check,
    find_ghost_candidates,
    media_item_has_exact_person_reference,
    normalize_person_name,
    person_name_protection_keys,
    reference_check_failure_message,
)


class PersonCleanupSafetyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.repo_root = Path(__file__).resolve().parents[1]
        handler_tree = ast.parse((cls.repo_root / 'handler' / 'emby.py').read_text())
        get_references_node = next(
            node
            for node in handler_tree.body
            if isinstance(node, ast.FunctionDef) and node.name == 'get_person_media_references'
        )
        get_item_people_node = next(
            node
            for node in handler_tree.body
            if isinstance(node, ast.FunctionDef) and node.name == 'get_emby_item_people_details'
        )
        cls.emby_globals = {
            'Any': Any,
            'Dict': Dict,
            'List': List,
            'Optional': Optional,
            'ThreadPoolExecutor': ThreadPoolExecutor,
            'as_completed': as_completed,
            'requests': requests,
            'logger': MagicMock(),
            'emby_client': MagicMock(),
            'get_emby_items_by_id': MagicMock(),
        }
        exec(
            compile(
                ast.Module(body=[get_item_people_node, get_references_node], type_ignores=[]),
                'handler/emby.py',
                'exec',
            ),
            cls.emby_globals,
        )
        cls.get_person_media_references = staticmethod(
            cls.emby_globals['get_person_media_references']
        )
        cls.get_emby_item_people_details = staticmethod(
            cls.emby_globals['get_emby_item_people_details']
        )

    def test_candidate_scan_excludes_every_referenced_person(self):
        people = [
            {'Id': '1', 'Name': '在用人物'},
            {'Id': '2', 'Name': '候选人物'},
            {'Name': '无 ID 人物'},
        ]

        candidates = find_ghost_candidates(people, {'1'})

        self.assertEqual([person['Id'] for person in candidates], ['2'])

    def test_protected_person_without_provider_ids_is_never_a_candidate(self):
        people = [
            {'Id': '1', 'Name': '普通幽灵人物', 'ProviderIds': {}},
            {'Id': '2', 'Name': '保护库人物', 'ProviderIds': {}},
        ]

        candidates = find_ghost_candidates(people, set() | {'2'})

        self.assertEqual([person['Id'] for person in candidates], ['1'])

    def test_protected_person_name_excludes_duplicate_emby_person_ids(self):
        people = [
            {'Id': '1', 'Name': '普通幽灵人物'},
            {'Id': '2', 'Name': ' 保护库 人物 '},
            {'Id': '3', 'Name': '保护库 人物'},
        ]

        candidates = find_ghost_candidates(
            people,
            referenced_person_ids={'2'},
            protected_person_names={'保护库  人物'},
        )

        self.assertEqual([person['Id'] for person in candidates], ['1'])
        self.assertEqual(normalize_person_name('  Alice  SMITH '), 'alice smith')

    def test_protected_person_name_excludes_numbered_translation_duplicates(self):
        people = [
            {'Id': '1', 'Name': '1①めぐみ'},
            {'Id': '2', 'Name': '2梅田'},
            {'Id': '3', 'Name': '2Pac'},
        ]

        candidates = find_ghost_candidates(
            people,
            referenced_person_ids=set(),
            protected_person_names={'めぐみ', '梅田'},
        )

        self.assertEqual([person['Id'] for person in candidates], ['3'])
        self.assertEqual(person_name_protection_keys('3②みさ'), {'3②みさ', 'みさ'})

    def test_only_explicit_zero_reference_count_is_deletable(self):
        self.assertEqual(classify_reference_check({'count': 0, 'items': []}), 'orphan')
        self.assertEqual(classify_reference_check({'count': 1, 'items': [{}]}), 'linked')
        self.assertEqual(classify_reference_check(None), 'invalid_response')
        self.assertEqual(classify_reference_check({}), 'invalid_response')
        self.assertEqual(classify_reference_check({'count': '0'}), 'invalid_response')
        self.assertEqual(classify_reference_check({'count': -1}), 'invalid_response')
        self.assertEqual(classify_reference_check({'count': False}), 'invalid_response')
        self.assertEqual(
            classify_reference_check({'status': 'people_unavailable', 'count': None}),
            'people_unavailable',
        )
        self.assertEqual(
            classify_reference_check({
                'status': 'identity_alias_only',
                'count': 0,
                'query_count': 2,
            }),
            'identity_alias_only',
        )
        self.assertEqual(
            classify_reference_check({'status': 'identity_alias_only', 'count': 0, 'query_count': 0}),
            'invalid_response',
        )

    def test_failure_messages_do_not_misreport_people_as_connection_failure(self):
        self.assertIn(
            '无法连接 Emby',
            reference_check_failure_message('connection_failed'),
        )
        people_message = reference_check_failure_message('people_unavailable')
        self.assertIn('人物明细不可核验', people_message)
        self.assertNotIn('无法连接', people_message)
        self.assertIn('不允许删除', people_message)

    def test_identity_comparison_uses_only_exact_supported_provider_ids(self):
        self.assertEqual(
            build_identity_provider_pairs({
                'Tmdb': '12345',
                'Imdb': 'nm0012345',
                'Douban': '67890',
                'Bad': 'value,other',
            }),
            ['douban.67890', 'imdb.nm0012345', 'tmdb.12345'],
        )
        self.assertEqual(build_identity_provider_pairs('{"Tmdb": "12345"}'), ['tmdb.12345'])
        self.assertEqual(build_identity_provider_pairs(None), [])

    def test_exact_reference_requires_embedded_person_id_or_name_only_match(self):
        self.assertTrue(
            media_item_has_exact_person_reference(
                {'People': [{'Id': 'p1', 'Name': '演员甲'}]},
                'p1',
                '演员甲',
            )
        )
        self.assertFalse(
            media_item_has_exact_person_reference(
                {'People': [{'Id': 'other', 'Name': '演员甲'}]},
                'p1',
                '演员甲',
            )
        )
        self.assertTrue(
            media_item_has_exact_person_reference(
                {'People': [{'Name': '演员甲'}]},
                'p1',
                '演员甲',
            )
        )
        self.assertIsNone(
            media_item_has_exact_person_reference(
                {'People': []},
                'p1',
                '演员甲',
            )
        )
        self.assertIsNone(
            media_item_has_exact_person_reference(
                {},
                'p1',
                '演员甲',
            )
        )

    @staticmethod
    def _emby_items_response(items):
        response = MagicMock()
        response.raise_for_status.return_value = None
        response.json.return_value = {'Items': items}
        return response

    def test_list_people_with_target_id_is_linked(self):
        list_item = {
            'Id': 'm1',
            'Name': '作品一',
            'People': [{'Id': 'p1', 'Name': '演员甲'}],
        }
        client = MagicMock()
        client.get.return_value = self._emby_items_response([list_item])
        detail_fetch = MagicMock()
        with patch.dict(self.emby_globals, {
            'emby_client': client,
            'get_emby_items_by_id': detail_fetch,
        }):
            result = self.get_person_media_references(
                'http://emby', 'token', 'p1', person_name='演员甲'
            )

        self.assertEqual(result['status'], 'linked')
        self.assertEqual(result['count'], 1)
        detail_fetch.assert_not_called()

    def test_individual_people_details_use_global_item_endpoint(self):
        def response_for_url(url, **_kwargs):
            item_id = url.rsplit('/', 1)[-1]
            response = MagicMock()
            response.raise_for_status.return_value = None
            response.json.return_value = {'Id': item_id, 'People': [{'Id': 'p1'}]}
            return response

        client = MagicMock()
        client.get.side_effect = response_for_url
        with patch.dict(self.emby_globals, {'emby_client': client}):
            details = self.get_emby_item_people_details(
                'http://emby', 'token', ['m1', 'm2'], max_workers=2
            )

        self.assertEqual([item['Id'] for item in details], ['m1', 'm2'])
        self.assertEqual(client.get.call_count, 2)
        self.assertEqual(
            {call.args[0] for call in client.get.call_args_list},
            {'http://emby/Items/m1', 'http://emby/Items/m2'},
        )

    def test_list_people_with_other_id_is_identity_alias_only(self):
        list_item = {
            'Id': 'm1',
            'People': [{'Id': 'other', 'Name': '演员甲'}],
        }
        client = MagicMock()
        client.get.return_value = self._emby_items_response([list_item])
        with patch.dict(self.emby_globals, {'emby_client': client}):
            result = self.get_person_media_references(
                'http://emby', 'token', 'p1', person_name='演员甲'
            )

        self.assertEqual(result['status'], 'identity_alias_only')
        self.assertEqual(result['count'], 0)
        self.assertEqual(result['query_count'], 1)

    def test_missing_list_people_uses_one_batched_detail_lookup(self):
        list_items = [
            {'Id': 'm1', 'Name': '作品一'},
            {'Id': 'm2', 'Name': '作品二', 'People': []},
        ]
        details = [
            {'Id': 'm1', 'People': [{'Id': 'p1', 'Name': '演员甲'}]},
            {'Id': 'm2', 'People': [{'Id': 'other', 'Name': '演员乙'}]},
        ]
        client = MagicMock()
        client.get.return_value = self._emby_items_response(list_items)
        detail_fetch = MagicMock(return_value=details)
        with patch.dict(self.emby_globals, {
            'emby_client': client,
            'get_emby_items_by_id': detail_fetch,
        }):
            result = self.get_person_media_references(
                'http://emby', 'token', 'p1', limit=10, person_name='演员甲'
            )

        self.assertEqual(result['status'], 'linked')
        self.assertEqual(result['count'], 1)
        detail_fetch.assert_called_once()
        self.assertEqual(detail_fetch.call_args.kwargs['item_ids'], ['m1', 'm2'])
        self.assertEqual(detail_fetch.call_args.kwargs['fields'], 'People')
        self.assertTrue(detail_fetch.call_args.kwargs['raise_on_error'])

    def test_detail_people_with_other_ids_is_verified_alias_only(self):
        client = MagicMock()
        client.get.return_value = self._emby_items_response([{'Id': 'm1', 'Name': '作品一'}])
        detail_fetch = MagicMock(
            return_value=[{'Id': 'm1', 'People': [{'Id': 'other'}]}]
        )
        with patch.dict(self.emby_globals, {
            'emby_client': client,
            'get_emby_items_by_id': detail_fetch,
        }):
            result = self.get_person_media_references(
                'http://emby', 'token', 'p1', person_name='演员甲'
            )

        self.assertEqual(result['status'], 'identity_alias_only')
        self.assertEqual(result['count'], 0)

    def test_detail_without_usable_people_fails_closed(self):
        list_item = {'Id': 'm1', 'Name': 'AOZ-313'}
        client = MagicMock()
        client.get.return_value = self._emby_items_response([list_item])
        detail_fetch = MagicMock(return_value=[{'Id': 'm1', 'People': []}])
        with patch.dict(self.emby_globals, {
            'emby_client': client,
            'get_emby_items_by_id': detail_fetch,
        }):
            result = self.get_person_media_references(
                'http://emby', 'token', '619697', person_name='复现人物'
            )

        self.assertEqual(result['status'], 'people_unavailable')
        self.assertIsNone(result['count'])
        self.assertEqual(result['unverified_items'][0]['Name'], 'AOZ-313')

    def test_unresolved_batch_detail_uses_bounded_individual_fallback(self):
        list_items = [
            {'Id': 'm1', 'Name': '作品一'},
            {'Id': 'm2', 'Name': '作品二'},
        ]
        detail_fetch = MagicMock(return_value=[
            {'Id': 'm1', 'People': [{'Id': 'other'}]},
            {'Id': 'm2', 'People': []},
        ])
        individual_fetch = MagicMock(return_value=[
            {'Id': 'm2', 'People': [{'Id': 'p1', 'Name': '演员甲'}]},
        ])
        client = MagicMock()
        client.get.return_value = self._emby_items_response(list_items)
        with patch.dict(self.emby_globals, {
            'emby_client': client,
            'get_emby_items_by_id': detail_fetch,
            'get_emby_item_people_details': individual_fetch,
        }):
            result = self.get_person_media_references(
                'http://emby', 'token', 'p1', person_name='演员甲'
            )

        self.assertEqual(result['status'], 'linked')
        self.assertEqual(result['count'], 1)
        individual_fetch.assert_called_once_with('http://emby', 'token', ['m2'])

    def test_individual_detail_without_people_still_fails_closed(self):
        client = MagicMock()
        client.get.return_value = self._emby_items_response([{'Id': 'm1'}])
        with patch.dict(self.emby_globals, {
            'emby_client': client,
            'get_emby_items_by_id': MagicMock(return_value=[{'Id': 'm1', 'People': []}]),
            'get_emby_item_people_details': MagicMock(return_value=[{'Id': 'm1', 'People': []}]),
        }):
            result = self.get_person_media_references('http://emby', 'token', 'p1')

        self.assertEqual(result['status'], 'people_unavailable')
        self.assertIsNone(result['count'])

    def test_too_many_individual_details_fail_closed_without_unbounded_requests(self):
        list_items = [{'Id': f'm{index}'} for index in range(201)]
        client = MagicMock()
        client.get.side_effect = [
            self._emby_items_response(list_items[:200]),
            self._emby_items_response(list_items[200:]),
        ]
        individual_fetch = MagicMock()
        with patch.dict(self.emby_globals, {
            'emby_client': client,
            'get_emby_items_by_id': MagicMock(
                return_value=[{'Id': item['Id'], 'People': []} for item in list_items]
            ),
            'get_emby_item_people_details': individual_fetch,
        }):
            result = self.get_person_media_references('http://emby', 'token', 'p1')

        self.assertEqual(result['status'], 'people_unavailable')
        individual_fetch.assert_not_called()

    def test_detail_connection_failure_has_distinct_status(self):
        client = MagicMock()
        client.get.return_value = self._emby_items_response([{'Id': 'm1'}])
        detail_fetch = MagicMock(side_effect=requests.exceptions.ConnectionError('offline'))
        with patch.dict(self.emby_globals, {
            'emby_client': client,
            'get_emby_items_by_id': detail_fetch,
        }):
            result = self.get_person_media_references(
                'http://emby', 'token', 'p1'
            )

        self.assertEqual(result['status'], 'connection_failed')

    def test_malformed_list_response_has_distinct_status(self):
        response = MagicMock()
        response.raise_for_status.return_value = None
        response.json.return_value = {'Items': 'not-a-list'}
        client = MagicMock()
        client.get.return_value = response
        with patch.dict(self.emby_globals, {'emby_client': client}):
            result = self.get_person_media_references(
                'http://emby', 'token', 'p1'
            )

        self.assertEqual(result['status'], 'invalid_response')

    def test_delete_task_skips_people_unavailable_without_calling_delete(self):
        processor = SimpleNamespace(
            emby_url='http://emby',
            emby_api_key='token',
            is_stop_requested=lambda: False,
        )
        candidate = {'person_id': 'p1', 'person_name': '演员甲'}
        unavailable = {
            'status': 'people_unavailable',
            'count': None,
            'items': [],
            'query_count': 1,
        }
        actor_tree = ast.parse((self.repo_root / 'tasks' / 'actors.py').read_text())
        delete_task_node = next(
            node
            for node in actor_tree.body
            if isinstance(node, ast.FunctionDef)
            and node.name == 'task_delete_selected_ghost_actors'
        )
        person_cleanup_db = MagicMock()
        person_cleanup_db.get_candidates_by_ids.return_value = [candidate]
        person_cleanup_db.list_protected_libraries.return_value = []
        person_cleanup_db.get_protected_person_ids.return_value = set()
        person_cleanup_db.get_protected_person_names.return_value = set()
        emby = MagicMock()
        emby.get_person_media_references.return_value = unavailable
        namespace = {
            'person_cleanup_db': person_cleanup_db,
            'task_manager': MagicMock(),
            '_scan_protected_library_people': MagicMock(return_value={}),
            'build_person_name_protection_keys': lambda values: set(values),
            'person_name_protection_keys': person_name_protection_keys,
            'classify_reference_check': classify_reference_check,
            'reference_check_failure_message': reference_check_failure_message,
            'emby': emby,
            'logger': MagicMock(),
            'get_db_connection': MagicMock(),
            'time': MagicMock(),
        }
        exec(
            compile(
                ast.Module(body=[delete_task_node], type_ignores=[]),
                'tasks/actors.py',
                'exec',
            ),
            namespace,
        )

        namespace['task_delete_selected_ghost_actors'](processor, ['p1'])

        emby.delete_person_custom_api.assert_not_called()
        person_cleanup_db.mark_candidate_checked.assert_called_once()
        self.assertIn(
            '人物明细不可核验',
            person_cleanup_db.mark_candidate_checked.call_args.args[1],
        )

    def test_route_reports_people_unavailable_instead_of_connection_failure(self):
        route_tree = ast.parse((self.repo_root / 'routes' / 'person_cleanup.py').read_text())
        verify_node = next(
            node
            for node in route_tree.body
            if isinstance(node, ast.FunctionDef)
            and node.name == 'verify_person_cleanup_candidate'
        )
        verify_node.decorator_list = []
        candidate = {
            'person_id': '619697',
            'person_name': '复现人物',
            'provider_ids_json': {},
        }
        refreshed_candidate = {**candidate, 'last_error': '作品人物明细不可核验'}
        person_cleanup_db = MagicMock()
        person_cleanup_db.get_candidates_by_ids.side_effect = [
            [candidate],
            [refreshed_candidate],
        ]
        emby = MagicMock()
        emby.get_person_media_references.return_value = {
            'status': 'people_unavailable',
            'count': None,
            'items': [],
            'query_count': 1,
            'unverified_items': [{'Id': 'm1', 'Name': 'AOZ-313'}],
        }
        namespace = {
            'person_cleanup_db': person_cleanup_db,
            'emby': emby,
            'extensions': SimpleNamespace(
                media_processor_instance=SimpleNamespace(
                    emby_url='http://emby',
                    emby_api_key='token',
                ),
                EMBY_SERVER_ID='',
            ),
            'classify_reference_check': classify_reference_check,
            'reference_check_failure_message': reference_check_failure_message,
            '_serialize_reference_items': lambda items: [
                {
                    'id': str(item.get('Id') or ''),
                    'name': item.get('Name') or '未命名作品',
                    'type': item.get('Type') or '',
                    'series_name': item.get('SeriesName') or '',
                    'production_year': item.get('ProductionYear'),
                }
                for item in items or []
            ],
            '_refreshed_candidate': lambda person_id, fallback: (
                person_cleanup_db.get_candidates_by_ids([person_id])[0]
            ),
            'config_manager': SimpleNamespace(APP_CONFIG={}),
            'constants': SimpleNamespace(
                CONFIG_OPTION_EMBY_PUBLIC_URL='emby_public_url',
                CONFIG_OPTION_EMBY_SERVER_URL='emby_server_url',
            ),
            'jsonify': lambda payload: payload,
        }
        exec(
            compile(
                ast.Module(body=[verify_node], type_ignores=[]),
                'routes/person_cleanup.py',
                'exec',
            ),
            namespace,
        )

        response, status_code = namespace['verify_person_cleanup_candidate']('619697')

        self.assertEqual(status_code, 409)
        self.assertEqual(response['status'], 'people_unavailable')
        self.assertIn('人物明细不可核验', response['message'])
        self.assertNotIn('无法连接', response['message'])
        self.assertEqual(response['unverified_items'][0]['name'], 'AOZ-313')
        self.assertEqual(response['candidate'], refreshed_candidate)

    def test_legacy_destructive_tasks_are_disabled_and_unregistered(self):
        repo_root = Path(__file__).resolve().parents[1]
        actor_tree = ast.parse((repo_root / 'tasks' / 'actors.py').read_text())
        functions = {
            node.name: node
            for node in actor_tree.body
            if isinstance(node, ast.FunctionDef)
        }
        for function_name in (
            '_disabled_legacy_task_purge_ghost_actors',
            '_disabled_legacy_task_purge_unregistered_actors',
        ):
            self.assertIsInstance(functions[function_name].body[0], ast.Raise)

        registry_source = (repo_root / 'tasks' / 'core.py').read_text()
        self.assertNotIn("'purge-ghost-actors':", registry_source)
        self.assertNotIn("'purge-unregistered-actors':", registry_source)

    def test_manual_verification_never_calls_person_delete_api(self):
        repo_root = Path(__file__).resolve().parents[1]
        route_tree = ast.parse((repo_root / 'routes' / 'person_cleanup.py').read_text())
        functions = {
            node.name: node
            for node in route_tree.body
            if isinstance(node, ast.FunctionDef)
        }
        verify_source = ast.unparse(functions['verify_person_cleanup_candidate'])
        delete_source = ast.unparse(functions['delete_person_cleanup_candidates'])

        self.assertIn('get_person_media_references', verify_source)
        self.assertIn('remove_candidate', verify_source)
        self.assertIn('reference_check_failure_message', verify_source)
        self.assertIn("reference_status == 'people_unavailable'", verify_source)
        self.assertNotIn('无法连接 Emby 完成人物关联核对', verify_source)
        self.assertNotIn('delete_person_custom_api', verify_source)
        self.assertNotIn('is_verified_orphan_candidate', delete_source)
        actor_source = (repo_root / 'tasks' / 'actors.py').read_text()
        self.assertIn('get_person_media_references', actor_source)
        self.assertIn("'people_unavailable'", actor_source)
        self.assertIn("'connection_failed'", actor_source)
        self.assertIn("'invalid_response'", actor_source)
        self.assertIn("reference_status == 'linked'", actor_source)
        self.assertIn("{'orphan', 'identity_alias_only'}", actor_source)
        self.assertIn('person_name=person_name', actor_source)
        emby_source = (repo_root / 'handler' / 'emby.py').read_text()
        self.assertIn("'Fields': 'SeriesName,ProductionYear,People'", emby_source)
        self.assertIn('media_item_has_exact_person_reference', emby_source)
        self.assertIn('get_emby_items_by_id', emby_source)
        self.assertIn('get_emby_item_people_details', emby_source)

    def test_protected_library_snapshots_are_merged_not_replaced(self):
        repo_root = Path(__file__).resolve().parents[1]
        db_tree = ast.parse((repo_root / 'database' / 'person_cleanup_db.py').read_text())
        functions = {
            node.name: node
            for node in db_tree.body
            if isinstance(node, ast.FunctionDef)
        }
        merge_source = ast.unparse(functions['merge_protected_people_for_library'])
        merge_names_source = ast.unparse(functions['merge_protected_names_for_library'])
        replace_candidates_source = ast.unparse(functions['replace_candidates'])
        task_source = (repo_root / 'tasks' / 'actors.py').read_text()
        schema_source = (repo_root / 'database' / 'connection.py').read_text()

        self.assertNotIn('DELETE FROM person_cleanup_protected_people', merge_source)
        self.assertNotIn('DELETE FROM person_cleanup_protected_names', merge_names_source)
        self.assertIn('_exclude_protected_candidates', replace_candidates_source)
        self.assertIn('ON CONFLICT', merge_source)
        self.assertIn('ON CONFLICT', merge_names_source)
        self.assertIn('merge_protected_people_for_library', task_source)
        self.assertIn('merge_protected_names_for_library', task_source)
        self.assertIn('_scan_protected_library_people', task_source)
        self.assertIn('all_person_names', task_source)
        self.assertIn('get_protected_person_ids', task_source)
        self.assertIn('get_protected_person_names', task_source)
        self.assertIn('capture_library_ids=protected_library_ids', task_source)
        self.assertIn('include_protected=True', task_source)
        self.assertIn('person_id in protected_person_ids', task_source)
        self.assertIn('person_name_protection_keys(person_name)', task_source)
        self.assertIn('person_cleanup_protected_libraries', schema_source)
        self.assertIn('person_cleanup_protected_people', schema_source)
        self.assertIn('person_cleanup_protected_names', schema_source)

    def test_protected_person_count_does_not_add_name_keys(self):
        db_tree = ast.parse((self.repo_root / 'database' / 'person_cleanup_db.py').read_text())
        list_node = next(
            node
            for node in db_tree.body
            if isinstance(node, ast.FunctionDef) and node.name == 'list_protected_libraries'
        )
        source = ast.unparse(list_node)

        self.assertIn('AS protected_person_count', source)
        self.assertIn('AS protected_name_count', source)
        self.assertNotIn(')) + (SELECT COUNT(*)', source)

    def test_unverified_person_cleanup_rows_are_not_selectable(self):
        source = (self.repo_root / 'emby-actor-ui' / 'src' / 'components' / 'PersonCleanupPage.vue').read_text()

        self.assertIn("disabled: (row) => !isVerifiedOrphan(row)", source)
        self.assertIn('必须先通过“核对详情”', source)


if __name__ == '__main__':
    unittest.main()
