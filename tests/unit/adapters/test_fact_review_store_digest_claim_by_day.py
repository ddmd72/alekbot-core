"""Facts S4 · T4b review L2, L5: the weekly digest is claimed by the owner's local day, keyed by the domain's ISO week,
stamped with the caller's time; once per week per user, and a release frees the week again."""
from datetime import date, datetime, timedelta

import pytest

from src.domain.fact_review_question import QUESTION_RETENTION
from tests.support.fact_review_world import ReviewWorld
from tests.support.review_rule_builders import NOW

SATURDAY = date(2026, 10, 24)          # ISO week 2026-W43
MONDAY = date(2026, 10, 19)            # the same week
NEXT_MONDAY = date(2026, 10, 26)       # 2026-W44


@pytest.fixture
def world():
    return ReviewWorld()


async def test_the_digest_is_claimed_once_per_iso_week_whatever_day_of_it(world):
    store = world.review_store
    assert await store.claim_digest("u1", MONDAY, NOW) is True
    assert await store.claim_digest("u1", SATURDAY, NOW) is False
    assert await store.claim_digest("u1", date(2026, 10, 25), NOW) is False
    assert await store.claim_digest("u1", NEXT_MONDAY, NOW) is True
    assert await store.claim_digest("u2", SATURDAY, NOW) is True
    assert set(world.message_docs) == {"u1:digest:2026-W43", "u1:digest:2026-W44", "u2:digest:2026-W43"}


async def test_a_day_in_january_is_keyed_by_its_iso_year(world):
    assert await world.review_store.claim_digest("u1", date(2027, 1, 1), NOW)
    assert set(world.message_docs) == {"u1:digest:2026-W53"}
    assert await world.review_store.claim_digest("u1", date(2026, 12, 28), NOW) is False


async def test_the_claim_is_stamped_with_the_callers_time_and_expires_from_it(world):
    await world.review_store.claim_digest("u1", SATURDAY, NOW)

    assert world.message_docs["u1:digest:2026-W43"] == {
        "user_id": "u1", "kind": "digest", "claimed_at": NOW, "purge_after": NOW + QUESTION_RETENTION,
    }


async def test_a_losing_claim_leaves_the_first_claim_as_it_was(world):
    await world.review_store.claim_digest("u1", SATURDAY, NOW)
    await world.review_store.claim_digest("u1", MONDAY, NOW + timedelta(hours=3))
    assert world.message_docs["u1:digest:2026-W43"]["claimed_at"] == NOW


async def test_a_release_by_any_day_of_the_week_frees_it_and_keeps_other_weeks(world):
    store = world.review_store
    await store.claim_digest("u1", SATURDAY, NOW)
    await store.claim_digest("u1", NEXT_MONDAY, NOW)

    await store.release_digest("u1", MONDAY)

    assert set(world.message_docs) == {"u1:digest:2026-W44"}
    assert await store.claim_digest("u1", SATURDAY, NOW) is True


async def test_releasing_a_week_never_claimed_is_no_error(world):
    await world.review_store.release_digest("u1", SATURDAY)
    assert world.message_docs == {}


async def test_the_claims_count_toward_purge_overdue_from_the_callers_time(world):
    await world.review_store.claim_digest("u1", SATURDAY, NOW)
    await world.review_store.claim_digest("u2", SATURDAY, NOW + timedelta(days=1))

    assert await world.review_store.purge_overdue(NOW + QUESTION_RETENTION) == 0
    assert await world.review_store.purge_overdue(NOW + QUESTION_RETENTION + timedelta(seconds=1)) == 1
    assert await world.review_store.purge_overdue(NOW + QUESTION_RETENTION + timedelta(days=2)) == 2


@pytest.mark.parametrize("call", [
    lambda s: s.claim_digest("", SATURDAY, NOW),
    lambda s: s.claim_digest("u1", datetime(2026, 10, 24, 9, 0, tzinfo=NOW.tzinfo), NOW),
    lambda s: s.claim_digest("u1", SATURDAY, datetime(2026, 10, 24, 9, 0)),
    lambda s: s.release_digest("", SATURDAY),
    lambda s: s.release_digest("u1", datetime(2026, 10, 24, 9, 0, tzinfo=NOW.tzinfo)),
])
async def test_an_empty_user_a_datetime_day_or_a_naive_time_is_refused(world, call):
    with pytest.raises(ValueError):
        await call(world.review_store)
    assert world.message_docs == {}
