"""Opt-in real Mac Emby / disposable PG test. Never use a production endpoint.

Emby transport and EVH Person pipeline are real. TMDb data is a deterministic
8-main / 36-guest fixture; neither TMDb nor Douban internet is used.
No DELETE, Person create, merge or cleanup endpoint is permitted.
"""
import argparse
import copy
import json
import logging
import os
from pathlib import Path
import shutil
import sys
import time
import xml.etree.ElementTree as ET
from unittest.mock import patch

import requests
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

BASE = 'http://evh-lab-emby:8096'
FIXTURE_NAME = 'EVH Governance Series A'
LIBRARY_NAME = 'EVH Person Governance Fixture'
MEDIA_ID = '910000001'


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--setup', action='store_true')
    args = parser.parse_args()
    assert os.environ.get('EVH_GOVERNANCE_MAC_LAB') == '1'
    state = Path('/work')
    state.mkdir(exist_ok=True)
    secrets = json.loads(Path('/run/lab-secrets.json').read_text())
    session = requests.Session()
    session.trust_env = False
    session.headers.update({'X-Emby-Token': secrets['emby_api_key']})
    user = secrets['emby_user_id']
    calls = []

    def request(method, path, params=None, body=None):
        assert method in {'GET', 'POST'}
        assert 'Delete' not in path and 'cleanup' not in path
        assert path.startswith(('/Items', '/Users/', '/Library/VirtualFolders', '/ScheduledTasks'))
        calls.append((method, path.split('?')[0]))
        response = session.request(method, BASE + path, params=params, json=body,
                                   timeout=30, allow_redirects=False)
        if response.status_code not in {200, 204}:
            raise RuntimeError(f'lab HTTP status={response.status_code} path={path}')
        return response.json() if response.content else None

    def items(types, **kwargs):
        result, expected = [], None
        while expected is None or len(result) < expected:
            data = request('GET', '/Items', dict(
                IncludeItemTypes=types, Recursive='true', Fields='People,ProviderIds,DateCreated',
                StartIndex=len(result), Limit=500, EnableTotalRecordCount='true', **kwargs,
            ))
            count = data.get('TotalRecordCount')
            assert type(count) is int and count >= 0
            if expected is None:
                expected = count
            assert expected == count
            assert data['Items'] or len(result) == expected
            result.extend(data['Items'])
        assert len({i['Id'] for i in result}) == expected
        return result

    def snapshot():
        persons = items('Person')
        media = items('Movie,Series,Season,Episode,Video,MusicVideo,Audio,MusicAlbum')
        referenced = {str(p['Id']) for m in media for p in m.get('People', [])
                      if isinstance(p, dict) and p.get('Id')}
        identities = {}
        for person in persons:
            for provider, value in (person.get('ProviderIds') or {}).items():
                identities.setdefault((provider.lower(), str(value)), []).append(person['Id'])
        return {
            'person_total': len(persons),
            'person_ids': sorted(p['Id'] for p in persons),
            'orphan_ids': sorted(p['Id'] for p in persons if p['Id'] not in referenced),
            'provider_duplicates': sorted(
                [provider, value, sorted(ids)] for (provider, value), ids in identities.items() if len(ids) > 1
            ),
        }

    def settle():
        deadline = time.monotonic() + 120
        while time.monotonic() < deadline:
            tasks = request('GET', '/ScheduledTasks')
            if all(t.get('State') not in {'Running', 'Cancelling'} for t in tasks):
                time.sleep(1)
                return
            time.sleep(1)
        raise RuntimeError('Mac lab task timeout')

    manifest_file = state / 'fixture.json'
    if args.setup:
        assert not manifest_file.exists(), 'fixture already exists; do not recreate'
        initial = snapshot()
        persons = items('Person')
        names, identities, chosen = set(), set(), []
        for p in sorted(persons, key=lambda p: int(p['Id'])):
            tmdb_id = (p.get('ProviderIds') or {}).get('Tmdb')
            if not tmdb_id or p['Name'] in names or tmdb_id in identities:
                continue
            names.add(p['Name'])
            identities.add(tmdb_id)
            chosen.append(p)
            if len(chosen) == 44:
                break
        assert len(chosen) == 44, 'need existing exact identities; never create fixture Persons'
        root = Path('/fixture-media/EVH Governance Series A (2099)')
        if root.exists():
            previous_nfo = ET.parse(root / 'tvshow.nfo').getroot()
            assert previous_nfo.findtext('title') == FIXTURE_NAME
            assert previous_nfo.findtext('uniqueid') == MEDIA_ID
        season = root / 'Season 1'
        season.mkdir(parents=True, exist_ok=True)
        shutil.copyfile('/sample/VidHub普通电影样本.mp4', season / 'Governance S01E01.mp4')

        def nfo(kind, path, selected):
            node = ET.Element(kind)
            ET.SubElement(node, 'title').text = FIXTURE_NAME if kind == 'tvshow' else 'Governance E01'
            ET.SubElement(node, 'year').text = '2099'
            ET.SubElement(node, 'plot').text = 'Local synthetic EVH Person governance test'
            ET.SubElement(node, 'uniqueid', type='tmdb', default='true').text = (
                MEDIA_ID if kind == 'tvshow' else '910000111')
            if kind != 'tvshow':
                ET.SubElement(node, 'season').text = '1'
                ET.SubElement(node, 'episode').text = '1'
            for index, p in enumerate(selected):
                actor = ET.SubElement(node, 'actor')
                ET.SubElement(actor, 'name').text = p['Name']
                ET.SubElement(actor, 'role').text = 'Main' if index < 8 else 'Guest'
                ET.SubElement(actor, 'order').text = str(index)
                ET.SubElement(actor, 'tmdbid').text = p['ProviderIds']['Tmdb']
            ET.ElementTree(node).write(path, encoding='utf-8', xml_declaration=True)

        nfo('tvshow', root / 'tvshow.nfo', chosen[:8])
        nfo('episodedetails', season / 'Governance S01E01.nfo', chosen)
        libraries = request('GET', '/Library/VirtualFolders')
        existing = [lib for lib in libraries if lib.get('Name') == LIBRARY_NAME]
        assert len(existing) <= 1
        if existing:
            assert existing[0].get('Locations') == ['/STRM/PersonGovernancePhase2']
        else:
            request('POST', '/Library/VirtualFolders', body={
            'Name': LIBRARY_NAME, 'CollectionType': 'tvshows',
            'Paths': ['/STRM/PersonGovernancePhase2'], 'RefreshLibrary': False,
            'LibraryOptions': {
                'EnableRealtimeMonitor': False, 'EnableInternetProviders': False,
                'TypeOptions': [{'Type': t, 'MetadataFetchers': [], 'ImageFetchers': []}
                                for t in ('Series', 'Season', 'Episode')],
            },
            })
        libraries = request('GET', '/Library/VirtualFolders')
        library = next(lib for lib in libraries if lib.get('Name') == LIBRARY_NAME)
        request('POST', f"/Items/{library['ItemId']}/Refresh", params={
            'Recursive': 'true', 'MetadataRefreshMode': 'FullRefresh',
            'ImageRefreshMode': 'Default', 'ReplaceAllMetadata': 'false',
        })
        settle()
        deadline = time.monotonic() + 60
        while True:
            series = items('Series', ParentId=library['ItemId'])
            children = items('Episode', ParentId=series[0]['Id']) if len(series) == 1 else []
            if len(series) == 1 and len(children) == 1:
                break
            assert time.monotonic() < deadline, 'fixture discovery timed out'
            time.sleep(2)
        assert series[0]['Name'] == FIXTURE_NAME
        after = snapshot()
        assert initial['person_ids'] == after['person_ids'], 'native fixture must reuse exact existing Persons'
        manifest = {'series_id': series[0]['Id'], 'episode_id': children[0]['Id'],
                    'library_id': library['ItemId'], 'main': chosen[:8], 'guests': chosen[8:],
                    'initial_snapshot': initial}
        manifest_file.write_text(json.dumps(manifest, ensure_ascii=False, indent=2))
        print(json.dumps({'setup': 'PASS', 'series_id': manifest['series_id'],
                          'episode_id': manifest['episode_id'], 'person_total': after['person_total']}))
        return

    manifest = json.loads(manifest_file.read_text())
    assert os.environ['EVH_TEST_POSTGRES_HOST'] == 'evh-person-governance-phase2-pg'
    import config_manager
    import constants
    from database.connection import init_db, get_db_connection
    from core_processor import MediaProcessor
    import handler.emby as emby

    logging.Logger.trace = logging.Logger.debug
    config = dict(
        emby_server_url=BASE, emby_api_key=secrets['emby_api_key'], emby_user_id=user,
        tmdb_api_key='deterministic-local-fixture', local_data_path='/STRM/HC',
        max_actors_to_process=50, remove_actors_without_avatars=False,
        ai_translate_actor_role=False, backup_episode_image=False,
        db_host='evh-person-governance-phase2-pg', db_port=5432,
        db_user='evh_governance_test', db_password='isolated_fixture_only',
        db_name='evh_governance_test',
    )
    config_manager.APP_CONFIG.update(config)
    init_db()
    processor = MediaProcessor(config)
    emby.emby_client.session.trust_env = False

    def actor(p, order):
        return {'id': int(p['ProviderIds']['Tmdb']), 'name': p['Name'], 'order': order,
                'character': 'Main' if order < 8 else 'Guest', 'profile_path': '/fixture.jpg'}

    main_cast = [actor(p, i) for i, p in enumerate(manifest['main'])]
    guests = [actor(p, i + 8) for i, p in enumerate(manifest['guests'])]
    episode = {'id': 910000111, 'name': 'Governance E01', 'season_number': 1,
               'episode_number': 1, 'air_date': '2099-01-01',
               'credits': {'cast': main_cast, 'guest_stars': guests, 'crew': []}}
    series = {'id': int(MEDIA_ID), 'name': FIXTURE_NAME, 'first_air_date': '2099-01-01',
              'last_air_date': '2099-01-01',
              'overview': 'Local deterministic Person pipeline fixture',
              'credits': {'cast': main_cast, 'crew': []},
              'genres': [], 'networks': [], 'production_companies': [],
              'seasons': [{'id': 910000101, 'season_number': 1, 'name': 'Season 1'}]}
    aggregate = {'series_details': series, 'episodes_details': {'S1E1': episode},
                 'seasons_details': [{'id': 910000101, 'season_number': 1, 'name': 'Season 1'}]}
    before = snapshot()
    runs = []
    actual_requests = []
    original_request = emby.emby_client.session.request

    def recorded(method, url, *args, **kwargs):
        assert url.startswith(BASE + '/'), 'only the Mac lab Emby endpoint is allowed'
        assert method.upper() in {'GET', 'POST'}, 'Person deletion is prohibited'
        actual_requests.append((method.upper(), url[len(BASE):].split('?')[0]))
        return original_request(method, url, *args, **kwargs)

    for iteration in (1, 2):
        source = request('GET', f"/Users/{user}/Items/{manifest['series_id']}")
        assert source['Name'] == FIXTURE_NAME
        assert len([p for p in source.get('People', []) if p.get('Type') == 'Actor']) == 8
        untouched = copy.deepcopy(episode)
        with patch('core_processor.tmdb.aggregate_full_series_data_from_tmdb',
                   return_value=copy.deepcopy(aggregate)), \
             patch('core_processor.tmdb.get_tv_details', return_value=copy.deepcopy(series)), \
             patch.object(processor, '_get_douban_data_with_local_cache', return_value=([], None)), \
             patch.object(processor, 'sync_item_images', return_value=True), \
             patch.object(emby.emby_client.session, 'request', side_effect=recorded):
            passed = processor._process_item_core_logic(source, force_full_update=True,
                                                       specific_episode_ids=[manifest['episode_id']])
        assert passed, 'EVH Person pipeline failed'
        settle()
        after_series = request('GET', f"/Users/{user}/Items/{manifest['series_id']}")
        after_episode = request('GET', f"/Users/{user}/Items/{manifest['episode_id']}")
        assert {p['Id'] for p in after_series['People'] if p.get('Type') == 'Actor'} == {
            p['Id'] for p in manifest['main']}
        assert {p['Id'] for p in after_episode['People'] if p.get('Type') == 'Actor'} == {
            p['Id'] for p in manifest['main'] + manifest['guests']}
        cache = Path('/STRM/HC/override/tmdb-tv') / MEDIA_ID
        parent_cache = json.loads((cache / 'series.json').read_text())
        child_cache = json.loads((cache / 'season-1-episode-1.json').read_text())
        parent_cast = (parent_cache.get('casts') or parent_cache.get('credits'))['cast']
        assert len(parent_cast) == 8
        assert len(child_cache['credits']['guest_stars']) == 36
        assert episode == untouched
        with patch('core_processor.tmdb.get_tv_details', return_value=copy.deepcopy(series)):
            editor = processor.get_cast_for_editing(manifest['series_id'])
        assert editor and len(editor['current_emby_cast']) == 8
        assert all(a.get('emby_person_id') and a.get('provider_identity_snapshot')
                   for a in editor['current_emby_cast'])
        current = snapshot()
        assert current['person_ids'] == before['person_ids']
        assert current['orphan_ids'] == before['orphan_ids']
        assert current['provider_duplicates'] == before['provider_duplicates']
        with get_db_connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute('SELECT tmdb_person_id,emby_person_id FROM person_identity_map ORDER BY map_id')
                mappings = [dict(row) for row in cursor.fetchall()]
        runs.append({'iteration': iteration, 'person_total': current['person_total'],
                     'series_people': len(after_series['People']),
                     'episode_people': len(after_episode['People']), 'parent_targets': len(parent_cast),
                     'episode_guest_count': 36, 'provider_mapping_count': len(mappings),
                     'new_orphan_count': 0, 'provider_duplicates_changed': False})
        runs[-1]['manual_editor_frozen_targets'] = 8
        print(json.dumps(runs[-1]), flush=True)
    report = {'scope': 'Mac single Emby lab and disposable PostgreSQL',
              'provider_source': 'deterministic fixture, not live TMDb',
              'real_emby_transport': True, 'real_postgres': True, 'images_tested': False,
              'full_evh_person_pipeline': True, 'before': before, 'runs': runs,
              'emby_request_count': len(actual_requests), 'DeletePerson': 0, 'DELETE': 0,
              'NAS_requests': 0, 'POST_person_count': sum(
                  method == 'POST' and path.startswith('/Items/')
                  and path.count('/') == 2 for method, path in actual_requests)}
    (state / 'pipeline-results.json').write_text(json.dumps(report, ensure_ascii=False, indent=2))
    print(json.dumps({'status': 'PERSON_PIPELINE_LAB_PASS', 'runs': runs}, ensure_ascii=False))


if __name__ == '__main__':
    main()
