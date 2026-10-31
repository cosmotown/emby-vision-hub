"""Queue bookkeeping only: exact recovery evidence and explicit non-replaying archive."""
import ast
import json
import logging
import os
from pathlib import Path
import unittest
import uuid
from unittest.mock import MagicMock, patch

from flask import Blueprint, Flask, jsonify, request, session
from database import webhook_event_db as queue
from extensions import admin_required

ROOT = Path(__file__).resolve().parents[1]


class WebhookArchiveApiTests(unittest.TestCase):
    def setUp(self):
        # Execute the real decorated endpoint without importing unrelated task SDKs.
        nodes = [n for n in ast.parse((ROOT / 'routes/tasks.py').read_text()).body
                 if isinstance(n, ast.FunctionDef) and n.name == 'archive_webhook_event']
        self.db = MagicMock()
        bp = Blueprint('archive_fixture', __name__)
        namespace = dict(tasks_bp=bp, admin_required=admin_required, request=request,
                         session=session, jsonify=jsonify, webhook_event_db=self.db,
                         logger=logging.getLogger(__name__))
        exec(compile(ast.Module(body=nodes, type_ignores=[]), 'routes/tasks.py', 'exec'), namespace)
        self.app = Flask(__name__)
        self.app.secret_key = 'offline-test-only'
        self.app.register_blueprint(bp, url_prefix='/api/tasks')
        self.client = self.app.test_client()
        self.auth = patch('config_manager.APP_CONFIG', {'auth_enabled': True})
        self.auth.start()
        self.addCleanup(self.auth.stop)
        self.permission = patch('database.user_db.is_user_admin', return_value=True)
        self.permission.start()
        self.addCleanup(self.permission.stop)
        with self.client.session_transaction() as s:
            s['emby_user_id'] = 'admin-exact-id'

    def test_archive_requires_explicit_boolean_confirmation(self):
        for body in (None, [], {}, {'confirmed_media_ok': False}, {'confirmed_media_ok': 'true'},
                     {'confirmed_media_ok': 1}):
            with self.subTest(body=body):
                self.assertEqual(self.client.post('/api/tasks/webhook-events/17/archive',
                                                 json=body).status_code, 400)
        self.db.archive_failed_event.assert_not_called()

    def test_archive_is_admin_only(self):
        with patch('database.user_db.is_user_admin', return_value=False):
            response = self.client.post('/api/tasks/webhook-events/17/archive',
                                        json={'confirmed_media_ok': True})
        self.assertEqual(response.status_code, 403)
        self.db.archive_failed_event.assert_not_called()

    def test_archive_is_not_replay_or_media_mutation(self):
        self.db.archive_failed_event.return_value = True
        response = self.client.post('/api/tasks/webhook-events/17/archive',
                                    json={'confirmed_media_ok': True})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.db.method_calls, [
            unittest.mock.call.archive_failed_event(17, 'admin-exact-id')])
        self.assertEqual(self.client.get('/api/tasks/webhook-events/17/archive').status_code, 405)

    def test_stale_state_is_conflict_not_success(self):
        self.db.archive_failed_event.return_value = False
        self.assertEqual(self.client.post('/api/tasks/webhook-events/17/archive',
                         json={'confirmed_media_ok': True}).status_code, 409)

    def test_database_failure_is_not_hidden(self):
        self.db.archive_failed_event.side_effect = RuntimeError('fixture-db-down')
        self.assertEqual(self.client.post('/api/tasks/webhook-events/17/archive',
                         json={'confirmed_media_ok': True}).status_code, 500)


@unittest.skipUnless(os.environ.get('EVH_TEST_POSTGRES_HOST'), 'isolated PostgreSQL required')
class WebhookFailureResolutionPostgresTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import psycopg2
        from psycopg2 import sql
        from psycopg2.extras import RealDictCursor
        cls.kwargs = dict(host=os.environ['EVH_TEST_POSTGRES_HOST'],
                          port=os.environ.get('EVH_TEST_POSTGRES_PORT', '5432'),
                          user=os.environ['EVH_TEST_POSTGRES_USER'],
                          password=os.environ['EVH_TEST_POSTGRES_PASSWORD'],
                          dbname=os.environ['EVH_TEST_POSTGRES_DB'],
                          connect_timeout=5, cursor_factory=RealDictCursor)
        cls.schema = 'webhook_resolution_test_' + uuid.uuid4().hex
        with psycopg2.connect(**cls.kwargs) as conn, conn.cursor() as cursor:
            cursor.execute(sql.SQL('CREATE SCHEMA {}').format(sql.Identifier(cls.schema)))
        cls.kwargs['options'] = '-c search_path=' + cls.schema
        cls.connect = staticmethod(lambda: psycopg2.connect(**cls.kwargs))
        # Use actual application DDL, twice, to exercise additive upgrade idempotence.
        cls.ddl = [n.args[0].value for n in ast.walk(ast.parse(
            (ROOT / 'database/connection.py').read_text()))
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
            and n.func.attr == 'execute' and n.args
            and isinstance(n.args[0], ast.Constant) and isinstance(n.args[0].value, str)
            and ('CREATE TABLE IF NOT EXISTS webhook_event_queue' in n.args[0].value
                 or 'ALTER TABLE webhook_event_queue' in n.args[0].value
                 or 'ON webhook_event_queue (' in n.args[0].value)]
        with cls.connect() as conn, conn.cursor() as cursor:
            for _ in range(2):
                for statement in cls.ddl:
                    cursor.execute(statement)

    @classmethod
    def tearDownClass(cls):
        import psycopg2
        from psycopg2 import sql
        kwargs = {k: v for k, v in cls.kwargs.items() if k != 'options'}
        with psycopg2.connect(**kwargs) as conn, conn.cursor() as cursor:
            cursor.execute(sql.SQL('DROP SCHEMA {} CASCADE').format(sql.Identifier(cls.schema)))

    def setUp(self):
        self.connection_patch = patch.object(queue, 'get_db_connection', self.connect)
        self.connection_patch.start()
        self.addCleanup(self.connection_patch.stop)
        with self.connect() as conn, conn.cursor() as cursor:
            cursor.execute('TRUNCATE webhook_event_queue RESTART IDENTITY')

    def event(self, status='failed', *, item='series-A', episodes=None, payload=None,
              kind='media_process', source='emby', item_type='Series'):
        if payload is None:
            payload = dict(item_id=item, force_full_update=False, is_new_item=False,
                           new_episode_ids=episodes)
        with self.connect() as conn, conn.cursor() as cursor:
            cursor.execute("""
                INSERT INTO webhook_event_queue (dedupe_key,task_kind,task_name,item_id,
                    item_name,item_type,event_source,payload_json,status,attempt_count,
                    completed_at,started_at,last_error)
                VALUES (%s,%s,'same display name',%s,'同名剧',%s,%s,%s::jsonb,%s,4,
                    CASE WHEN %s IN ('failed','completed') THEN NOW()-INTERVAL '1 hour' END,
                    NOW()-INTERVAL '2 hours','original failure') RETURNING id
                """, (f'{source}:{kind}:{item}', kind, item, item_type, source,
                      json.dumps(payload), status, status))
            return cursor.fetchone()['id']

    def row(self, event):
        with self.connect() as conn, conn.cursor() as cursor:
            cursor.execute('SELECT * FROM webhook_event_queue WHERE id=%s', (event,))
            return dict(cursor.fetchone())

    def success(self, **kwargs):
        event = self.event('processing', **kwargs)
        with self.connect() as conn, conn.cursor() as cursor:
            cursor.execute('UPDATE webhook_event_queue SET started_at=NOW() WHERE id=%s', (event,))
        queue.mark_completed(event)
        return event

    def test_exact_episode_superset_recovers_preserving_original_error(self):
        failed = self.event(episodes=['e1', 'e2'])
        succeeded = self.success(episodes=['e2', 'e1', 'e3'])
        row = self.row(failed)
        self.assertEqual(row['status'], 'recovered')
        self.assertEqual(row['last_error'], 'original failure')
        self.assertEqual(row['resolution_json'], {'method': 'covered_by_success', 'event_id': succeeded})
        self.assertIsNotNone(row['resolved_at'])
        self.assertFalse(queue.retry_event(failed))
        summary = queue.get_queue_summary()
        self.assertEqual((summary['failed_count'], summary['recovered_count'], summary['completed_count']), (0, 1, 1))

    def test_missing_or_disjoint_episodes_stay_failed(self):
        for episodes in (['e1'], ['other'], None, []):
            with self.subTest(episodes=episodes):
                failed = self.event(episodes=['e1', 'e2'])
                self.success(episodes=episodes)
                self.assertEqual(self.row(failed)['status'], 'failed')

    def test_same_name_different_exact_item_cannot_recover(self):
        failed = self.event(item='series-A')
        self.success(item='series-B')
        self.assertEqual(self.row(failed)['status'], 'failed')

    def test_names_do_not_control_recovery(self):
        failed = self.event(episodes=['e1'])
        with self.connect() as conn, conn.cursor() as cursor:
            cursor.execute("UPDATE webhook_event_queue SET item_name='old title' WHERE id=%s", (failed,))
        self.success(episodes=['e1'])
        self.assertEqual(self.row(failed)['status'], 'recovered')

    def test_different_task_source_type_or_flags_do_not_recover(self):
        failed = self.event(episodes=['e1'])
        for kwargs in ({'kind': 'metadata_sync'}, {'kind': 'image_sync'}, {'source': 'other'},
                       {'item_type': 'Movie'}, {'payload': dict(item_id='series-A',
                        force_full_update=True, is_new_item=False, new_episode_ids=['e1'])}):
            self.success(episodes=['e1'], **kwargs)
            self.assertEqual(self.row(failed)['status'], 'failed')

    def test_unknown_or_malformed_payload_is_not_proof(self):
        for value in ('e1', {}, [1], [''], ['  '], [None]):
            failed = self.event(episodes=value)
            self.success(episodes=value)
            self.assertEqual(self.row(failed)['status'], 'failed')
        payload = dict(item_id='series-A', force_full_update=False, is_new_item=False,
                       unknown_new_semantics=True)
        failed = self.event(payload=payload)
        self.success(payload=payload)
        self.assertEqual(self.row(failed)['status'], 'failed')

    def test_empty_episode_target_is_not_covered_by_specific_batch(self):
        failed = self.event(episodes=None)
        self.success(episodes=['e1'])
        self.assertEqual(self.row(failed)['status'], 'failed')
        self.success(episodes=[])
        self.assertEqual(self.row(failed)['status'], 'recovered')

    def test_older_success_or_overlapping_execution_is_not_proof(self):
        self.success(episodes=['e1'])
        failed = self.event(episodes=['e1'])
        overlapping = self.event('completed', episodes=['e1'])
        self.assertEqual(queue.reconcile_failed_events(), 0)
        self.assertEqual(self.row(failed)['status'], 'failed')
        self.assertEqual(self.row(overlapping)['status'], 'completed')

    def test_upgrade_backfill_uses_existing_success_evidence_idempotently(self):
        failed = self.event(episodes=['e1'])
        succeeded = self.event('completed', episodes=['e1'])
        with self.connect() as conn, conn.cursor() as cursor:
            cursor.execute("UPDATE webhook_event_queue SET started_at=NOW()-INTERVAL '1 minute', completed_at=NOW() WHERE id=%s", (succeeded,))
        self.assertEqual(queue.reconcile_failed_events(), 1)
        self.assertEqual(queue.reconcile_failed_events(), 0)
        self.assertEqual(self.row(failed)['status'], 'recovered')

    def test_active_or_successful_events_cannot_be_archived(self):
        for status in ('pending', 'retry', 'processing', 'completed', 'superseded', 'recovered', 'archived'):
            event = self.event(status, item=status)
            self.assertFalse(queue.archive_failed_event(event, 'admin'))
            self.assertEqual(self.row(event)['status'], status)
        self.assertFalse(queue.archive_failed_event(999999, 'admin'))

    def test_manual_archive_preserves_evidence_and_never_requeues(self):
        failed = self.event(episodes=['e1'])
        before = self.row(failed)
        self.assertTrue(queue.archive_failed_event(failed, 'admin-id'))
        after = self.row(failed)
        for field in ('last_error', 'payload_json', 'attempt_count', 'completed_at'):
            self.assertEqual(after[field], before[field])
        self.assertEqual(after['status'], 'archived')
        self.assertEqual(after['resolution_json'], {'method': 'manual_confirmation', 'actor_id': 'admin-id'})
        self.assertFalse(queue.archive_failed_event(failed, 'admin-id'))
        self.assertFalse(queue.retry_event(failed))
        self.assertFalse(queue.has_pending_events())
        self.assertEqual(queue.get_queue_summary()['failed_count'], 0)
        self.assertEqual(queue.list_recent_events()[0]['resolution_json']['actor_id'], 'admin-id')

    def test_terminal_retention_starts_at_resolution_not_original_failure(self):
        failed = self.event()
        with self.connect() as conn, conn.cursor() as cursor:
            cursor.execute("UPDATE webhook_event_queue SET completed_at=NOW()-INTERVAL '90 days' WHERE id=%s", (failed,))
        queue.archive_failed_event(failed, 'admin')
        self.assertEqual(queue.prune_old_events(), 0)
        with self.connect() as conn, conn.cursor() as cursor:
            cursor.execute("UPDATE webhook_event_queue SET resolved_at=NOW()-INTERVAL '31 days' WHERE id=%s", (failed,))
        unresolved = self.event()
        self.assertEqual(queue.prune_old_events(), 1)
        self.assertEqual(self.row(unresolved)['status'], 'failed')

    def test_reconciliation_is_one_set_update_not_per_event_sql(self):
        cursor = MagicMock(rowcount=1000)
        self.assertEqual(queue._reconcile_failed_events(cursor), 1000)
        self.assertEqual(cursor.execute.call_count, 1)
        self.assertIn("WHERE f.id = covered.id AND f.status = 'failed'", cursor.execute.call_args.args[0])

    def test_thousand_old_failures_are_reconciled_without_replay(self):
        original = self.event(episodes=['e1'])
        with self.connect() as conn, conn.cursor() as cursor:
            cursor.execute("""INSERT INTO webhook_event_queue (
                dedupe_key,task_kind,task_name,item_id,item_type,event_source,payload_json,
                status,attempt_count,completed_at,started_at,last_error)
                SELECT dedupe_key,task_kind,task_name,item_id,item_type,event_source,payload_json,
                status,attempt_count,completed_at,started_at,last_error
                FROM webhook_event_queue CROSS JOIN generate_series(1,999) WHERE id=%s""", (original,))
        self.success(episodes=['e1', 'e2'])
        summary = queue.get_queue_summary()
        self.assertEqual(summary['recovered_count'], 1000)
        self.assertEqual(summary['failed_count'], 0)
        self.assertEqual(summary['pending_count'], 0)

    def test_concurrent_retry_and_archive_have_one_winner(self):
        from concurrent.futures import ThreadPoolExecutor
        failed = self.event()
        with ThreadPoolExecutor(max_workers=2) as pool:
            archive = pool.submit(queue.archive_failed_event, failed, 'admin')
            retry = pool.submit(queue.retry_event, failed)
            self.assertEqual(sum((archive.result(), retry.result())), 1)
        self.assertIn(self.row(failed)['status'], ('pending', 'archived'))


if __name__ == '__main__':
    unittest.main()
