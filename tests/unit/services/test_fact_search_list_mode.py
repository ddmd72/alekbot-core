"""FactSearchService, list mode (list mode RFC §2, §8): an `all` phrase with a structural key makes no kNN call and
returns its whole set; the request returns the union of the sets and the fused `top` phrases, each fact once, naming
every phrase that found it; the `[FactSearch]` line marks each phrase's mode and a set's keys. Same world as the
scored-search tests; examples from the invented universe."""
import logging
from dataclasses import replace
from datetime import timedelta

from src.domain.fact_search_lists import CUT
from tests.unit.services.test_fact_search_service_scored import ACC, T1, World, embed, lines, query

CLARA = {"name": "Clara Moreira", "role": "partner", "name_keys": [], "role_keys": []}
FABIA = "Bought the 2015 Skoda Fabia"


async def _world() -> World:
    """A Skoda Fabia fact, more than CUT gym facts and a few car notes, so both the gym set and a top list overflow the
    cut."""
    world = World()
    await world.seed(FABIA, domain_keys=["transport"], primary_domain="transport", kinds=["ownership"],
                     label_vector=embed("ownership"))
    for n in range(CUT + 5):
        await world.seed(f"Iron Temple gym session {n}", domain_keys=["sport"], primary_domain="sport")
    for n in range(5):
        await world.seed(f"Skoda Fabia note {n}", domain_keys=["transport"], primary_domain="transport")
    await world.baseline()
    return world


def gym_set(**kw):
    return replace(query(subject="gym habits", kind="habit", domains=["sport"], **kw), scope="all")


async def test_a_set_phrase_makes_no_knn_call_and_embeds_no_kind():
    world = await _world()
    await world.phrases(gym_set())
    assert world.repo.named("nearest_fact_ids") == []
    assert world.embedder.batches == [(("gym habits",), "RETRIEVAL_QUERY")]


async def test_a_single_set_phrase_returns_its_whole_set_past_the_cut():
    world = await _world()
    result = await world.service.search_phrases(ACC, [gym_set()])
    assert len(result.facts) == CUT + 5 and {f.domain_keys[0] for f in result.facts} == {"sport"}
    assert set(result.phrase_matches) == {(0,)}


async def test_the_result_is_the_whole_set_then_the_fused_top_phrases_each_fact_once():
    world = await _world()
    first, second = query(subject="Skoda Fabia note 1"), query(subject="Skoda Fabia note 2")
    top_only = [f.id for f in (await world.service.search_phrases(ACC, [first, second])).facts]
    result = await world.service.search_phrases(ACC, [first, second, gym_set()])
    ids = [f.id for f in result.facts]
    gym = [i for i in ids if "gym" in i]
    assert len(ids) == len(set(ids)) and len(top_only) == CUT
    assert ids[:len(gym)] == gym and len(gym) == CUT + 5
    assert ids[len(gym):] == [i for i in top_only if i not in gym]
    matches = dict(zip(ids, result.phrase_matches))
    assert all(2 in matches[i] for i in gym)
    assert all(matches[i] and 2 not in matches[i] for i in ids[len(gym):])


async def test_a_fact_in_a_set_and_a_top_list_appears_once_naming_both_phrases():
    world = await _world()
    owned = replace(query(subject="Skoda Fabia", domains=["transport"], kinds=["ownership"]), scope="all")
    result = await world.service.search_phrases(ACC, [owned, query(subject=FABIA)])
    ids = [f.id for f in result.facts]
    assert ids[0] == f"f-{FABIA}" and ids.count(f"f-{FABIA}") == 1
    assert result.phrase_matches[0] == (0, 1)


async def test_without_a_subject_the_message_orders_the_set():
    world = await _world()
    q = replace(query(domains=["sport"]), scope="all", message="Iron Temple gym session 7")
    assert (await world.phrases(q))[0] == "f-Iron Temple gym session 7"


async def test_the_log_marks_each_phrase_mode_and_the_keys_of_a_set(caplog):
    world = await _world()
    clara = replace(query(people=["Clara"], domains=["family"]), scope="all")
    keyless = replace(query(subject="Skoda Fabia"), scope="all")
    with caplog.at_level(logging.INFO):
        await world.phrases(gym_set(), query(subject="Skoda Fabia"), clara, keyless)
    (line,) = lines(caplog)
    assert " modes=all[domains],top,all[domains+people],all>top " in line
    assert f" lists=set:{CUT + 5} " in line


async def test_an_all_phrase_without_a_key_runs_top_and_is_cut():
    world = await _world()
    result = await world.service.search_phrases(ACC, [replace(query(subject="Skoda Fabia"), scope="all")])
    assert len(result.facts) == CUT
    assert world.repo.named("nearest_fact_ids") != []


async def test_a_seeded_partner_fact_is_found_by_relation_in_a_set():
    world = await _world()
    await world.seed("Weekend in Coimbra with Clara", people=[CLARA], domain_keys=["travel"], primary_domain="travel")
    await world.baseline(T1 + timedelta(hours=1))
    q = replace(query(people=["my partner"]), scope="all")
    assert await world.phrases(q) == ["f-Weekend in Coimbra with Clara"]
