"""FactSearchService.search_phrases: per returned fact, the phrases whose own kept top list holds it. Same
world as the scored-search tests; examples from the invented universe."""
from src.domain.fact_search_lists import CUT, DEPTH_KNN
from tests.unit.services.test_fact_search_service_scored import ACC, World, query

COIMBRA = "Weekend in Coimbra with Clara Mar 6-8"
BIRTHDAY = "Clara's birthday is on 07-14"


async def _crowded_world() -> World:
    """The two facts the phrases ask for among more than CUT others, so a phrase's list is cut."""
    world = World()
    await world.seed(COIMBRA, domain_keys=["travel"], primary_domain="travel")
    await world.seed(BIRTHDAY, domain_keys=["family"], primary_domain="family")
    for n in range(CUT + 10):
        await world.seed(f"Car service {n} at the Springfield garage")
    await world.baseline()
    return world


async def test_phrase_indices_of_a_fact_found_by_two_phrases_and_one_found_by_only_the_second():
    world = await _crowded_world()
    first, second = query(subject=COIMBRA), query(subject=BIRTHDAY)
    own = [{f.id for f in (await world.service.search(ACC, q))[:CUT]} for q in (first, second)]
    result = await world.service.search_phrases(ACC, [first, second])
    matches = {f.id: m for f, m in zip(result.facts, result.phrase_matches)}
    for fact_id, got in matches.items():
        assert got == tuple(n for n, ids in enumerate(own) if fact_id in ids)
    assert len(matches) == len(result.facts)
    assert {m for m in matches.values()} >= {(0,), (0, 1), (1,)}
    by_text = {f.text: m for f, m in zip(result.facts, result.phrase_matches)}
    assert by_text[COIMBRA][0] == 0 and by_text[BIRTHDAY][-1] == 1


async def test_one_phrase_tags_every_returned_fact_with_it():
    world = await _crowded_world()
    result = await world.service.search_phrases(ACC, [query(subject=COIMBRA)])
    assert result.facts and set(result.phrase_matches) == {(0,)}


async def test_the_log_shows_each_lists_size_before_its_cap(caplog):
    import logging
    world = World()
    for n in range(DEPTH_KNN + 15):
        await world.seed(f"Car service {n} at the Springfield garage")
    await world.baseline()
    with caplog.at_level(logging.INFO):
        await world.service.search_phrases(ACC, [query(subject="Car service 3 at the Springfield garage")])
    line = next(r.getMessage() for r in caplog.records if r.getMessage().startswith("[FactSearch]"))
    assert f"semantic:{DEPTH_KNN}/{DEPTH_KNN + 15}" in line
