import copy
import json
import os
from pathlib import Path
import random
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from core_processor import MediaProcessor, _aggregate_series_cast_from_tmdb_data
from handler import emby
from services.person_write_safety import (
    ExactPersonTarget, PersonOperationJournal, cap_episode_credits, identity_lock,
    manual_person_target, provider_snapshot, select_person_candidates,
    validate_person_write_target, restrict_series_cached_cast,
)


def person(identity='100', object_id='p1', name='Same Name'):
    return {'Id': object_id, 'Name': name, 'Type': 'Person', 'ProviderIds': {'Tmdb': identity}}


class PersonScopeTests(unittest.TestCase):
    def test_series_eight_main_not_thirty_six_episode_people(self):
        main = [{'id': i, 'name': str(i), 'order': i} for i in range(1, 9)]
        episodes = [{'credits': {
            'cast': [{'id': i} for i in range(101, 119)],
            'guest_stars': [{'id': i} for i in range(119, 137)],
        }}]
        original = copy.deepcopy(episodes)
        result = _aggregate_series_cast_from_tmdb_data({'credits': {'cast': main}}, episodes)
        self.assertEqual([p['id'] for p in result], list(range(1, 9)))
        self.assertEqual(episodes, original)

    def test_series_empty_main_does_not_promote_episode(self):
        result = _aggregate_series_cast_from_tmdb_data(
            {'credits': {'cast': []}}, [{'credits': {'cast': [{'id': 1}]}}],
        )
        self.assertEqual(result, [])

    def test_old_aggregate_cache_is_restricted_to_current_main_scope(self):
        cache = [{'id': i} for i in range(1, 45)]
        source = {'credits': {'cast': [{'id': i} for i in range(1, 9)]}}
        self.assertEqual(len(restrict_series_cached_cast(cache, source)), 8)

    def test_aggregate_only_source_does_not_prove_main_scope(self):
        with self.assertRaises(ValueError):
            restrict_series_cached_cast([{'id': 1}], {'aggregate_credits': {'cast': [{'id': 1}]}})

    def test_related_identity_scope_caps_before_full_enrichment(self):
        processor = MediaProcessor.__new__(MediaProcessor)
        processor.config = {'max_actors_to_process': 50}
        processor.emby_url, processor.emby_api_key = 'http://fixture', 'hidden'
        people = [{'Id': str(i), 'Type': 'Actor'} for i in range(1, 101)]
        by_id = {str(i): person(str(i), str(i)) for i in range(1, 101)}
        provider_cast = [{'id': i, 'order': i} for i in range(1, 101)]
        with patch.object(emby, 'read_person_identity_batch', return_value=by_id):
            chosen = processor._select_related_person_targets(people, provider_cast, 'Series')
            random.Random(42).shuffle(people)
            again = processor._select_related_person_targets(people, provider_cast, 'Series')
        self.assertEqual(len(chosen), 50)
        self.assertEqual(chosen, again)
        self.assertEqual([p['Id'] for p in chosen], [str(i) for i in range(1, 51)])

    def test_related_episode_guests_do_not_enter_series_enrichment(self):
        processor = MediaProcessor.__new__(MediaProcessor)
        processor.config = {}
        processor.emby_url, processor.emby_api_key = 'http://fixture', 'hidden'
        people = [{'Id': str(i), 'Type': 'Actor'} for i in range(1, 45)]
        by_id = {str(i): person(str(i), str(i)) for i in range(1, 45)}
        with patch.object(emby, 'read_person_identity_batch', return_value=by_id):
            chosen = processor._select_related_person_targets(
                people, [{'id': i, 'order': i} for i in range(1, 9)], 'Series')
        self.assertEqual(len(chosen), 8)

    def test_cap_is_deterministic_for_one_hundred_candidates(self):
        actors = [{'id': i, 'order': 0} for i in range(1, 101)]
        expected = select_person_candidates(actors, 50)
        for seed in range(10):
            random.Random(seed).shuffle(actors)
            self.assertEqual(select_person_candidates(actors, 50), expected)
        self.assertEqual(len(expected), 50)

    def test_cap_prioritizes_existing_direct_identity(self):
        actors = [{'id': i, 'order': i} for i in range(1, 101)]
        chosen = select_person_candidates(actors, 2, direct_provider_ids=['99'])
        self.assertEqual([a['id'] for a in chosen], [99, 1])

    def test_eight_does_not_expand_to_fifty(self):
        self.assertEqual(len(select_person_candidates([{'id': i} for i in range(1, 9)], 50)), 8)

    def test_invalid_and_duplicate_identity_do_not_consume_slots(self):
        actors = [{'id': 1}, {'id': '1'}, {'name': 'Name only'}, {'id': '-1'}, {'id': 2}]
        self.assertEqual(len(select_person_candidates(actors, 50)), 2)

    def test_episode_cast_and_guests_share_cap_without_parent_mutation(self):
        credits = {'cast': [{'id': i, 'order': i} for i in range(1, 31)],
                   'guest_stars': [{'id': i, 'order': i} for i in range(21, 81)],
                   'crew': [{'id': 900}]}
        original = copy.deepcopy(credits)
        result = cap_episode_credits(credits, 50)
        self.assertEqual(credits, original)
        self.assertEqual(len(result['cast']) + len(result['guest_stars']), 50)
        self.assertEqual(result['crew'], credits['crew'])
        self.assertTrue(result['guest_stars'])

    def test_pipeline_caps_before_provider_and_mapping_work(self):
        processor = MediaProcessor.__new__(MediaProcessor)
        processor.config = {'max_actors_to_process': 50}
        import constants
        processor.config[constants.CONFIG_OPTION_MAX_ACTORS_TO_PROCESS] = 50
        processor.config[constants.CONFIG_OPTION_REMOVE_ACTORS_WITHOUT_AVATARS] = False
        processor.ai_translator = None
        processor.emby_url, processor.emby_api_key, processor.emby_user_id = 'http://fixture', 'hidden', 'u'
        processor.actor_db_manager = MagicMock()
        processor.actor_db_manager.upsert_person.return_value = (1, 'UPDATED')
        processor.is_stop_requested = lambda: False
        actors = [{'id': i, 'order': i, 'name': 'Actor ' + str(i), 'profile_path': '/test.jpg'}
                  for i in range(1, 101)]
        with patch('core_processor.tmdb.get_person_details_tmdb',
                   return_value={'profile_path': '/test.jpg'}) as lookup, \
             patch.object(emby, 'resolve_exact_person_identity', return_value=[]) as identity_lookup, \
             patch('core_processor.logger.trace', create=True), \
             patch('actor_utils.logger.trace', create=True):
            result = processor._process_cast_list(
                actors, [], [], {'Id': 'm', 'Type': 'Series', 'Genres': []},
                MagicMock(), 'test', None,
            )
        self.assertEqual(len(result), 50)
        self.assertLessEqual(lookup.call_count, 50)
        self.assertEqual(identity_lookup.call_count, 50)
        self.assertEqual(processor.actor_db_manager.upsert_person.call_count, 50)
        self.assertEqual({str(c.args[1]['id']) for c in processor.actor_db_manager.upsert_person.call_args_list},
                         {str(i) for i in range(1, 51)})


class ExactTargetTests(unittest.TestCase):
    def setUp(self):
        self.current = person()
        self.target = ExactPersonTarget.from_person(self.current, 'media', 'Series')

    def test_name_change_does_not_change_identity(self):
        current = dict(self.current, Name='Different')
        self.assertIsNone(validate_person_write_target(self.target, current, [current], {'Name': 'New'}))

    def test_same_name_other_provider_does_not_pass(self):
        other = person('200', 'p1')
        self.assertEqual(validate_person_write_target(self.target, other, [other]), 'identity_changed')

    def test_stale_object_mapping_does_not_pass(self):
        other = person('100', 'p2')
        self.assertEqual(validate_person_write_target(self.target, other, [other]), 'stale_emby_mapping')

    def test_missing_provider_fails_closed(self):
        with self.assertRaises(ValueError):
            ExactPersonTarget.from_person(dict(self.current, ProviderIds={}), 'media', 'Series')
        self.assertEqual(validate_person_write_target(None, self.current, [self.current]), 'identity_missing')

    def test_missing_person_fails_closed(self):
        self.assertEqual(validate_person_write_target(self.target, None, []), 'person_not_found')

    def test_duplicate_provider_fails_closed(self):
        self.assertEqual(validate_person_write_target(self.target, self.current,
                         [self.current, person('100', 'other')]), 'identity_ambiguous')

    def test_unavailable_lookup_is_not_absence(self):
        self.assertEqual(validate_person_write_target(self.target, self.current, None), 'identity_unavailable')

    def test_identity_cannot_be_rebound_by_payload(self):
        self.assertEqual(validate_person_write_target(self.target, self.current, [self.current],
                         {'ProviderIds': {'Tmdb': '200'}}), 'provider_mutation_unproven')

    def test_manual_frozen_snapshot_rejects_identity_drift(self):
        actor = {'emby_person_id': 'p1', 'tmdbId': '100', 'name': 'Edited',
                 'provider_identity_snapshot': {'Tmdb': '100'}}
        target = manual_person_target(actor, 'media', 'Series')
        self.assertEqual(validate_person_write_target(target, person('200'), [person('200')]), 'identity_changed')
        actor['tmdbId'] = '200'
        with self.assertRaises(ValueError):
            manual_person_target(actor, 'media', 'Series')

    def test_manual_missing_snapshot_is_not_guessed(self):
        with self.assertRaises(ValueError):
            manual_person_target({'emby_person_id': 'p1', 'tmdbId': '100'}, 'm', 'Movie')

    def test_manual_identity_drift_aborts_before_database_or_person_writes(self):
        processor = MediaProcessor.__new__(MediaProcessor)
        processor.emby_url, processor.emby_api_key, processor.emby_user_id = 'http://fixture', 'hidden', 'u'
        processor.manual_edit_cache = {}
        processor.config = {}
        source = {'Id': 'media', 'Type': 'Series', 'People': [{'Id': 'p1', 'Type': 'Actor'}]}
        submitted = [{'emby_person_id': 'p1', 'tmdbId': '100', 'name': 'Edited',
                      'provider_identity_snapshot': {'Tmdb': '100'}}]
        with patch.object(emby, 'get_emby_item_details', side_effect=[source, person('200')]), \
             patch.object(emby, 'resolve_exact_person_identity', return_value=[person('200')]), \
             patch('core_processor.get_central_db_connection') as db, \
             patch.object(emby, 'update_person_details') as write:
            self.assertFalse(processor.process_item_with_manual_cast('media', submitted, 'Fixture'))
        db.assert_not_called()
        write.assert_not_called()


class WriteGateTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.env = patch.dict(os.environ, APP_DATA_DIR=self.temp.name)
        self.env.start()
        self.addCleanup(self.env.stop)

    def test_only_exact_target_posted_same_name_other_person_untouched(self):
        a, b = person(), person('200', 'p2')
        target = ExactPersonTarget.from_person(a, 'media', 'Series')
        with patch.object(emby, 'wait_for_server_idle'), \
             patch.object(emby, 'is_person_name_write_safe', return_value=True), \
             patch.object(emby, 'resolve_exact_person_identity', return_value=[a]), \
             patch.object(emby, 'get_emby_item_details', return_value=a), \
             patch.object(emby.emby_client, 'post') as post:
            self.assertTrue(emby.update_person_details('p1', {'Name': 'New'}, 'http://fixture', 'hidden', 'u', target=target))
        post.assert_called_once()
        self.assertTrue(post.call_args.args[0].endswith('/Items/p1'))
        self.assertEqual(b['Name'], 'Same Name')

    def test_changed_identity_does_not_post(self):
        target = ExactPersonTarget.from_person(person(), 'media', 'Series')
        with patch.object(emby, 'wait_for_server_idle'), \
             patch.object(emby, 'resolve_exact_person_identity', return_value=[person('200')]), \
             patch.object(emby, 'get_emby_item_details', return_value=person('200')), \
             patch.object(emby.emby_client, 'post') as post:
            self.assertFalse(emby.update_person_details('p1', {'Name': 'New'}, 'http://fixture', 'hidden', 'u', target=target))
        post.assert_not_called()

    def test_generic_item_updater_cannot_bypass_person_gate(self):
        with patch.object(emby, 'wait_for_server_idle'), \
             patch.object(emby, 'get_emby_item_details', return_value=person()), \
             patch.object(emby.emby_client, 'post') as post:
            self.assertFalse(emby.update_emby_item_details('p1', {'Name': 'New'}, 'http://fixture', 'hidden', 'u'))
        post.assert_not_called()

    def test_resolver_does_not_accept_truncated_or_inexact_response(self):
        for data in ({'Items': [person()], 'TotalRecordCount': 3},
                     {'Items': [person('200')], 'TotalRecordCount': 1},
                     {'Items': [], 'TotalRecordCount': None}):
            response = MagicMock()
            response.json.return_value = data
            with patch.object(emby.emby_client, 'get', return_value=response):
                self.assertIsNone(emby.resolve_exact_person_identity('http://fixture', 'hidden', 'tmdb', '100'))

    def test_resolver_uses_emby_dot_provider_syntax(self):
        response = MagicMock()
        response.json.return_value = {'Items': [person()], 'TotalRecordCount': 1}
        with patch.object(emby.emby_client, 'get', return_value=response) as read:
            self.assertEqual(emby.resolve_exact_person_identity('http://fixture', 'hidden', 'tmdb', '100'), [person()])
        self.assertEqual(read.call_args.kwargs['params']['AnyProviderIdEquals'], 'tmdb.100')

    def test_secondary_provider_ambiguity_also_denies_write(self):
        current = dict(person(), ProviderIds={'Tmdb': '100', 'Douban': '999'})
        target = ExactPersonTarget.from_person(current, 'media', 'Series')
        with patch.object(emby, 'wait_for_server_idle'), \
             patch.object(emby, 'resolve_exact_person_identity',
                          side_effect=lambda base, key, provider, identity:
                          [current, dict(current, Id='p2')] if provider == 'douban' else [current]), \
             patch.object(emby, 'get_emby_item_details', return_value=current), \
             patch.object(emby.emby_client, 'post') as post:
            self.assertFalse(emby.update_person_details('p1', {'Name': 'New'}, 'http://fixture', 'hidden', 'u', target=target))
        post.assert_not_called()

    def test_name_only_media_relation_cannot_create_person(self):
        with patch.object(emby, 'wait_for_server_idle'), \
             patch.object(emby, 'get_emby_item_details', return_value={'Id': 'media', 'Type': 'Movie', 'People': []}), \
             patch.object(emby.emby_client, 'post') as post:
            self.assertFalse(emby.update_emby_item_details('media', {'People': [{'Name': 'Name only'}]},
                             'http://fixture', 'hidden', 'u'))
        post.assert_not_called()

    def test_avatar_upload_requires_exact_target_before_download(self):
        with patch.object(emby.emby_client, 'get') as read, patch.object(emby.emby_client, 'post') as post:
            self.assertFalse(emby.upload_item_primary_image_from_url('p1',
                             'https://img1.doubanio.com/test.jpg', 'http://fixture', 'hidden'))
        read.assert_not_called()
        post.assert_not_called()

    def test_same_name_collision_is_deny_only_not_identity_resolution(self):
        response = MagicMock()
        response.json.return_value = {'Items': [person(), person('200', 'p2')], 'TotalRecordCount': 2}
        with patch.object(emby.emby_client, 'get', return_value=response):
            self.assertFalse(emby.is_person_name_write_safe('http://fixture', 'hidden', 'Same Name', 'p1'))
            self.assertFalse(emby.is_person_name_write_safe('http://fixture', 'hidden', 'Same Name', 'p1', allow_absent=True))

    def test_new_unique_name_may_be_assigned_only_with_existing_exact_target(self):
        response = MagicMock()
        response.json.return_value = {'Items': [], 'TotalRecordCount': 0}
        with patch.object(emby.emby_client, 'get', return_value=response):
            self.assertTrue(emby.is_person_name_write_safe('http://fixture', 'hidden', 'New unique name', 'p1', allow_absent=True))
            self.assertFalse(emby.is_person_name_write_safe('http://fixture', 'hidden', 'New unique name', 'p1'))

    def test_non_latin_zero_count_requires_empty_tail_and_stable_head(self):
        head = {'Items': [person(name='구교환')], 'TotalRecordCount': 0}
        def response(data):
            value = MagicMock()
            value.json.return_value = data
            return value
        with patch.object(emby.emby_client, 'get', side_effect=[
            response(head), response({'Items': [], 'TotalRecordCount': 0}), response(head),
        ]):
            self.assertTrue(emby.is_person_name_write_safe('http://fixture', 'hidden', '구교환', 'p1'))
        with patch.object(emby.emby_client, 'get', side_effect=[
            response(head), response({'Items': [person('200', 'p2')], 'TotalRecordCount': 0}),
            response(head),
        ]):
            self.assertFalse(emby.is_person_name_write_safe('http://fixture', 'hidden', '구교환', 'p1'))

    def test_non_latin_name_search_drift_fails_closed(self):
        def response(data):
            value = MagicMock()
            value.json.return_value = data
            return value
        with patch.object(emby.emby_client, 'get', side_effect=[
            response({'Items': [person(name='구교환')], 'TotalRecordCount': 0}),
            response({'Items': [], 'TotalRecordCount': 0}),
            response({'Items': [person('200', 'p2', '구교환')], 'TotalRecordCount': 0}),
        ]):
            self.assertFalse(emby.is_person_name_write_safe('http://fixture', 'hidden', '구교환', 'p1'))

    def processor(self):
        processor = MediaProcessor.__new__(MediaProcessor)
        processor.config = {}
        processor.emby_url, processor.emby_api_key, processor.emby_user_id = 'http://fixture', 'hidden', 'u'
        return processor

    def test_implicit_metadata_creation_is_quarantined(self):
        processor = self.processor()
        source = {'Id': 'media', 'Type': 'Series', 'People': []}
        with patch.object(emby, 'get_emby_item_details', return_value=source), \
             patch.object(emby, 'resolve_exact_person_identity', return_value=[]), \
             patch.object(emby.emby_client, 'post') as post:
            result = processor._prepare_person_metadata_cast([{'id': 100, 'name': 'New'}], source)
        self.assertEqual(result, [])
        post.assert_not_called()
        record = PersonOperationJournal('http://fixture', 'tmdb', '100').read()
        self.assertEqual(record['failure_stage'], 'implicit_create_blocked')
        self.assertIsNone(record['created_person_id'])

    def test_metadata_pending_source_defers_person_part(self):
        processor = self.processor()
        with patch.object(emby, 'get_emby_item_details') as read:
            self.assertEqual(processor._prepare_person_metadata_cast([{'id': 100}], {'Id': 'pending'}), [])
        read.assert_not_called()

    def test_metadata_preserves_unprocessed_existing_relations(self):
        processor = self.processor()
        a, b = person(), person('200', 'p2', 'Historical person')
        source = {'Id': 'media', 'Type': 'Series', 'People': [
            {'Id': 'p1', 'Type': 'Actor'}, {'Id': 'p2', 'Type': 'Actor', 'Role': 'Old role'}]}
        objects = {'media': source, 'p1': a, 'p2': b}
        with patch.object(emby, 'get_emby_item_details', side_effect=lambda item, *args: copy.deepcopy(objects[item])), \
             patch.object(emby, 'resolve_exact_person_identity', return_value=[a]):
            result = processor._prepare_person_metadata_cast([{'id': 100, 'name': a['Name']}], source)
        self.assertEqual({p['emby_person_id'] for p in result}, {'p1', 'p2'})
        self.assertTrue(result[-1]['_preserved_existing_relation'])

    def test_unknown_legacy_relation_holds_file_update(self):
        processor = self.processor()
        source = {'Id': 'media', 'Type': 'Series', 'People': [{'Id': 'p1', 'Type': 'Actor'}]}
        unknown = dict(person(), ProviderIds={})
        with patch.object(emby, 'get_emby_item_details', side_effect=[source, unknown]):
            with self.assertRaises(ValueError):
                processor._prepare_person_metadata_cast([], source)

    def test_over_cap_legacy_cast_is_held_without_detaching_or_enrichment(self):
        processor = self.processor()
        source = {'Id': 'media', 'Type': 'Movie', 'People': [
            {'Id': str(i), 'Type': 'Actor'} for i in range(51)]}
        before = copy.deepcopy(source)
        with patch.object(emby, 'get_emby_item_details', return_value=source), \
             patch.object(emby, 'resolve_exact_person_identity') as resolve, \
             patch.object(emby.emby_client, 'post') as post:
            with self.assertRaisesRegex(ValueError, 'legacy_relation_set_exceeds_processing_cap'):
                processor._prepare_person_metadata_cast([], source)
        self.assertEqual(source, before)
        resolve.assert_not_called()
        post.assert_not_called()

    def test_reentrant_identity_lock_does_not_deadlock_write_gate(self):
        with identity_lock('http://fixture', 'tmdb', '100'):
            with identity_lock('http://fixture', 'tmdb', '100'):
                self.assertTrue(True)

    def test_manual_api_rejects_name_only_request_without_scheduling(self):
        from flask import Flask
        from routes.media import api_update_edited_cast_sa
        function = api_update_edited_cast_sa
        while hasattr(function, '__wrapped__'):
            function = function.__wrapped__
        app = Flask(__name__)
        with app.test_request_context('/api/update_media_cast_sa/media', method='POST',
                                      json={'cast': [{'tmdbId': 100, 'name': 'Name only'}]}), \
             patch('routes.media.task_manager.submit_task') as submit:
            response, code = function('media')
        self.assertEqual(code, 400)
        submit.assert_not_called()

    def test_manual_api_accepts_frozen_identity_intent(self):
        from flask import Flask
        from routes.media import api_update_edited_cast_sa
        function = api_update_edited_cast_sa
        while hasattr(function, '__wrapped__'):
            function = function.__wrapped__
        app = Flask(__name__)
        actor = {'tmdbId': 100, 'name': 'Name', 'emby_person_id': 'p1',
                 'provider_identity_snapshot': {'Tmdb': '100'}}
        with app.test_request_context('/api/update_media_cast_sa/media', method='POST', json={'cast': [actor]}), \
             patch('routes.media.task_manager.submit_task') as submit:
            response, code = function('media')
        self.assertEqual(code, 202)
        self.assertEqual(submit.call_args.kwargs['manual_cast_list'], [actor])

    def test_manual_worker_reports_identity_denial_as_failed_task(self):
        from tasks.media import task_manual_update
        processor = MagicMock()
        processor.process_item_with_manual_cast.return_value = False
        with self.assertRaises(RuntimeError):
            task_manual_update(processor, 'media', [], 'Fixture')

    def test_journal_survives_reopen_without_credentials(self):
        with identity_lock('http://fixture', 'tmdb', '100'):
            journal = PersonOperationJournal('http://fixture', 'tmdb', '100')
            state = journal.record('media', 'create_result_unknown', 'owned', created=True)
        recovered = PersonOperationJournal('http://fixture', 'tmdb', '100').read()
        self.assertEqual(recovered, state)
        self.assertEqual(recovered['created_person_id'], 'owned')
        self.assertNotIn('http', json.dumps(recovered))
        self.assertNotIn('token', json.dumps(recovered))

    def test_corrupt_journal_fails_closed(self):
        journal = PersonOperationJournal('http://fixture', 'tmdb', '100')
        journal.path.write_text('invalid')
        with self.assertRaises(ValueError):
            journal.read()

    def test_global_translation_has_no_same_name_broadcast(self):
        from tasks.actors import task_actor_translation
        processor = MagicMock()
        processor.is_stop_requested.return_value = False
        processor.ai_translator.batch_translate.return_value = {'Same Name': '译名'}
        processor.emby_url = 'http://fixture'
        with patch('tasks.actors.emby.get_all_persons_from_emby',
                   return_value=[[person(), person('200', 'p2')]]), \
             patch('tasks.actors.emby.update_person_details', return_value=True) as write, \
             patch('tasks.actors.task_manager.update_status_from_thread'):
            task_actor_translation(processor)
        self.assertEqual(processor.ai_translator.batch_translate.call_count, 2)
        self.assertEqual({c.kwargs['target'].provider_person_id for c in write.call_args_list}, {'100', '200'})


if __name__ == '__main__':
    unittest.main()
