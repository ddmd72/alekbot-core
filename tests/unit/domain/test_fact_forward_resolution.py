"""forward_step, the accepted sets and closure_fields: following a fact id to the fact a caller may write."""

from datetime import datetime, timezone

import pytest

from src.domain.entities import FACT_SYSTEM_TAGS, FactEntity, FactState, FactType
from src.domain.exceptions import FactResolutionError, FactWriteConflict, InPlaceFieldRefused
from src.domain.fact_lifecycle import (
    CABINET_ACCEPTS,
    CONSOLIDATION_ACCEPTS,
    IN_PLACE_FIELDS,
    MAX_FORWARD_HOPS,
    closure_fields,
    forward_step,
    lifecycle_fields,
)
from src.domain.stored_fact import StoredFact

NOW = datetime(2026, 10, 23, 9, 0, tzinfo=timezone.utc)
ACC = "acc-1"
LINEAGE = "lin-1"


def fact(fid, state=FactState.CURRENT, superseded_by=None, lineage=LINEAGE, account=ACC):
    return FactEntity(
        id=fid,
        account_id=account,
        created_by_user_id="u1",
        lineage_id=lineage,
        text=f"text {fid}",
        type=FactType.EVENT,
        state=state,
        superseded_by=superseded_by,
    )


def walk(store, start_id, accepted):
    """The loop a repository runs: read, forward_step, follow. The cap is forward_step's."""
    lineage = store[start_id].fact.lineage_id
    current_id = start_id
    for hops in range(MAX_FORWARD_HOPS + 5):
        fact = store[current_id].fact
        nxt = forward_step(fact, accepted, lineage, start_id, ACC, hops)
        if nxt is None:
            return fact.id
        current_id = nxt
    pytest.fail("forward_step never ended the walk")


def chain(*facts):
    return {f.id: StoredFact(f, update_time=i) for i, f in enumerate(facts)}


def test_a_current_fact_is_its_own_end():
    f = fact("a")
    assert forward_step(f, CONSOLIDATION_ACCEPTS, LINEAGE, "a", ACC, 0) is None


def test_a_superseded_fact_points_at_its_successor():
    f = fact("a", FactState.SUPERSEDED, superseded_by="b")
    assert forward_step(f, CONSOLIDATION_ACCEPTS, LINEAGE, "a", ACC, 0) == "b"


def test_the_same_lineage_is_followed_over_several_versions():
    store = chain(
        fact("a", FactState.SUPERSEDED, superseded_by="b"),
        fact("b", FactState.SUPERSEDED, superseded_by="c"),
        fact("c"),
    )
    assert walk(store, "a", CONSOLIDATION_ACCEPTS) == "c"


def test_a_merge_end_names_the_merged_id_and_is_an_error_for_both_callers():
    store = chain(fact("src", FactState.SUPERSEDED, superseded_by="merged"), fact("merged", lineage="lin-merged"))
    for accepted in (CONSOLIDATION_ACCEPTS, CABINET_ACCEPTS):
        with pytest.raises(FactResolutionError) as exc:
            walk(store, "src", accepted)
        assert exc.value.current_id == "merged"
        assert str(exc.value) == "fact src was merged into merged; search again"


def test_a_version_of_a_merged_fact_is_still_a_merge_end():
    store = chain(
        fact("src", FactState.SUPERSEDED, superseded_by="merged"),
        fact("merged", FactState.SUPERSEDED, superseded_by="merged-v2", lineage="lin-merged"),
        fact("merged-v2", lineage="lin-merged"),
    )
    with pytest.raises(FactResolutionError) as exc:
        walk(store, "src", CONSOLIDATION_ACCEPTS)
    assert exc.value.current_id == "merged"
    assert str(exc.value) == "fact src was merged into merged; search again"


def test_superseded_with_no_successor_has_no_current_id():
    store = chain(fact("old", FactState.SUPERSEDED))
    with pytest.raises(FactResolutionError) as exc:
        walk(store, "old", CONSOLIDATION_ACCEPTS)
    assert exc.value.current_id is None
    assert str(exc.value) == "fact old was superseded and its successor is unknown; search again"


def test_superseded_with_no_successor_is_an_error_for_the_cabinet_too():
    with pytest.raises(FactResolutionError):
        forward_step(fact("old", FactState.SUPERSEDED), CABINET_ACCEPTS, LINEAGE, "old", ACC, 0)


def test_an_archived_end_is_refused_for_consolidation_and_accepted_for_the_cabinet():
    f = fact("a", FactState.ARCHIVED)
    with pytest.raises(FactResolutionError) as exc:
        forward_step(f, CONSOLIDATION_ACCEPTS, LINEAGE, "a", ACC, 0)
    assert exc.value.current_id == "a"
    assert str(exc.value) == "fact a is archived"
    assert forward_step(f, CABINET_ACCEPTS, LINEAGE, "a", ACC, 0) is None


def test_an_archived_successor_is_named_with_the_asked_id():
    store = chain(fact("a", FactState.SUPERSEDED, superseded_by="b"), fact("b", FactState.ARCHIVED))
    with pytest.raises(FactResolutionError) as exc:
        walk(store, "a", CONSOLIDATION_ACCEPTS)
    assert exc.value.current_id == "b"
    assert str(exc.value) == "fact a was replaced by b, which is archived"
    assert walk(store, "a", CABINET_ACCEPTS) == "b"


@pytest.mark.parametrize("state", [FactState.STALE, FactState.INVALIDATED, FactState.ARCHIVED])
def test_every_non_superseded_state_ends_the_chain_for_the_cabinet(state):
    assert forward_step(fact("a", state), CABINET_ACCEPTS, LINEAGE, "a", ACC, 0) is None


@pytest.mark.parametrize("state", [FactState.STALE, FactState.INVALIDATED, FactState.ARCHIVED])
def test_only_current_ends_the_chain_for_consolidation(state):
    with pytest.raises(FactResolutionError):
        forward_step(fact("a", state), CONSOLIDATION_ACCEPTS, LINEAGE, "a", ACC, 0)


def test_invalidated_with_superseded_by_is_an_end_for_cabinet_and_an_error_for_consolidation():
    f = fact("a", FactState.INVALIDATED, superseded_by="b")
    assert forward_step(f, CABINET_ACCEPTS, LINEAGE, "a", ACC, 0) is None
    with pytest.raises(FactResolutionError) as exc:
        forward_step(f, CONSOLIDATION_ACCEPTS, LINEAGE, "a", ACC, 0)
    assert exc.value.current_id == "a"
    assert str(exc.value) == "fact a is invalidated"


def test_a_hop_to_another_account_is_an_error_that_does_not_name_the_foreign_id():
    foreign = fact("theirs", account="acc-2")
    for accepted in (CONSOLIDATION_ACCEPTS, CABINET_ACCEPTS):
        with pytest.raises(FactResolutionError) as exc:
            forward_step(foreign, accepted, LINEAGE, "mine", ACC, 0)
        assert exc.value.current_id is None
        assert str(exc.value) == "fact mine cannot be resolved; search again"


def test_the_account_is_checked_before_the_lineage():
    foreign = fact("theirs", account="acc-2", lineage="lin-other")
    with pytest.raises(FactResolutionError) as exc:
        forward_step(foreign, CONSOLIDATION_ACCEPTS, LINEAGE, "mine", ACC, 0)
    assert exc.value.current_id is None


HOP_CAP_MESSAGE = "fact a has more than 50 forward hops; search again"


def test_hop_49_follows_and_hop_50_raises():
    f = fact("a", FactState.SUPERSEDED, superseded_by="b")
    assert forward_step(f, CONSOLIDATION_ACCEPTS, LINEAGE, "a", ACC, MAX_FORWARD_HOPS - 1) == "b"
    with pytest.raises(FactResolutionError) as exc:
        forward_step(f, CONSOLIDATION_ACCEPTS, LINEAGE, "a", ACC, MAX_FORWARD_HOPS)
    assert exc.value.current_id is None
    assert str(exc.value) == HOP_CAP_MESSAGE


def test_the_hop_cap_is_50():
    assert MAX_FORWARD_HOPS == 50


def test_a_chain_of_exactly_the_cap_still_resolves():
    facts = [fact(f"f{i}", FactState.SUPERSEDED, superseded_by=f"f{i + 1}") for i in range(MAX_FORWARD_HOPS)]
    store = chain(*facts, fact(f"f{MAX_FORWARD_HOPS}"))
    assert walk(store, "f0", CONSOLIDATION_ACCEPTS) == f"f{MAX_FORWARD_HOPS}"


def test_a_chain_one_hop_over_the_cap_raises():
    facts = [fact(f"f{i}", FactState.SUPERSEDED, superseded_by=f"f{i + 1}") for i in range(MAX_FORWARD_HOPS + 1)]
    store = chain(*facts, fact(f"f{MAX_FORWARD_HOPS + 1}"))
    with pytest.raises(FactResolutionError) as exc:
        walk(store, "f0", CONSOLIDATION_ACCEPTS)
    assert str(exc.value) == "fact f0 has more than 50 forward hops; search again"


def test_a_self_pointing_fact_ends_in_the_hop_cap_error():
    store = chain(fact("a", FactState.SUPERSEDED, superseded_by="a"))
    with pytest.raises(FactResolutionError) as exc:
        walk(store, "a", CONSOLIDATION_ACCEPTS)
    assert str(exc.value) == HOP_CAP_MESSAGE


def test_a_cycle_ends_in_the_hop_cap_error():
    store = chain(
        fact("a", FactState.SUPERSEDED, superseded_by="b"),
        fact("b", FactState.SUPERSEDED, superseded_by="a"),
    )
    with pytest.raises(FactResolutionError) as exc:
        walk(store, "a", CONSOLIDATION_ACCEPTS)
    assert str(exc.value) == HOP_CAP_MESSAGE


def test_an_end_state_at_the_cap_is_still_an_end():
    assert forward_step(fact("a"), CONSOLIDATION_ACCEPTS, LINEAGE, "a", ACC, MAX_FORWARD_HOPS) is None


def test_accepted_sets():
    assert CONSOLIDATION_ACCEPTS == {FactState.CURRENT}
    assert CABINET_ACCEPTS == set(FactState) - {FactState.SUPERSEDED}


def test_closure_fields_close_the_fact_in_place_in_favour_of_the_successor():
    fields = closure_fields("next", NOW)
    assert fields == {**lifecycle_fields(FactState.SUPERSEDED, NOW), "superseded_by": "next", "last_updated": NOW}
    assert fields["state"] is FactState.SUPERSEDED and fields["is_current"] is False and fields["valid_to"] == NOW


def test_closure_fields_are_in_place_fields():
    assert set(closure_fields("next", NOW)) <= IN_PLACE_FIELDS


def test_in_place_fields_exclude_content_and_lineage_keys():
    # S2's six keys plus the five expiry/lifecycle keys Facts S3 writes in place (plan §1.1, RFC D2).
    assert IN_PLACE_FIELDS == {
        "state", "is_current", "valid_to", "superseded_by", "reported_date", "last_updated",
        "expiry_kind", "ttl_days", "expires_at", "stale_since", "lifecycle_events",
    }
    assert not IN_PLACE_FIELDS & {"text", "tags", "metadata", "temporal_class", "version", "lineage_id", "id"}


def test_the_resolution_exceptions_are_not_value_errors():
    assert not issubclass(FactResolutionError, ValueError)
    assert not issubclass(FactWriteConflict, ValueError)
    assert not issubclass(InPlaceFieldRefused, ValueError)


def test_exceptions_carry_what_callers_branch_on():
    assert FactWriteConflict("f1").fact_id == "f1"
    assert InPlaceFieldRefused(["text", "tags"]).fields == ("tags", "text")
    assert "tags, text" in str(InPlaceFieldRefused(["text", "tags"]))
    assert FactResolutionError("m").current_id is None
    assert FactResolutionError("m", current_id="x").current_id == "x"


def test_stored_fact_keeps_the_token_and_is_immutable():
    f = fact("a")
    stored = StoredFact(f, update_time=7)
    assert stored.fact is f and stored.update_time == 7
    with pytest.raises(AttributeError):
        stored.update_time = 8  # type: ignore[misc]


def test_superseded_by_defaults_to_none_and_system_tags_are_the_two_known():
    assert fact("a").superseded_by is None
    assert FACT_SYSTEM_TAGS == {"consolidated", "anchor"}
