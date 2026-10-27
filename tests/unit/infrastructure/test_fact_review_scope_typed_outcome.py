"""`FactReviewScope` reads the typed write path's outcome (Facts S4 · T8e, T3c review M2): what it records comes from
`FactUpdateOutcome`, and the dict it hands the model is that outcome rendered by `update_result`, nothing else.

The writer is the real adapter on the review world, wrapped to keep the outcomes it returned; the cases that need one
refusal kind on a fact in a given state script the outcome instead."""
import json
from datetime import timedelta

from src.domain.entities import ExpiryKind, FactState
from src.domain.fact_review_batch import ReviewBatch
from src.domain.fact_review_rules import UPDATE_FACT_PLACE, changed_outside_refusal
from src.domain.fact_tool_result import update_result
from src.domain.fact_update_outcome import FactUpdateOutcome, UpdateRefusal
from src.infrastructure.fact_review_scope import FactReviewScope
from src.ports.fact_update_port import FactUpdatePort
from tests.support.fact_kit import ACC
from tests.support.fact_lifecycle_world import NOW
from tests.support.fact_review_world import ReviewWorld

TZ = "Europe/Lisbon"
REVIVE = {"state": "current", "expiry": {"kind": "horizon", "days": 30}}


class KeptOutcomes(FactUpdatePort):
    """The real typed write path, keeping each outcome it returned; `script` answers instead, writing nothing."""

    def __init__(self, inner: FactUpdatePort) -> None:
        self._inner = inner
        self.outcomes: list = []
        self.script = None

    async def apply_fact_update(self, fact_id, update, account_id, *, context, evidence_at=None, reason=None,
                                history=None, expected_id=None):
        outcome = self.script(fact_id) if self.script is not None else await self._inner.apply_fact_update(
            fact_id, update, account_id, context=context, evidence_at=evidence_at, reason=reason, history=history)
        self.outcomes.append(outcome)
        return outcome


async def setup(world):
    fact = await world.seed_stale("a", expiry_kind=ExpiryKind.HORIZON, ttl_days=14, reported_date=NOW - timedelta(days=60))
    ev = await world.seed("moved to Lisbon in October", reported_date=NOW - timedelta(days=1))
    writer = KeptOutcomes(world.store.adapter)
    batch = ReviewBatch((await world.repo.get_fact_by_id(fact),), ())
    return fact, ev, writer, FactReviewScope(batch, ACC, TZ, world.now, world.repo, writer)


def on(fact_id):
    return {"kind": "evidence", "evidence_fact_id": fact_id}


async def test_a_written_update_is_recorded_from_the_outcome_and_handed_over_rendered():
    world = ReviewWorld()
    fact, ev, writer, scope = await setup(world)

    res = await scope.update_fact({"fact_id": fact, "updates": {**REVIVE, "content": "lives in Lisbon"}, "basis": on(ev)})

    [outcome] = writer.outcomes
    assert isinstance(outcome, FactUpdateOutcome) and outcome.written and outcome.written_id != fact
    assert json.dumps(res) == json.dumps(update_result(outcome))
    [decision] = scope.decisions()
    assert decision.written_id == outcome.written_id and world.docs[outcome.written_id]["text"] == "lives in Lisbon"


async def test_a_refused_write_on_an_unchanged_fact_is_the_rendered_refusal():
    world = ReviewWorld()
    fact, ev, writer, scope = await setup(world)
    writer.script = lambda fact_id: FactUpdateOutcome.refused(fact_id, UpdateRefusal.CONFLICT, "fact changed; search again")

    res = await scope.update_fact({"fact_id": fact, "updates": REVIVE, "basis": on(ev)})

    assert res == {"fact_id": fact, "status": "failed", "message": "fact changed; search again"}
    assert scope.decisions() == [] and world.docs[fact]["state"] == "stale"


async def test_an_expiry_refusal_is_handed_over_even_when_the_fact_changed():
    world = ReviewWorld()
    fact, ev, writer, scope = await setup(world)

    def refused_after_a_change(fact_id):
        world.docs[fact]["state"] = FactState.ARCHIVED.value
        return FactUpdateOutcome.refused(fact_id, UpdateRefusal.BORN_DUE, "already due")

    writer.script = refused_after_a_change

    res = await scope.update_fact({"fact_id": fact, "updates": REVIVE, "basis": on(ev)})

    assert res == {"fact_id": fact, "status": "failed", "refused": "expiry", "born_due": True, "message": "already due"}


async def test_another_refusal_on_a_changed_fact_reads_as_changed_outside():
    world = ReviewWorld()
    fact, ev, writer, scope = await setup(world)

    def refused_after_a_change(fact_id):
        world.docs[fact]["state"] = FactState.ARCHIVED.value
        return FactUpdateOutcome.refused(fact_id, UpdateRefusal.UNRESOLVED, "chain ends elsewhere")

    writer.script = refused_after_a_change

    res = await scope.update_fact({"fact_id": fact, "updates": REVIVE, "basis": on(ev)})

    assert res == {
        "fact_id": fact, "status": "failed", "refused": "scope", "message": changed_outside_refusal(UPDATE_FACT_PLACE),
    }
