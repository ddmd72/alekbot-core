"""get_legacy_facts on the evaluating Firestore fake (FactStore): the port's docstring for the one fact read that only an
owner-run export script calls. Account scope is the point: a read of one account must never return another's documents.
"""
import pytest

from tests.support.fact_kit import ACC, DROP, OTHER, put_raw
from tests.support.fact_store import INDEXES_FILE, FactStore


@pytest.fixture
def store():
    return FactStore(indexes=INDEXES_FILE)


def ids(facts):
    return sorted(f.id for f in facts)


async def test_returns_only_the_asked_accounts_legacy_facts(store):
    put_raw(store, "own-missing-domain", domain=DROP)
    put_raw(store, "own-null-domain", domain=None)
    put_raw(store, "foreign-missing-domain", account_id=OTHER, domain=DROP)
    put_raw(store, "foreign-null-domain", account_id=OTHER, domain=None)

    assert ids(await store.repo.get_legacy_facts(ACC)) == ["own-missing-domain", "own-null-domain"]
    assert ids(await store.repo.get_legacy_facts(OTHER)) == ["foreign-missing-domain", "foreign-null-domain"]


async def test_a_fact_with_a_domain_is_not_legacy(store):
    put_raw(store, "legacy", domain=DROP)
    put_raw(store, "taxonomised", domain="health")

    assert ids(await store.repo.get_legacy_facts(ACC)) == ["legacy"]


async def test_superseded_facts_are_skipped_and_a_missing_state_is_kept(store):
    put_raw(store, "current", domain=DROP, state="current")
    put_raw(store, "archived", domain=DROP, state="archived")
    put_raw(store, "no-state", domain=DROP, state=DROP)
    put_raw(store, "superseded", domain=DROP, state="superseded")

    assert ids(await store.repo.get_legacy_facts(ACC)) == ["archived", "current", "no-state"]


async def test_stops_at_the_limit(store):
    for i in range(5):
        put_raw(store, f"legacy-{i}", domain=DROP)

    assert len(await store.repo.get_legacy_facts(ACC, limit=3)) == 3
    assert len(await store.repo.get_legacy_facts(ACC)) == 5
