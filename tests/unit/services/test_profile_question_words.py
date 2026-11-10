"""The owner's words to a profile question, read by code and recorded (fact profile RFC §4.7, PR 5e): a place, a person
and a date reply is recorded in the account's answers store and closes the question (owner, no answer kind, source
`words`); a reply that is none of the forms read leaves the question open with nothing recorded and no `reply_text`. On the
review world with the real review store. Invented cast."""
from typing import Optional

import pytest

from src.domain.confirmed_people import ConfirmedPeople, parse_confirmed_people
from src.domain.fact_review_question import ClosedBy
from src.domain.language import LanguageCode
from src.domain.place_lookup import PlaceRow
from src.domain.place_names import PlaceNames
from src.domain.profile_answers import DateAnswer, PersonAnswer, PlaceAnswer
from src.domain.ui_messages import UIMessage
from src.ports.confirmed_people_repository import ConfirmedPeopleRepository
from src.services.fact_answer_service import FactAnswerService
from src.services.fact_question_texts import FactQuestionTexts
from src.services.profile_question_answers import ProfileQuestionAnswers
from tests.support.fact_review_world import ReviewWorld
from tests.unit.services.test_profile_question_answers import ACC, Answers, FixedZones, ack, ask, reply

# Quoted facts that name no user: the reading of the US row and of a local place is tested on them, so no test says
# Alex's trip is anywhere but Italy; "Jamie" answers the daughter's school.
NO_PERSON = "A conference in Rome"
DAUGHTER = "The daughter goes to Example School"
ATLAS_NAMES = PlaceNames([
    PlaceRow(1.0, 1.0, "Rome", "Rome", "United States", 10, country_names=("United States",), geonameid="2000"),
    PlaceRow(2.0, 2.0, "Rome", "Roma", "Italy", 1000, country_names=("Italia", "Italy"), geonameid="1000"),
])
PEOPLE = parse_confirmed_people({
    "user": {"name": "Alex Doe", "forms": ["Alex"]},
    "people": [{"name": "Jamie Doe", "forms": ["Jamie"], "relation": "daughter"}]})


class Lists(ConfirmedPeopleRepository):
    def __init__(self, people: Optional[ConfirmedPeople] = PEOPLE) -> None:
        self.people = people

    async def get(self, account_id: str) -> Optional[ConfirmedPeople]:
        return self.people

    async def replace(self, account_id: str, people: ConfirmedPeople, source: str) -> None:
        self.people = people


async def atlas():
    return ATLAS_NAMES, "atlas-1"


def service(world: ReviewWorld, store: Answers, *, with_atlas: bool = True, with_list: bool = True) -> FactAnswerService:
    return FactAnswerService(
        fact_updates=world.store.adapter, repo=world.repo, review_store=world.review_store, counters=world.counters,
        task_queue=world.queue, session_store=world.session_store,
        texts=FactQuestionTexts(world.language, world.localization, world.review_store, world.counters,
                                LanguageCode.EN, clock=world.clock),
        interactive=world.interactive, owner_zones=FixedZones(), clock=world.clock,
        profile_answers=ProfileQuestionAnswers(
            store, atlas=atlas if with_atlas else None, confirmed_people=Lists() if with_list else None))


async def closed_by_words(world: ReviewWorld, question_id: str) -> None:
    doc = world.question_docs[question_id]
    assert doc["status"] == "closed" and doc["closed_by"] == ClosedBy.OWNER.value
    assert doc["answer"] is None and doc["answer_source"] == "words" and doc["reply_text"] is None


async def test_a_city_and_a_country_the_atlas_places_in_one_row_records_that_row_and_closes_the_question():
    world, store = ReviewWorld(), Answers()
    await ask(world, "place")

    text = await reply(world, service(world, store), "Rome, Italy", "pq-place")

    assert text == ack(world, UIMessage.MEMORY_ANSWER_NOTED)
    (answer,) = store.recorded
    assert isinstance(answer, PlaceAnswer)
    assert (answer.resolution, answer.geonameid, answer.key, answer.question_id) == (
        "atlas", "1000", store.recorded[0].key, "pq-place")
    await closed_by_words(world, "pq-place")


async def test_a_city_the_atlas_lacks_in_a_known_country_is_recorded_local_as_typed():
    world, store = ReviewWorld(), Answers()
    await ask(world, "place", quoted_text=NO_PERSON)

    await reply(world, service(world, store), "Porto, Italy", "pq-place")

    (answer,) = store.recorded
    assert isinstance(answer, PlaceAnswer)
    assert (answer.resolution, answer.city, answer.country, answer.geonameid) == ("local", "Porto", "Italy", "")
    await closed_by_words(world, "pq-place")


async def test_a_country_that_names_one_shown_option_records_that_row():
    world, store = ReviewWorld(), Answers()
    await ask(world, "place", quoted_text=NO_PERSON)

    await reply(world, service(world, store), "united states", "pq-place")

    (answer,) = store.recorded
    assert isinstance(answer, PlaceAnswer) and (answer.resolution, answer.geonameid) == ("atlas", "2000")


async def test_a_name_of_a_shown_entry_records_that_person():
    world, store = ReviewWorld(), Answers()
    await ask(world, "person", quoted_text=DAUGHTER)

    text = await reply(world, service(world, store), "Jamie", "pq-person")

    assert text == ack(world, UIMessage.MEMORY_ANSWER_NOTED)
    (answer,) = store.recorded
    assert isinstance(answer, PersonAnswer) and (answer.resolution, answer.name) == ("entry", "Jamie Doe")
    await closed_by_words(world, "pq-person")


@pytest.mark.parametrize("text, start, end, precision", [
    ("2026-10-01", "2026-10-01", "", "day"),
    ("2026", "2026", "", "year"),
    ("2026-10-01 - 2026-12-01", "2026-10-01", "2026-12-01", "day"),
])
async def test_a_date_in_a_form_the_derivation_reads_is_recorded(text, start, end, precision):
    world, store = ReviewWorld(), Answers()
    await ask(world, "date")

    reply_text = await reply(world, service(world, store), text, "pq-date")

    assert reply_text == ack(world, UIMessage.MEMORY_ANSWER_NOTED)
    (answer,) = store.recorded
    assert isinstance(answer, DateAnswer)
    assert (answer.resolution, answer.start, answer.end, answer.precision) == ("date", start, end, precision)
    await closed_by_words(world, "pq-date")


@pytest.mark.parametrize("kind, text, note", [
    ("place", "Shelbyville, Italy 2", UIMessage.MEMORY_PROFILE_NOT_READ),
    ("place", "Jamie", UIMessage.MEMORY_PROFILE_NOT_READ),
    ("person", "Doe", UIMessage.MEMORY_PROFILE_NOT_READ),
    ("person", "Clara", UIMessage.MEMORY_PROFILE_NOT_READ),
    ("date", "2026-13", UIMessage.MEMORY_PROFILE_DATE_NOT_READ),
    ("date", "Oct 2026", UIMessage.MEMORY_PROFILE_DATE_NOT_READ),
])
async def test_words_that_are_not_a_form_read_leave_the_question_open_and_name_the_form(kind, text, note):
    world, store = ReviewWorld(), Answers()
    await ask(world, kind)

    answer = await reply(world, service(world, store), text, f"pq-{kind}")

    assert answer == ack(world, note)
    doc = world.question_docs[f"pq-{kind}"]
    assert doc["status"] == "asked" and doc["reply_text"] is None and store.recorded == []


async def test_a_first_answer_wins_a_later_reply_to_the_same_point_changes_nothing():
    world, store = ReviewWorld(), Answers()
    await ask(world, "place")
    first = await reply(world, service(world, store), "Rome, Italy", "pq-place")
    await ask(world, "place", question_id="pq-place-again", quoted_text=NO_PERSON)

    again = await reply(world, service(world, store), "united states", "pq-place-again")

    assert first == again == ack(world, UIMessage.MEMORY_ANSWER_NOTED)
    assert len(store.recorded) == 1 and store.recorded[0].geonameid == "1000"  # type: ignore[union-attr]


async def test_a_place_or_a_person_reply_without_the_atlas_or_the_list_is_not_read_but_a_date_is():
    world, store = ReviewWorld(), Answers()
    await ask(world, "place")
    await ask(world, "person", quoted_text=DAUGHTER)
    await ask(world, "date")
    svc = service(world, store, with_atlas=False, with_list=False)

    place = await reply(world, svc, "Rome, Italy", "pq-place")
    person = await reply(world, svc, "Jamie", "pq-person")
    date = await reply(world, svc, "2026", "pq-date")

    assert place == person == ack(world, UIMessage.MEMORY_PROFILE_NOT_READ)
    assert date == ack(world, UIMessage.MEMORY_ANSWER_NOTED) and len(store.recorded) == 1


async def test_a_words_close_counts_as_a_profile_answer_not_a_reply_in_words():
    world, store = ReviewWorld(), Answers()
    await ask(world, "place")

    await reply(world, service(world, store), "Rome, Italy", "pq-place")

    row = [r for r in await world.counters.for_day(world.now.date()) if r.account_id == ACC][0]
    assert row.profile_answered == 1 and row.replies_in_words == 0 and row.profile_reply_not_read == 0
