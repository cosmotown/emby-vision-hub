import inspect
import logging
import unittest
from contextlib import ExitStack
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import Mock, call, patch

import config_manager
if not hasattr(logging.Logger, 'trace'):
    logging.Logger.trace = logging.Logger.debug

from database import person_cleanup_db
from tasks import actors


def ready_result():
    return {
        'forensic_state': 'verified_stale_index_signature',
        'identity_signal': 'stale_index_no_identity_owner',
        'people_signal': 'stale_index_different_people',
        'query_count': 1,
        'actual_people_count': 1,
        'same_name_other_count': 0,
        'different_name_people_count': 1,
        'identity_owner_count': 0,
        'error': None,
    }


def snapshot(person_ids=('A',)):
    details = {
        person_id: {
            'Id': person_id, 'Name': f'Person {person_id}', 'Type': 'Person',
            'ProviderIds': {'Tmdb': str(index + 1)},
        }
        for index, person_id in enumerate(person_ids)
    }
    return {
        'generation': 7,
        'protection_hash': 'protection',
        'normal_people_relationship_hash': 'relationships',
        'person_hash': 'persons',
        'normal_ids': {'B'},
        'item_people': {'m1': {'people': (('B', 'Live B'),)}},
        'person_details': details,
        'identity_index': {},
        'contract': {},
        'root_contract': {},
    }


def job(item_count=1):
    return {
        'job_id': 'job-1',
        'state': 'confirmed',
        'preview_snapshot_generation': 7,
        'preview_protection_hash': 'protection',
        'preview_relationship_hash': 'relationships',
        'preview_person_hash': 'persons',
        'latest_run_id': 'latest',
        'previous_run_id': 'previous',
        'candidate_total': item_count,
    }


def item(person_id='A'):
    return {
        'person_id': person_id,
        'person_name': f'Person {person_id}',
        'provider_ids': {'Tmdb': '1'},
        'candidate_fingerprint': f'fingerprint-{person_id}',
        'preview_state': 'canary_delete_ready',
        'execute_state': 'pending',
        'post_attempts': 0,
    }


class CanaryEligibilityContractTests(unittest.TestCase):
    def test_only_exact_different_people_signal_is_ready(self):
        self.assertTrue(actors._stale_delete_canary_result_ready(ready_result()))
        for key, value in (
            ('people_signal', 'stale_index_same_name_other_person'),
            ('identity_signal', 'stale_index_identity_owner_not_live'),
            ('query_count', 0),
            ('actual_people_count', 0),
            ('same_name_other_count', 1),
            ('identity_owner_count', 1),
        ):
            changed = ready_result()
            changed[key] = value
            with self.subTest(key=key):
                self.assertFalse(actors._stale_delete_canary_result_ready(changed))

    def test_snapshot_comparison_covers_full_relationship_and_person_facts(self):
        first = snapshot()
        second = snapshot()
        self.assertTrue(actors._stale_delete_canary_snapshot_equal(first, second))
        second['item_people'] = {'m1': {'people': (('C', 'Changed'),)}}
        self.assertFalse(actors._stale_delete_canary_snapshot_equal(first, second))

    def test_canary_is_independent_from_orphan_eligibility(self):
        source = inspect.getsource(person_cleanup_db.list_explicit_verified_orphans)
        self.assertNotIn('stable_stale_index_signature', source)
        self.assertNotIn('canary_delete_ready', source)


class CanaryExecutionTests(unittest.TestCase):
    def setUp(self):
        for name, kw in (
            ('authenticate_canary_admin_once', {'side_effect':lambda c, **_: c}),
            ('verify_canary_admin_session', {'return_value':True}),
        ):
            mocked=patch.object(actors.emby,name,**kw);mocked.start();self.addCleanup(mocked.stop)
        verified=patch.object(person_cleanup_db,'verify_stale_delete_canary_admin_auth');verified.start();self.addCleanup(verified.stop)
        context = patch.object(actors.emby, 'ensure_admin_delete_context', return_value=SimpleNamespace(binding_hash='a'*64,user_id='admin'))
        context.start()
        self.addCleanup(context.stop)
        binding = patch.object(person_cleanup_db, 'bind_stale_delete_canary_admin_context')
        binding.start()
        self.addCleanup(binding.stop)
        claimant = patch.object(person_cleanup_db, 'claim_stale_delete_canary_execution', return_value=True)
        claimant.start()
        self.addCleanup(claimant.stop)
        self.processor = SimpleNamespace(
            emby_url='http://emby', emby_api_key='secret',
            is_stop_requested=Mock(return_value=False),
        )

    def common_patches(self, final_snapshot=None):
        start = snapshot()
        final_snapshot = final_snapshot or snapshot(person_ids=())
        return (
            patch.object(
                person_cleanup_db, 'validate_stale_delete_canary_chain',
                return_value={'job': job(), 'latest': {}, 'previous': {}},
            ),
            patch.object(actors, '_build_stale_delete_canary_snapshot',
                         side_effect=[start, start, final_snapshot]),
            patch.object(person_cleanup_db, 'start_stale_delete_canary_job'),
            patch.object(person_cleanup_db, 'list_stale_delete_canary_items',
                         return_value=[item()]),
            patch.object(person_cleanup_db, 'stale_delete_canary_stop_requested',
                         return_value=False),
            patch.object(person_cleanup_db, 'require_ready_protection_snapshot',
                         return_value=7),
            patch.object(actors, '_check_stale_delete_canary_candidate',
                         return_value=ready_result()),
            patch.object(person_cleanup_db, 'reserve_stale_delete_canary_attempt',
                         return_value=True),
            patch.object(person_cleanup_db, 'finish_stale_delete_canary_item'),
            patch.object(person_cleanup_db, 'remove_candidate'),
            patch.object(person_cleanup_db, 'complete_stale_delete_canary_job'),
            patch.object(person_cleanup_db, 'fail_stale_delete_canary_job'),
            patch.object(actors.task_manager, 'update_status_from_thread'),
        )

    def test_success_commits_reservation_before_single_post_and_exact_readback(self):
        events = []
        patches = self.common_patches()
        with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5], patches[6], \
                patches[7] as reserve, patches[8] as finish, patches[9], \
                patches[10] as complete, patches[11] as fail, patches[12], \
                patch.object(
                    person_cleanup_db, 'reserve_stale_delete_canary_attempt',
                    side_effect=lambda *args: events.append('reserved') or True,
                ), patch.object(
                    actors.emby, 'delete_person_custom_api_outcome',
                    side_effect=lambda *args, **kwargs: kwargs['before_submit']() and (events.append('post') or 'confirmed'),
                ) as delete_post, patch.object(
                    actors.emby, 'get_person_detail_forensic_strict',
                    side_effect=lambda *args: events.append('readback') or {
                        'status': 'person_missing', 'detail': None,
                    },
                ):
            actors.task_execute_stale_delete_canary(self.processor, 'job-1')
        self.assertEqual(events, ['reserved', 'post', 'readback'])
        delete_post.assert_called_once()
        finish.assert_any_call(
            'job-1', 'A', 'confirmed_deleted', {'readback': 'person_missing'},
        )
        complete.assert_called_once()
        self.assertTrue(complete.call_args.args[1]['person_delta_exact'])
        fail.assert_not_called()

    def test_ambiguous_post_stops_and_never_replays_or_reads_back(self):
        patches = self.common_patches(final_snapshot=snapshot())
        with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5], patches[6], \
                patches[7], patches[8] as finish, patches[9], patches[10], \
                patches[11] as fail, patches[12], patch.object(
                    actors.emby, 'delete_person_custom_api_outcome',
                    side_effect=lambda *args, **kwargs: kwargs['before_submit']() and 'ambiguous',
                ) as delete_post, patch.object(
                    actors.emby, 'get_person_detail_forensic_strict',
                ) as readback:
            actors.task_execute_stale_delete_canary(self.processor, 'job-1')
        delete_post.assert_called_once()
        readback.assert_not_called()
        finish.assert_any_call(
            'job-1', 'A', 'delete_ambiguous',
            evidence={},
            error='DeletePerson 结果不确定；禁止自动重放',
        )
        fail.assert_called_once()

    def test_preview_drift_sends_zero_delete_posts(self):
        bad = snapshot()
        bad['protection_hash'] = 'changed'
        with patch.object(
            person_cleanup_db, 'validate_stale_delete_canary_chain',
            return_value={'job': job(), 'latest': {}, 'previous': {}},
        ), patch.object(
            actors, '_build_stale_delete_canary_snapshot', return_value=bad,
        ), patch.object(
            person_cleanup_db, 'fail_stale_delete_canary_job',
        ) as fail, patch.object(
            person_cleanup_db, 'get_stale_delete_canary_job',
            return_value={'state': 'confirmed'},
        ), patch.object(person_cleanup_db, 'list_stale_delete_canary_items', return_value=[]
        ), patch.object(
            actors.emby, 'delete_person_custom_api_outcome',
        ) as delete_post:
            with self.assertRaises(RuntimeError):
                actors.task_execute_stale_delete_canary(self.processor, 'job-1')
        delete_post.assert_not_called()
        fail.assert_called_once()

    def test_success_response_but_existing_person_is_not_confirmed_and_not_replayed(self):
        patches = self.common_patches(final_snapshot=snapshot())
        with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5], patches[6], \
                patches[7], patches[8] as finish, patches[9], patches[10], \
                patches[11] as fail, patches[12], patch.object(
                    actors.emby, 'delete_person_custom_api_outcome',
                    side_effect=lambda *args, **kwargs: kwargs['before_submit']() and 'confirmed',
                ) as delete_post, patch.object(
                    actors.emby, 'get_person_detail_forensic_strict',
                    return_value={'status': 'ok', 'detail': {'Id': 'A'}},
                ):
            actors.task_execute_stale_delete_canary(self.processor, 'job-1')
        delete_post.assert_called_once()
        finish.assert_any_call(
            'job-1', 'A', 'delete_not_confirmed',
            {'readback': 'person_exists'},
            'DeletePerson 返回成功但 exact GET 仍存在；禁止重放',
        )
        fail.assert_called_once()

    def test_stop_before_first_item_sends_zero_posts_and_has_no_resume_path(self):
        self.processor.is_stop_requested.return_value = True
        patches = self.common_patches(final_snapshot=snapshot())
        with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5], patches[6], \
                patches[7], patches[8] as finish, patches[9], patches[10], \
                patches[11] as fail, patches[12], patch.object(
                    actors.emby, 'delete_person_custom_api_outcome',
                ) as delete_post:
            actors.task_execute_stale_delete_canary(self.processor, 'job-1')
        delete_post.assert_not_called()
        self.assertEqual(finish.call_args.args[2], 'stopped_before_start')
        self.assertEqual(fail.call_args.args[1], 'stopped')
        self.assertFalse(hasattr(person_cleanup_db, 'resume_stale_delete_canary_job'))


class CanaryFailureMatrixTests(unittest.TestCase):
    def exercise(self, count=100, fail_at=None, failure='linked', final_change=None,
                 reserve_failure=False, stop_at=None, claimed=True, preview_change=None, session_lost_at=None):
        ids = [str(i) for i in range(1, count + 1)]
        start = snapshot(ids)
        start['person_details']['unrelated'] = {'Id': 'unrelated', 'Type': 'Person', 'Name': 'Unrelated', 'ProviderIds': {}}
        processor = SimpleNamespace(emby_url='http://emby', emby_api_key='secret', is_stop_requested=lambda: False)
        posts, reserved, checked = [], [], []
        def build(*_):
            fresh = deepcopy(start)
            fresh['person_details'] = {key: value for key, value in start['person_details'].items() if key not in posts}
            if preview_change and not posts:
                preview_change(fresh)
            if final_change and len(posts) == count:
                final_change(fresh)
            return fresh
        def check(*args):
            person_id = args[1]['person_id']
            checked.append(person_id)
            if int(person_id) == fail_at:
                raise person_cleanup_db.CanarySafetyError(failure, 'injected safety failure')
            return ready_result()
        def reserve(*args):
            if reserve_failure:
                raise RuntimeError('injected commit failure')
            reserved.append(args[1])
            return True
        def transport(*args, **kwargs):
            self.assertTrue(kwargs['cached_auth_only'])
            self.assertTrue(kwargs['before_submit']())
            self.assertEqual(reserved[-1], args[2])
            posts.append(args[2])
            kwargs['response_observer'](204)
            return 'confirmed'
        with ExitStack() as stack:
            def p(obj, name, **kwargs):
                return stack.enter_context(patch.object(obj, name, **kwargs))
            p(person_cleanup_db, 'claim_stale_delete_canary_execution', return_value=claimed)
            p(actors.emby, 'ensure_admin_delete_context', return_value=SimpleNamespace(binding_hash='a'*64,user_id='admin'))
            auth=p(actors.emby, 'authenticate_canary_admin_once', side_effect=lambda c,**_:c)
            def verify(*args,**kwargs):
                if session_lost_at is not None and len(posts)>=session_lost_at:
                    raise actors.emby.AdminDeleteContextError('admin_session_lost')
                return True
            p(actors.emby, 'verify_canary_admin_session', side_effect=verify)
            p(person_cleanup_db,'verify_stale_delete_canary_admin_auth')
            p(person_cleanup_db, 'bind_stale_delete_canary_admin_context')
            p(person_cleanup_db, 'validate_stale_delete_canary_chain', return_value={'job': job(count)})
            p(actors, '_build_stale_delete_canary_snapshot', side_effect=build)
            p(actors, '_check_stale_delete_canary_candidate', side_effect=check)
            p(person_cleanup_db, 'start_stale_delete_canary_job')
            p(person_cleanup_db, 'list_stale_delete_canary_items', return_value=[item(key) for key in ids])
            p(person_cleanup_db, 'get_stale_delete_canary_job', return_value={'state': 'running'})
            p(person_cleanup_db, 'stale_delete_canary_stop_requested', side_effect=lambda *_: stop_at is not None and len(posts) >= stop_at)
            p(person_cleanup_db, 'reserve_stale_delete_canary_attempt', side_effect=reserve)
            finish = p(person_cleanup_db, 'finish_stale_delete_canary_item')
            p(person_cleanup_db, 'remove_candidate')
            complete = p(person_cleanup_db, 'complete_stale_delete_canary_job')
            fail = p(person_cleanup_db, 'fail_stale_delete_canary_job')
            p(actors.task_manager, 'update_status_from_thread')
            p(actors.emby, 'delete_person_custom_api_outcome', side_effect=transport)
            p(actors.emby, 'get_person_detail_forensic_strict', return_value={'status': 'person_missing'})
            try:
                actors.task_execute_stale_delete_canary(processor, 'job-1')
            except RuntimeError:
                self.assertTrue(fail.called)
            self.assertEqual(auth.call_count,1 if claimed else 0)
        return posts, reserved, checked, complete, fail, finish

    def test_100_success_serial_each_reserved_once_and_final_verified(self):
        posts, reserved, checked, complete, fail, finish = self.exercise()
        self.assertEqual(posts, [str(i) for i in range(1, 101)])
        self.assertEqual(posts, reserved)
        self.assertEqual(posts, checked)
        self.assertEqual(finish.call_count, 100)
        complete.assert_called_once()
        fail.assert_not_called()

    def test_person_37_linked_stops_remaining(self):
        posts, _, _, complete, fail, _ = self.exercise(fail_at=37)
        self.assertEqual(len(posts), 36)
        self.assertEqual(fail.call_args.args[1], 'linked')
        complete.assert_not_called()

    def test_person_20_protection_drift_stops_remaining(self):
        posts, _, _, complete, fail, _ = self.exercise(fail_at=20, failure='protection_drift')
        self.assertEqual(len(posts), 19)
        self.assertEqual(fail.call_args.args[1], 'protection_drift')
        complete.assert_not_called()

    def test_db_commit_failure_sends_zero_posts(self):
        posts, reserved, _, complete, fail, _ = self.exercise(count=1, reserve_failure=True)
        self.assertEqual((posts, reserved), ([], []))
        fail.assert_called_once()
        complete.assert_not_called()

    def test_stop_after_12_never_starts_13(self):
        posts, _, _, complete, fail, _ = self.exercise(stop_at=12)
        self.assertEqual(len(posts), 12)
        self.assertEqual(fail.call_args.args[1], 'stopped')
        complete.assert_not_called()

    def test_losing_executor_does_not_modify_winner(self):
        posts, reserved, checked, complete, fail, finish = self.exercise(claimed=False)
        self.assertEqual((posts, reserved, checked), ([], [], []))
        for mock in (complete, fail, finish):
            mock.assert_not_called()

    def test_final_relationship_change_cannot_be_verified(self):
        *_, complete, fail, _ = self.exercise(count=1, final_change=lambda s: s.update(normal_people_relationship_hash='changed'))
        self.assertEqual(fail.call_args.args[1], 'environment_drift')
        complete.assert_not_called()

    def test_final_person_addition_cannot_be_verified(self):
        *_, complete, fail, _ = self.exercise(count=1, final_change=lambda s: s['person_details'].update(extra={'Name': 'Other'}))
        self.assertEqual(fail.call_args.args[1], 'environment_drift')
        complete.assert_not_called()

    def test_preview_to_execution_relationship_drift_zero_posts(self):
        posts, _, _, complete, fail, _ = self.exercise(count=1, preview_change=lambda s: s.update(normal_people_relationship_hash='drift'))
        self.assertEqual(posts, [])
        self.assertEqual(fail.call_args.args[1], 'relationship_drift')
        complete.assert_not_called()

    def test_preview_to_execution_person_drift_zero_posts(self):
        posts, _, _, complete, fail, _ = self.exercise(count=1, preview_change=lambda s: s.update(person_hash='drift'))
        self.assertEqual(posts, [])
        self.assertEqual(fail.call_args.args[1], 'person_snapshot_drift')
        complete.assert_not_called()

    def test_final_unrelated_person_name_or_provider_change_is_drift(self):
        for field, value in [('Name', 'Changed'), ('ProviderIds', {'Tmdb': '7'})]:
            with self.subTest(field=field):
                def mutate(s):
                    s['person_details']['unrelated'] = {**s['person_details']['unrelated'], field: value}
                *_, complete, fail, _ = self.exercise(count=1, final_change=mutate)
                self.assertEqual(fail.call_args.args[1], 'environment_drift')
                complete.assert_not_called()


    def test_session_lost_after_ten_no_eleventh_reservation_or_relogin(self):
        posts,reserved,_,complete,fail,_=self.exercise(count=12,session_lost_at=10)
        self.assertEqual(len(posts),10);self.assertEqual(posts,reserved)
        complete.assert_not_called();self.assertEqual(fail.call_args.args[1],'admin_session_lost')


class CanaryFreshPeopleTests(unittest.TestCase):
    def test_fresh_exact_people_link_beats_old_empty_reference(self):
        roots = {'complete': True, 'roots': ({'library_id': 'L', 'library_name': 'L', 'style': 'posix', 'path': '/normal'},)}
        snap = snapshot()
        snap['all_roots'] = roots
        snap['item_people']['m1'].update(library_id='L', item_type='Movie')
        hit = {'Id': 'm1', 'Type': 'Movie', 'Path': '/normal/movie.mkv'}
        response = Mock(status_code=200)
        response.json.return_value = {**hit, 'People': [{'Id': 'A', 'Name': 'Person A'}]}
        proc = SimpleNamespace(emby_url='http://emby', emby_api_key='secret')
        with patch.object(actors.emby, 'get_person_media_query_items_strict', return_value=[hit]), \
                patch.object(actors.emby.emby_client, 'get', return_value=response) as get:
            with self.assertRaises(person_cleanup_db.CanarySafetyError) as error:
                actors._check_stale_delete_canary_candidate(proc, actors._stale_delete_canary_source_item(item()), snap)
        self.assertEqual(error.exception.state, 'linked')
        self.assertFalse(get.call_args.kwargs['allow_redirects'])


class CanaryTransportTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(patch.object(actors.emby, 'wait_for_server_idle'))
        self.stack.enter_context(patch.dict(actors.emby._admin_token_cache, {'access_token': 'secret', 'user_id': 'admin'}, clear=True))
        import constants
        self.stack.enter_context(patch.dict(config_manager.APP_CONFIG, {constants.CONFIG_OPTION_EMBY_SERVER_URL: 'http://emby'}))
        response = Mock(status_code=200)
        response.json.return_value = {'Id': 'admin', 'Policy': {'IsAdministrator': True}}
        self.stack.enter_context(patch.object(actors.emby.emby_client, 'get', return_value=response))
        self.login = self.stack.enter_context(patch.object(actors.emby, 'get_admin_access_token'))

    def test_commit_exception_prevents_transport_call(self):
        with patch.object(actors.emby.emby_client, 'post_once') as post:
            with self.assertRaises(RuntimeError):
                actors.emby.delete_person_custom_api_outcome('http://emby', 'key', 'A', cached_auth_only=True,
                    before_submit=Mock(side_effect=RuntimeError('commit failed')))
        post.assert_not_called()
        self.login.assert_not_called()

    def test_timeout_5xx_redirect_single_submission_no_login_post(self):
        import requests
        for status in (204, 307, 308, 500, 'timeout', 'connection'):
            with self.subTest(status=status):
                events = []
                def send(*args, **kwargs):
                    events.append('post')
                    self.assertEqual(events, ['commit', 'post'])
                    if status == 'timeout': raise requests.Timeout()
                    if status == 'connection': raise requests.ConnectionError()
                    return Mock(status_code=status)
                with patch.object(actors.emby.emby_client, 'post_once', side_effect=send) as post:
                    result = actors.emby.delete_person_custom_api_outcome('http://emby', 'key', 'A', cached_auth_only=True,
                        before_submit=lambda: events.append('commit') or True)
                self.assertEqual(post.call_count, 1)
                self.assertEqual(result, 'confirmed' if status == 204 else 'failed' if status in (307, 308) else 'ambiguous')
        self.login.assert_not_called()

    def test_absent_cache_fails_without_login_or_reservation(self):
        with patch.dict(actors.emby._admin_token_cache, {}, clear=True), patch.object(actors.emby.emby_client, 'post_once') as post:
            callback = Mock(return_value=True)
            result = actors.emby.delete_person_custom_api_outcome('http://emby', 'key', 'A', cached_auth_only=True, before_submit=callback)
        self.assertEqual(result, 'auth_unavailable')
        callback.assert_not_called()
        post.assert_not_called()
        self.login.assert_not_called()


class CanaryAdminContextTests(unittest.TestCase):
    def setUp(self):
        import constants
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(patch.dict(config_manager.APP_CONFIG, {
            constants.CONFIG_OPTION_EMBY_SERVER_URL: 'http://emby',
            constants.CONFIG_OPTION_EMBY_API_KEY: 'current-secret',
            constants.CONFIG_OPTION_EMBY_ADMIN_USER: 'Configured Admin',
        }))
        self.stack.enter_context(patch.dict(actors.emby._admin_token_cache, {
            'user_id': 'expired-user', 'access_token': 'expired-token',
        }, clear=True))
        self.stack.enter_context(patch.object(actors.emby, 'wait_for_server_idle'))
        self.login = self.stack.enter_context(patch.object(actors.emby, 'get_admin_access_token', side_effect=AssertionError('login forbidden')))
        self.post = self.stack.enter_context(patch.object(actors.emby.emby_client, 'post_once', return_value=Mock(status_code=204)))
        self.user = {'Id': 'admin123', 'Name': 'Configured Admin', 'Policy': {'IsAdministrator': True, 'IsDisabled': False}}
        self.get = self.stack.enter_context(patch.object(actors.emby.emby_client, 'get', side_effect=self.response))

    def response(self, url, **kwargs):
        self.assertEqual(kwargs['headers'], {'X-Emby-Token': 'current-secret'})
        self.assertFalse(kwargs['allow_redirects'])
        self.assertEqual(kwargs['timeout'], 15)
        value = [self.user] if url.endswith('/Users') else self.user
        if url.endswith('/Auth/Keys'):
            value = {'Items':[{'AccessToken':'current-secret','UserId':'admin123','IsActive':True}]}
        return Mock(status_code=200, json=Mock(return_value=value))

    def context(self):
        return actors.emby.ensure_admin_delete_context('http://emby', 'current-secret', 'job1')

    def test_cold_cache_get_only_exact_configured_admin_and_no_secret_repr(self):
        actors.emby._admin_token_cache.clear()
        ctx = self.context()
        self.assertEqual(ctx.user_id, 'admin123')
        self.assertNotIn('current-secret', repr(ctx))
        self.assertEqual(self.get.call_count, 2)
        self.assertEqual(actors.emby._admin_token_cache, {})
        self.post.assert_not_called()
        self.login.assert_not_called()

    def test_expired_cache_ignored_and_no_per_person_rediscovery(self):
        from dataclasses import replace
        ctx = replace(self.context(), access_token='user-token', session_id='session', device_id='device')
        for pid in ('A', 'B'):
            reserve = Mock(return_value=True)
            result = actors.emby.delete_person_custom_api_outcome('http://emby', 'current-secret', pid,
                admin_context=ctx, context_job_id='job1', cached_auth_only=True, before_submit=reserve)
            self.assertEqual(result, 'confirmed')
            reserve.assert_called_once()
        self.assertEqual(self.get.call_count, 2)
        self.assertEqual(self.post.call_count, 2)
        self.assertEqual(self.post.call_args.kwargs['params'], {'UserId':'admin123'})
        self.login.assert_not_called()

    def test_missing_disabled_nonadmin_duplicate_or_mismatched_user_fail_closed(self):
        variants = [[], [self.user, self.user], [{'Id':'other','Name':'Other Admin','Policy':self.user['Policy']}]]
        for value in variants:
            self.get.side_effect = None
            self.get.return_value = Mock(status_code=200, json=Mock(return_value=value))
            with self.assertRaisesRegex(RuntimeError, 'admin_session_not_admin'):
                self.context()
        self.get.side_effect = self.response
        for policy in ({'IsAdministrator':False,'IsDisabled':False}, {'IsAdministrator':True,'IsDisabled':True}, {'IsAdministrator':True}):
            self.user['Policy'] = policy
            with self.assertRaises(RuntimeError): self.context()
        self.post.assert_not_called()

    def test_http_failure_redirect_timeout_and_malformed_never_login(self):
        import requests
        for status in (301, 307, 401, 403, 500):
            self.get.side_effect = None
            self.get.return_value = Mock(status_code=status)
            with self.assertRaises(RuntimeError): self.context()
        self.get.side_effect = requests.Timeout('unsafe token=secret')
        with self.assertRaises(RuntimeError) as raised: self.context()
        self.assertNotIn('token', str(raised.exception))
        self.post.assert_not_called()
        self.login.assert_not_called()

    def test_wrong_job_process_or_changed_config_zero_reservation(self):
        from dataclasses import replace
        import constants
        ctx = self.context()
        for bad, job_id in ((ctx,'other-job'), (replace(ctx,process_id=-1),'job1')):
            reserve = Mock(return_value=True)
            result = actors.emby.delete_person_custom_api_outcome('http://emby', 'current-secret', 'A', admin_context=bad, context_job_id=job_id, before_submit=reserve)
            self.assertEqual(result,'auth_unavailable');reserve.assert_not_called()
        with patch.dict(config_manager.APP_CONFIG,{constants.CONFIG_OPTION_EMBY_ADMIN_USER:'Changed'}):
            reserve=Mock(return_value=True)
            self.assertEqual(actors.emby.delete_person_custom_api_outcome('http://emby','current-secret','A',admin_context=ctx,context_job_id='job1',before_submit=reserve),'auth_unavailable')
            reserve.assert_not_called()
        self.post.assert_not_called()

    def test_execution_auth_failure_precedes_snapshot_and_reservation(self):
        self.get.side_effect = RuntimeError('expired')
        proc=SimpleNamespace(emby_url='http://emby',emby_api_key='current-secret')
        with patch.object(person_cleanup_db,'claim_stale_delete_canary_execution',return_value=True), \
                patch.object(person_cleanup_db,'validate_stale_delete_canary_chain',return_value={'job':job()}), \
                patch.object(person_cleanup_db,'get_stale_delete_canary_job',return_value={'state':'preflighting'}), \
                patch.object(person_cleanup_db,'list_stale_delete_canary_items',return_value=[]), \
                patch.object(person_cleanup_db,'fail_stale_delete_canary_job') as fail, \
                patch.object(person_cleanup_db,'reserve_stale_delete_canary_attempt') as reserve, \
                patch.object(actors,'_build_stale_delete_canary_snapshot') as build:
            with self.assertRaises(RuntimeError): actors.task_execute_stale_delete_canary(proc,'job1')
            reserve.assert_not_called();build.assert_not_called();fail.assert_called_once()
        self.post.assert_not_called()

    def test_real_emby_server_key_shape_is_not_an_admin_session(self):
        original=self.response
        def response(url, **kwargs):
            if url.endswith('/Auth/Keys'):
                return Mock(status_code=200,json=Mock(return_value={'Items':[{'AccessToken':'current-secret','UserId':0,'IsActive':True}],'TotalRecordCount':0}))
            return original(url,**kwargs)
        self.get.side_effect=response
        principal=self.context()
        self.assertEqual(principal.access_token,'')
        reserve=Mock(return_value=True)
        self.assertEqual(actors.emby.delete_person_custom_api_outcome('http://emby','current-secret','A',
            admin_context=principal,context_job_id='job1',before_submit=reserve),'auth_unavailable')
        reserve.assert_not_called()
        self.login.assert_not_called();self.post.assert_not_called()

    def test_does_not_adopt_another_admin_token_from_auth_keys(self):
        original=self.response
        def response(url,**kwargs):
            if url.endswith('/Auth/Keys'):
                return Mock(status_code=200,json=Mock(return_value={'Items':[{'AccessToken':'another-admin-secret','UserId':'admin123','IsActive':True}]}))
            return original(url,**kwargs)
        self.get.side_effect=response
        self.assertEqual(self.context().access_token,'')
        self.assertFalse(any('/Auth/Keys' in c.args[0] for c in self.get.call_args_list))
        self.post.assert_not_called()


class CanaryAdminAuthenticationTests(unittest.TestCase):
    def setUp(self):
        import constants,os,hashlib
        self.stack=ExitStack();self.addCleanup(self.stack.close)
        self.stack.enter_context(patch.dict(config_manager.APP_CONFIG,{
            constants.CONFIG_OPTION_EMBY_SERVER_URL:'http://emby',constants.CONFIG_OPTION_EMBY_API_KEY:'api-key',
            constants.CONFIG_OPTION_EMBY_ADMIN_USER:'Admin',constants.CONFIG_OPTION_EMBY_ADMIN_PASS:'test-password'}))
        import json
        binding=hashlib.sha256(json.dumps(['http://emby','u1','Admin','job','api-key'],separators=(',',':')).encode()).hexdigest()
        self.principal=actors.emby.AdminDeleteContext('http://emby','u1','Admin','job',binding,os.getpid(),'')
        self.device_id=hashlib.sha256(b'canary:job').hexdigest()
        self.user={'Id':'u1','Name':'Admin','Policy':{'IsAdministrator':True,'IsDisabled':False}}
        self.auth={'AccessToken':'user-token','User':self.user,'SessionInfo':{'Id':'s1'}}
        self.session={'Id':'s1','UserId':'u1','DeviceId':self.device_id}
        self.events=[]
        def post(*args,**kw):self.events.append('post');return Mock(status_code=200,json=Mock(return_value=self.auth))
        def get(url,**kw):
            self.events.append('user_get' if '/Users/' in url else 'session_get')
            self.assertEqual(kw['headers'],{'X-Emby-Token':'user-token'})
            self.assertFalse(kw['allow_redirects'])
            return Mock(status_code=200,json=Mock(return_value=self.user if '/Users/' in url else [self.session]))
        self.post=self.stack.enter_context(patch.object(actors.emby.emby_client,'post_once',side_effect=post))
        self.get=self.stack.enter_context(patch.object(actors.emby.emby_client,'get',side_effect=get))
        self.login=self.stack.enter_context(patch.object(actors.emby,'get_admin_access_token',side_effect=AssertionError('fallback forbidden')))
        self.reserve=Mock(side_effect=lambda:self.events.append('commit') or True)

    def authenticate(self):
        return actors.emby.authenticate_canary_admin_once(self.principal,before_submit=self.reserve)

    def test_auth_commit_post_and_two_get_verification_no_cache(self):
        context=self.authenticate()
        self.assertEqual(self.events,['commit','post','user_get','session_get'])
        self.assertEqual(context.user_id,'u1');self.assertEqual(context.session_id,'s1')
        self.assertNotIn('user-token',repr(context))
        self.post.assert_called_once();self.login.assert_not_called()
        self.assertEqual(self.post.call_args.args[0],'http://emby/Users/AuthenticateByName')
        self.assertEqual(self.post.call_args.kwargs['json'],{'Username':'Admin','Pw':'test-password'})
        self.assertFalse(self.post.call_args.kwargs['allow_redirects'])
        self.assertEqual(self.post.call_args.kwargs['timeout'],30)

    def test_auth_timeout_connection_tls_proxy_never_replayed(self):
        import requests
        for exc in (requests.Timeout,requests.ConnectionError,requests.exceptions.SSLError,requests.exceptions.ProxyError):
            self.post.reset_mock();self.reserve.reset_mock();self.get.reset_mock()
            self.post.side_effect=exc('password=test-password token=user-token')
            with self.assertRaises(actors.emby.AdminDeleteContextError) as result:self.authenticate()
            self.assertEqual(str(result.exception),'admin_auth_ambiguous')
            self.post.assert_called_once();self.reserve.assert_called_once();self.get.assert_not_called()

    def test_auth_307_308_4xx_failed_and_5xx_ambiguous_no_retry(self):
        for status in (307,308,401,403,500,503):
            self.post.reset_mock();self.get.reset_mock()
            self.post.side_effect=None;self.post.return_value=Mock(status_code=status)
            with self.assertRaises(actors.emby.AdminDeleteContextError) as result:self.authenticate()
            self.assertEqual(result.exception.state,'admin_auth_failed' if status<500 else 'admin_auth_ambiguous')
            self.post.assert_called_once();self.get.assert_not_called()

    def test_commit_failure_zero_auth_post(self):
        self.reserve.side_effect=RuntimeError('commit failed')
        with self.assertRaises(RuntimeError):self.authenticate()
        self.post.assert_not_called();self.get.assert_not_called()

    def test_credentials_missing_zero_auth_reservation(self):
        import constants
        with patch.dict(config_manager.APP_CONFIG,{constants.CONFIG_OPTION_EMBY_ADMIN_PASS:''}):
            with self.assertRaisesRegex(actors.emby.AdminDeleteContextError,'admin_credentials_missing'):self.authenticate()
        self.reserve.assert_not_called();self.post.assert_not_called()

    def test_changed_server_or_api_key_does_not_send_password(self):
        import constants
        for change in ({constants.CONFIG_OPTION_EMBY_SERVER_URL:'http://different'}, {constants.CONFIG_OPTION_EMBY_API_KEY:'changed'}):
            with patch.dict(config_manager.APP_CONFIG,change):
                with self.assertRaisesRegex(actors.emby.AdminDeleteContextError,'admin_session_invalid'):self.authenticate()
        self.reserve.assert_not_called();self.post.assert_not_called()

    def test_200_without_valid_session_not_admin_ready(self):
        self.get.side_effect=None;self.get.return_value=Mock(status_code=403)
        with self.assertRaisesRegex(actors.emby.AdminDeleteContextError,'admin_session_invalid'):self.authenticate()
        self.post.assert_called_once();self.login.assert_not_called()

    def test_non_admin_and_unbound_session_rejected(self):
        self.user['Policy']['IsAdministrator']=False
        with self.assertRaisesRegex(actors.emby.AdminDeleteContextError,'admin_session_not_admin'):self.authenticate()
        self.user['Policy']['IsAdministrator']=True;self.session['UserId']='someone-else'
        with self.assertRaisesRegex(actors.emby.AdminDeleteContextError,'admin_session_invalid'):self.authenticate()

    def test_auth_response_cannot_promote_server_api_key(self):
        self.auth['AccessToken']='api-key'
        with self.assertRaisesRegex(actors.emby.AdminDeleteContextError,'admin_auth_ambiguous'):self.authenticate()
        self.get.assert_not_called();self.login.assert_not_called()


class CanaryStaticBoundaryTests(unittest.TestCase):
    def test_delete_transport_is_reused_and_post_attempt_is_reserved_first(self):
        source = inspect.getsource(actors.task_execute_stale_delete_canary)
        self.assertIn('reserve_stale_delete_canary_attempt', source)
        self.assertIn('delete_person_custom_api_outcome', source)
        self.assertLess(
            source.index('reserve_stale_delete_canary_attempt'),
            source.index('delete_person_custom_api_outcome'),
        )
        self.assertIn('get_person_detail_forensic_strict', source)

    def test_hard_limit_and_phrase_are_backend_constants(self):
        self.assertEqual(person_cleanup_db.STALE_DELETE_CANARY_LIMIT, 100)
        from routes import person_cleanup
        route_source = inspect.getsource(person_cleanup.confirm_stale_delete_canary)
        self.assertIn('确认删除稳定陈旧索引 Canary 人物', route_source)


if __name__ == '__main__':
    unittest.main()
