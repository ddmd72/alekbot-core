"""The people-card step of `FactProfileService.run` (fact profile RFC §4.4, §7), on PR 4's service rig: cards are rebuilt
after the writes and count this run's profiles; a fact that left `current` lowers a count; a failing card store leaves the
run's counts; with no repository there is no read. Examples come from the invented universe."""
import logging
from typing import List, Sequence

import pytest

from src.domain.entities import FactState
from src.domain.fact_profile_answer import ProfileAnswer
from src.domain.person_cards import PersonCard
from src.ports.person_card_repository import PersonCardRepository
from tests.support.fact_kit import ACC
from tests.unit.services.test_fact_profile_service import World, answer, ok

MARIA_FACTS = [f"Maria Doe fact number {i}" for i in range(10)]


class Cards(PersonCardRepository):
    def __init__(self, fail: bool = False) -> None:
        self.stored: List[PersonCard] = []
        self.replaced = 0
        self.fail = fail

    async def list_cards(self, account_id):
        return list(self.stored)

    async def replace_cards(self, account_id: str, cards: Sequence[PersonCard]) -> None:
        if self.fail:
            raise RuntimeError("store down")
        self.replaced += 1
        self.stored = list(cards)


def maria() -> ProfileAnswer:
    return answer(people=[{"name": "Maria Doe", "role": "mother", "source": "Maria Doe"}])


def cards_world(person_cards, **over) -> World:
    world = World()
    world.service._person_cards = person_cards
    return world


async def seed_maria(world: World, count: int = 10) -> None:
    for text in MARIA_FACTS[:count]:
        await world.seed(text)
        world.profiler.replies[text] = ok(maria())


async def test_the_cards_are_rebuilt_after_the_writes_and_count_this_runs_profiles():
    cards = Cards()
    world = cards_world(cards)
    await seed_maria(world)

    run = await world.run()

    assert run.written == 10 and run.cards == 1 and not run.error
    (card,) = cards.stored
    assert (card.card_key, card.display, card.fact_count, card.relation) == ("doe_mari", "Maria Doe", 10, "mother")


async def test_nine_profiled_facts_make_no_card_and_the_set_is_still_replaced():
    cards = Cards()
    cards.stored = [PersonCard("old", "Old", ("Old",), (), None, 10)]
    world = cards_world(cards)
    await seed_maria(world, 9)

    run = await world.run()

    assert run.cards == 0 and cards.stored == [] and cards.replaced == 1


async def test_a_fact_leaving_current_lowers_a_count_even_when_nothing_is_written():
    cards = Cards()
    world = cards_world(cards)
    await seed_maria(world)
    await world.run()
    assert len(cards.stored) == 1

    world.docs[f"f-{MARIA_FACTS[0]}"]["state"] = FactState.ARCHIVED.value
    run = await world.run()

    assert run.written == 0 and run.cards == 0 and cards.stored == []


async def test_a_raising_card_store_is_logged_and_the_runs_counts_stay(caplog):
    world = cards_world(Cards(fail=True))
    await seed_maria(world)

    with caplog.at_level(logging.ERROR):
        run = await world.run()

    assert run.written == 10 and run.cards == 0 and not run.error and run.refreshed
    assert any("cards failed" in r.getMessage() for r in caplog.records)
    assert MARIA_FACTS[0] not in caplog.text and "Maria" not in caplog.text


async def test_the_run_logs_counts_only(caplog):
    world = cards_world(Cards())
    await seed_maria(world)
    with caplog.at_level(logging.INFO):
        await world.run()
    line = next(r.getMessage() for r in caplog.records if "] cards " in r.getMessage())
    assert line == f"[FactProfile] cards account={ACC[:8]} cards=1 ambiguous=0"


async def test_without_a_card_repository_the_people_are_not_read():
    world = World()
    reads: List[str] = []
    real = world.repo.list_profile_people

    async def spy(account_id):
        reads.append(account_id)
        return await real(account_id)

    world.repo.list_profile_people = spy      # type: ignore[method-assign]
    await seed_maria(world)

    run = await world.run()

    assert run.written == 10 and run.cards == 0 and reads == []
