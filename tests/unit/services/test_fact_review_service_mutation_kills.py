"""FactReviewService: best-effort handlers (counters, digest claim release) log a WARNING and never raise."""
import logging
from datetime import date

from src.domain.fact_lifecycle_day import CounterField
from src.services.fact_review_service import _Tally
from tests.support.fact_kit import ACC
from tests.support.fact_review_world import ReviewWorld
from tests.unit.services.test_fact_review_selection import Rig


async def test_a_failing_counter_write_is_logged_as_a_warning_and_never_raises(caplog):
    world = ReviewWorld()
    rig = Rig(world)
    field = next(iter(CounterField))
    tally = _Tally()
    tally.count(field)

    async def broken(account_id, day, counts):
        raise RuntimeError("counters down")

    world.counters.add = broken

    with caplog.at_level(logging.WARNING):
        await rig.service._count(ACC, world.clock(), tally)

    assert any(
        r.levelno == logging.WARNING and "not counted" in r.getMessage() and field.value in r.getMessage()
        for r in caplog.records
    )


async def test_a_failing_digest_claim_release_is_logged_as_a_warning_and_never_raises(caplog):
    world = ReviewWorld()
    rig = Rig(world)

    async def broken(user_id, day):
        raise RuntimeError("store down")

    world.review_store.release_digest = broken

    with caplog.at_level(logging.WARNING):
        await rig.service._release_digest("user-1", date(2026, 1, 5))

    assert any(
        r.levelno == logging.WARNING and "not released" in r.getMessage() and "store down" in r.getMessage()
        for r in caplog.records
    )
