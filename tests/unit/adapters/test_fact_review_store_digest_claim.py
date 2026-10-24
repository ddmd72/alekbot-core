"""Facts S4 · T4b: the weekly digest is claimed once per ISO week per user, and a release frees the week again
(plan §2.2, `{prefix}fact_review_messages/{user_id}:digest:{iso_week}`). Since the T4b fix round the claim takes the
owner's local day and the caller's time; the week is the domain's `iso_week_of(day)`."""
from datetime import date

import pytest

from src.domain.fact_review_question import QUESTION_RETENTION
from tests.support.fact_review_world import ReviewWorld
from tests.support.review_rule_builders import NOW

W43 = date(2026, 10, 24)           # ISO week 2026-W43
W44 = date(2026, 10, 26)           # ISO week 2026-W44
W01 = date(2026, 1, 1)             # ISO week 2026-W01


@pytest.fixture
def world():
    return ReviewWorld()


async def test_the_digest_is_claimed_once_per_iso_week(world):
    store = world.review_store
    assert await store.claim_digest("u1", W43, NOW) is True
    assert await store.claim_digest("u1", W43, NOW) is False
    assert await store.claim_digest("u1", W44, NOW) is True
    assert await store.claim_digest("u2", W43, NOW) is True
    assert set(world.message_docs) == {"u1:digest:2026-W43", "u1:digest:2026-W44", "u2:digest:2026-W43"}


async def test_the_claim_document_names_its_user_and_kind_and_expires(world):
    await world.review_store.claim_digest("u1", W43, NOW)

    doc = world.message_docs["u1:digest:2026-W43"]
    assert set(doc) == {"user_id", "kind", "claimed_at", "purge_after"}
    assert (doc["user_id"], doc["kind"]) == ("u1", "digest")
    assert doc["claimed_at"] == NOW and doc["claimed_at"].tzinfo is not None
    assert doc["purge_after"] == doc["claimed_at"] + QUESTION_RETENTION


async def test_a_released_week_can_be_claimed_again_and_other_claims_stay(world):
    store = world.review_store
    await store.claim_digest("u1", W43, NOW)
    await store.claim_digest("u1", W44, NOW)

    await store.release_digest("u1", W43)

    assert set(world.message_docs) == {"u1:digest:2026-W44"}
    assert await store.claim_digest("u1", W43, NOW) is True
    assert await store.claim_digest("u1", W44, NOW) is False


async def test_releasing_a_week_never_claimed_is_no_error(world):
    await world.review_store.release_digest("u1", W01)
    assert world.message_docs == {}


async def test_an_empty_user_is_refused(world):
    with pytest.raises(ValueError):
        await world.review_store.claim_digest("", W43, NOW)
    with pytest.raises(ValueError):
        await world.review_store.release_digest("", W43)
    assert world.message_docs == {}
