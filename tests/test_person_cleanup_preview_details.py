import unittest
from unittest.mock import patch

from flask import Flask

import config_manager
from routes.person_cleanup import person_cleanup_bp


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
