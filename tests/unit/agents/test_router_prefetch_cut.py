"""A scope=all search returns its whole list; the Router's prefetch still holds at most the user's
semantic_search_limit facts, in the search's order (fact profile RFC §5.4)."""
from src.domain.settings import SearchConfig

from tests.unit.agents.test_router_prefetch_v2 import _fact, _message, _router, _Search, _Slots


async def _routed(facts, prefetch_limit=None):
    router, coordinator = _router(_Slots(), _Search(facts))
    if prefetch_limit is not None:
        router.prefetch_limit = prefetch_limit
    await router.execute(_message())
    enriched = coordinator.route_message.call_args.args[0].context["enriched_context"]
    return [f["content"] for f in enriched["facts"]]


async def test_default_limit_is_the_system_default_and_keeps_order():
    facts = [_fact(f"Gym visit {i}") for i in range(SearchConfig.DEFAULT_SEMANTIC_SEARCH_LIMIT + 40)]
    got = await _routed(facts)
    assert got == [f"Gym visit {i}" for i in range(SearchConfig.DEFAULT_SEMANTIC_SEARCH_LIMIT)]
    assert len(got) == 30


async def test_user_override_larger_than_default_is_honoured():
    facts = [_fact(f"Gym visit {i}") for i in range(120)]
    got = await _routed(facts, prefetch_limit=75)
    assert got == [f"Gym visit {i}" for i in range(75)]
