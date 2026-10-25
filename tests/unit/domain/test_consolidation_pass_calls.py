"""Each pass statistic, computed from typed `PassCall`s (Facts S4 T6b, S1–S3 review L2): the stats see no tool argument."""

import pytest

from src.domain.consolidation_pass_stats import (
    OUTCOME_BORN_DUE,
    OUTCOME_ERROR,
    OUTCOME_EXPIRY,
    OUTCOME_OK,
    ConsolidationPassStats,
    PassCall,
    PassTool,
)
from src.domain.entities import FactDomain
from src.domain.pass_actor import PassActor


def stats(actor=PassActor.STAGE1, max_turns=15):
    return ConsolidationPassStats(actor, max_turns)


def turn(s, calls, outcomes=None):
    s.record_turn(calls)
    s.record_outcomes(calls, outcomes if outcomes is not None else [OUTCOME_OK] * len(calls))


def search(fp="s"):
    return PassCall(PassTool.SEARCH, fp)


def create(text, *, sources=frozenset(), domain=FactDomain.LOCATION, fp=None):
    return PassCall.with_text(PassTool.CREATE, fp or "c", text, sources=frozenset(sources), domain=domain)


def merge(text, sources=("a", "b")):
    return PassCall.with_text(PassTool.MERGE, "m", text, sources=frozenset(sources))


def update(fact_id, closing=False, fp=None):
    return PassCall(PassTool.UPDATE, fp or f"u:{fact_id}:{closing}", key=fact_id, closing=closing)


def test_with_text_keys_a_call_by_a_hash_of_its_text_and_keeps_no_text():
    call = create("User flew to Lisbon")
    assert call.size == len("User flew to Lisbon") and len(call.key) == 64
    assert call.key == create("User flew to Lisbon", fp="other").key != create("User flew to Porto").key
    assert "Lisbon" not in repr(call)
    assert call.words == create("user FLEW to lisbon").words
    assert create("the and for 2026 ab").words == frozenset()


def test_counts_writes_turns_and_turns_used():
    s = stats()
    turn(s, [search(), search("s2"), create("a")])
    turn(s, [update("f1"), merge("m"), PassCall(PassTool.COUNT, "c")])
    turn(s, [])
    assert s.counts == {PassTool.SEARCH: 2, PassTool.CREATE: 1, PassTool.UPDATE: 1, PassTool.MERGE: 1, PassTool.COUNT: 1}
    assert s.writes == 3 and s.turns_used == 3
    assert s.turns == [(PassTool.SEARCH, PassTool.SEARCH, PassTool.CREATE), (PassTool.UPDATE, PassTool.MERGE, PassTool.COUNT), ()]
    assert s.max_turns == 15 and stats(max_turns=7).max_turns == 7


def test_repeats_are_identical_consecutive_turns_in_any_order():
    s = stats()
    s.record_turn([search("a"), update("f", fp="b")])
    s.record_turn([update("f", fp="b"), search("a")])
    s.record_turn([search("x")])
    s.record_turn([search("x")])
    s.record_turn([search("a")])
    assert s.repeats == 2


def test_the_same_fingerprint_on_another_tool_is_no_repeat():
    s = stats()
    s.record_turn([search("a")])
    s.record_turn([PassCall(PassTool.COUNT, "a")])
    assert s.repeats == 0


def test_error_turns_expiry_errors_and_born_due():
    s = stats()
    turn(s, [create("a"), create("b"), create("c")], [OUTCOME_EXPIRY, OUTCOME_BORN_DUE, OUTCOME_OK])
    turn(s, [search()], [OUTCOME_ERROR])
    turn(s, [search()])
    assert (s.error_turns, s.expiry_errors, s.born_due) == (2, 2, 1)


def test_fewer_outcomes_than_calls_are_paired_from_the_front():
    s = stats()
    turn(s, [create("a"), create("b")], [OUTCOME_EXPIRY])
    assert s.expiry_errors == 1 and s.abandoned == 1


def test_an_expiry_refusal_answered_by_the_same_text_is_not_abandoned():
    s = stats()
    turn(s, [create("walks daily")], [OUTCOME_EXPIRY])
    turn(s, [merge("walks daily")])
    assert s.abandoned == 0


def test_an_expiry_refusal_answered_only_by_a_failure_or_before_it_is_abandoned():
    s = stats()
    turn(s, [create("walks daily")])
    turn(s, [create("walks daily")], [OUTCOME_EXPIRY])
    turn(s, [create("walks daily")], [OUTCOME_ERROR])   # an error is no refusal: it is not counted itself
    assert s.abandoned == 1 and s.error_turns == 2


def test_an_update_refusal_is_answered_by_a_non_closing_update_of_the_same_fact_only():
    s = stats()
    turn(s, [update("f1"), update("f2"), update("f3")], [OUTCOME_EXPIRY] * 3)
    turn(s, [update("f1"), update("f2", closing=True), create("f3")])
    assert [c.key for c in s.abandoned_calls()] == ["f2", "f3"]


@pytest.mark.parametrize("actor", [PassActor.STAGE1, PassActor.EMAIL])
def test_a_born_due_refusal_on_a_conversation_pass_is_never_abandoned(actor):
    s = stats(actor)
    turn(s, [create("the trip on the 3rd"), update("f1")], [OUTCOME_BORN_DUE] * 2)
    assert s.born_due == 2 and s.abandoned == 0


@pytest.mark.parametrize("actor", [PassActor.STAGE2A, PassActor.STAGE2B, PassActor.CLUSTER])
def test_a_born_due_refusal_on_a_cleanup_pass_is_answered_by_its_rewrite_or_abandoned(actor):
    answered = stats(actor)
    turn(answered, [create("User plans a trip to Lisbon on 2026-10-10", sources={"f1"})], [OUTCOME_BORN_DUE])
    turn(answered, [create("User took a trip to Lisbon on 2026-10-10", sources={"f1"})])
    assert answered.abandoned == 0

    other_source = stats(actor)
    turn(other_source, [create("User plans a trip to Lisbon on 2026-10-10", sources={"f1"})], [OUTCOME_BORN_DUE])
    turn(other_source, [create("User took a trip to Lisbon on 2026-10-10", sources={"f2"})])
    assert other_source.abandoned == 1

    other_domain = stats(actor)
    turn(other_domain, [create("User plans a trip to Lisbon on 2026-10-10")], [OUTCOME_BORN_DUE])
    turn(other_domain, [create("User took a trip to Lisbon on 2026-10-10", domain=FactDomain.WORK)])
    assert other_domain.abandoned == 1

    merged = stats(actor)
    turn(merged, [merge("Will visit Lisbon", ("a", "b"))], [OUTCOME_BORN_DUE])
    turn(merged, [merge("Visited Lisbon", ("b", "a"))])
    assert merged.abandoned == 0

    closed = stats(actor)
    turn(closed, [update("f1")], [OUTCOME_BORN_DUE])
    turn(closed, [update("f1", closing=True)])
    assert [c.key for c in closed.abandoned_calls()] == ["f1"]


def test_a_search_or_count_refusal_is_never_abandoned():
    s = stats(PassActor.STAGE2A)
    turn(s, [search(), PassCall(PassTool.COUNT, "c"), PassCall(PassTool.OTHER, "o")], [OUTCOME_EXPIRY] * 3)
    assert s.expiry_errors == 3 and s.abandoned == 0


def test_the_stats_read_the_pass_from_its_actor_and_refuse_a_label_that_is_none():
    assert stats(PassActor.CLUSTER).actor is PassActor.CLUSTER
    assert ConsolidationPassStats("stage2b", 15).actor is PassActor.STAGE2B   # type: ignore[arg-type]
    with pytest.raises(ValueError):
        ConsolidationPassStats("consolidation", 15)                              # type: ignore[arg-type]
