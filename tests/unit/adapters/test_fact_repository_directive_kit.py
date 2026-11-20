"""FactRepository kit, the directive read (FACT_PROFILE_RFC §9 row 13): `get_active_facts_ordered(directive_only=True)`
checked against the port's docstring on the evaluating Firestore fake with the index refusal ON (a query no declared index
serves fails here), and on the test database through the cloud gate. A directive is the stored `directive == true`; the
old `domain` value marks nothing.
"""
import pytest

from src.domain.entities import ContextPriority, FactState
from src.domain.fact_lifecycle import is_directive
from tests.support.fact_kit import ACC, DROP, OTHER, put_raw, make_fact
from tests.support.fact_store import INDEXES_FILE, FactStore


@pytest.fixture
def store(kit_db):
    return FactStore(indexes=INDEXES_FILE, kit_db=kit_db, vector_dimension={3, 768})


async def _seed(store):
    """Directives of the account by priority and age, plus everything the directive read must leave out."""
    for text, directive, priority, age in (
            ("Suggest only vegetarian recipes", True, "high", 1),
            ("Answer in English", True, "critical", 0),
            ("Suggest only vegetarian recipes, always", True, "high", 2),
            ("Alex plays tennis on Sundays", False, "critical", 3),
            ("Alex does morning yoga for 30 minutes", None, "critical", 4),
            ("Answer in English, always", True, "archival", 5)):
        entity = {"context_priority": ContextPriority(priority)} | ({} if directive is None else {"directive": directive})
        await store.repo.add_fact(make_fact(text, age_days=age, **entity))
    await store.repo.add_fact(make_fact("Answer in English to the other account", account=OTHER, directive=True))
    await store.repo.add_fact(make_fact("Suggest only vegetarian recipes, stale", directive=True, state=FactState.STALE))
    put_raw(store, "old-domain", "Suggest only vegetarian recipes, old marker", domain="agent_directive", directive=DROP)


async def test_directive_only_returns_the_directives_by_priority_then_newest_first(store):
    await _seed(store)
    got = await store.repo.get_active_facts_ordered(ACC, directive_only=True)
    assert [f.text for f in got] == ["Answer in English", "Suggest only vegetarian recipes, always", "Suggest only vegetarian recipes"]
    assert all(is_directive(f) for f in got)


async def test_directive_only_limit_keeps_the_head_of_the_order(store):
    await _seed(store)
    got = await store.repo.get_active_facts_ordered(ACC, directive_only=True, limit=2)
    assert [f.text for f in got] == ["Answer in English", "Suggest only vegetarian recipes, always"]


async def test_a_document_with_only_the_old_directive_domain_is_no_directive(store):
    await _seed(store)
    assert "old-domain" not in {f.id for f in await store.repo.get_active_facts_ordered(ACC, directive_only=True)}
    [old] = [f for f in await store.repo.get_active_facts_ordered(ACC) if f.id == "old-domain"]
    assert old.directive is False and not is_directive(old)


async def test_the_unfiltered_read_carries_the_flag(store):
    await _seed(store)
    flags = {f.text: f.directive for f in await store.repo.get_active_facts_ordered(ACC)}
    assert flags["Answer in English"] is True
    assert flags["Alex plays tennis on Sundays"] is False
    assert flags["Alex does morning yoga for 30 minutes"] is False


async def test_identity_only_and_directive_only_together_are_refused_before_any_read(store):
    with pytest.raises(ValueError):
        await store.repo.get_active_facts_ordered(ACC, identity_only=True, directive_only=True)
