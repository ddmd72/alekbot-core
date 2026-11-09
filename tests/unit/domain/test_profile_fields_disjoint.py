"""The profile step writes only its own fields (fact profile RFC §2.1, §2.2): PROFILE_FIELDS are flat FactEntity fields,
disjoint from the in-place lifecycle fields and from everything a fact's other writers own."""
import pytest

from src.domain.entities import FactEntity
from src.domain.exceptions import InPlaceFieldRefused
from src.domain.fact_lifecycle import IN_PLACE_FIELDS
from src.domain.fact_profile import PROFILE_FIELDS, require_profile_fields
from src.domain.fact_profile_fingerprint import failure_fields

# RFC §2.2: kept by their own writers, never written by the profiler.
OWNED_ELSEWHERE = {
    "id", "account_id", "created_by_user_id", "lineage_id", "text", "metadata", "vector", "tags", "tags_vector",
    "metadata_vector", "type", "temporal_class", "domain", "state", "context_priority", "ttl_days", "last_updated",
    "expires_at", "expiry_kind", "stale_since", "lifecycle_events", "version", "replaces_fact_id", "superseded_by",
    "context", "reported_date", "created_at", "valid_from", "valid_to", "is_current", "visibility",
}


def test_every_profile_field_is_an_optional_fact_field_defaulting_to_none():
    fields = FactEntity.model_fields
    assert PROFILE_FIELDS <= set(fields)
    assert all(fields[name].default is None for name in PROFILE_FIELDS)


def test_profile_fields_are_disjoint_from_the_in_place_fields():
    assert not PROFILE_FIELDS & IN_PLACE_FIELDS


def test_profile_fields_are_disjoint_from_the_fields_other_writers_own():
    assert not PROFILE_FIELDS & OWNED_ELSEWHERE
    assert set(FactEntity.model_fields) == PROFILE_FIELDS | OWNED_ELSEWHERE


def test_the_failure_write_names_only_profile_fields():
    assert set(failure_fields(None, None, "fp")) <= PROFILE_FIELDS


def test_a_profile_write_naming_another_field_is_refused():
    require_profile_fields({"primary_domain": "travel", "profile_fingerprint": "fp"})
    with pytest.raises(InPlaceFieldRefused) as caught:
        require_profile_fields({"primary_domain": "travel", "text": "x", "state": "current"})
    assert caught.value.fields == ("state", "text")
