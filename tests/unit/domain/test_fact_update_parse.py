"""parse_fact_update: every argument of an update_fact call is validated before any embedding or write."""

from datetime import datetime, timezone

import pytest

from src.domain.entities import FactState, TemporalClass
from src.domain.fact_update import ParsedFactUpdate, parse_fact_update


def refused(updates, *fragments):
    with pytest.raises(ValueError) as caught:
        parse_fact_update(updates)
    message = str(caught.value)
    for fragment in fragments:
        assert fragment in message
    return message


def test_an_empty_call_parses_to_nothing_set():
    assert parse_fact_update({}) == ParsedFactUpdate()


def test_a_full_call_parses_every_key_and_lowercases_enums():
    parsed = parse_fact_update({
        "content": "mother is Mariia", "tags": ["family"], "metadata": {"name": "Masha"},
        "state": "ARCHIVED", "temporal_class": "DYNAMIC", "reported_date": "2020-01-02T03:04:05Z",
    })
    assert parsed == ParsedFactUpdate(
        content="mother is Mariia", tags=["family"], metadata={"name": "Masha"}, state=FactState.ARCHIVED,
        temporal_class=TemporalClass.DYNAMIC, reported_date=datetime(2020, 1, 2, 3, 4, 5, tzinfo=timezone.utc))


def test_a_datetime_reported_date_is_taken_as_it_is():
    given = datetime(2021, 5, 6, 7, 8, 9, tzinfo=timezone.utc)
    assert parse_fact_update({"reported_date": given}).reported_date == given


def test_unknown_keys_are_ignored():
    assert parse_fact_update({"domain": "work", "foo": "bar", "context_priority": "low"}) == ParsedFactUpdate()


def test_blank_state_temporal_class_and_reported_date_mean_absent():
    assert parse_fact_update({"state": "", "temporal_class": None, "reported_date": ""}) == ParsedFactUpdate()


# -------------------------------------------------------------------------------------------------- tags and content

@pytest.mark.parametrize("tags", [None, []])
def test_null_and_empty_tags_are_refused(tags):
    refused({"tags": tags}, "tags must be a non-empty list of strings")


@pytest.mark.parametrize("tags", ["family", ["family", 3], [["family"]], {"a": "b"}, ("family",)])
def test_non_string_tag_values_are_refused(tags):
    refused({"tags": tags}, "tags must be a non-empty list of strings")


def test_the_tag_list_is_copied():
    given = ["family"]
    parsed = parse_fact_update({"tags": given})
    assert parsed.tags == given and parsed.tags is not given


@pytest.mark.parametrize("content", [None, "", "   ", 7, ["text"]])
def test_empty_or_non_string_content_is_refused(content):
    refused({"content": content}, "content must be a non-empty string")


# -------------------------------------------------------------------------------------------------- metadata

@pytest.mark.parametrize("metadata", ["name=Masha", ["a"], 3, None])
def test_a_non_dict_metadata_is_ignored(metadata):
    # Reviewer edit (facts S3, T2): the model may no longer send STALE (D1); ARCHIVED carries the same point.
    assert parse_fact_update({"metadata": metadata, "state": "ARCHIVED"}) == ParsedFactUpdate(state=FactState.ARCHIVED)


# -------------------------------------------------------------------------------------------------- enums and dates

@pytest.mark.parametrize("key,value", [("state", "zombie"), ("temporal_class", "sometimes")])
def test_a_bad_enum_value_is_refused_and_names_the_allowed_ones(key, value):
    message = refused({key: value}, key, value)
    assert "use one of" in message


@pytest.mark.parametrize("key", ["state", "temporal_class"])
def test_a_non_string_enum_value_is_refused(key):
    refused({key: 5}, f"{key} must be a string")


@pytest.mark.parametrize("date", ["yesterday", "2026-13-45", 20260304, ["2026-03-04"]])
def test_a_bad_date_is_refused(date):
    refused({"reported_date": date}, "reported_date")


# -------------------------------------------------------------------------------------------------- superseded_by

def test_superseded_needs_superseded_by():
    refused({"state": "SUPERSEDED"}, "SUPERSEDED needs superseded_by")


def test_superseded_with_superseded_by_parses():
    parsed = parse_fact_update({"state": "superseded", "superseded_by": "new-1", "reported_date": "2020-01-02"})
    assert parsed.state is FactState.SUPERSEDED and parsed.superseded_by == "new-1"
    assert parsed.reported_date == datetime(2020, 1, 2, tzinfo=timezone.utc)


def test_superseded_by_alone_is_refused():
    refused({"superseded_by": "new-1"}, "only together with state SUPERSEDED")


# Reviewer edit (facts S3, T2): STALE is refused before the superseded_by rule speaks (D1, S3-D3), so its case expects
# that refusal; every other state still gets the superseded_by one.
@pytest.mark.parametrize("state,fragment", [
    ("CURRENT", "only together with state SUPERSEDED"), ("STALE", "only the system marks a fact stale"),
    ("ARCHIVED", "only together with state SUPERSEDED"), ("INVALIDATED", "only together with state SUPERSEDED")])
def test_superseded_by_with_another_state_is_refused(state, fragment):
    refused({"state": state, "superseded_by": "new-1"}, fragment)


@pytest.mark.parametrize("extra", [
    {"content": "new text"}, {"tags": ["a"]}, {"metadata": {"a": 1}}, {"temporal_class": "STABLE"}])
def test_superseded_with_any_content_key_is_refused(extra):
    refused({"state": "SUPERSEDED", "superseded_by": "new-1", **extra}, "cannot be combined")


@pytest.mark.parametrize("value", ["", "  ", 7, ["x"]])
def test_a_malformed_superseded_by_is_refused(value):
    refused({"state": "SUPERSEDED", "superseded_by": value}, "superseded_by must be the id")


def test_a_null_superseded_by_is_absent():
    assert parse_fact_update({"superseded_by": None}) == ParsedFactUpdate()
    refused({"state": "SUPERSEDED", "superseded_by": None}, "needs superseded_by")
