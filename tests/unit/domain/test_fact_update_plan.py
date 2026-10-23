"""plan_fact_update: a field-masked in-place write or a new version document, decided by code."""

from datetime import datetime, timedelta, timezone

import pytest

from src.domain.entities import (
    ContextPriority, FactDomain, FactEntity, FactState, FactType, FactVisibility, TemporalClass)
from src.domain.fact_embedding import fact_embedding_texts
from src.domain.fact_lifecycle import IN_PLACE_FIELDS, lifecycle_fields
from src.domain.fact_update import ParsedFactUpdate, parse_fact_update
from src.domain.fact_update_plan import plan_fact_update

CREATED = datetime(2026, 1, 1, tzinfo=timezone.utc)
VALID_FROM = datetime(2026, 2, 1, tzinfo=timezone.utc)
NOW = datetime(2026, 10, 23, 9, 0, tzinfo=timezone.utc)
VECTORS = {"vector": [0.1, 0.2], "tags_vector": [0.3, 0.4], "metadata_vector": [0.5, 0.6]}


def current_fact(**overrides):
    base = dict(
        id="f1", account_id="acc", created_by_user_id="u1", lineage_id="lin", text="mother is Maria",
        tags=["family", "mother", "consolidated"], type=FactType.PRINCIPLE, metadata={"name": "Maria"},
        domain=FactDomain.NETWORK, temporal_class=TemporalClass.STABLE, context_priority=ContextPriority.HIGH,
        ttl_days=30, expires_at=CREATED + timedelta(days=30), version=3, replaces_fact_id="f0", context="at lunch",
        reported_date=CREATED, created_at=CREATED, valid_from=VALID_FROM, last_updated=VALID_FROM,
        visibility=FactVisibility.USER_PRIVATE, **VECTORS)
    base.update(overrides)
    return FactEntity(**base)


def plan(updates, fact=None):
    return plan_fact_update(fact or current_fact(), parse_fact_update(updates), NOW)


# ------------------------------------------------------------------------------------------------ the version kinds

@pytest.mark.parametrize("updates", [
    {"content": "mother is Mariia"},
    {"tags": ["relatives"]},
    {"tags": ["family", "mother", "friend"]},
    {"tags": ["family", "mother", "consolidated", "anchor"]},
    {"metadata": {"name": "Masha"}},
    {"metadata": {"city": "Springfield"}},
    {"temporal_class": "DYNAMIC"},
])
def test_each_content_kind_is_a_version(updates):
    result = plan(updates)
    assert result.kind == "version" and result.fields == {} and result.version_fact is not None


@pytest.mark.parametrize("updates", [
    {"state": "ARCHIVED"},
    {"state": "STALE"},
    {"reported_date": "2020-01-02T03:04:05Z"},
    {},
    {"domain": "work", "foo": "bar"},
])
def test_state_only_reported_date_only_and_empty_updates_are_in_place_with_the_version_unchanged(updates):
    fact = current_fact()
    result = plan(updates, fact)
    assert result.kind == "in_place" and result.version_fact is None
    assert not result.vectors_stale and result.embedding_texts is None
    assert "version" not in result.fields and "id" not in result.fields
    assert fact.version == 3


def test_an_empty_update_writes_only_last_updated():
    assert plan({}).fields == {"last_updated": NOW}


def test_updating_with_the_stored_values_is_in_place_and_changes_only_last_updated():
    result = plan({"content": "mother is Maria", "tags": ["family", "mother", "consolidated"],
                   "metadata": {"name": "Maria"}, "temporal_class": "STABLE"})
    assert result.kind == "in_place" and result.fields == {"last_updated": NOW}


# ------------------------------------------------------------------------------------------------ tags

def test_system_tags_the_update_omits_are_kept_at_the_end_in_stored_order():
    fact = current_fact(tags=["consolidated", "family", "anchor", "mother"])
    version = plan({"tags": ["relatives", "friend"]}, fact).version_fact
    assert version.tags == ["relatives", "friend", "consolidated", "anchor"]


def test_a_system_tag_the_update_sends_is_not_duplicated_and_keeps_the_update_position():
    fact = current_fact(tags=["family", "consolidated", "anchor"])
    version = plan({"tags": ["consolidated", "relatives"]}, fact).version_fact
    assert version.tags == ["consolidated", "relatives", "anchor"]


def test_a_system_tag_in_the_update_that_the_fact_never_had_is_kept_as_sent():
    fact = current_fact(tags=["family"])
    assert plan({"tags": ["family", "anchor"]}, fact).version_fact.tags == ["family", "anchor"]


def test_resending_stored_non_system_tags_in_stored_order_is_in_place():
    fact = current_fact(tags=["consolidated", "family", "mother"])
    result = plan({"tags": ["family", "mother"]}, fact)
    assert result.kind == "in_place" and result.fields == {"last_updated": NOW}


def test_resending_the_tags_in_another_order_is_not_a_content_change():
    fact = current_fact(tags=["family", "mother", "consolidated"])
    result = plan({"tags": ["mother", "family"]}, fact)
    assert result.kind == "in_place" and "tags" not in result.fields


def test_a_tag_order_difference_alongside_a_real_change_does_not_hide_the_change():
    result = plan({"tags": ["mother", "family"], "content": "mother is Mariia"})
    assert result.kind == "version" and result.version_fact.tags == ["mother", "family", "consolidated"]


def test_a_removed_non_system_tag_is_a_version():
    assert plan({"tags": ["family", "consolidated"]}).kind == "version"


def test_a_version_that_changes_only_a_system_tag_keeps_the_vectors():
    result = plan({"tags": ["family", "mother", "consolidated", "anchor"]})
    assert result.kind == "version" and not result.vectors_stale and result.embedding_texts is None
    assert result.version_fact.vector == VECTORS["vector"]


# ------------------------------------------------------------------------------------------------ the version entity

def test_the_version_entity_follows_the_design():
    old = current_fact()
    version = plan({"content": "mother is Mariia", "tags": ["relatives"]}, old).version_fact
    assert version.id != old.id and version.id
    assert version.version == 4 and version.replaces_fact_id == "f1"
    assert (version.valid_from, version.last_updated) == (NOW, NOW)
    assert (version.superseded_by, version.is_current, version.valid_to) == (None, True, None)
    assert version.state is FactState.CURRENT
    assert (version.text, version.tags) == ("mother is Mariia", ["relatives", "consolidated"])
    assert version.metadata == {"name": "Maria"}
    assert version.reported_date == NOW


def test_the_version_copies_the_identity_and_classification_of_the_old_document():
    old = current_fact()
    version = plan({"content": "mother is Mariia"}, old).version_fact
    for field in ("created_at", "lineage_id", "account_id", "created_by_user_id", "domain", "context_priority", "type",
                  "context", "visibility", "ttl_days", "expires_at", "temporal_class"):
        assert getattr(version, field) == getattr(old, field), field


def test_the_old_fact_object_is_not_touched_and_shares_no_mutable_state_with_the_version():
    old = current_fact()
    before = old.model_copy(deep=True)
    version = plan({"content": "mother is Mariia", "metadata": {"city": "Springfield"}}, old).version_fact
    version.metadata["city"] = "Lviv"
    version.tags.append("x")
    assert old == before


def test_a_given_reported_date_wins_over_now():
    given = "2020-01-02T03:04:05Z"
    version = plan({"content": "edited", "reported_date": given}).version_fact
    assert version.reported_date == datetime(2020, 1, 2, 3, 4, 5, tzinfo=timezone.utc)


def test_a_tags_only_version_re_embeds_with_the_new_tags_and_drops_the_old_vectors():
    result = plan({"tags": ["relatives"]})
    assert result.vectors_stale
    assert result.embedding_texts == fact_embedding_texts("mother is Maria", ["relatives", "consolidated"], {"name": "Maria"})
    assert result.embedding_texts[1] == "relatives"
    assert (result.version_fact.vector, result.version_fact.tags_vector, result.version_fact.metadata_vector) == (None,) * 3


def test_a_metadata_only_version_re_embeds_with_the_merged_metadata_and_drops_the_old_vectors():
    result = plan({"metadata": {"city": "Springfield"}})
    merged = {"name": "Maria", "city": "Springfield"}
    assert result.vectors_stale
    assert result.embedding_texts == fact_embedding_texts("mother is Maria", ["family", "mother", "consolidated"], merged)
    assert result.embedding_texts[2] == '{"name": "Maria", "city": "Springfield"}'
    assert (result.version_fact.vector, result.version_fact.tags_vector, result.version_fact.metadata_vector) == (None,) * 3


def test_a_text_only_version_re_embeds_all_three_texts():
    result = plan({"content": "mother is Mariia"})
    assert result.vectors_stale
    assert result.embedding_texts == fact_embedding_texts("mother is Mariia", ["family", "mother", "consolidated"], {"name": "Maria"})
    assert (result.version_fact.vector, result.version_fact.tags_vector, result.version_fact.metadata_vector) == (None,) * 3


def test_a_text_change_marks_the_vectors_stale_and_lists_the_texts_to_embed():
    result = plan({"content": "mother is Mariia", "tags": ["relatives"], "metadata": {"city": "Springfield"}})
    assert result.vectors_stale
    assert result.embedding_texts == fact_embedding_texts(
        "mother is Mariia", ["relatives", "consolidated"], {"name": "Maria", "city": "Springfield"})
    version = result.version_fact
    assert (version.vector, version.tags_vector, version.metadata_vector) == (None, None, None)


def test_metadata_is_merged_over_the_stored_keys():
    version = plan({"metadata": {"name": "Masha", "city": "Springfield"}}).version_fact
    assert version.metadata == {"name": "Masha", "city": "Springfield"}


def test_a_temporal_class_only_update_is_a_version_that_keeps_the_vectors():
    result = plan({"temporal_class": "DYNAMIC"})
    assert result.kind == "version" and not result.vectors_stale and result.embedding_texts is None
    version = result.version_fact
    assert version.temporal_class is TemporalClass.DYNAMIC
    assert (version.vector, version.tags_vector, version.metadata_vector) == tuple(VECTORS.values())
    assert version.reported_date == NOW


def test_a_state_in_the_same_call_as_content_is_applied_to_the_version():
    version = plan({"content": "mother is Mariia", "state": "ARCHIVED"}).version_fact
    expected = lifecycle_fields(FactState.ARCHIVED, NOW)
    assert (version.state, version.is_current, version.valid_to) == (
        expected["state"], expected["is_current"], expected["valid_to"])
    assert version.superseded_by is None


def test_the_stored_state_sent_with_content_leaves_the_version_open():
    fact = current_fact(state=FactState.STALE)
    version = plan({"content": "mother is Valentyna", "state": "STALE"}, fact).version_fact
    assert (version.state, version.is_current, version.valid_to) == (FactState.STALE, True, None)


# ------------------------------------------------------------------------------------------------ in place

@pytest.mark.parametrize("state", ["CURRENT", "STALE", "ARCHIVED", "INVALIDATED"])
def test_a_state_change_writes_the_lifecycle_fields_and_never_superseded_by(state):
    target = FactState(state.lower())
    fact = current_fact(state=FactState.ARCHIVED if state == "CURRENT" else FactState.CURRENT)
    fields = plan({"state": state}, fact).fields
    assert fields == {**lifecycle_fields(target, NOW), "last_updated": NOW}
    assert "superseded_by" not in fields


def test_superseded_writes_the_closure_fields_with_the_given_successor():
    fields = plan({"state": "SUPERSEDED", "superseded_by": "new-1"}).fields
    assert fields == {"state": FactState.SUPERSEDED, "is_current": False, "valid_to": NOW,
                      "superseded_by": "new-1", "last_updated": NOW}


def test_reapplying_the_stored_state_does_not_move_valid_to():
    closed_at = NOW - timedelta(days=5)
    fact = current_fact(state=FactState.ARCHIVED, valid_to=closed_at)
    fields = plan({"state": "ARCHIVED"}, fact).fields
    assert fields == {"last_updated": NOW}


def test_a_reported_date_only_update_writes_that_date_and_last_updated():
    fields = plan({"reported_date": "2020-01-02T03:04:05Z"}).fields
    assert fields == {"last_updated": NOW, "reported_date": datetime(2020, 1, 2, 3, 4, 5, tzinfo=timezone.utc)}


def test_a_state_with_a_reported_date_writes_both():
    fields = plan({"state": "ARCHIVED", "reported_date": "2020-01-02"}).fields
    assert set(fields) == {"state", "is_current", "valid_to", "last_updated", "reported_date"}


@pytest.mark.parametrize("updates", [
    {}, {"state": "ARCHIVED"}, {"state": "CURRENT"}, {"state": "STALE", "reported_date": "2020-01-02"},
    {"state": "INVALIDATED"}, {"state": "SUPERSEDED", "superseded_by": "n"}, {"reported_date": "2020-01-02"},
    {"tags": ["mother", "family"]}])
def test_in_place_fields_are_within_the_in_place_set_and_last_updated_is_always_set(updates):
    fields = plan(updates).fields
    assert set(fields) <= IN_PLACE_FIELDS and fields["last_updated"] == NOW


@pytest.mark.parametrize("updates", [{}, {"state": "ARCHIVED"}, {"state": "INVALIDATED"}, {"state": "STALE"},
                                     {"reported_date": "2020-01-02"}])
def test_superseded_by_appears_only_on_a_superseded_transition(updates):
    assert "superseded_by" not in plan(updates).fields


# ------------------------------------------------------------------------------------------------ guards

def test_a_hand_built_superseded_update_without_a_successor_is_refused():
    with pytest.raises(ValueError, match="needs superseded_by"):
        plan_fact_update(current_fact(), ParsedFactUpdate(state=FactState.SUPERSEDED), NOW)


def test_a_hand_built_superseded_update_with_content_is_refused():
    update = ParsedFactUpdate(state=FactState.SUPERSEDED, superseded_by="n", content="x")
    with pytest.raises(ValueError, match="cannot be combined"):
        plan_fact_update(current_fact(), update, NOW)


# ------------------------------------------------------------------------------------------------ review additions

@pytest.mark.parametrize("state", [FactState.ARCHIVED, FactState.INVALIDATED])
def test_a_version_of_a_closed_state_carries_that_states_flags(state):
    fact = current_fact(state=state, valid_to=NOW - timedelta(days=5))
    version = plan({"content": "mother is Mariia"}, fact).version_fact
    expected = lifecycle_fields(state, NOW)
    assert (version.state, version.is_current, version.valid_to) == (state, expected["is_current"], NOW)


def test_a_version_never_inherits_a_stray_superseded_by_or_a_stale_valid_to():
    fact = current_fact(superseded_by="x", valid_to=NOW - timedelta(days=5))
    version = plan({"content": "mother is Mariia"}, fact).version_fact
    assert (version.superseded_by, version.is_current, version.valid_to, version.state) == (
        None, True, None, FactState.CURRENT)


@pytest.mark.parametrize("updates,field", [
    ({"content": "mother is Mariia"}, "metadata"), ({"content": "mother is Mariia"}, "tags"),
    ({"temporal_class": "DYNAMIC"}, "metadata"), ({"temporal_class": "DYNAMIC"}, "tags")])
def test_a_version_shares_no_metadata_or_tags_object_with_the_old_fact(updates, field):
    old = current_fact()
    before = old.model_copy(deep=True)
    version = plan(updates, old).version_fact
    assert getattr(version, field) is not getattr(old, field)
    if field == "metadata":
        version.metadata["k"] = "v"
    else:
        version.tags.append("k")
    assert old == before


def test_duplicate_tags_are_counted_so_adding_a_repeat_is_a_version():
    result = plan({"tags": ["family", "family", "mother"]})
    assert result.kind == "version" and result.version_fact.tags == ["family", "family", "mother", "consolidated"]


def test_the_same_duplicates_in_another_order_are_not_a_change():
    fact = current_fact(tags=["family", "mother", "family", "consolidated"])
    result = plan({"tags": ["mother", "family", "family"]}, fact)
    assert result.kind == "in_place"
