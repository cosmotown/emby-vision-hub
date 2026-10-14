"""Exact identity intents. Names are never write authority.

This module does not delete or merge people. A successful read is not an atomic
server-side compare-and-swap; callers must still hold the cooperative EVH lock.
"""
from dataclasses import dataclass
import json
import logging
import re
import uuid
import copy
import hashlib
import os
import tempfile
import fcntl
import threading
from pathlib import Path
from contextlib import contextmanager
from datetime import datetime, timezone
_held_identity_locks = threading.local()


def governance_root():
    import config_manager
    return Path(os.environ.get('APP_DATA_DIR') or config_manager.PERSISTENT_DATA_PATH) / 'person-governance'


def identity_key(server, provider, identity):
    pair = provider_identity(provider, identity)
    return hashlib.sha256(json.dumps([server.rstrip('/'), *pair]).encode()).hexdigest()


@contextmanager
def identity_lock(server, provider, identity):
    """Cross-process cooperative lock on the EVH persistent volume, not Name."""
    root = governance_root() / 'locks'
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    path = root / (identity_key(server, provider, identity) + '.lock')
    held = getattr(_held_identity_locks, 'keys', None)
    if held is None:
        held = _held_identity_locks.keys = {}
    key = str(path)
    if held.get(key) == os.getpid():
        yield
        return
    with path.open('a', encoding='utf-8') as handle:
        os.chmod(path, 0o600)
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        held[key] = os.getpid()
        try:
            yield
        finally:
            held.pop(key, None)
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


class PersonOperationJournal:
    """One identity's durable quarantine. Caller MUST hold identity_lock.

    No raw DTO or credentials are persisted. An interrupted reserved operation
    is deliberately NOT replayed. No automatic detach or delete compensation.
    """
    def __init__(self, server, provider, identity):
        self.provider, self.identity = provider_identity(provider, identity)
        self.root = governance_root() / 'operations'
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.path = self.root / (identity_key(server, provider, identity) + '.json')
        self._new_operation_id = str(uuid.uuid4())

    @property
    def operation_id(self):
        return (self.read() or {}).get('operation_id') or self._new_operation_id

    def read(self):
        if not self.path.exists():
            return None
        # Corrupt/unreadable state fails closed; it never becomes "not found".
        with self.path.open(encoding='utf-8') as handle:
            record = json.load(handle)
        if not isinstance(record, dict) or record.get('provider_person_id') != self.identity:
            raise ValueError('operation_state_invalid')
        return record

    def record(self, source_item_id, stage, person_id=None, operation_id=None, *, created=False):
        if not source_item_id:
            raise ValueError('source_item_missing')
        previous = self.read() or {}
        record = dict(
            operation_id=operation_id or previous.get('operation_id') or self._new_operation_id,
            created_person_id=(person_id if created else previous.get('created_person_id')),
            target_person_id=person_id or previous.get('target_person_id'),
            provider_name=self.provider, provider_person_id=self.identity,
            source_item_id=str(source_item_id), failure_stage=stage,
            created_at=previous.get('created_at') or datetime.now(timezone.utc).isoformat(),
            updated_at=datetime.now(timezone.utc).isoformat(),
        )
        fd, temporary = tempfile.mkstemp(dir=self.root, prefix='.operation-')
        try:
            with os.fdopen(fd, 'w', encoding='utf-8') as handle:
                json.dump(record, handle, ensure_ascii=False)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self.path)
            directory = os.open(self.root, os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
        return record

logger = logging.getLogger(__name__)
PROVIDERS = ('tmdb', 'douban', 'imdb')


def provider_identity(provider, value):
    provider = str(provider or '').strip().lower()
    value = str(value or '').strip()
    pattern = r'nm[0-9]+' if provider == 'imdb' else r'[1-9][0-9]*'
    if provider not in PROVIDERS or not re.fullmatch(pattern, value):
        raise ValueError('identity_missing')
    return provider, value


def provider_snapshot(person):
    values = person.get('ProviderIds')
    if not isinstance(values, dict):
        raise ValueError('identity_missing')
    result = {}
    for key, value in values.items():
        if str(key).lower() not in PROVIDERS:
            continue
        provider, identity = provider_identity(key, value)
        if provider in result and result[provider] != identity:
            raise ValueError('identity_ambiguous')
        result[provider] = identity
    if not result:
        raise ValueError('identity_missing')
    return tuple(sorted(result.items()))


@dataclass(frozen=True)
class ExactPersonTarget:
    emby_person_id: str
    provider_name: str
    provider_person_id: str
    source_item_id: str
    source_item_type: str
    display_name: str
    provider_ids: tuple
    operation_id: str

    @classmethod
    def for_intent(cls, provider, identity, source_item_id, source_item_type, display_name, operation_id):
        pair = provider_identity(provider, identity)
        return cls('', *pair, str(source_item_id), str(source_item_type),
                   str(display_name or ''), (pair,), operation_id)

    @classmethod
    def from_person(cls, person, source_item_id, source_item_type, operation_id=None):
        if not source_item_id or source_item_id == 'pending' or not source_item_type:
            raise ValueError('source_item_missing')
        if not isinstance(person, dict) or person.get('Type') != 'Person' or not person.get('Id'):
            raise ValueError('person_not_found')
        snapshot = provider_snapshot(person)
        preferred = next((pair for name in PROVIDERS for pair in snapshot if pair[0] == name), None)
        return cls(str(person['Id']), *preferred, str(source_item_id), str(source_item_type),
                   str(person.get('Name') or ''), snapshot, operation_id or str(uuid.uuid4()))

    @classmethod
    def unresolved(cls, person, source_item_id, source_item_type):
        """Diagnostic context only; an empty provider set can never authorize a write."""
        return cls(str(person.get('Id') or ''), '', '', str(source_item_id),
                   str(source_item_type), str(person.get('Name') or ''), (), str(uuid.uuid4()))


def audit(target, action, decision, reason):
    # Explicit fields only: no raw response, tokens, URLs or headers.
    record = {key: getattr(target, key, '') for key in (
        'operation_id', 'source_item_id', 'source_item_type', 'emby_person_id',
        'provider_name', 'provider_person_id', 'display_name')}
    record.update(action=action, decision=decision, reason=reason)
    logger.info('[PERSON_GOVERNANCE] %s', json.dumps(record, ensure_ascii=False))


def validate_person_write_target(target, current, exact_matches, new_data=None):
    """Return an explicit denial code, or None. Unknown identity never passes."""
    if not isinstance(target, ExactPersonTarget):
        return 'identity_missing'
    if not target.emby_person_id or not target.provider_name or not target.provider_person_id or not target.provider_ids:
        return 'identity_missing'
    if not isinstance(current, dict) or current.get('Type') != 'Person':
        return 'person_not_found'
    if str(current.get('Id') or '') != target.emby_person_id:
        return 'stale_emby_mapping'
    try:
        if provider_snapshot(current) != target.provider_ids:
            return 'identity_changed'
        if exact_matches is None:
            return 'identity_unavailable'
        if len(exact_matches) != 1:
            return 'identity_ambiguous' if exact_matches else 'stale_emby_mapping'
        match = exact_matches[0]
        if str(match.get('Id') or '') != target.emby_person_id:
            return 'stale_emby_mapping'
        if provider_snapshot(match) != target.provider_ids:
            return 'identity_changed'
        if new_data and 'ProviderIds' in new_data:
            if provider_snapshot(new_data) != target.provider_ids:
                return 'provider_mutation_unproven'
            supplied = {str(k).lower(): str(v) for k, v in new_data['ProviderIds'].items()}
            existing = {str(k).lower(): str(v) for k, v in current['ProviderIds'].items()}
            if supplied != existing:
                return 'provider_mutation_unproven'
        if new_data and any(k in new_data for k in ('Id', 'Type')):
            return 'identity_field_mutation'
    except (ValueError, TypeError, AttributeError):
        return 'identity_missing'
    return None


def cast_identity(actor):
    for provider, key in (('tmdb', 'id'), ('douban', 'douban_id')):
        if actor.get(key):
            try:
                return provider_identity(provider, actor[key])
            except ValueError:
                return None
    return None


def actor_cap(value):
    try:
        return max(1, int(value)) if int(value) > 0 else 50
    except (ValueError, TypeError):
        return 50


def select_person_candidates(actors, limit, direct_provider_ids=()):
    """Scope must be decided by caller. Deterministic dedupe/cap before I/O."""
    direct = set(map(str, direct_provider_ids))
    def key(actor):
        identity = cast_identity(actor)
        order = actor.get('order', 999)
        if not isinstance(order, (int, float)) or isinstance(order, bool) or not (-1e12 < order < 1e12):
            order = 999
        return (0 if actor.get('emby_person_id') or str(actor.get('id')) in direct else 1,
                order, identity or ('', ''),
                json.dumps(actor, sort_keys=True, ensure_ascii=True, default=str))
    result, seen = [], set()
    for actor in sorted((a for a in actors if isinstance(a, dict) and cast_identity(a)), key=key):
        identity = cast_identity(actor)
        if identity in seen:
            continue
        seen.add(identity)
        result.append(dict(actor))
        if len(result) >= actor_cap(limit):
            break
    return result


def series_main_cast(series_data):
    credits = series_data.get('credits')
    if not isinstance(credits, dict) or not isinstance(credits.get('cast'), list):
        return []
    cast = credits.get('cast') if isinstance(credits, dict) else None
    return select_person_candidates(cast or [], max(1, len(cast or [])))


def restrict_series_cached_cast(cached_cast, series_data):
    """Old aggregate caches are not authority for the Series target set."""
    credits = series_data.get('credits')
    if not isinstance(credits, dict) or not isinstance(credits.get('cast'), list):
        raise ValueError('series_main_scope_unavailable')
    allowed = {cast_identity(a) for a in series_main_cast(series_data)}
    return [dict(a) for a in cached_cast if cast_identity(a) in allowed]


def cap_episode_credits(credits, limit):
    """Keep cast/guest membership, copy before translation, cap their union."""
    result = copy.deepcopy(credits)
    cast = result.get('cast') or []
    guests = result.get('guest_stars') or []
    selected = select_person_candidates(cast + guests, limit)
    identities = {cast_identity(a) for a in selected}
    seen = set()
    for group in ('cast', 'guest_stars'):
        output = []
        for actor in select_person_candidates(result.get(group) or [], max(1, len(cast + guests))):
            identity = cast_identity(actor)
            if identity in identities and identity not in seen:
                output.append(actor)
                seen.add(identity)
        result[group] = output
    return result


def manual_person_target(actor, source_item_id, source_item_type):
    """Use the UI's frozen object/provider pair, never a newly enriched DB map."""
    person = {'Type': 'Person', 'Id': actor.get('emby_person_id'),
              'Name': actor.get('name'), 'ProviderIds': actor.get('provider_identity_snapshot')}
    target = ExactPersonTarget.from_person(person, source_item_id, source_item_type)
    if ('tmdb', str(actor.get('tmdbId') or '')) not in target.provider_ids:
        raise ValueError('identity_changed')
    return target
