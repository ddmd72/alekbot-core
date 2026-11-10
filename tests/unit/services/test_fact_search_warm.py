"""Warming an account's snapshot: the load a user's agent build starts runs through the search's own guarded loader, so
a search that arrives during it waits and reads what it cached. Invented cast."""
import asyncio

from tests.unit.services.test_fact_search_service import World


def _count_loads(world):
    loads = []
    real = world.service._load

    async def slow(account_id):
        loads.append(account_id)
        await asyncio.sleep(0.02)
        return await real(account_id)

    world.service._load = slow
    return loads


async def test_a_search_arriving_during_the_warm_up_does_not_load_a_second_time():
    world = World()
    await world.seed("passport renewal")
    await world.baseline()
    loads = _count_loads(world)

    warming = asyncio.ensure_future(world.service.warm("acc-1"))
    await asyncio.sleep(0)  # the warm-up is under way
    found = await world.search(subject="passport renewal")
    await warming

    assert found == ["f-passport renewal"] and len(loads) == 1


async def test_a_search_after_the_warm_up_hits_the_cache():
    world = World()
    await world.seed("passport renewal")
    await world.baseline()
    loads = _count_loads(world)

    await world.service.warm("acc-1")
    await world.search(subject="passport renewal")

    assert len(loads) == 1


async def test_without_a_baseline_stamp_nothing_is_loaded():
    world = World()
    await world.seed("passport renewal")
    loads = _count_loads(world)

    await world.service.warm("acc-1")

    assert loads == []
