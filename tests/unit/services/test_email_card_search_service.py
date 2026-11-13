"""EmailCardSearchService (email index v2 §5; search slice 4) on the real domain, the in-memory card index, an
in-test embedding service and an in-memory confirmed-people repository. Invented cast only; every card is built by
the card core."""
from datetime import date, timedelta
from typing import Dict, List, Optional, Sequence

import pytest

from src.adapters.in_memory_email_card_index import InMemoryEmailCardIndex
from src.domain.confirmed_people import ConfirmedPeople
from src.domain.email_search_query import MailSearchRequest, TimeSlot, parse_query_slots
from src.ports.confirmed_people_repository import ConfirmedPeopleRepository
from src.ports.embedding_service import EmbeddingService
from src.services.email_card_search_service import EmailCardSearchService
from tests.unit.domain.email_card_fixtures import PEOPLE
from tests.unit.domain.email_search_fixtures import everything, policy, school_news

USER, OTHER_USER, ACCOUNT = "usr-1", "usr-2", "acc-1"
TODAY = date(2026, 11, 13)
FLAT = [1.0, 0.0, 0.0]


class Embeddings(EmbeddingService):
    """Vectors the test sets per text; every other text gets the flat vector. `fail` makes every call raise."""

    def __init__(self) -> None:
        self.vectors: Dict[str, List[float]] = {}
        self.calls: List[str] = []
        self.fail = False

    async def get_embedding(self, text: str, task_type: str = "RETRIEVAL_DOCUMENT") -> List[float]:
        self.calls.append(text)
        if self.fail:
            raise RuntimeError("embedding service down")
        return self.vectors.get(text, FLAT)

    async def get_embeddings_batch(self, texts: List[str], task_type: str = "RETRIEVAL_DOCUMENT") -> List[List[float]]:
        return [await self.get_embedding(t, task_type) for t in texts]


class People(ConfirmedPeopleRepository):
    def __init__(self, people: Optional[ConfirmedPeople] = PEOPLE, broken: bool = False) -> None:
        self.people, self.broken = people, broken

    async def get(self, account_id: str) -> Optional[ConfirmedPeople]:
        if self.broken:
            raise ValueError("the stored list does not parse")
        return self.people

    async def replace(self, account_id: str, people: ConfirmedPeople, source: str) -> None:
        raise AssertionError("search never writes the list")


class World:
    def __init__(self, cards: Sequence = (), people: Optional[People] = None) -> None:
        self.index = InMemoryEmailCardIndex()
        self.embeddings = Embeddings()
        for card in cards or everything():
            self.index.add(USER, card, FLAT)
        self.service = EmailCardSearchService(self.index, self.embeddings, people or People())

    async def ask(self, text: str, today: date = TODAY, slots: Optional[dict] = None, user: str = USER):
        parsed = parse_query_slots(slots, text) if slots is not None else None
        return await self.service.search(user, ACCOUNT, MailSearchRequest(text, today, parsed))


@pytest.fixture
def world() -> World:
    return World()


def _school_cards(n: int) -> list:
    """n Example School mails of n different templates, one per day."""
    return [school_news(f"msg_b{i:02d}", f"Example School: topic words{'x' * i}", date(2026, 5, 1) + timedelta(days=i))
            for i in range(n)]


# -- ranked mode ----------------------------------------------------------------------------------------------------

async def test_an_invoice_id_finds_the_invoice_first(world):
    result = await world.ask("invoice FA-0137")
    assert result.mode == "ranked" and result.shown_ids == ("msg_invoice01",)
    assert 'invoice number "FA-0137" (printed)' in result.text


async def test_an_exact_key_slot_makes_it_an_about_hit_with_the_footer(world):
    result = await world.ask("invoice FA-0137", slots={"exact_keys": ["FA-0137"]})
    assert result.shown_ids == ("msg_invoice01",)
    assert result.text.splitlines()[-1] == "No other indexed mail matches all of these."


async def test_an_exact_key_slot_finds_the_invoice_despite_a_word_no_card_holds(world):
    without_slot = await world.ask("the hardware bill FA-0137")
    assert without_slot.mode == "nothing" and 'No indexed mail names "bill".' in without_slot.text
    result = await world.ask("the hardware bill FA-0137", slots={"exact_keys": ["FA-0137"]})
    assert result.shown_ids == ("msg_invoice01",)
    assert 'No indexed mail names "bill".' in result.text


async def test_a_name_and_a_type_word_find_the_booking(world):
    result = await world.ask("the Albergo Immaginario booking")
    assert result.shown_ids == ("msg_rome01",)
    assert 'organization "Albergo Immaginario" (printed)' in result.text


async def test_a_month_in_the_text_filters_every_list_to_that_month(world):
    result = await world.ask("what did Example School send in March", today=date(2026, 4, 10))
    assert result.shown_ids[0] == "msg_school_meeting"
    assert "msg_school_feb" not in result.ranked_ids and "msg_school_apr" not in result.ranked_ids
    assert set(result.ranked_ids) <= {"msg_school_meeting", "msg_policy01", "msg_receipt_2026-03-10"}


async def test_the_same_request_a_year_on_reads_march_as_the_latest_march_that_started(world):
    result = await world.ask("what did Example School send in March", today=date(2027, 2, 1))
    assert result.shown_ids[0] == "msg_school_meeting"       # March 2026 is still the latest March by 2027-02-01
    later = await world.ask("what did Example School send in March", today=date(2027, 4, 1))
    assert "msg_school_meeting" not in later.ranked_ids      # now March 2027


async def test_a_people_slot_finds_the_mail_that_names_the_daughter(world):
    result = await world.ask("about my daughter", slots={"people": ["my daughter"]})
    assert result.shown_ids == ("msg_school_meeting",)
    assert 'No indexed mail names "daughter".' in result.text


async def test_a_person_the_list_does_not_hold_is_searched_by_the_typed_name(world):
    result = await world.ask("Jamie", slots={"people": ["Jamie"]})
    assert result.shown_ids == ("msg_school_meeting",)
    typed = await World(people=People(None)).ask("Jamie", slots={"people": ["Jamie"]})
    assert typed.shown_ids == ("msg_school_meeting",)


async def test_an_unreadable_people_list_is_searched_as_no_list():
    result = await World(people=People(broken=True)).ask("about Jamie", slots={"people": ["Jamie"]})
    assert result.shown_ids == ("msg_school_meeting",)


async def test_a_label_slot_adds_the_cards_of_that_type(world):
    result = await world.ask("the paper from the insurer", slots={"doc_types": ["insurance_policy"]})
    assert "msg_policy01" in result.shown_ids


async def test_a_word_no_card_holds_is_named_with_the_count_of_the_rest(world):
    result = await world.ask("Example Air tickets")
    assert result.mode == "nothing" and result.shown_ids == ()
    lines = result.text.splitlines()
    assert lines[0] == "No indexed mail matches: for the index this answer is final."
    assert 'No indexed mail names "air".' in lines and 'No indexed mail names "tickets".' in lines
    assert "Without these words: 4 mails match the rest." in lines


async def test_a_nonsense_request_is_a_final_nothing(world):
    result = await world.ask("zzqx vvkk")
    assert result.mode == "nothing" and result.text.splitlines()[0].startswith("No indexed mail matches")
    assert (await world.ask("mail")).text == "No indexed mail matches: for the index this answer is final."


async def test_a_recard_in_progress_is_said_after_every_result(world):
    world.index.add_uncarded(USER, "msg_new1")
    world.index.add_uncarded(USER, "msg_new2")
    for text in ("invoice FA-0137", "zzqx", "mail from September 2026"):
        result = await world.ask(text)
        assert result.text.splitlines()[-1] == ("Re-carding in progress: 2 indexed mails still have old entries and "
                                                "are not searched here.")


async def test_more_matches_than_the_caps_say_how_many_are_shown_and_scope_all_shows_more():
    w = World([*everything(), *_school_cards(25)])
    top = await w.ask("Example School")
    assert len(top.shown_ids) == 10 and "of 28 matching mails." in top.text.splitlines()[-1]
    everything_ = await w.ask("Example School", slots={"scope": "all"})
    assert len(everything_.shown_ids) == 27 and "Showing" not in everything_.text    # 28 mails, one fold of two
    assert everything_.total == 28


async def test_the_meaning_list_adds_a_card_no_word_found():
    w = World()
    w.index.add(USER, policy(), [0.0, 0.0, 1.0])
    w.embeddings.vectors["mail"] = [0.0, 0.0, 1.0]
    result = await w.ask("mail")
    assert result.mode == "ranked" and result.shown_ids == ("msg_policy01",)
    assert "Closest by meaning only (no word of the request matched these cards):" in result.text


async def test_a_small_pool_or_a_failed_embedding_leaves_the_word_lists_alone():
    small = World([c for c in everything()[:5]])
    small.embeddings.vectors["Albergo booking"] = [0.0, 0.0, 1.0]
    assert (await small.ask("Albergo booking")).shown_ids == ("msg_rome01",)
    down = World()
    down.embeddings.fail = True
    assert (await down.ask("invoice FA-0137")).shown_ids == ("msg_invoice01",)


async def test_the_subject_slot_is_what_is_embedded(world):
    await world.ask("the paper about the lock", slots={"subject": "lock replacement"})
    assert world.embeddings.calls == ["lock replacement"]
    await world.ask("the invoice")
    assert world.embeddings.calls[-1] == "the invoice"


async def test_another_users_cards_are_never_found(world):
    assert (await world.ask("invoice FA-0137", user=OTHER_USER)).mode == "nothing"
    world.index.add(OTHER_USER, policy(), FLAT)
    assert "msg_policy01" not in (await world.ask("invoice FA-0137")).ranked_ids


async def test_a_hidden_card_is_never_found():
    from dataclasses import replace

    from src.domain.email_card import CardState
    from tests.unit.domain.email_search_fixtures import invoice
    w = World([replace(invoice(), state=CardState.EMPTY)])
    assert (await w.ask("invoice FA-0137")).mode == "nothing"


# -- list mode ------------------------------------------------------------------------------------------------------

async def test_a_named_month_lists_every_card_of_that_month_with_the_receipts_folded_and_no_embedding_call(world):
    result = await world.ask("mail from September 2026")
    assert result.mode == "list" and result.total == 3
    assert result.shown_ids == ("msg_receipt_2026-09-22", "msg_invoice01")
    assert result.folded_ids == {"msg_receipt_2026-09-22": ("msg_receipt_2026-09-08",)}
    assert result.text.splitlines()[0] == "3 indexed mails for September 2026, all shown, newest first."
    assert world.embeddings.calls == []


async def test_a_ukrainian_period_request_gives_the_same_list(world):
    english = await world.ask("mail from September 2026")
    ukrainian = await world.ask("листи за вересень", today=TODAY)
    assert ukrainian.text == english.text and world.embeddings.calls == []


async def test_a_list_reaches_a_mail_by_its_verified_event_month_too(world):
    result = await world.ask("mail from August 2025")
    assert result.mode == "list" and result.shown_ids == ("msg_rome01",)


async def test_a_period_with_no_mail_says_so(world):
    result = await world.ask("mail from January 2020")
    assert result.mode == "list" and result.text == "No indexed mail for January 2020." and result.shown_ids == ()


async def test_a_relative_period_comes_from_the_slot_and_lists_when_nothing_else_is_asked(world):
    slots = {"time": {"intent": "period", "from": "2026-09-01", "to": "2026-09-30"}}
    result = await world.ask("mail from last month", slots=slots)
    assert result.mode == "list" and result.shown_ids == ("msg_receipt_2026-09-22", "msg_invoice01")


async def test_a_period_with_a_topic_word_a_person_or_a_type_is_not_a_list(world):
    for text, slots in (("school mail from March", None), ("mail from March", {"people": ["Jamie"]}),
                        ("mail from March", {"doc_types": ["receipt"]})):
        assert (await world.ask(text, today=date(2026, 4, 10), slots=slots)).mode != "list"


async def test_a_three_year_period_is_fetched_in_batches_of_thirty_months(world):
    slots = {"time": {"intent": "past", "from": "2024-01-01", "to": "2026-12-31"}}
    result = await world.ask("mail from the last years", slots=slots)        # 36 month keys: the index refuses 31
    assert result.mode == "list" and result.total == 18
    assert result.text.splitlines()[0] == "18 indexed mails for 1 Jan 2024 – 31 Dec 2026, all shown, newest first."


async def test_a_year_lists_its_mail(world):
    result = await world.ask("mail from 2025")
    assert result.mode == "list" and result.total == 8        # seven receipts and the booking
