import unittest
from types import SimpleNamespace
from unittest.mock import patch

from flask import Flask

import config_manager
from routes.person_cleanup import person_cleanup_bp
from tasks import actors


class PersonCleanupPreviewDetailsApiTests(unittest.TestCase):
    def setUp(self):
        self.previous_auth = config_manager.APP_CONFIG.get('auth_enabled')
        config_manager.APP_CONFIG['auth_enabled'] = False
        self.app = Flask(__name__)
        self.app.config['TESTING'] = True
        self.app.register_blueprint(person_cleanup_bp)
        self.client = self.app.test_client()

    def tearDown(self):
        if self.previous_auth is None:
            config_manager.APP_CONFIG.pop('auth_enabled', None)
        else:
            config_manager.APP_CONFIG['auth_enabled'] = self.previous_auth

    @staticmethod
    def summary():
        return {
            'candidate_total': 22714,
            'items_total': 22714,
            'preview_progress_count': 22714,
            'preview_expected_count': 22714,
            'preview_complete': True,
            'verified_orphan': 712,
            'non_verified_orphan': 22002,
            'states': [
                {'status': 'verified_orphan', 'count': 712, 'percentage': 3.13},
                {'status': 'future_unknown_state', 'count': 22002, 'percentage': 96.87},
            ],
            'counts': {'verified_orphan': 712, 'future_unknown_state': 22002},
            'consistent': True,
            'consistency_warning': None,
        }

    def test_job_detail_returns_dynamic_persisted_summary_without_emby(self):
        job = {'job_id': 'job-1', 'state': 'completed', 'candidate_total': 22714}
        with patch(
            'routes.person_cleanup.person_cleanup_db.get_cleanup_job', return_value=job,
        ), patch(
            'routes.person_cleanup.person_cleanup_db.get_cleanup_job_preview_summary',
            return_value=self.summary(),
        ), patch(
            'routes.person_cleanup.emby.get_person_media_references',
        ) as emby_lookup:
            response = self.client.get('/api/person-cleanup/cleanup-jobs/job-1')

        self.assertEqual(response.status_code, 200)
        payload = response.get_json()['job']['preview_summary']
        self.assertEqual(payload['candidate_total'], 22714)
        self.assertEqual(payload['verified_orphan'], 712)
        self.assertEqual(payload['non_verified_orphan'], 22002)
        self.assertEqual(payload['counts']['future_unknown_state'], 22002)
        emby_lookup.assert_not_called()

    def test_latest_job_exposes_historical_preview_without_rerun(self):
        job = {'job_id': 'job-old', 'state': 'completed', 'candidate_total': 22714}
        with patch(
            'routes.person_cleanup.person_cleanup_db.get_latest_cleanup_job',
            return_value=job,
        ), patch(
            'routes.person_cleanup.person_cleanup_db.get_cleanup_job_preview_summary',
            return_value=self.summary(),
        ), patch(
            'routes.person_cleanup.task_manager.submit_task',
        ) as submit_task:
            response = self.client.get('/api/person-cleanup/cleanup-jobs/latest')

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()['job']['job_id'], 'job-old')
        submit_task.assert_not_called()

    def test_history_lists_old_job_after_new_preview_without_emby_or_writes(self):
        newest = {
            'job_id': 'job-new', 'state': 'previewing', 'candidate_total': 22002,
            'verified_orphan_count': 0, 'verification_failed_count': 0,
            'protected_count': 0, 'linked_count': 0, 'deleted_count': 0,
            'skipped_count': 0, 'failed_count': 0,
        }
        old = {
            'job_id': 'job-old', 'state': 'completed', 'candidate_total': 22714,
            'verified_orphan_count': 712, 'verification_failed_count': 22002,
            'protected_count': 0, 'linked_count': 0, 'deleted_count': 712,
            'skipped_count': 0, 'failed_count': 0,
        }
        summaries = {
            'job-new': {**self.summary(), 'candidate_total': 22002},
            'job-old': self.summary(),
        }
        with patch(
            'routes.person_cleanup.person_cleanup_db.list_cleanup_jobs',
            return_value=[newest, old],
        ) as list_jobs, patch(
            'routes.person_cleanup.person_cleanup_db.get_cleanup_job_preview_summary',
            side_effect=lambda job_id: summaries[job_id],
        ), patch(
            'routes.person_cleanup.person_cleanup_db.add_cleanup_job_item',
        ) as add_item, patch(
            'routes.person_cleanup.person_cleanup_db.finish_cleanup_preview',
        ) as finish_preview, patch(
            'routes.person_cleanup.emby.get_person_media_references',
        ) as emby_lookup:
            response = self.client.get('/api/person-cleanup/cleanup-jobs?limit=20')

        self.assertEqual(response.status_code, 200)
        jobs = response.get_json()['jobs']
        self.assertEqual([job['job_id'] for job in jobs], ['job-new', 'job-old'])
        self.assertEqual(jobs[1]['candidate_total'], 22714)
        self.assertEqual(jobs[1]['verified_orphan_count'], 712)
        self.assertEqual(jobs[1]['preview_summary']['non_verified_orphan'], 22002)
        list_jobs.assert_called_once_with(limit=20)
        add_item.assert_not_called()
        finish_preview.assert_not_called()
        emby_lookup.assert_not_called()

    def test_preview_task_fixes_candidate_total_before_processing(self):
        processor = SimpleNamespace(is_stop_requested=lambda: True)
        call_order = []
        candidates = [
            {'person_id': 'p1', 'person_name': 'P1'},
            {'person_id': 'p2', 'person_name': 'P2'},
        ]
        with patch.object(
            actors, '_refresh_protected_snapshot', return_value=(7, {}),
        ), patch.object(
            actors.person_cleanup_db, 'get_protection_contract',
            return_value={'generation': 7},
        ), patch.object(
            actors, '_build_protected_root_contract', return_value={},
        ), patch.object(
            actors.person_cleanup_db, 'list_candidates_raw', return_value=candidates,
        ), patch.object(
            actors.person_cleanup_db, 'initialize_cleanup_job_candidate_total',
            side_effect=lambda job_id, total: call_order.append(('total', job_id, total)),
        ) as initialize_total, patch.object(
            actors.person_cleanup_db, 'cleanup_job_stop_requested', return_value=False,
        ), patch.object(
            actors.person_cleanup_db, 'finish_cleanup_job',
            side_effect=lambda job_id, stopped: call_order.append(('finish', job_id, stopped)),
        ), patch.object(
            actors.task_manager, 'update_status_from_thread',
        ), patch.object(
            actors.emby, 'get_person_media_references',
        ) as emby_lookup:
            actors.task_preview_safe_person_cleanup(processor, 'job-new')

        initialize_total.assert_called_once_with('job-new', 2)
        self.assertEqual(call_order, [
            ('total', 'job-new', 2),
            ('finish', 'job-new', True),
        ])
        emby_lookup.assert_not_called()

    def test_samples_are_exact_state_paginated_and_read_only(self):
        sample = {
            'person_id': 'p-1',
            'person_name': '样本人物',
            'provider_ids_json': {'Tmdb': '123'},
            'preview_state': 'identity_alias_only',
            'execute_state': 'pending',
            'last_error': '仅命中其他 Person',
        }
        with patch(
            'routes.person_cleanup.person_cleanup_db.get_cleanup_job',
            return_value={'job_id': 'job-1'},
        ), patch(
            'routes.person_cleanup.person_cleanup_db.list_cleanup_job_preview_items',
            return_value={
                'status': 'identity_alias_only',
                'items': [sample],
                'total': 9,
                'page': 2,
                'page_size': 5,
            },
        ) as list_items, patch(
            'routes.person_cleanup.person_cleanup_db.add_cleanup_job_item',
        ) as add_item, patch(
            'routes.person_cleanup.emby.get_person_media_references',
        ) as emby_lookup:
            response = self.client.get(
                '/api/person-cleanup/cleanup-jobs/job-1/preview-items'
                '?state=identity_alias_only&page=2&page_size=5'
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()['items'][0]['provider_ids_json'], {'Tmdb': '123'})
        list_items.assert_called_once_with(
            'job-1', 'identity_alias_only', page=2, page_size=5,
        )
        add_item.assert_not_called()
        emby_lookup.assert_not_called()

    def test_samples_reject_invalid_pagination_without_database_read(self):
        with patch(
            'routes.person_cleanup.person_cleanup_db.get_cleanup_job',
        ) as get_job:
            response = self.client.get(
                '/api/person-cleanup/cleanup-jobs/job-1/preview-items'
                '?state=linked&page=0&page_size=5'
            )
        self.assertEqual(response.status_code, 400)
        get_job.assert_not_called()


if __name__ == '__main__':
    unittest.main()
