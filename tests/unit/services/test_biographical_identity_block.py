"""The baseline identity block (FACT_PROFILE_RFC §6 row 1, §9 PR 11): `BiographicalContextService` puts the current facts
whose profile says `user_identity == true` first, by priority rank then newest, and fills from all facts minus directives;
every cached item carries the fact's `primary_domain` and `sensitivity`.

The real service over the real repository on the evaluating Firestore fake with the declared indexes (a query no index
serves fails here); assertions are on what the refreshed context and the stored cache hold."""
import pytest

from src.domain.entities import ContextPriority, FactDomain
from src.domain.fact_domains import Domain
from src.services.biographical_context_service import BiographicalContextService
from tests.support.fact_kit import ACC, make_fact
from tests.support.fact_store import INDEXES_FILE, FactStore

BORN = "Alex Doe was born on 1980-01-01 in Springfield"
CITIZEN = "Alex Doe is a Portuguese citizen"
PARTNER = "Alex's partner is Clara Moreira"
YOGA = "Alex does morning yoga for 30 minutes"
VEGETARIAN = "Alex is vegetarian"
DIABETES = "Alex was diagnosed with type 2 diabetes in 2022"
UNION = "Alex joined the Acme employees' trade union in November 2026, dues 12 EUR a month"
TENNIS = "Alex plays tennis on Sundays"
DIRECTIVE = "Suggest only vegetarian recipes to Alex"


@pytest.fixture
def store(kit_db):
    return FactStore(indexes=INDEXES_FILE, kit_db=kit_db, vector_dimension={3, 768})


async def _put(store, text, *, priority="medium", age=0, identity=None, primary=None, sensitivity=None,
               domain=FactDomain.HEALTH, tags=None):
    await store.repo.add_fact(make_fact(
        text, age_days=age, context_priority=ContextPriority(priority), user_identity=identity,
        primary_domain=primary, sensitivity=sensitivity, domain=domain, tags=tags or []))


async def _profiled_account(store):
    """Three identity facts below two critical non-identity facts, an unprofiled critical fact and a directive."""
    await _put(store, BORN, priority="high", age=1, identity=True, primary=Domain.BIOGRAPHY, sensitivity="ordinary")
    await _put(store, CITIZEN, priority="critical", age=0, identity=True, primary=Domain.BIOGRAPHY,
               sensitivity="ordinary")
    await _put(store, PARTNER, priority="high", age=2, identity=True, primary=Domain.BIOGRAPHY, sensitivity="ordinary")
    await _put(store, VEGETARIAN, priority="critical", age=5, identity=False, primary=Domain.FOOD, sensitivity="ordinary")
    await _put(store, UNION, priority="critical", age=4, identity=False, primary=Domain.WORK, sensitivity="discreet")
    await _put(store, YOGA, priority="critical", age=6)                          # no profile at all
    await _put(store, DIRECTIVE, priority="critical", age=9, domain=FactDomain.AGENT_DIRECTIVE)


def _texts(items):
    return [item["text"] for item in items]


class TestIdentityBlock:

    async def test_identity_facts_come_first_by_priority_then_newest_then_the_fill(self, store):
        await _profiled_account(store)
        context = await BiographicalContextService(store.repo).refresh_context(ACC, facts_limit=10)
        assert _texts(context["facts"]) == [CITIZEN, PARTNER, BORN, YOGA, VEGETARIAN, UNION]

    async def test_a_full_identity_block_leaves_no_room_for_the_higher_ranked_non_identity_facts(self, store):
        await _profiled_account(store)
        context = await BiographicalContextService(store.repo).refresh_context(ACC, facts_limit=3)
        assert _texts(context["facts"]) == [CITIZEN, PARTNER, BORN]

    async def test_the_block_is_cut_at_the_limit_in_its_own_order(self, store):
        await _profiled_account(store)
        context = await BiographicalContextService(store.repo).refresh_context(ACC, facts_limit=2)
        assert _texts(context["facts"]) == [CITIZEN, PARTNER]

    async def test_an_unprofiled_fact_is_not_in_the_block_even_at_critical_priority(self, store):
        await _put(store, YOGA, priority="critical", age=3)
        await _put(store, BORN, priority="low", age=0, identity=True, primary=Domain.BIOGRAPHY, sensitivity="ordinary")
        context = await BiographicalContextService(store.repo).refresh_context(ACC, facts_limit=1)
        assert _texts(context["facts"]) == [BORN]

    async def test_a_non_identity_fact_reaches_the_cache_only_through_the_fill(self, store):
        await _put(store, TENNIS, priority="critical", age=3, identity=False, primary=Domain.SPORT,
                   sensitivity="ordinary")
        await _put(store, BORN, priority="low", age=0, identity=True, primary=Domain.BIOGRAPHY, sensitivity="ordinary")
        context = await BiographicalContextService(store.repo).refresh_context(ACC, facts_limit=5)
        assert _texts(context["facts"]) == [BORN, TENNIS]

    async def test_directives_are_never_in_the_fill_and_stay_in_their_own_list(self, store):
        await _profiled_account(store)
        context = await BiographicalContextService(store.repo).refresh_context(ACC, facts_limit=50)
        assert DIRECTIVE not in _texts(context["facts"]) + _texts(context["principles"])
        assert _texts(context["directives"]) == [DIRECTIVE]

    async def test_an_identity_fact_tagged_mindset_is_a_principle(self, store):
        await _put(store, VEGETARIAN, priority="high", identity=True, primary=Domain.FOOD, sensitivity="ordinary",
                   tags=["mindset"])
        await _put(store, BORN, priority="low", identity=True, primary=Domain.BIOGRAPHY, sensitivity="ordinary")
        context = await BiographicalContextService(store.repo).refresh_context(ACC, facts_limit=5)
        assert (_texts(context["facts"]), _texts(context["principles"])) == ([BORN], [VEGETARIAN])


class TestCachedItems:

    async def test_cached_items_carry_primary_domain_and_sensitivity(self, store):
        await _profiled_account(store)
        await _put(store, DIABETES, priority="low", age=7, identity=False, primary=Domain.HEALTH,
                   sensitivity="ordinary")
        await BiographicalContextService(store.repo).refresh_cache(ACC, facts_limit=10)
        cached = await store.repo.get_biographical_context_cached(ACC)
        by_text = {item["text"]: (item["primary_domain"], item["sensitivity"]) for item in cached}
        assert by_text[CITIZEN] == (Domain.BIOGRAPHY, "ordinary")
        assert by_text[UNION] == (Domain.WORK, "discreet")
        assert by_text[DIABETES] == (Domain.HEALTH, "ordinary")
        assert by_text[YOGA] == (None, None)

    async def test_cached_items_keep_the_fields_they_had(self, store):
        await _put(store, BORN, priority="high", identity=True, primary=Domain.BIOGRAPHY, sensitivity="ordinary")
        await BiographicalContextService(store.repo).refresh_cache(ACC)
        (item,) = await store.repo.get_biographical_context_cached(ACC)
        assert set(item) == {"id", "text", "domain", "primary_domain", "sensitivity", "tags", "context_priority",
                             "created_at"}
        assert (item["id"], item["domain"], item["context_priority"]) == (f"f-{BORN}", "health", "high")
