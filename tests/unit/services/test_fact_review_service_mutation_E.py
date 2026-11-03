"""FactReviewService: a silent history question whose fact cannot be resolved closes resolved_elsewhere, and a failed
revive alert is a logged WARNING that does not raise."""
import logging

from src.domain.exceptions import FactResolutionError
from src.domain.fact_review_question import ClosedBy
from tests.support.fact_kit import ACC
from tests.support.fact_review_world import ReviewWorld
from tests.unit.services.test_fact_review_p2_silence import dated_fact, silent
from tests.unit.services.test_fact_review_selection import Rig, stored_question


async def test_a_silent_history_question_whose_fact_no_longer_resolves_closes_resolved_elsewhere():
    world = ReviewWorld()
    fact = await dated_fact(world)
    await silent(world, "qg", fact)
    await world.seed_state(account_id=ACC)

    async def gone(fact_id, account_id, accepts):
        raise FactResolutionError(f"fact {fact_id} was merged into another")

    world.repo.resolve_current = gone

    await Rig(world).run()

    assert (await stored_question(world, "qg")).closed_by is ClosedBy.RESOLVED_ELSEWHERE


async def test_a_failing_revive_alert_is_logged_and_never_raises(caplog):
    world = ReviewWorld()
    rig = Rig(world)

    async def broken(fact_id):
        raise RuntimeError("repo down")

    world.repo.get_fact_by_id = broken

    with caplog.at_level(logging.WARNING):
        await rig.service._revive_alert(ACC, ["f1"], world.clock())

    assert any("revive alert" in r.getMessage() and "repo down" in r.getMessage() for r in caplog.records)
