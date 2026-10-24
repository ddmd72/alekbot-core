"""Facts S4 · T4b: the weekly digest is claimed once per ISO week per user, and a release frees the week again
(plan §2.2, `{prefix}fact_review_messages/{user_id}:digest:{iso_week}`)."""
from datetime import datetime, timedelta, timezone

import pytest

from src.domain.fact_review_question import QUESTION_RETENTION
from tests.support.fact_review_world import ReviewWorld


@pytest.fixture
def world():
    return ReviewWorld()


async def test_the_digest_is_claimed_once_per_iso_week(world):
    store = world.review_store
    assert await store.claim_digest("u1", "2026-W43") is True
    assert await store.claim_digest("u1", "2026-W43") is False
    assert await store.claim_digest("u1", "2026-W44") is True
    assert await store.claim_digest("u2", "2026-W43") is True
    assert set(world.message_docs) == {"u1:digest:2026-W43", "u1:digest:2026-W44", "u2:digest:2026-W43"}


async def test_the_claim_document_names_its_user_and_kind_and_expires(world):
    before = datetime.now(timezone.utc)
    await world.review_store.claim_digest("u1", "2026-W43")

    doc = world.message_docs["u1:digest:2026-W43"]
    assert set(doc) == {"user_id", "kind", "claimed_at", "purge_after"}
    assert (doc["user_id"], doc["kind"]) == ("u1", "digest")
    assert before <= doc["claimed_at"] <= datetime.now(timezone.utc) + timedelta(seconds=1)
    assert doc["purge_after"] == doc["claimed_at"] + QUESTION_RETENTION


async def test_a_released_week_can_be_claimed_again_and_other_claims_stay(world):
    store = world.review_store
    await store.claim_digest("u1", "2026-W43")
    await store.claim_digest("u1", "2026-W44")

    await store.release_digest("u1", "2026-W43")

    assert set(world.message_docs) == {"u1:digest:2026-W44"}
    assert await store.claim_digest("u1", "2026-W43") is True
    assert await store.claim_digest("u1", "2026-W44") is False


async def test_releasing_a_week_never_claimed_is_no_error(world):
    await world.review_store.release_digest("u1", "2026-W01")
    assert world.message_docs == {}


@pytest.mark.parametrize("user_id,iso_week", [
    ("", "2026-W43"), ("u1", ""), ("u1", "2026-43"), ("u1", "2026-W00"), ("u1", "2026-W54"), ("u1", "2026-W4"),
    ("u1", "2026-10-24"), ("u1", "x2026-W43"),
])
async def test_an_empty_user_or_a_malformed_week_is_refused(world, user_id, iso_week):
    with pytest.raises(ValueError):
        await world.review_store.claim_digest(user_id, iso_week)
    with pytest.raises(ValueError):
        await world.review_store.release_digest(user_id, iso_week)
    assert world.message_docs == {}
