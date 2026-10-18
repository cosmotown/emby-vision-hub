"""Optional Person failure boundary; never converts media/DB errors to success."""
from functools import wraps
import hashlib
import json
import os
import tempfile
from datetime import datetime, timezone

from services.person_write_safety import governance_root


# Used ONLY at Person preparation/revalidation call sites, not around media work.
OPTIONAL_PERSON_DENIALS = frozenset({
    'person_name_collision_or_unknown', 'metadata_name_collision',
    'metadata_display_drift', 'identity_missing', 'identity_ambiguous',
    'identity_conflict', 'identity_changed', 'identity_unavailable',
    'stale_mapping', 'stale_emby_mapping', 'provider_not_found',
    'person_not_found', 'unsupported_existing_provider_relation',
    'legacy_relation_set_exceeds_processing_cap',
    'source_people_identity_batch_incomplete',
    'source_people_unavailable',
})


class PersonEnrichmentHold(ValueError):
    def __init__(self, reason, source_id='', person_id=''):
        if reason not in OPTIONAL_PERSON_DENIALS:
            raise ValueError('not_an_optional_person_denial')
        super().__init__(reason)
        self.reason = reason
        self.source_id = str(source_id or '')
        self.person_id = str(person_id or '')


def optional_person_gate(function):
    """Tag known identity denials at their origin; unknown exceptions escape."""
    @wraps(function)
    def guarded(*args, **kwargs):
        try:
            return function(*args, **kwargs)
        except PersonEnrichmentHold:
            raise
        except ValueError as error:
            if str(error) not in OPTIONAL_PERSON_DENIALS:
                raise
            raise PersonEnrichmentHold(str(error)) from error
    return guarded


def persist_person_hold(server, source, hold):
    """One bounded atomic warning per media/reason/Person; no raw DTO/secrets.

    Persistence errors deliberately propagate. A warning is NOT a claim that
    media processing succeeded; the webhook queue records that independently.
    """
    source_id = str(hold.source_id or source.get('Id') or '')
    if not source_id:
        raise ValueError('source_item_missing')
    root = governance_root() / 'media-holds'
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    key = hashlib.sha256(json.dumps(
        [server.rstrip('/'), source_id, hold.reason, hold.person_id],
    ).encode()).hexdigest()
    record = dict(source_item_id=source_id, source_item_type=source.get('Type'),
                  person_id=hold.person_id or None, reason=hold.reason,
                  person_enrichment='HOLD', review_required=True,
                  media_processing='NOT_ASSERTED',
                  updated_at=datetime.now(timezone.utc).isoformat())
    fd, temporary = tempfile.mkstemp(dir=root, prefix='.hold-')
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as handle:
            json.dump(record, handle, ensure_ascii=False)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, root / (key + '.json'))
        directory = os.open(root, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    return record
