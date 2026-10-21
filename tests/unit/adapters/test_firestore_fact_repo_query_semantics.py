"""FirestoreFactRepository queries run against an evaluating Firestore fake (order, filters, projections, kNN)."""
from datetime import datetime, timedelta, timezone

import pytest

from src.domain.entities import FactDomain, FactEntity, FactState, FactType
from tests.support.fact_store import FactStore, embed

ACC = "acc-1"
T0 = datetime(2026, 1, 1, tzinfo=timezone.utc)


def _fact(text, *, account=ACC, domain=FactDomain.HEALTH, state=FactState.CURRENT, age_days=0, context=None):
    return FactEntity(
        account_id=account, created_by_user_id="usr", lineage_id=f"lin-{text}", text=text, type=FactType.EVENT,
        domain=domain, state=state, context=context,
        created_at=T0 + timedelta(days=age_days), valid_from=T0 + timedelta(days=age_days),
        vector=embed(text), tags_vector=embed("t" + text), metadata_vector=embed("m" + text),
        valid_to=None if state in (FactState.CURRENT, FactState.STALE) else T0,
        is_current=state != FactState.SUPERSEDED,
    )


@pytest.fixture
def store():
    return FactStore()


async def test_search_by_domain_returns_newest_current_facts_first_within_limit(store):
    for i in range(5):
        await store.repo.add_fact(_fact(f"health {i}", age_days=i))
    await store.repo.add_fact(_fact("work 9", domain=FactDomain.WORK, age_days=9))
    await store.repo.add_fact(_fact("poss 10", domain=FactDomain.POSSESSION, age_days=10))
    await store.repo.add_fact(_fact("archived health", state=FactState.ARCHIVED, age_days=20))
    await store.repo.add_fact(_fact("foreign health", account="acc-2", age_days=30))
    got = await store.repo.search_facts_by_domain(["health", "work"], limit=3, account_id=ACC)
    assert [f.text for f in got] == ["work 9", "health 4", "health 3"]


async def test_add_if_unique_adds_text_equal_to_an_archived_or_invalidated_fact(store):
    await store.repo.add_fact(_fact("old claim", state=FactState.ARCHIVED))
    await store.repo.add_fact(_fact("dead claim", state=FactState.INVALIDATED))
    for text in ("old claim", "dead claim"):
        added, _ = await store.repo.add_fact_if_unique(_fact(text))
        assert added is True
    assert len(store.docs) == 4


async def test_add_if_unique_skips_text_equal_to_a_current_fact(store):
    existing = _fact("drinks green tea")
    await store.repo.add_fact(existing)
    added, dup_id = await store.repo.add_fact_if_unique(_fact("drinks green tea"))
    assert added is False and dup_id == existing.id
    assert len(store.docs) == 1


async def test_add_if_unique_ignores_current_facts_of_another_account(store):
    await store.repo.add_fact(_fact("drinks green tea", account="acc-2"))
    added, _ = await store.repo.add_fact_if_unique(_fact("drinks green tea"))
    assert added is True


async def test_vector_search_returns_current_facts_with_context_populated(store):
    await store.repo.add_fact(_fact("has a dog named Rex", context="said while walking"))
    await store.repo.add_fact(_fact("has a dog named Rex (old)", state=FactState.ARCHIVED, context="stale"))
    await store.repo.add_fact(_fact("foreign dog", account="acc-2", context="x"))
    got = await store.repo.search_facts(embed("has a dog named Rex"), "vector", limit=5, user_id="usr", account_id=ACC)
    assert [f.text for f in got] == ["has a dog named Rex"]
    assert got[0].context == "said while walking"
    assert got[0].state == FactState.CURRENT
