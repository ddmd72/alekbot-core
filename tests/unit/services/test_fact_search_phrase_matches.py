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


async def test_an_all_phrase_lifts_the_cut_only_for_its_own_matches():
    # List mode RFC §2: the result is the `all` phrase's whole set plus the `top` phrases fused and cut among themselves;
    # a fact outside the set is returned only from within that cut.
    from dataclasses import replace
    world = World()
    for n in range(CUT + 20):
        await world.seed(f"Car service {n} at the Springfield garage", domain_keys=["vehicles"],
                         primary_domain="vehicles")
    for n in range(CUT + 20):
        await world.seed(f"Car wash {n} at the Springfield garage")
    await world.baseline()
    plain = [query(subject="Car service 1 at the Springfield garage"),
             query(subject="Car wash 2 at the Springfield garage")]
    everything = replace(query(subject="Car service 3 at the Springfield garage", domains=["vehicles"]), scope="all")
    only_all = await world.service.search_phrases(ACC, [everything])
    mixed = await world.service.search_phrases(ACC, [*plain, everything])
    capped = await world.service.search_phrases(ACC, plain)
    own = {f.id for f in only_all.facts}
    top = {f.id for f in capped.facts}
    outside = {f.id for f in mixed.facts} - own
    assert len(only_all.facts) == CUT + 20 and len(capped.facts) == CUT
    assert outside and outside <= top
    assert {f.id for f in mixed.facts} == own | top and len(mixed.facts) == len(own | top)


async def test_a_single_all_phrase_returns_every_fact_its_key_matches_and_a_keyless_one_is_cut():
    # List mode RFC §3.1 and §3.4: a key defines the set, returned whole; an `all` phrase with only a subject runs `top`.
    from dataclasses import replace
    world = World()
    for n in range(CUT + 20):
        await world.seed(f"Car service {n} at the Springfield garage")
    await world.baseline()
    keyed = await world.service.search_phrases(
        ACC, [replace(query(subject="Car service 3 at the Springfield garage", domains=["family"]), scope="all")])
    assert len(keyed.facts) == CUT + 20
    keyless = await world.service.search_phrases(
        ACC, [replace(query(subject="Car service 3 at the Springfield garage"), scope="all")])
    assert len(keyless.facts) == CUT
