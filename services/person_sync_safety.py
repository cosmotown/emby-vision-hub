"""Completeness evidence and conservative identity comparison; no I/O."""
from dataclasses import dataclass, field
from enum import Enum
import re


class PersonResultScope(str, Enum):
    FULL_SNAPSHOT = 'full_snapshot'
    PARTIAL_PAGE = 'partial_page'
    FILTERED_RESULT = 'filtered_result'
    INCREMENTAL_RESULT = 'incremental_result'
    UNKNOWN = 'unknown_completeness'


@dataclass
class PersonEnumeration:
    scope: PersonResultScope = PersonResultScope.UNKNOWN
    expected_count: int | None = None
    seen_ids: set = field(default_factory=set)
    complete: bool = False
    error: str | None = None

    def fail(self, reason):
        self.complete = False
        self.error = reason

    def begin_full(self, count):
        self.scope = PersonResultScope.PARTIAL_PAGE
        self.complete = False
        self.seen_ids.clear()
        self.expected_count = count if type(count) is int and count >= 0 else None
        self.error = None if self.expected_count is not None else 'missing_total'

    def record_page(self, data):
        if not isinstance(data, dict) or not isinstance(data.get('Items'), list):
            self.fail('invalid_page')
            return False
        count = data.get('TotalRecordCount')
        if type(count) is not int or count != self.expected_count:
            self.fail('total_changed_or_missing')
        ids = []
        for person in data['Items']:
            if (not isinstance(person, dict) or not person.get('Id')
                    or not person.get('Name') or person.get('Type', 'Person') != 'Person'):
                self.fail('invalid_person')
                return False
            ids.append(str(person['Id']))
        if len(set(ids)) != len(ids) or self.seen_ids.intersection(ids):
            self.fail('duplicate_person')
            return False
        self.seen_ids.update(ids)
        if self.expected_count is not None and len(self.seen_ids) > self.expected_count:
            self.fail('count_exceeded')
        return True

    def finish_full(self, final_count):
        if (self.error or type(final_count) is not int
                or self.expected_count != final_count
                or len(self.seen_ids) != final_count):
            self.fail(self.error or 'incomplete_snapshot')
            return
        self.scope = PersonResultScope.FULL_SNAPSHOT
        self.complete = True


IDENTITY_FIELDS = ('tmdb_person_id', 'douban_celebrity_id', 'imdb_id', 'emby_person_id')


def classify_identity_match(left, right):
    """SAFE_EXACT_MATCH is evidence, never permission to merge/delete rows.

    Names never establish identity. Any conflicting populated ID overrides an
    exact match on another provider. Empty/invalid stable IDs require review.
    """
    shared_stable_id = False
    for key in IDENTITY_FIELDS:
        a = str(left.get(key) or '').strip()
        b = str(right.get(key) or '').strip()
        if a and b and a != b:
            return 'CONFLICT'
        if key in ('tmdb_person_id', 'douban_celebrity_id'):
            for value in (a, b):
                if value and not re.fullmatch(r'[1-9][0-9]*', value):
                    return 'REVIEW_REQUIRED'
            shared_stable_id |= bool(a and a == b)
    return 'SAFE_EXACT_MATCH' if shared_stable_id else 'REVIEW_REQUIRED'
