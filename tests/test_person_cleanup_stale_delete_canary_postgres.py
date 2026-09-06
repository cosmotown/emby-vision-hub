import hashlib
import logging
import os
import unittest
from unittest.mock import patch
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, HTTPServer
from threading import Thread
from types import SimpleNamespace
from urllib.parse import urlparse, parse_qs
import json
import multiprocessing
from concurrent.futures import ThreadPoolExecutor

import config_manager
import constants
from database import person_cleanup_db
from database.connection import get_db_connection, init_db


POSTGRES_HOST = os.environ.get('EVH_TEST_POSTGRES_HOST')
if not hasattr(logging.Logger, 'trace'):
    logging.Logger.trace = logging.Logger.debug


def _claim_canary_in_independent_process(job_id, db_config, queue):
    config_manager.APP_CONFIG.update(db_config)
    queue.put(person_cleanup_db.claim_stale_delete_canary_execution(job_id))


@unittest.skipUnless(POSTGRES_HOST, 'isolated PostgreSQL is not configured')
class StaleDeleteCanaryPostgresTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        config_manager.APP_CONFIG.update({
            constants.CONFIG_OPTION_DB_HOST: POSTGRES_HOST,
            constants.CONFIG_OPTION_DB_PORT: int(os.environ.get('EVH_TEST_POSTGRES_PORT', '5432')),
            constants.CONFIG_OPTION_DB_USER: os.environ.get('EVH_TEST_POSTGRES_USER', 'evh_test'),
            constants.CONFIG_OPTION_DB_PASSWORD: os.environ.get('EVH_TEST_POSTGRES_PASSWORD', 'evh_test'),
            constants.CONFIG_OPTION_DB_NAME: os.environ.get('EVH_TEST_POSTGRES_DB', 'evh_test'),
        })
        init_db()

    def setUp(self):
        with get_db_connection() as conn:
            with conn.cursor() as cursor:
                cursor.execute("""
                    TRUNCATE TABLE
                        person_cleanup_stale_delete_job_items,
                        person_cleanup_stale_delete_jobs,
                        person_cleanup_delete_attempts,
                        person_cleanup_stale_index_items,
                        person_cleanup_stale_index_runs,
                        person_cleanup_alias_proof_items,
                        person_cleanup_alias_proof_runs,
                        person_cleanup_candidates,
                        person_cleanup_protected_aliases,
                        person_cleanup_protected_identities,
                        person_cleanup_protected_names,
                        person_cleanup_protected_people,
                        person_cleanup_protected_libraries,
                        person_cleanup_protection_state
                    CASCADE
                """)
                cursor.execute(
                    "INSERT INTO person_cleanup_protection_state (singleton) VALUES (TRUE)"
                )
        self.generation = person_cleanup_db.begin_protection_snapshot()
        person_cleanup_db.complete_protection_snapshot(self.generation)

    def create_completed_source(self, count=3):
        people = [
            {
                'Id': f'p{index:03d}', 'Name': f'Person {index:03d}',
                'ProviderIds': {'Tmdb': str(1000 + index)},
            }
            for index in range(count)
        ]
        person_cleanup_db.replace_candidates(people)
        with get_db_connection() as conn:
            with conn.cursor() as cursor:
                cursor.execute("""
                    UPDATE person_cleanup_candidates
                    SET verification_status = 'identity_alias_only', last_checked_at = NOW()
                """)
        candidates = person_cleanup_db.list_candidates_raw()
        proof = person_cleanup_db.create_alias_proof_run(
            self.generation, 'protection', 'normal', 'persons', candidates,
        )
        if count > 1000:
            # Persist production-sized evidence fixture, not 18k network calls.
            with get_db_connection() as conn:
                with conn.cursor() as cursor:
                    cursor.execute("UPDATE person_cleanup_alias_proof_items SET proof_state = 'identity_not_found', checked_at = NOW() WHERE proof_id = %s", (proof['proof_id'],))
                    cursor.execute("UPDATE person_cleanup_alias_proof_runs SET checked_count = candidate_total, rejected_count = candidate_total WHERE proof_id = %s", (proof['proof_id'],))
        while True:
            claimed = person_cleanup_db.claim_alias_proof_items(proof['proof_id'], 4)
            if not claimed:
                break
            for item in claimed:
                person_cleanup_db.finish_alias_proof_item(
                    proof['proof_id'], item['person_id'],
                    {'proof_state': 'identity_not_found'},
                )
        person_cleanup_db.complete_alias_proof_run(proof['proof_id'], self.generation)
        return proof

    def complete_stable_run(self, proof_id):
        run = person_cleanup_db.create_stale_index_run(
            proof_id, self.generation, 'protection', 'relationships', 'persons',
        )
        if run['candidate_total'] > 1000:
            with get_db_connection() as conn:
                with conn.cursor() as cursor:
                    cursor.execute("""
                        UPDATE person_cleanup_stale_index_items
                        SET forensic_state = 'verified_stale_index_signature',
                            identity_signal = 'stale_index_no_identity_owner',
                            people_signal = 'stale_index_different_people',
                            query_count = 1, actual_people_count = 1,
                            different_name_people_count = 1, stable_pass_count = 1, checked_at = NOW()
                        WHERE run_id = %s
                    """, (run['run_id'],))
                    person_cleanup_db._refresh_stale_index_run_counts(cursor, run['run_id'])
        while True:
            claimed = person_cleanup_db.claim_stale_index_items(run['run_id'], 4)
            if not claimed:
                break
            for item in claimed:
                person_cleanup_db.finish_stale_index_item(
                    run['run_id'], item['person_id'], {
                        'forensic_state': 'verified_stale_index_signature',
                        'identity_signal': 'stale_index_no_identity_owner',
                        'people_signal': 'stale_index_different_people',
                        'query_count': 1,
                        'actual_people_count': 1,
                        'different_name_people_count': 1,
                    },
                )
        return person_cleanup_db.complete_stale_index_run(
            run['run_id'], self.generation, 'protection', 'relationships', 'persons',
        )

    def create_chain(self, count=3):
        proof = self.create_completed_source(count)
        previous = self.complete_stable_run(proof['proof_id'])
        latest = self.complete_stable_run(proof['proof_id'])
        return previous, latest

    def test_deterministic_sha256_sample_is_bounded_to_100(self):
        _, latest = self.create_chain(105)
        job = person_cleanup_db.create_stale_delete_canary_job(100)
        self.assertEqual(job['eligible_total'], 105)
        self.assertEqual(job['candidate_total'], 100)
        expected = sorted(
            (
                hashlib.sha256(f"{latest['run_id']}:p{index:03d}".encode()).hexdigest(),
                f'p{index:03d}',
            )
            for index in range(105)
        )[:100]
        self.assertEqual(
            [(row['deterministic_rank'], row['person_id']) for row in job['items']],
            expected,
        )
        with self.assertRaises(ValueError):
            person_cleanup_db.create_stale_delete_canary_job(101)

    def test_same_name_and_identity_owner_signals_are_excluded(self):
        _, latest = self.create_chain(3)
        with get_db_connection() as conn:
            with conn.cursor() as cursor:
                cursor.execute("""
                    UPDATE person_cleanup_stale_index_items
                    SET people_signal = 'stale_index_same_name_other_person'
                    WHERE run_id = %s AND person_id = 'p000'
                """, (latest['run_id'],))
                cursor.execute("""
                    UPDATE person_cleanup_stale_index_items
                    SET identity_signal = 'stale_index_identity_owner_not_live'
                    WHERE run_id = %s AND person_id = 'p001'
                """, (latest['run_id'],))
        job = person_cleanup_db.create_stale_delete_canary_job(100)
        self.assertEqual(job['eligible_total'], 1)
        self.assertEqual([row['person_id'] for row in job['items']], ['p002'])

    def test_only_one_active_job_can_be_created_concurrently(self):
        self.create_chain(1)
        with ThreadPoolExecutor(max_workers=2) as executor:
            futures = [
                executor.submit(person_cleanup_db.create_stale_delete_canary_job, 1)
                for _ in range(2)
            ]
        successes = 0
        failures = 0
        for future in futures:
            try:
                future.result()
                successes += 1
            except RuntimeError:
                failures += 1
        self.assertEqual((successes, failures), (1, 1))

    def ready_job(self):
        self.create_chain(1)
        job = person_cleanup_db.create_stale_delete_canary_job(1)
        person_cleanup_db.bind_stale_delete_canary_admin_context(job['job_id'], 'a'*64)
        person_cleanup_db.set_stale_delete_canary_preview_snapshot(job['job_id'], {
            'generation': self.generation,
            'protection_hash': 'protection',
            'normal_people_relationship_hash': 'relationships',
            'person_hash': 'persons',
        })
        person_cleanup_db.mark_stale_delete_canary_preview_item(
            job['job_id'], job['items'][0]['person_id'],
            'canary_delete_ready', {'query_count': 1},
        )
        person_cleanup_db.finish_stale_delete_canary_preview(job['job_id'])
        return person_cleanup_db.get_stale_delete_canary_job(
            job['job_id'], include_items=True,
        )

    def start_job(self, job_id, snapshot):
        person_cleanup_db.bind_stale_delete_canary_admin_context(job_id, 'a'*64, execution=True)
        self.assertTrue(person_cleanup_db.reserve_stale_delete_canary_admin_auth(job_id))
        person_cleanup_db.verify_stale_delete_canary_admin_auth(job_id,'admin','a'*64)
        person_cleanup_db.start_stale_delete_canary_job(job_id, snapshot)

    def test_admin_binding_required_for_preview_execution_and_reservation(self):
        job=self.ready_job()
        token=person_cleanup_db.issue_stale_delete_canary_confirmation_token(job['job_id'])
        person_cleanup_db.confirm_stale_delete_canary_job(job['job_id'],token)
        person_cleanup_db.claim_stale_delete_canary_execution(job['job_id'])
        with self.assertRaises(person_cleanup_db.CanarySafetyError):
            person_cleanup_db.bind_stale_delete_canary_admin_context(job['job_id'],'b'*64,execution=True)
        with self.assertRaises(RuntimeError):
            person_cleanup_db.start_stale_delete_canary_job(job['job_id'],{
                'generation':self.generation,'protection_hash':'protection',
                'normal_people_relationship_hash':'relationships','person_hash':'persons'})
        self.assertFalse(person_cleanup_db.reserve_stale_delete_canary_attempt(job['job_id'],job['items'][0]['person_id']))
        item=person_cleanup_db.get_stale_delete_canary_job(job['job_id'],include_items=True)['items'][0]
        self.assertEqual(item['post_attempts'],0)

    def test_admin_binding_is_part_of_confirmation_fingerprint(self):
        job=self.ready_job()
        token=person_cleanup_db.issue_stale_delete_canary_confirmation_token(job['job_id'])
        with get_db_connection() as conn:
            with conn.cursor() as cursor:
                cursor.execute('UPDATE person_cleanup_stale_delete_jobs SET preview_admin_context_hash=%s WHERE job_id=%s',('b'*64,job['job_id']))
        with self.assertRaises(person_cleanup_db.CanarySafetyError):
            person_cleanup_db.confirm_stale_delete_canary_job(job['job_id'],token)

    def confirmed_job(self):
        job=self.ready_job()
        token=person_cleanup_db.issue_stale_delete_canary_confirmation_token(job['job_id'])
        person_cleanup_db.confirm_stale_delete_canary_job(job['job_id'],token)
        return job

    def bind_auth_fixture(self,job,base_url,api_key):
        binding=hashlib.sha256(json.dumps([base_url,'admin','Admin',job['job_id'],api_key],separators=(',',':')).encode()).hexdigest()
        with get_db_connection() as conn:
            with conn.cursor() as cursor:
                cursor.execute('UPDATE person_cleanup_stale_delete_jobs SET preview_admin_context_hash=%s WHERE job_id=%s AND state=%s',(binding,job['job_id'],'preview_ready'))
                cursor.execute('SELECT * FROM person_cleanup_stale_delete_jobs WHERE job_id=%s',(job['job_id'],))
                fingerprint=person_cleanup_db._canary_preview_fingerprint(cursor,dict(cursor.fetchone()))
                cursor.execute('UPDATE person_cleanup_stale_delete_jobs SET preview_fingerprint=%s WHERE job_id=%s',(fingerprint,job['job_id']))
        job=person_cleanup_db.get_stale_delete_canary_job(job['job_id'],include_items=True)
        token=person_cleanup_db.issue_stale_delete_canary_confirmation_token(job['job_id'])
        person_cleanup_db.confirm_stale_delete_canary_job(job['job_id'],token)
        return job

    def test_auth_reservation_two_connections_only_once_and_commit_rollback(self):
        job=self.confirmed_job()
        person_cleanup_db.claim_stale_delete_canary_execution(job['job_id'])
        person_cleanup_db.bind_stale_delete_canary_admin_context(job['job_id'],'a'*64,execution=True)
        @contextmanager
        def rollback():
            with get_db_connection() as conn:
                yield conn
                raise RuntimeError('admin auth commit failed')
        with patch.object(person_cleanup_db,'get_db_connection',rollback):
            with self.assertRaises(RuntimeError):person_cleanup_db.reserve_stale_delete_canary_admin_auth(job['job_id'])
        self.assertEqual(person_cleanup_db.get_stale_delete_canary_job(job['job_id'])['admin_auth_attempts'],0)
        with ThreadPoolExecutor(max_workers=2) as pool:
            results=list(pool.map(lambda _:person_cleanup_db.reserve_stale_delete_canary_admin_auth(job['job_id']),range(2)))
        self.assertEqual(sorted(results),[False,True])
        persisted=person_cleanup_db.get_stale_delete_canary_job(job['job_id'],include_items=True)
        self.assertEqual(persisted['admin_auth_attempts'],1)
        self.assertEqual(sum(i['post_attempts'] for i in persisted['items']),0)

    def test_auth_timeout_and_get_verification_failure_are_durable_no_replay(self):
        from tasks import actors
        import requests
        for scenario in ('timeout','invalid_session'):
            job=self.bind_auth_fixture(self.ready_job(),'http://emby','api-key')
            principal=actors.emby.AdminDeleteContext('http://emby','admin','Admin',job['job_id'],job['preview_admin_context_hash'],os.getpid(),'')
            processor=SimpleNamespace(emby_url='http://emby',emby_api_key='api-key',is_stop_requested=lambda:False)
            data={'AccessToken':'user-token','User':{'Id':'admin','Name':'Admin'},'SessionInfo':{'Id':'session'}}
            with patch.object(actors.emby,'ensure_admin_delete_context',return_value=principal), \
                    patch.dict(config_manager.APP_CONFIG,{constants.CONFIG_OPTION_EMBY_SERVER_URL:'http://emby',constants.CONFIG_OPTION_EMBY_API_KEY:'api-key',constants.CONFIG_OPTION_EMBY_ADMIN_USER:'Admin',constants.CONFIG_OPTION_EMBY_ADMIN_PASS:'test-password'}), \
                    patch.object(actors.emby.emby_client,'post_once',side_effect=requests.Timeout() if scenario=='timeout' else None,return_value=SimpleNamespace(status_code=200,json=lambda:data)) as post, \
                    patch.object(actors.emby,'verify_canary_admin_session',side_effect=actors.emby.AdminDeleteContextError('admin_session_invalid')), \
                    patch.object(actors,'_build_stale_delete_canary_snapshot') as snapshot:
                with self.assertRaises(RuntimeError):actors.task_execute_stale_delete_canary(processor,job['job_id'])
                actors.task_execute_stale_delete_canary(processor,job['job_id'])
                post.assert_called_once();snapshot.assert_not_called()
            persisted=person_cleanup_db.get_stale_delete_canary_job(job['job_id'],include_items=True)
            self.assertEqual(persisted['state'],'admin_auth_ambiguous' if scenario=='timeout' else 'admin_session_invalid')
            self.assertEqual(persisted['admin_auth_attempts'],1)
            self.assertEqual(sum(i['post_attempts'] for i in persisted['items']),0)

    def test_restart_after_auth_verified_before_person_never_relogs(self):
        from tasks import actors
        job=self.confirmed_job()
        person_cleanup_db.claim_stale_delete_canary_execution(job['job_id'])
        person_cleanup_db.bind_stale_delete_canary_admin_context(job['job_id'],'a'*64,execution=True)
        person_cleanup_db.reserve_stale_delete_canary_admin_auth(job['job_id'])
        person_cleanup_db.verify_stale_delete_canary_admin_auth(job['job_id'],'admin','a'*64)
        init_db()
        with patch.object(actors.emby,'authenticate_canary_admin_once') as auth:
            actors.task_execute_stale_delete_canary(SimpleNamespace(),job['job_id'])
            auth.assert_not_called()
        persisted=person_cleanup_db.get_stale_delete_canary_job(job['job_id'],include_items=True)
        self.assertEqual(persisted['state'],'interrupted_requires_review')
        self.assertEqual(persisted['admin_auth_attempts'],1)
        self.assertEqual(sum(i['post_attempts'] for i in persisted['items']),0)

    def test_hard_limits_omitted_one_100_and_invalid_never_exceed_100(self):
        self.create_chain(105)
        for value in (101,1000,-1,'malformed',None,{}):
            with self.subTest(value=value),self.assertRaises((ValueError,TypeError)):
                person_cleanup_db.create_stale_delete_canary_job(value)
        for value in (1,100,'omitted'):
            job=(person_cleanup_db.create_stale_delete_canary_job() if value=='omitted'
                 else person_cleanup_db.create_stale_delete_canary_job(value))
            self.assertEqual(job['candidate_total'],100 if value=='omitted' else value)
            self.assertLessEqual(len(job['items']),100)
            person_cleanup_db.fail_stale_delete_canary_job(job['job_id'],'stopped','fixture boundary')

    def test_confirmation_token_is_short_lived_single_use_and_job_bound(self):
        job = self.ready_job()
        token = person_cleanup_db.issue_stale_delete_canary_confirmation_token(job['job_id'])
        person_cleanup_db.confirm_stale_delete_canary_job(job['job_id'], token)
        with self.assertRaises(RuntimeError):
            person_cleanup_db.confirm_stale_delete_canary_job(job['job_id'], token)

    def test_production_18207_excludes_four_same_names_and_caps_100(self):
        previous, latest = self.create_chain(18207)
        with get_db_connection() as conn:
            with conn.cursor() as cursor:
                cursor.execute("""
                    UPDATE person_cleanup_stale_index_items
                    SET people_signal = 'stale_index_same_name_other_person'
                    WHERE run_id = ANY(%s) AND person_id = ANY(%s)
                """, ([previous['run_id'], latest['run_id']], ['p000', 'p001', 'p002', 'p003']))
        job = person_cleanup_db.create_stale_delete_canary_job(100)
        self.assertEqual((job['stable_total'], job['eligible_total'], job['same_name_excluded'], job['candidate_total']), (18207, 18203, 4, 100))
        self.assertTrue(set(row['person_id'] for row in job['items']).isdisjoint({'p000', 'p001', 'p002', 'p003'}))
        with self.assertRaises(ValueError):
            person_cleanup_db.create_stale_delete_canary_job(1000)

    def test_token_binds_full_preview_evidence(self):
        job = self.ready_job()
        token = person_cleanup_db.issue_stale_delete_canary_confirmation_token(job['job_id'])
        with get_db_connection() as conn:
            with conn.cursor() as cursor:
                cursor.execute("UPDATE person_cleanup_stale_delete_job_items SET preview_evidence = '{\"query_count\": 99}' WHERE job_id = %s", (job['job_id'],))
        with self.assertRaises(person_cleanup_db.CanarySafetyError):
            person_cleanup_db.confirm_stale_delete_canary_job(job['job_id'], token)
        self.assertEqual(person_cleanup_db.get_stale_delete_canary_job(job['job_id'])['state'], 'preview_ready')

    def test_expired_token_cannot_confirm(self):
        job = self.ready_job()
        token = person_cleanup_db.issue_stale_delete_canary_confirmation_token(job['job_id'])
        with get_db_connection() as conn:
            with conn.cursor() as cursor:
                cursor.execute("UPDATE person_cleanup_stale_delete_jobs SET confirmation_token_expires_at = NOW() - INTERVAL '1 second' WHERE job_id = %s", (job['job_id'],))
        with self.assertRaises(RuntimeError):
            person_cleanup_db.confirm_stale_delete_canary_job(job['job_id'], token)
        self.assertFalse(person_cleanup_db.claim_stale_delete_canary_execution(job['job_id']))

    def test_two_execution_processes_only_one_claim(self):
        job = self.ready_job()
        token = person_cleanup_db.issue_stale_delete_canary_confirmation_token(job['job_id'])
        person_cleanup_db.confirm_stale_delete_canary_job(job['job_id'], token)
        context = multiprocessing.get_context('spawn')
        queue = context.Queue()
        keys = (constants.CONFIG_OPTION_DB_HOST, constants.CONFIG_OPTION_DB_PORT,
                constants.CONFIG_OPTION_DB_USER, constants.CONFIG_OPTION_DB_PASSWORD,
                constants.CONFIG_OPTION_DB_NAME)
        db_config = {key: config_manager.APP_CONFIG[key] for key in keys}
        processes = [context.Process(target=_claim_canary_in_independent_process,
            args=(job['job_id'], db_config, queue)) for _ in range(2)]
        try:
            for process in processes: process.start()
            results = [queue.get(timeout=30) for _ in processes]
            for process in processes:
                process.join(timeout=10)
                self.assertEqual(process.exitcode, 0)
        finally:
            for process in processes:
                if process.is_alive(): process.terminate()
                process.join(timeout=5)
            queue.close()
        self.assertEqual(sorted(results), [False, True])
        self.assertEqual(person_cleanup_db.get_stale_delete_canary_job(job['job_id'])['state'], 'preflighting')

    def test_source_and_latest_chain_cannot_change(self):
        job = self.ready_job()
        person_cleanup_db.validate_stale_delete_canary_chain(job['job_id'])
        self.complete_stable_run(job['latest_source_proof_id'])
        with self.assertRaises(person_cleanup_db.CanarySafetyError):
            person_cleanup_db.validate_stale_delete_canary_chain(job['job_id'])

    def test_stop_prevents_new_reservation(self):
        job = self.ready_job()
        token = person_cleanup_db.issue_stale_delete_canary_confirmation_token(job['job_id'])
        person_cleanup_db.confirm_stale_delete_canary_job(job['job_id'], token)
        self.assertTrue(person_cleanup_db.claim_stale_delete_canary_execution(job['job_id']))
        self.start_job(job['job_id'], {
            'generation': self.generation, 'protection_hash': 'protection',
            'normal_people_relationship_hash': 'relationships', 'person_hash': 'persons',
        })
        self.assertTrue(person_cleanup_db.request_stale_delete_canary_stop(job['job_id']))
        self.assertFalse(person_cleanup_db.reserve_stale_delete_canary_attempt(job['job_id'], job['items'][0]['person_id']))

    def test_never_confirm_deleted_without_post_reservation(self):
        job = self.ready_job()
        self.assertFalse(person_cleanup_db.finish_stale_delete_canary_item(job['job_id'], job['items'][0]['person_id'], 'confirmed_deleted'))

    def test_real_http_post_observes_committed_pg_boundary_and_no_replay(self):
        from tasks import actors
        from tests.test_person_cleanup_stale_delete_canary import snapshot, ready_result
        job = self.ready_job()
        person_id = job['items'][0]['person_id']
        observed = []
        auth_observed = []
        auth_payloads = []
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_): pass
            def do_GET(self):
                parsed = urlparse(self.path)
                if parsed.path == '/Users/admin':
                    payload = {'Id': 'admin', 'Name':'Admin', 'Policy': {'IsAdministrator': True,'IsDisabled':False}}
                elif parsed.path == '/Sessions':
                    payload = [{'Id':'session','UserId':'admin','DeviceId':hashlib.sha256(('canary:'+job['job_id']).encode()).hexdigest()}]
                elif parsed.path == '/Items' and parse_qs(parsed.query).get('Ids') == [person_id]:
                    payload = {'Items': [], 'TotalRecordCount': 0}
                else:
                    self.send_error(404)
                    return
                self.send_response(200)
                self.send_header('Content-Type', 'application/json')
                encoded = json.dumps(payload).encode()
                self.send_header('Content-Length', str(len(encoded)))
                self.end_headers()
                self.wfile.write(encoded)
            def do_POST(self):
                # Consume the real request body before closing the socket.
                # An unread login body can cause TCP RST rather than a complete
                # HTTP response; the product correctly treats that as ambiguous.
                body = self.rfile.read(int(self.headers.get('Content-Length', '0')))
                if urlparse(self.path).path == '/Users/AuthenticateByName':
                    auth_payloads.append(json.loads(body))
                    with get_db_connection() as conn:
                        with conn.cursor() as cursor:
                            cursor.execute('SELECT admin_auth_state,admin_auth_attempts FROM person_cleanup_stale_delete_jobs WHERE job_id=%s',(job['job_id'],))
                            auth_observed.append(dict(cursor.fetchone()))
                    payload={'AccessToken':'fixture-user-token','User':{'Id':'admin','Name':'Admin'},'SessionInfo':{'Id':'session'}}
                    encoded = json.dumps(payload).encode()
                    self.send_response(200)
                    self.send_header('Content-Type','application/json')
                    self.send_header('Content-Length',str(len(encoded)))
                    self.end_headers()
                    self.wfile.write(encoded);return
                if urlparse(self.path).path != f'/Items/{person_id}/DeletePerson':
                    self.send_error(400)
                    return
                # Independent connection while HTTP is in flight: commit visible.
                with get_db_connection() as conn:
                    with conn.cursor() as cursor:
                        cursor.execute("SELECT execute_state, post_attempts FROM person_cleanup_stale_delete_job_items WHERE job_id=%s AND person_id=%s", (job['job_id'], person_id))
                        observed.append(dict(cursor.fetchone()))
                self.send_response(204)
                self.end_headers()
        server = HTTPServer(('127.0.0.1', 0), Handler)
        thread = Thread(target=server.serve_forever, daemon=True)
        thread.start()
        address = f'http://127.0.0.1:{server.server_port}'
        job = self.bind_auth_fixture(job,address,'fixture-key')
        start = snapshot([person_id])
        start['generation'] = self.generation
        final = {**start, 'person_details': {}, 'person_hash': 'empty'}
        proc = SimpleNamespace(emby_url=address, emby_api_key='fixture-key', is_stop_requested=lambda: False)
        try:
            with patch.object(actors, '_build_stale_delete_canary_snapshot', side_effect=[start, start, final]), \
                    patch.object(actors, '_check_stale_delete_canary_candidate', return_value=ready_result()), \
                    patch.object(actors.emby, 'wait_for_server_idle'), \
                    patch.object(actors.emby, 'ensure_admin_delete_context', return_value=actors.emby.AdminDeleteContext(address, 'admin', 'Admin', job['job_id'], job['preview_admin_context_hash'], os.getpid(), '')), \
                    patch.dict(actors.emby._admin_token_cache, {'access_token': 'fixture-token', 'user_id': 'admin'}, clear=True), \
                    patch.dict(config_manager.APP_CONFIG, {constants.CONFIG_OPTION_EMBY_SERVER_URL: address, constants.CONFIG_OPTION_EMBY_API_KEY: 'fixture-key', constants.CONFIG_OPTION_EMBY_ADMIN_USER: 'Admin',constants.CONFIG_OPTION_EMBY_ADMIN_PASS:'fixture-password'}), \
                    patch.object(actors.task_manager, 'update_status_from_thread'):
                actors.task_execute_stale_delete_canary(proc, job['job_id'])
                # Same confirmed request delivered a second time cannot get lock.
                actors.task_execute_stale_delete_canary(proc, job['job_id'])
            persisted = person_cleanup_db.get_stale_delete_canary_job(job['job_id'], include_items=True)
            self.assertEqual(observed, [{'execute_state': 'post_reserved', 'post_attempts': 1}])
            self.assertEqual(auth_observed,[{'admin_auth_state':'post_reserved','admin_auth_attempts':1}])
            self.assertEqual(auth_payloads,[{'Username':'Admin','Pw':'fixture-password'}])
            self.assertEqual(persisted['state'], 'canary_verified')
            self.assertEqual(persisted['items'][0]['http_status'], 204)
            self.assertEqual(persisted['items'][0]['readback_state'], 'person_missing')
            self.assertIsNotNone(persisted['items'][0]['deleted_at'])
            init_db()
            actors.task_execute_stale_delete_canary(proc, job['job_id'])
            self.assertEqual(len(observed), 1)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)

    def test_real_pg_rollback_at_commit_leaves_no_delete_permission(self):
        job = self.ready_job()
        token = person_cleanup_db.issue_stale_delete_canary_confirmation_token(job['job_id'])
        person_cleanup_db.confirm_stale_delete_canary_job(job['job_id'], token)
        person_cleanup_db.claim_stale_delete_canary_execution(job['job_id'])
        self.start_job(job['job_id'], {
            'generation': self.generation, 'protection_hash': 'protection',
            'normal_people_relationship_hash': 'relationships', 'person_hash': 'persons',
        })
        @contextmanager
        def fail_commit():
            with get_db_connection() as conn:
                yield conn
                raise RuntimeError('injected failure before COMMIT')
        with patch.object(person_cleanup_db, 'get_db_connection', fail_commit):
            with self.assertRaises(RuntimeError):
                person_cleanup_db.reserve_stale_delete_canary_attempt(job['job_id'], job['items'][0]['person_id'])
        row = person_cleanup_db.get_stale_delete_canary_job(job['job_id'], include_items=True)['items'][0]
        self.assertEqual((row['execute_state'], row['post_attempts']), ('pending', 0))
        with get_db_connection() as conn:
            with conn.cursor() as cursor:
                cursor.execute('SELECT COUNT(*) AS total FROM person_cleanup_delete_attempts')
                self.assertEqual(cursor.fetchone()['total'], 0)

    def test_reservation_rechecks_candidate_and_protection_in_transaction(self):
        job = self.ready_job()
        token = person_cleanup_db.issue_stale_delete_canary_confirmation_token(job['job_id'])
        person_cleanup_db.confirm_stale_delete_canary_job(job['job_id'], token)
        person_cleanup_db.claim_stale_delete_canary_execution(job['job_id'])
        self.start_job(job['job_id'], {
            'generation': self.generation, 'protection_hash': 'protection',
            'normal_people_relationship_hash': 'relationships', 'person_hash': 'persons',
        })
        person_id = job['items'][0]['person_id']
        with get_db_connection() as conn:
            with conn.cursor() as cursor:
                cursor.execute("UPDATE person_cleanup_candidates SET person_name = 'Changed' WHERE person_id = %s", (person_id,))
        with self.assertRaises(person_cleanup_db.CanarySafetyError) as error:
            person_cleanup_db.reserve_stale_delete_canary_attempt(job['job_id'], person_id)
        self.assertEqual(error.exception.state, 'candidate_changed')
        person_cleanup_db.begin_protection_snapshot()
        with self.assertRaises(person_cleanup_db.CanarySafetyError) as error:
            person_cleanup_db.reserve_stale_delete_canary_attempt(job['job_id'], person_id)
        self.assertEqual(error.exception.state, 'protection_drift')
        row = person_cleanup_db.get_stale_delete_canary_job(job['job_id'], include_items=True)['items'][0]
        self.assertEqual(row['post_attempts'], 0)

    def test_post_boundary_is_atomic_global_and_never_reservable_twice(self):
        job = self.ready_job()
        token = person_cleanup_db.issue_stale_delete_canary_confirmation_token(job['job_id'])
        person_cleanup_db.confirm_stale_delete_canary_job(job['job_id'], token)
        self.assertTrue(person_cleanup_db.claim_stale_delete_canary_execution(job['job_id']))
        self.start_job(job['job_id'], {
            'generation': self.generation,
            'protection_hash': 'protection',
            'normal_people_relationship_hash': 'relationships',
            'person_hash': 'persons',
        })
        person_id = job['items'][0]['person_id']
        self.assertTrue(person_cleanup_db.reserve_stale_delete_canary_attempt(
            job['job_id'], person_id,
        ))
        self.assertFalse(person_cleanup_db.reserve_stale_delete_canary_attempt(
            job['job_id'], person_id,
        ))
        with get_db_connection() as conn:
            with conn.cursor() as cursor:
                cursor.execute("""
                    SELECT items.execute_state, items.post_attempts,
                           attempts.operation_id, attempts.state
                    FROM person_cleanup_stale_delete_job_items items
                    JOIN person_cleanup_delete_attempts attempts USING (person_id)
                    WHERE items.job_id = %s AND items.person_id = %s
                """, (job['job_id'], person_id))
                row = cursor.fetchone()
        self.assertEqual(row['execute_state'], 'post_reserved')
        self.assertEqual(row['post_attempts'], 1)
        self.assertEqual(row['operation_id'], f"stale-canary:{job['job_id']}")
        self.assertEqual(row['state'], 'submitting')

    def test_restart_marks_running_job_interrupted_and_never_reopens_items(self):
        job = self.ready_job()
        token = person_cleanup_db.issue_stale_delete_canary_confirmation_token(job['job_id'])
        person_cleanup_db.confirm_stale_delete_canary_job(job['job_id'], token)
        self.assertTrue(person_cleanup_db.claim_stale_delete_canary_execution(job['job_id']))
        self.start_job(job['job_id'], {
            'generation': self.generation,
            'protection_hash': 'protection',
            'normal_people_relationship_hash': 'relationships',
            'person_hash': 'persons',
        })
        person_id = job['items'][0]['person_id']
        self.assertTrue(person_cleanup_db.reserve_stale_delete_canary_attempt(job['job_id'], person_id))
        init_db()
        recovered = person_cleanup_db.get_stale_delete_canary_job(job['job_id'])
        self.assertEqual(recovered['state'], 'interrupted_requires_review')
        self.assertTrue(recovered['stop_requested'])
        self.assertFalse(person_cleanup_db.claim_stale_delete_canary_execution(job['job_id']))
        self.assertFalse(person_cleanup_db.reserve_stale_delete_canary_attempt(job['job_id'], person_id))
        row = person_cleanup_db.get_stale_delete_canary_job(job['job_id'], include_items=True)['items'][0]
        self.assertEqual((row['execute_state'], row['post_attempts']), ('post_reserved', 1))
        self.assertFalse(hasattr(person_cleanup_db, 'resume_stale_delete_canary_job'))


if __name__ == '__main__':
    unittest.main()
