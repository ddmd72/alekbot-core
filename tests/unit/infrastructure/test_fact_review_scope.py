"""`FactReviewScope` (Facts S4 · T6a1): the review pass's input, scope, state, outcome and texts (plan §3.5), over the real
repository, write service and adapter on the review world's evaluating Firestore fake. Assertions are on the tool results,
the scope's record and the raw stored documents."""
import asyncio
from datetime import timedelta

import pytest

from src.domain.entities import ExpiryKind, FactState
from src.domain.exceptions import FactWriteConflict
from src.domain.fact_review_batch import ReviewBatch
from src.domain.fact_review_basis import ReviewBasis
from src.domain.fact_review_question import QuestionStatus
from src.domain.fact_review_reasons import basis_reason, superseded_reason
from src.domain.fact_review_rules import (
    ASK_OWNER_PLACE, STAYS_STALE_REFUSAL, UPDATE_FACT_PLACE, asked_refusal, changed_outside_refusal, decided_refusal, not_in_batch_refusal,
    not_under_review_refusal,
)
from src.domain.fact_write_context import REVIEW_REPORTED_DATE_NOTE, kept_reported_date_note
from src.infrastructure.fact_review_scope import FactReviewScope
from tests.support.fact_kit import ACC
from tests.support.fact_lifecycle_world import NOW
from tests.support.fact_review_world import ReviewWorld
from tests.support.fact_store import FACTS
from tests.support.review_rule_builders import question as build_question

TZ = "Europe/Lisbon"
HORIZON = {"kind": "horizon", "days": 30}
REVIVE = {"state": "current", "expiry": HORIZON}
UPDATE, ASK = UPDATE_FACT_PLACE, ASK_OWNER_PLACE


@pytest.fixture
def world():
    return ReviewWorld()


async def stale(world, text):
    return await world.seed_stale(
        text, expiry_kind=ExpiryKind.HORIZON, ttl_days=14, reported_date=NOW - timedelta(days=60)
    )


async def evidence(world, text="moved back to Springfield in September"):
    return await world.seed(text, reported_date=NOW - timedelta(days=1))


async def scope_of(world, *fact_ids, questions=()):
    facts = tuple([await world.repo.get_fact_by_id(fact_id) for fact_id in fact_ids])
    return FactReviewScope(ReviewBatch(facts, tuple(questions)), ACC, TZ, world.now, world.repo, world.store.adapter)


def on(fact_id):
    return {"kind": "evidence", "evidence_fact_id": fact_id}


def state_of(world, fact_id):
    return world.docs[fact_id]["state"]


def last_reason(world, fact_id):
    return world.docs[fact_id]["lifecycle_events"][-1]["reason"]


# ------------------------------------------------------------------------------------------------ the batch

async def test_a_fact_outside_the_batch_is_refused_and_left_as_it_is(world):
    in_batch, outside, ev = await stale(world, "a"), await stale(world, "b"), await evidence(world)
    scope = await scope_of(world, in_batch)

    res = await scope.update_fact({"fact_id": outside, "updates": REVIVE, "basis": on(ev)})
    ask = await scope.ask_owner({"fact_id": outside, "fallback": "revive"})

    assert res == {"fact_id": outside, "status": "failed", "refused": "scope", "message": not_in_batch_refusal(UPDATE)}
    assert ask["refused"] == "scope" and ask["message"] == not_in_batch_refusal(ASK)
    assert state_of(world, outside) == "stale" and scope.decisions() == [] and scope.asks() == []


async def test_a_write_to_an_unrelated_current_fact_is_refused(world):
    batch_fact, current, ev = await stale(world, "a"), await world.seed("current one"), await evidence(world)
    scope = await scope_of(world, batch_fact)

    res = await scope.update_fact({"fact_id": current, "updates": {"state": "archived"}, "basis": on(ev)})

    assert res["refused"] == "scope" and res["message"] == not_in_batch_refusal(UPDATE)
    assert state_of(world, current) == "current"


async def test_a_current_batch_fact_with_no_question_is_refused(world):
    current, ev = await world.seed("current one"), await evidence(world)
    scope = await scope_of(world, current)

    res = await scope.update_fact({"fact_id": current, "updates": {"state": "archived"}, "basis": on(ev)})

    assert res["refused"] == "scope" and res["message"] == not_under_review_refusal(UPDATE)
    assert state_of(world, current) == "current" and [f.id for f in scope.kept()] == [current]


async def test_a_write_to_the_queued_current_fact_of_a_batch_question_passes(world):
    queued, ev = await world.seed("queued for the owner"), await evidence(world)
    waiting = build_question(QuestionStatus.QUEUED, question_id="q1", account_id=ACC, fact_id=queued)
    scope = await scope_of(world, queued, questions=[waiting])

    # P2 revised (P2-2): evidence no longer backs an archive; the scope rule is read through an invalidation it backs
    res = await scope.update_fact({"fact_id": queued, "updates": {"state": "invalidated"}, "basis": on(ev)})

    assert res["status"] == "updated", res
    assert state_of(world, queued) == "invalidated"
    assert [(d.fact_id, d.outcome) for d in scope.decisions()] == [(queued, "invalidated")]


# ------------------------------------------------------------------------------------------------ one result per fact

async def test_an_ask_then_a_write_in_one_gathered_turn_keeps_the_ask(world):
    fact, ev = await stale(world, "a"), await evidence(world)
    scope = await scope_of(world, fact)
    world.store.db.collection(FACTS).yield_on_get = True   # every read yields: without the lock the two calls interleave

    ask, write = await asyncio.gather(
        scope.ask_owner({"fact_id": fact, "fallback": "revive"}),
        scope.update_fact({"fact_id": fact, "updates": REVIVE, "basis": on(ev)}),
    )

    assert ask["status"] == "asked" and ask["fact_id"] == fact
    assert write == {"fact_id": fact, "status": "failed", "refused": "scope", "message": asked_refusal(UPDATE)}
    assert state_of(world, fact) == "stale"
    assert [a.fact_id for a in scope.asks()] == [fact] and scope.decisions() == [] and scope.kept() == []


async def test_a_write_then_an_ask_in_one_gathered_turn_keeps_the_write(world):
    fact, ev = await stale(world, "a"), await evidence(world)
    scope = await scope_of(world, fact)
    world.store.db.collection(FACTS).yield_on_get = True   # every read yields: without the lock the two calls interleave

    write, ask = await asyncio.gather(
        scope.update_fact({"fact_id": fact, "updates": REVIVE, "basis": on(ev)}),
        scope.ask_owner({"fact_id": fact, "fallback": "revive"}),
    )

    assert write["status"] == "updated" and state_of(world, fact) == "current"
    assert ask == {"fact_id": fact, "status": "failed", "refused": "scope", "message": decided_refusal(ASK)}
    assert scope.asks() == [] and [d.outcome for d in scope.decisions()] == ["revived"]


async def test_a_closed_fact_is_decided_and_takes_no_second_write(world):
    fact, ev = await stale(world, "a"), await evidence(world)
    scope = await scope_of(world, fact)
    # P2 revised (P2-2): the fact is closed by an invalidation on evidence (evidence no longer backs an archive)
    first = await scope.update_fact({"fact_id": fact, "updates": {"state": "invalidated"}, "basis": on(ev)})

    again = await scope.update_fact({"fact_id": fact, "updates": REVIVE, "basis": on(ev)})

    assert first["status"] == "updated", first
    assert again["refused"] == "scope" and again["message"] == decided_refusal(UPDATE)
    assert state_of(world, fact) == "invalidated"


async def test_a_follow_up_write_to_a_lineage_this_pass_made_current_passes(world):
    fact, ev = await stale(world, "a"), await evidence(world)
    scope = await scope_of(world, fact)

    first = await scope.update_fact(
        {"fact_id": fact, "updates": {"content": "lives in Springfield", "expiry": HORIZON}, "basis": on(ev)}
    )
    new_id = first["fact_id"]
    second = await scope.update_fact(
        {"fact_id": new_id, "updates": {"tags": ["home", "lisbon"], "expiry": HORIZON}, "basis": on(ev)}
    )

    assert first["status"] == "updated" and new_id != fact
    assert second["status"] == "updated" and second["fact_id"] != new_id, second
    assert state_of(world, fact) == "superseded" and state_of(world, new_id) == "superseded"
    assert state_of(world, second["fact_id"]) == "current"
    [decision] = scope.decisions()
    assert (decision.fact_id, decision.outcome, decision.written_id) == (fact, "rewritten", second["fact_id"])


async def test_a_follow_up_by_the_batch_id_lands_on_the_version_this_pass_wrote(world):
    fact, ev = await stale(world, "a"), await evidence(world)
    scope = await scope_of(world, fact)
    first = await scope.update_fact(
        {"fact_id": fact, "updates": {"content": "lives in Springfield", "expiry": HORIZON}, "basis": on(ev)}
    )

    # P2 revised (P2-2): the follow-up closes the version by an invalidation on evidence (no archive on evidence)
    second = await scope.update_fact({"fact_id": fact, "updates": {"state": "invalidated"}, "basis": on(ev)})

    assert second["status"] == "updated" and second["fact_id"] == first["fact_id"], second
    assert state_of(world, first["fact_id"]) == "invalidated"
    assert [d.outcome for d in scope.decisions()] == ["invalidated"]


# ------------------------------------------------------------------------------------------------ input

async def test_a_basis_inside_updates_is_honoured(world):
    fact, ev = await stale(world, "a"), await evidence(world)
    scope = await scope_of(world, fact)

    res = await scope.update_fact({"fact_id": fact, "updates": {**REVIVE, "basis": on(ev)}})

    assert res["status"] == "updated", res
    assert state_of(world, fact) == "current"
    assert last_reason(world, fact) == basis_reason("revive", ReviewBasis("evidence", evidence_fact_id=ev))
    assert [(d.outcome, d.basis_kind) for d in scope.decisions()] == [("revived", "evidence")]


async def test_a_top_level_basis_wins_over_one_inside_updates(world):
    fact, ev, other = await stale(world, "a"), await evidence(world), await evidence(world, "another neighbour")
    scope = await scope_of(world, fact)

    await scope.update_fact({"fact_id": fact, "updates": {**REVIVE, "basis": on(other)}, "basis": on(ev)})

    assert last_reason(world, fact) == basis_reason("revive", ReviewBasis("evidence", evidence_fact_id=ev))


async def test_the_evidence_date_moves_a_revived_facts_reported_date(world):
    fact = await stale(world, "a")
    later = NOW + timedelta(hours=1)
    ev = await world.seed("still takes it", reported_date=later)
    scope = await scope_of(world, fact)

    await scope.update_fact({"fact_id": fact, "updates": REVIVE, "basis": on(ev)})

    assert world.docs[fact]["reported_date"] == later


async def test_updates_that_are_not_an_object_are_refused_with_their_shape(world):
    fact = await stale(world, "a")
    scope = await scope_of(world, fact)

    res = await scope.update_fact({"fact_id": fact, "updates": "state current"})

    assert res["refused"] == "scope" and res["message"].startswith("update_fact.updates must be an object")
    assert state_of(world, fact) == "stale"


async def test_updates_the_parser_refuses_are_a_scope_refusal_with_the_parsers_text(world):
    fact = await stale(world, "plays tennis on Sundays")
    scope = await scope_of(world, fact)
    before = dict(world.docs[fact])

    result = await scope.update_fact({"fact_id": fact, "updates": {"state": "flying"}})

    assert {k: result[k] for k in ("fact_id", "status", "refused")} == {"fact_id": fact, "status": "failed", "refused": "scope"}
    assert result["message"].startswith("Invalid state 'flying'")
    assert world.docs[fact] == before
    assert scope.decisions() == [] and [f.id for f in scope.kept()] == [fact]


async def test_a_malformed_basis_is_a_basis_refusal(world):
    fact = await stale(world, "a")
    scope = await scope_of(world, fact)

    res = await scope.update_fact({"fact_id": fact, "updates": REVIVE, "basis": "the neighbour"})

    assert res["refused"] == "basis" and res["message"].startswith("basis must be an object")
    assert state_of(world, fact) == "stale" and scope.decisions() == []


async def test_a_malformed_expiry_keeps_the_expiry_tag(world):
    fact, ev = await stale(world, "a"), await evidence(world)
    scope = await scope_of(world, fact)

    res = await scope.update_fact({"fact_id": fact, "updates": {"state": "current", "expiry": {"kind": "soon"}},
                                   "basis": on(ev)})

    assert res["status"] == "failed" and res["refused"] == "expiry", res
    assert state_of(world, fact) == "stale"


async def test_a_sent_reported_date_gets_the_review_note(world):
    fact, ev = await stale(world, "a"), await evidence(world)
    scope = await scope_of(world, fact)
    stored = world.docs[fact]["reported_date"]

    res = await scope.update_fact(
        {"fact_id": fact, "updates": {**REVIVE, "reported_date": "2026-10-20T10:00:00+00:00"}, "basis": on(ev)}
    )

    assert res["status"] == "updated" and REVIEW_REPORTED_DATE_NOTE in res["note"], res
    assert kept_reported_date_note(stored, TZ) not in res["note"]


# ------------------------------------------------------------------------------------------------ state and outcome

async def test_a_call_that_leaves_a_stale_fact_stale_is_refused(world):
    fact = await stale(world, "a")
    scope = await scope_of(world, fact)
    before = dict(world.docs[fact])

    res = await scope.update_fact({"fact_id": fact, "updates": {"reported_date": "2026-10-20T10:00:00+00:00"}})

    assert res == {"fact_id": fact, "status": "failed", "refused": "scope", "message": STAYS_STALE_REFUSAL}
    assert world.docs[fact] == before and [f.id for f in scope.kept()] == [fact]


async def test_two_stale_duplicates_end_as_one_current_and_one_superseded(world):
    a, b, ev = await stale(world, "a"), await stale(world, "b"), await evidence(world)
    scope = await scope_of(world, a, b)

    revived = await scope.update_fact({"fact_id": a, "updates": REVIVE, "basis": on(ev)})
    closed = await scope.update_fact({"fact_id": b, "updates": {"state": "superseded", "superseded_by": a}})

    assert revived["status"] == "updated" and closed["status"] == "updated", (revived, closed)
    assert state_of(world, a) == "current" and state_of(world, b) == "superseded"
    assert world.docs[b]["superseded_by"] == a and last_reason(world, b) == superseded_reason(a)
    assert [(d.fact_id, d.outcome, d.basis_kind) for d in scope.decisions()] == [
        (a, "revived", "evidence"), (b, "superseded", None)
    ]


@pytest.mark.parametrize("updates, outcome, stored_state", [
    (REVIVE, "revived", "current"),
    ({"state": "current", "content": "lives in Springfield", "expiry": HORIZON}, "rewritten", "superseded"),
    # P2 revised (P2-1, P2-2): evidence backs a history rewrite, no archive; a history rewrite is a version
    ({"state": "current", "content": "The user lived in Lisbon until Sep 2026.", "expiry": {"kind": "none"},
      "as_history": True}, "rewritten", "superseded"),
    ({"state": "invalidated"}, "invalidated", "invalidated"),
])
async def test_the_outcome_comes_from_the_state_the_write_left(world, updates, outcome, stored_state):
    fact, ev = await stale(world, "a"), await evidence(world)
    scope = await scope_of(world, fact)

    res = await scope.update_fact({"fact_id": fact, "updates": updates, "basis": on(ev)})

    assert res["status"] == "updated", res
    [decision] = scope.decisions()
    assert decision.outcome == outcome and decision.written_id == res["fact_id"]
    assert state_of(world, fact) == stored_state and state_of(world, res["fact_id"]) in ("current", stored_state)


# ------------------------------------------------------------------------------------------------ changed outside

async def test_a_fact_closed_outside_the_review_is_left_with_no_search_in_the_text(world):
    fact, ev = await stale(world, "a"), await evidence(world)
    scope = await scope_of(world, fact)
    world.docs[fact]["state"] = FactState.ARCHIVED.value

    res = await scope.update_fact({"fact_id": fact, "updates": REVIVE, "basis": on(ev)})

    assert res == {"fact_id": fact, "status": "failed", "refused": "scope", "message": changed_outside_refusal(UPDATE)}
    assert "search" not in res["message"] and state_of(world, fact) == "archived"


async def test_a_fact_revived_outside_the_review_is_changed_not_written(world):
    fact, ev = await stale(world, "a"), await evidence(world)
    scope = await scope_of(world, fact)
    world.docs[fact]["state"] = FactState.CURRENT.value

    res = await scope.update_fact({"fact_id": fact, "updates": {"state": "archived"}, "basis": on(ev)})

    assert res["message"] == changed_outside_refusal(UPDATE) and state_of(world, fact) == "current"


async def test_a_conflict_on_the_write_reads_as_changed_outside(world, monkeypatch):
    fact, ev = await stale(world, "a"), await evidence(world)
    scope = await scope_of(world, fact)

    async def changed_meanwhile(stored, fields):
        world.docs[fact]["state"] = FactState.INVALIDATED.value
        raise FactWriteConflict(stored.fact.id)

    monkeypatch.setattr(world.store.repo, "update_fact_fields", changed_meanwhile)

    res = await scope.update_fact({"fact_id": fact, "updates": REVIVE, "basis": on(ev)})

    assert res == {"fact_id": fact, "status": "failed", "refused": "scope", "message": changed_outside_refusal(UPDATE)}
    assert "search" not in res["message"] and scope.decisions() == []


async def test_a_failure_with_nothing_changed_keeps_the_write_paths_message(world, monkeypatch):
    fact, ev = await stale(world, "a"), await evidence(world)
    scope = await scope_of(world, fact)

    async def broken(stored, fields):
        raise RuntimeError("store unavailable")

    monkeypatch.setattr(world.store.repo, "update_fact_fields", broken)

    res = await scope.update_fact({"fact_id": fact, "updates": REVIVE, "basis": on(ev)})

    assert res["status"] == "failed" and "refused" not in res and "store unavailable" in res["message"], res
    assert state_of(world, fact) == "stale" and scope.decisions() == []


# ------------------------------------------------------------------------------------------------ asks and kept

async def test_an_accepted_ask_is_held_with_its_batch_fact(world):
    a, b = await stale(world, "a"), await stale(world, "b")
    scope = await scope_of(world, a, b)

    res = await scope.ask_owner({"fact_id": a, "fallback": "archive", "noise": True})

    assert res["status"] == "asked" and res["fact_id"] == a
    [held] = scope.asks()
    assert (held.fact_id, held.fallback, held.noise) == (a, "archive", True)
    assert [f.id for f in scope.kept()] == [b] and state_of(world, a) == "stale"


async def test_malformed_ask_arguments_are_an_ask_refusal(world):
    fact = await stale(world, "a")
    scope = await scope_of(world, fact)

    res = await scope.ask_owner({"fact_id": fact})

    assert res["refused"] == "ask" and res["message"].startswith("ask_owner.fallback must be revive, archive or history")
    assert scope.asks() == []
