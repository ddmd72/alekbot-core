"""parse_new_fact: a fact dict is validated before any embedding or write."""

from datetime import datetime, timezone

import pytest

from src.domain.entities import ContextPriority, FactState, FactType, TemporalClass
from src.domain.new_fact_attributes import NewFactAttributes, parse_new_fact


def refused(data, *fragments):
    with pytest.raises(ValueError) as caught:
        parse_new_fact(data)
    for fragment in fragments:
        assert fragment in str(caught.value)


def test_defaults():
    # Reviewer edit (facts S3, T2): `ttl_days` left the attributes (D1: code computes it); the S3 fields default empty.
    # Reviewer edit (fact profile PR 13): the old `domain` became the `directive` flag; missing reads as not a directive.
    assert parse_new_fact({"text": "likes tea"}) == NewFactAttributes(
        content="likes tea", tags=[], type=FactType.EVENT, metadata={}, directive=False, temporal_class=None,
        state=FactState.CURRENT, context_priority=ContextPriority.MEDIUM, context=None,
        reported_date=None, expiry=None, derived_from=(), expiry_fields=None, notes=())


def test_content_wins_over_text():
    assert parse_new_fact({"content": "from content", "text": "from text"}).content == "from content"


def test_given_attributes_are_parsed():
    # Reviewer edit (facts S3, T2): the non-default state is ARCHIVED (the model may not send STALE, S3-D3), and a sent
    # `ttl_days` is not read (D1). Reviewer edit (fact profile PR 13): `directive` in place of the old `domain`.
    parsed = parse_new_fact({
        "content": "a fact", "tags": ["x"], "type": "state", "metadata": {"k": 1}, "directive": True,
        "temporal_class": "dynamic", "state": "archived", "context_priority": "high", "ttl_days": 30,
        "context": "said at lunch", "reported_date": "2026-03-04T10:00:00+00:00"})
    assert (parsed.directive, parsed.temporal_class, parsed.state, parsed.context_priority) == (
        True, TemporalClass.DYNAMIC, FactState.ARCHIVED, ContextPriority.HIGH)
    assert (parsed.type, parsed.tags, parsed.metadata, parsed.context) == (
        FactType.STATE, ["x"], {"k": 1}, "said at lunch")
    assert not hasattr(parsed, "ttl_days") and parsed.expiry is None and parsed.expiry_fields is None
    assert parsed.reported_date == datetime(2026, 3, 4, 10, 0, tzinfo=timezone.utc)


def test_a_datetime_reported_date_is_kept():
    given = datetime(2025, 1, 2, tzinfo=timezone.utc)
    assert parse_new_fact({"text": "a fact", "reported_date": given}).reported_date == given


@pytest.mark.parametrize("given,expected", [
    ("state", FactType.STATE), ("STATE", FactType.STATE), ("principle", FactType.PRINCIPLE),
    ("system", FactType.SYSTEM), ("alert", FactType.ALERT), ("event", FactType.EVENT), ("gadget", FactType.EVENT),
    (None, FactType.EVENT), ("", FactType.EVENT)])
def test_type_is_case_insensitive_and_an_unknown_type_is_an_event(given, expected):
    assert parse_new_fact({"text": "a fact", "type": given}).type is expected


def test_the_tags_list_is_the_callers_own_list():
    tags = ["a"]
    assert parse_new_fact({"text": "a fact", "tags": tags}).tags is tags


@pytest.mark.parametrize("state", ["superseded", "SUPERSEDED"])
def test_superseded_is_refused(state):
    refused({"text": "a fact", "state": state}, "cannot have state SUPERSEDED")


# Reviewer edit (facts S3, T2): STALE left the states a new fact may have (S3-D3); its case is the refusal below.
@pytest.mark.parametrize("state", ["current", "archived", "invalidated"])
def test_the_other_states_are_kept(state):
    assert parse_new_fact({"text": "a fact", "state": state}).state is FactState(state)


@pytest.mark.parametrize("state", ["stale", "STALE"])
def test_stale_is_refused(state):
    refused({"text": "a fact", "state": state}, "cannot have state STALE")


# Reviewer edit (fact profile PR 13): the `domain` case left (the key is unknown now, next test); its place as the
# refused classification value is taken by a non-bool `directive` (the test after it).
@pytest.mark.parametrize("bad", [{"temporal_class": "sometimes"}, {"state": "zombie"}, {"context_priority": "urgent"}])
def test_a_bad_enum_value_is_refused(bad):
    refused({"text": "a fact", **bad}, "use one of")


@pytest.mark.parametrize("domain", ["astrology", "agent_directive"])
def test_an_old_domain_is_not_read_so_not_refused(domain):
    parsed = parse_new_fact({"text": "a fact", "domain": domain})
    assert not hasattr(parsed, "domain") and parsed.directive is False


@pytest.mark.parametrize("bad", ["true", "yes", 1, 0, ["x"]])
def test_a_non_bool_directive_is_refused(bad):
    refused({"text": "a fact", "directive": bad}, "Invalid directive", "use true or false")


@pytest.mark.parametrize("given", [True, False])
def test_a_bool_directive_is_kept(given):
    assert parse_new_fact({"text": "a fact", "directive": given}).directive is given


@pytest.mark.parametrize("tags", [None, "family", ["a", 1], {"a": 1}])
def test_malformed_tags_are_refused(tags):
    refused({"text": "a fact", "tags": tags}, "tags must be a list of strings")


@pytest.mark.parametrize("data", [{}, {"text": ""}, {"content": "  "}, {"text": None}, {"text": 5}])
def test_missing_or_empty_content_is_refused(data):
    refused(data, "non-empty content")


def test_a_non_string_type_is_refused():
    refused({"text": "a fact", "type": 3}, "type must be a string")


@pytest.mark.parametrize("metadata", ["x", ["a"], None])
def test_a_non_dict_metadata_is_ignored(metadata):
    assert parse_new_fact({"text": "a fact", "metadata": metadata}).metadata == {}


def test_a_bad_date_is_refused():
    refused({"text": "a fact", "reported_date": "yesterday"}, "reported_date")


# Reviewer edit (facts S3, T2): the model's `ttl_days` is no longer read (D1), so a malformed one is not refused (next
# test); a malformed context still is.
@pytest.mark.parametrize("data,name", [({"context": 5}, "context")])
def test_a_malformed_ttl_or_context_is_refused(data, name):
    refused({"text": "a fact", **data}, f"Invalid {name}")


@pytest.mark.parametrize("ttl", ["soon", [1]])
def test_a_malformed_ttl_days_is_not_read_so_not_refused(ttl):
    parsed = parse_new_fact({"text": "a fact", "ttl_days": ttl})
    assert not hasattr(parsed, "ttl_days") and parsed.expiry is None
