"""The digest's people line (fact profile RFC §7, OD-5 a): the language-neutral last section of `digest_message`, and
`FactReviewService` adding it from the people cards when a digest is sent. Rig and world of the run-skeleton and digest
tests; examples come from the invented universe."""
from typing import List, Sequence

import pytest

from src.domain.fact_review_digest import digest_message
from src.domain.fact_review_language import CANONICAL_LABELS
from src.domain.person_cards import PersonCard
from src.ports.person_card_repository import PersonCardRepository
from src.services.fact_review_service import FactReviewService
from tests.support.fact_kit import ACC
from tests.support.fact_review_world import ReviewWorld
from tests.unit.services.test_fact_review_digest import closed, digests, due_world
from tests.unit.services.test_fact_review_selection import USER, Rig
from tests.unit.services.test_fact_review_service_run import _kwargs_of

LABELS = dict(CANONICAL_LABELS)


def card(display: str, count: int) -> PersonCard:
    return PersonCard(display.lower().replace(" ", "_"), display, (display,), (), None, count)


class Cards(PersonCardRepository):
    def __init__(self, *held: PersonCard, fail: bool = False) -> None:
        self.held, self.fail, self.listed = list(held), fail, 0

    async def list_cards(self, account_id):
        self.listed += 1
        if self.fail:
            raise RuntimeError("store down")
        return list(self.held)

    async def replace_cards(self, account_id: str, cards: Sequence[PersonCard]) -> None:
        raise AssertionError("the digest only reads")


def with_cards(world: ReviewWorld, cards) -> FactReviewService:
    """The digest rig's service, rebuilt from its own parts (the run-skeleton tests' `_kwargs_of`) with the card
    repository (or none)."""
    return FactReviewService(**_kwargs_of(Rig(world)), run_timeout_s=600, clock=world.clock, person_cards=cards)


async def send(world: ReviewWorld, cards) -> List[str]:
    service = with_cards(world, cards)
    await service.run(USER, ACC, "daily")
    return digests(world)


# ------------------------------------------------------------------------------------------------ the domain section

def test_the_people_section_is_last_and_untranslated():
    text = digest_message(LABELS, [], histories=["Alex plans a weekend in Coimbra"], people=["Maria Doe — 11", "Clara Moreira — 10"])
    sections = text.split("\n\n")
    assert sections[-1] == "👥 Maria Doe — 11; Clara Moreira — 10"
    assert len(sections) == 2


def test_the_section_is_left_out_when_no_people_are_given():
    assert "👥" not in digest_message(LABELS, [], histories=["Alex plans a weekend in Coimbra"])
    assert "👥" not in digest_message(LABELS, [], histories=["Alex plans a weekend in Coimbra"], people=[])


def test_people_alone_never_make_a_digest():
    with pytest.raises(ValueError):
        digest_message(LABELS, [], people=["Maria Doe — 11"])


# ------------------------------------------------------------------------------------------------ the service

async def test_a_sent_digest_ends_with_the_people_line_most_facts_first_then_by_name():
    world = await due_world()
    await closed(world, "Alex keeps a weight log")
    cards = Cards(card("Hanna Doe", 10), card("Maria Doe", 12), card("Clara Moreira", 10))

    [text] = await send(world, cards)

    assert text.split("\n\n")[-1] == "👥 Maria Doe — 12; Clara Moreira — 10; Hanna Doe — 10"
    assert "weight log" in text


async def test_cards_alone_send_nothing_and_are_not_read():
    world = await due_world()        # due, but nothing was closed since the last digest
    cards = Cards(card("Maria Doe", 12))
    assert await send(world, cards) == []
    assert cards.listed == 0


async def test_the_digest_goes_without_the_line_when_the_cards_cannot_be_read():
    world = await due_world()
    await closed(world, "Alex keeps a weight log")

    [text] = await send(world, Cards(card("Maria Doe", 12), fail=True))

    assert "👥" not in text and "weight log" in text


async def test_the_digest_goes_without_the_line_when_no_repository_is_wired():
    world = await due_world()
    await closed(world, "Alex keeps a weight log")

    [text] = await send(world, None)

    assert "👥" not in text and "weight log" in text


async def test_no_cards_means_no_line():
    world = await due_world()
    await closed(world, "Alex keeps a weight log")
    [text] = await send(world, Cards())
    assert "👥" not in text
