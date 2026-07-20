import unittest
from unittest.mock import MagicMock, patch

from core_processor import MediaProcessor
from database.actor_db import ActorDBManager


class DoubanOnlyActorIngestTests(unittest.TestCase):
    def test_database_accepts_douban_identity_without_fake_tmdb_id(self):
        with patch('database.actor_db.logger.trace', create=True):
            manager = ActorDBManager()
        cursor = MagicMock()
        cursor.fetchone.side_effect = [
            None,
            {'map_id': 7, 'action': 'INSERTED'},
        ]

        map_id, action = manager.upsert_person(
            cursor,
            {
                'id': None,
                'douban_id': '12345',
                'name': '豆瓣独有演员',
                'profile_path': 'https://img1.doubanio.com/p12345.jpg',
            },
            {'url': 'http://emby.invalid', 'api_key': 'hidden', 'user_id': 'u1'},
        )

        self.assertEqual((map_id, action), (7, 'INSERTED'))
        executed_sql = '\n'.join(call.args[0] for call in cursor.execute.call_args_list)
        self.assertIn('ON CONFLICT (douban_celebrity_id)', executed_sql)
        self.assertNotIn("'None'", executed_sql)

    @patch('core_processor.emby.upload_item_primary_image_from_url')
    @patch('core_processor.emby.update_person_details')
    @patch('core_processor.emby.update_emby_item_details', return_value=True)
    @patch('core_processor.emby.get_emby_item_details')
    @patch('core_processor.emby.get_people_by_provider_ids')
    def test_existing_douban_person_is_reused_and_verified(
        self,
        get_people,
        get_details,
        update_item,
        update_person,
        upload_avatar,
    ):
        processor = MediaProcessor.__new__(MediaProcessor)
        processor.emby_url = 'http://emby.invalid'
        processor.emby_api_key = 'hidden'
        processor.emby_user_id = 'u1'
        processor.actor_db_manager = MagicMock()
        processor.actor_db_manager.find_person_by_any_id.return_value = None
        processor.actor_db_manager.upsert_person.return_value = (9, 'UPDATED')
        cursor = MagicMock()
        actor = {
            'id': None,
            'douban_id': '5678',
            'name': '已有演员',
            'character': '角色甲',
            'profile_path': 'https://img1.doubanio.com/p5678.jpg',
        }
        person = {
            'Id': 'person-1',
            'Name': '已有演员',
            'ProviderIds': {'Douban': '5678'},
            'ImageTags': {'Primary': 'tag'},
        }
        get_people.return_value = [person]
        get_details.side_effect = [
            person,
            person,
            {'Id': 'media-1', 'People': []},
            {'Id': 'media-1', 'People': [{'Id': 'person-1', 'Type': 'Actor'}]},
        ]

        synced = processor._sync_douban_only_cast_to_emby(
            'media-1', [actor], cursor
        )

        self.assertEqual(synced, 1)
        self.assertEqual(actor['emby_person_id'], 'person-1')
        update_item.assert_called_once()
        update_person.assert_not_called()
        upload_avatar.assert_not_called()

    @patch('core_processor.emby.upload_item_primary_image_from_url', return_value=True)
    @patch('core_processor.emby.update_person_details', return_value=True)
    @patch('core_processor.emby.update_emby_item_details', return_value=True)
    @patch('core_processor.emby.get_emby_item_details')
    @patch('core_processor.emby.get_people_by_provider_ids', return_value=[])
    def test_new_person_uses_unique_temporary_name_then_locks_douban_id(
        self,
        _get_people,
        get_details,
        update_item,
        update_person,
        upload_avatar,
    ):
        processor = MediaProcessor.__new__(MediaProcessor)
        processor.emby_url = 'http://emby.invalid'
        processor.emby_api_key = 'hidden'
        processor.emby_user_id = 'u1'
        processor.actor_db_manager = MagicMock()
        processor.actor_db_manager.find_person_by_any_id.return_value = None
        processor.actor_db_manager.upsert_person.return_value = (10, 'UPDATED')
        cursor = MagicMock()
        actor = {
            'id': None,
            'douban_id': '9012',
            'name': '新演员',
            'character': '角色乙',
            'profile_path': 'https://img1.doubanio.com/p9012.jpg',
        }
        temp_person = {
            'Id': 'person-2',
            'Name': '__ETK_DOUBAN_9012__',
            'ProviderIds': {},
            'ImageTags': {},
        }
        verified_person = {
            'Id': 'person-2',
            'Name': '新演员',
            'ProviderIds': {'Douban': '9012'},
            'ImageTags': {},
        }
        get_details.side_effect = [
            {'Id': 'media-2', 'People': []},
            {'Id': 'media-2', 'People': [temp_person]},
            temp_person,
            verified_person,
            {'Id': 'media-2', 'People': [temp_person.copy()]},
            {'Id': 'media-2', 'People': [{'Id': 'person-2', 'Type': 'Actor'}]},
        ]

        synced = processor._sync_douban_only_cast_to_emby(
            'media-2', [actor], cursor
        )

        self.assertEqual(synced, 1)
        self.assertEqual(actor['emby_person_id'], 'person-2')
        self.assertEqual(update_item.call_count, 2)
        update_person.assert_called_once()
        person_update = update_person.call_args.args[1]
        self.assertEqual(person_update['Name'], '新演员')
        self.assertEqual(person_update['ProviderIds']['Douban'], '9012')
        upload_avatar.assert_called_once()

    @patch('core_processor.time_module.sleep')
    @patch('core_processor.emby.update_emby_item_details', return_value=True)
    @patch('core_processor.emby.get_emby_item_details')
    @patch('core_processor.emby.get_people_by_provider_ids', return_value=[])
    def test_unconfirmed_temporary_person_is_detached(
        self,
        _get_people,
        get_details,
        update_item,
        _sleep,
    ):
        processor = MediaProcessor.__new__(MediaProcessor)
        processor.emby_url = 'http://emby.invalid'
        processor.emby_api_key = 'hidden'
        processor.emby_user_id = 'u1'
        processor.actor_db_manager = MagicMock()
        processor.actor_db_manager.find_person_by_any_id.return_value = None
        cursor = MagicMock()
        actor = {
            'id': None,
            'douban_id': '3456',
            'name': '待确认演员',
            'character': '角色丙',
            'profile_path': 'https://img1.doubanio.com/p3456.jpg',
        }
        temporary_person_without_id = {
            'Name': '__ETK_DOUBAN_3456__',
            'Type': 'Actor',
        }
        get_details.side_effect = [
            {'Id': 'media-3', 'People': []},
            {'Id': 'media-3', 'People': [temporary_person_without_id]},
            {'Id': 'media-3', 'People': [temporary_person_without_id]},
            {'Id': 'media-3', 'People': [temporary_person_without_id]},
            {'Id': 'media-3', 'People': [temporary_person_without_id]},
        ]

        synced = processor._sync_douban_only_cast_to_emby(
            'media-3', [actor], cursor
        )

        self.assertEqual(synced, 0)
        self.assertEqual(update_item.call_count, 2)
        self.assertEqual(update_item.call_args.args[1]['People'], [])


if __name__ == '__main__':
    unittest.main()
