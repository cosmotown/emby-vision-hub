import unittest
from unittest.mock import MagicMock, patch

import requests
from services.person_sync_safety import PersonEnumeration, PersonResultScope, classify_identity_match
from handler import emby
from handler.actor_sync import UnifiedSyncHandler
from tasks.maintenance import _merge_person_identity_map_data


def page(ids, total):
    return {'TotalRecordCount': total, 'Items': [
        {'Id': pid, 'Name': 'fixture', 'Type': 'Person', 'ProviderIds': {}} for pid in ids]}


def response(data):
    result = MagicMock()
    result.json.return_value = data
    return result


class IdentityTests(unittest.TestCase):
    def test_exact_tmdb_without_conflicts(self):
        self.assertEqual(classify_identity_match({'tmdb_person_id': 123}, {'tmdb_person_id': '123'}), 'SAFE_EXACT_MATCH')

    def test_exact_douban_without_tmdb(self):
        self.assertEqual(classify_identity_match({'douban_celebrity_id': '456'}, {'douban_celebrity_id': '456'}), 'SAFE_EXACT_MATCH')

    def test_matching_tmdb_with_conflicting_douban(self):
        self.assertEqual(classify_identity_match({'tmdb_person_id': 123, 'douban_celebrity_id': '456'},
                                                {'tmdb_person_id': 123, 'douban_celebrity_id': '999'}), 'CONFLICT')

    def test_same_name_is_not_identity(self):
        self.assertEqual(classify_identity_match({'primary_name': '同名'}, {'primary_name': '同名'}), 'REVIEW_REQUIRED')

    def test_same_name_different_provider(self):
        self.assertEqual(classify_identity_match({'primary_name': '同名', 'tmdb_person_id': 1},
                                                {'primary_name': '同名', 'tmdb_person_id': 2}), 'CONFLICT')

    def test_distinct_names_same_stable_id(self):
        self.assertEqual(classify_identity_match({'primary_name': 'Name', 'tmdb_person_id': 1},
                                                {'primary_name': '名字', 'tmdb_person_id': 1}), 'SAFE_EXACT_MATCH')

    def test_empty_and_malformed_ids_need_review(self):
        for value in (None, '', True, '0', '1,2', '-1'):
            with self.subTest(value=value):
                self.assertEqual(classify_identity_match({'tmdb_person_id': value}, {'tmdb_person_id': value}), 'REVIEW_REQUIRED')

    def test_different_emby_ids_are_not_overwritten(self):
        self.assertEqual(classify_identity_match({'tmdb_person_id': 1, 'emby_person_id': 'old'},
                                                {'tmdb_person_id': 1, 'emby_person_id': 'new'}), 'CONFLICT')


class EnumerationTests(unittest.TestCase):
    def enumerate(self, responses, **kwargs):
        state = PersonEnumeration()
        with patch.object(emby.config_manager, 'APP_CONFIG', {}), patch.object(emby.emby_client, 'get', side_effect=responses):
            batches = list(emby.get_all_persons_from_emby('http://fixture.invalid', 'test', 'u',
                batch_size=2, force_full_scan=True, enumeration=state, **kwargs))
        return state, batches

    def test_full_snapshot(self):
        state, batches = self.enumerate([response(page([], 2)), response(page(['1', '2'], 2)),
                                        response(page([], 2)), response(page([], 2))])
        self.assertTrue(state.complete)
        self.assertEqual(state.scope, PersonResultScope.FULL_SNAPSHOT)
        self.assertEqual(len(batches[0]), 2)

    def test_partial_http_failure_keeps_partial_evidence(self):
        for status in (429, 500):
            with self.subTest(status=status):
                state, batches = self.enumerate([response(page([], 3)), response(page(['1', '2'], 3)),
                                                requests.HTTPError(str(status))])
                self.assertFalse(state.complete)
                self.assertEqual(state.scope, PersonResultScope.PARTIAL_PAGE)
                self.assertEqual(len(batches), 1)

    def test_premature_empty_is_not_full(self):
        state, _ = self.enumerate([response(page([], 2)), response(page([], 2)), response(page([], 2))])
        self.assertFalse(state.complete)

    def test_proven_empty_is_full(self):
        state, batches = self.enumerate([response(page([], 0)), response(page([], 0)), response(page([], 0))])
        self.assertTrue(state.complete)
        self.assertEqual(batches, [])

    def test_missing_count_with_callback_remains_incomplete(self):
        state, batches = self.enumerate([response({}), response(page(['1'], 1)), response(page([], 1)),
                                        response(page([], 1))], update_status_callback=MagicMock())
        self.assertFalse(state.complete)
        self.assertEqual(len(batches), 1)

    def test_duplicate_id_fails_closed(self):
        state, batches = self.enumerate([response(page([], 3)), response(page(['1', '2'], 3)), response(page(['2'], 3))])
        self.assertEqual(state.error, 'duplicate_person')
        self.assertEqual(len(batches), 1)

    def test_final_count_drift(self):
        state, _ = self.enumerate([response(page([], 1)), response(page(['1'], 1)), response(page([], 1)), response(page([], 2))])
        self.assertFalse(state.complete)

    def test_invalid_response(self):
        state, _ = self.enumerate([response(page([], 1)), response({'Items': None})])
        self.assertFalse(state.complete)

    def test_filtered_never_claims_full(self):
        state = PersonEnumeration()
        with patch.object(emby.config_manager, 'APP_CONFIG', {emby.constants.CONFIG_OPTION_EMBY_LIBRARIES_TO_PROCESS: ['library']}), \
             patch.object(emby, 'get_emby_library_items', return_value=[{'People': [{'Id': '1'}]}]), \
             patch.object(emby, 'get_emby_items_by_id', return_value=page(['1'], 1)['Items']), \
             patch.object(emby.emby_client, 'get') as http:
            self.assertEqual(len(list(emby.get_all_persons_from_emby('http://fixture.invalid', 'test', 'u', enumeration=state))), 1)
            self.assertFalse(state.complete)
            self.assertEqual(state.scope, PersonResultScope.FILTERED_RESULT)
            http.assert_not_called()

    def test_sync_never_unlinks_missing_mapping_for_any_scope(self):
        for scope in PersonResultScope:
            with self.subTest(scope=scope):
                handler = UnifiedSyncHandler.__new__(UnifiedSyncHandler)
                handler.actor_db_manager = MagicMock()
                handler.emby_url, handler.emby_api_key, handler.emby_user_id = 'http://fixture.invalid', 'test', 'u'
                conn = MagicMock()
                cursor = conn.cursor.return_value.__enter__.return_value
                cursor.fetchall.return_value = [{'emby_person_id': 'old'}]
                def enumerate_people(*args, **kwargs):
                    state = kwargs['enumeration']
                    state.scope, state.complete = scope, scope == PersonResultScope.FULL_SNAPSHOT
                    yield from []
                with patch('handler.actor_sync.connection.get_db_connection') as connect, \
                     patch('handler.actor_sync.emby.get_all_persons_from_emby', side_effect=enumerate_people), \
                     patch('handler.actor_sync.logger.trace', create=True):
                    connect.return_value.__enter__.return_value = conn
                    handler.sync_emby_person_map_to_db()
                handler.actor_db_manager.disassociate_emby_ids.assert_not_called()
                self.assertFalse(any('UPDATE' in str(call) or 'DELETE' in str(call) for call in cursor.execute.call_args_list))


class ImportTests(unittest.TestCase):
    def test_conflict_and_multiple_records_are_report_only(self):
        cases = [
            ([{'map_id': 1, 'tmdb_person_id': 123, 'douban_celebrity_id': '456'}], 1),
            ([{'map_id': 1, 'tmdb_person_id': 123}, {'map_id': 2, 'douban_celebrity_id': '999'}], 0),
        ]
        for records, conflicts in cases:
            cursor = MagicMock()
            cursor.fetchall.return_value = records
            stats = _merge_person_identity_map_data(cursor, 'person_identity_map',
                ['tmdb_person_id', 'douban_celebrity_id'], [(123, '999')])
            self.assertEqual(stats['merged_and_deleted'], 0)
            self.assertEqual(len(stats['review_items']), 1)
            self.assertEqual(stats['conflicts'], conflicts)
            self.assertEqual(len(cursor.execute.call_args_list), 1)
            # SELECT ... FOR UPDATE locks only; it is not an UPDATE statement.
            self.assertIn('SELECT * FROM person_identity_map', str(cursor.execute.call_args.args[0]))

    def test_name_only_import_is_review(self):
        cursor = MagicMock()
        stats = _merge_person_identity_map_data(cursor, 'person_identity_map', ['primary_name'], [('同名',)])
        self.assertEqual(stats['review_required'], 1)
        cursor.execute.assert_not_called()


if __name__ == '__main__':
    unittest.main()
