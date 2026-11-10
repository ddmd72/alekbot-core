"""Two searches of one account that miss the cache together load the snapshot once: the one waiting for the lock finds
what the other cached (fact search, snapshot cache). Invented cast."""
import asyncio

from tests.unit.services.test_fact_search_service import World


async def test_a_search_waiting_for_the_load_of_another_reads_its_snapshot():
    world = World()
    await world.seed("alpha")
    await world.baseline()
    loads = 0
    real = world.service._load

    async def slow(account_id):
        nonlocal loads
        loads += 1
        await asyncio.sleep(0.01)
        return await real(account_id)
    world.service._load = slow

    first, second = await asyncio.gather(world.search(subject="alpha"), world.search(subject="alpha"))

    assert first == second == ["f-alpha"] and loads == 1
