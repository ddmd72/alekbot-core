"""A place the owner kept as written (fact profile RFC §4.7, §5.3): the query side adds `lp:<folded name>` for a named
city, so a query naming Springfield reaches a fact whose profile holds `lp:springfield` although the atlas lacks it."""
from tests.unit.services.test_fact_search_service import World


async def test_a_query_naming_springfield_reaches_the_fact_holding_its_local_key():
    world = World()
    await world.seed("springfield", place_keys=["lp:springfield", "co:portugal"])
    await world.seed("rome")
    await world.baseline()

    assert await world.search(places=[("Springfield", "")], domains=["nothing"]) == ["f-springfield"]
    assert await world.search(places=[("springfield", "Portugal")], domains=["nothing"]) == ["f-springfield"]


async def test_the_local_key_is_one_more_key_and_atlas_places_still_reach_their_facts():
    world = World()
    await world.seed("springfield", place_keys=["lp:springfield"])
    await world.seed("rome")
    await world.baseline()

    assert await world.search(places=[("Rome", "")], domains=["nothing"]) == ["f-rome"]
