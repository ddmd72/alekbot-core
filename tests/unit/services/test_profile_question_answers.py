"""The owner's answers to profile questions (fact profile RFC §4.7, PR 5d): a digit or a button records the answer in the
account's store and closes the question (owner, no answer kind, source `digit` or `button`), the next question is released,
words are not read yet (the question stays open, nothing is recorded, no `reply_text`), and a profile question never reaches
`apply`. On the review world with the real review store and a recording answers store. Invented cast."""
import logging
from datetime import timedelta
from typing import Any, Dict, List, Optional

from src.domain.choice_outcome import ChoiceOutcome
from src.domain.fact_answer_result import AnswerResult
from src.domain.fact_review_answer_kind import AnswerKind
from src.domain.fact_review_frame import reply_part
from src.domain.fact_review_language import fact_question_message
from src.domain.fact_review_question import FACT_QUESTION_KIND, FactReviewQuestion, QuestionStatus
from src.domain.interactive_binding import InteractiveBinding
from src.domain.interactive_choice import InteractiveChoice
from src.domain.language import LanguageCode
from src.domain.messaging import MessageRef
from src.domain.owner_timezone import ResolvedOwnerZone
from src.domain.profile_answers import EMPTY_ANSWERS, OwnerAnswer, ProfileAnswers
from src.domain.ui_messages import UIMessage
from src.ports.profile_answer_repository import ProfileAnswerRepository
from src.services.fact_answer_service import FactAnswerService
from src.services.fact_question_texts import FactQuestionTexts
from src.services.profile_question_answers import ProfileQuestionAnswers
from tests.support.fact_lifecycle_world import NOW
from tests.support.fact_review_world import ReviewWorld
from tests.support.profile_question_builders import porto_point, profile_question

ACC = "acc"
USER = "u1"
CHANNEL = "D1"
SESSION = f"{USER}:{CHANNEL}"


class Answers(ProfileAnswerRepository):
    def __init__(self) -> None:
        self.answers = EMPTY_ANSWERS
        self.recorded: List[OwnerAnswer] = []

    async def get(self, account_id: str) -> ProfileAnswers:
        return self.answers

    async def record(self, account_id: str, answer: OwnerAnswer) -> bool:
        if self.answers.has(answer.key):
            return False
        self.answers = self.answers.with_answer(answer)
        self.recorded.append(answer)
        return True


class FixedZones:
    async def resolve(self, account_id: str):
        return ResolvedOwnerZone(USER, "Europe/Lisbon")


def ack(world: ReviewWorld, message: UIMessage) -> str:
    return world.localization.get_ui_string(LanguageCode.EN, message)


def service(world: ReviewWorld, store: Optional[Answers], wired: bool = True) -> FactAnswerService:
    return FactAnswerService(
        fact_updates=world.store.adapter, repo=world.repo, review_store=world.review_store, counters=world.counters,
        task_queue=world.queue, session_store=world.session_store,
        texts=FactQuestionTexts(world.language, world.localization, world.review_store, world.counters,
                                LanguageCode.EN, clock=world.clock),
        interactive=world.interactive, owner_zones=FixedZones(), clock=world.clock,
        profile_answers=ProfileQuestionAnswers(store) if wired and store is not None else None,
    )


async def ask(world: ReviewWorld, kind: str = "place", status: QuestionStatus = QuestionStatus.ASKED,
              **over: Any) -> FactReviewQuestion:
    """A profile question posted on Slack first (so its ref is one the platform gave) and stored."""
    draft = profile_question(kind, **over)
    labels = await FactQuestionTexts(world.language, world.localization, world.review_store, world.counters,
                                     LanguageCode.EN).labels_for(USER, ACC)
    ref = await world.interactive.post("slack", CHANNEL, fact_question_message(draft, labels))
    fields: Dict[str, Any] = dict(over, message_ref=ref, session_id=SESSION)
    if status is QuestionStatus.PENDING:
        fields["posted"] = True
    return await world.put_question(profile_question(kind, status, **fields))


def ref_of(world: ReviewWorld, question_id: str) -> MessageRef:
    doc = world.question_docs[question_id]
    return MessageRef(platform=doc["message_platform"], channel_id=doc["message_channel"], message_id=doc["message_id"])


async def reply(world: ReviewWorld, svc: FactAnswerService, text: str, question_id: str) -> Optional[str]:
    found = ref_of(world, question_id)
    return await svc.answer_reply(USER, ACC, found.platform, found.channel_id, found.message_id, text, world.now)


async def click(world: ReviewWorld, svc: FactAnswerService, option: str, question_id: str,
                ref: Optional[MessageRef] = None) -> Optional[ChoiceOutcome]:
    choice = InteractiveChoice(
        binding=InteractiveBinding(kind=FACT_QUESTION_KIND, id=question_id), option_id=option, platform="slack",
        platform_user_id="U-slack", message_ref=ref or ref_of(world, question_id), at=NOW + timedelta(hours=1))
    return await svc.handle(choice, USER, ACC)


async def history(world: ReviewWorld) -> List[tuple]:
    return [(m.role, m.parts[0].text) for m in await world.session_store.messages(SESSION)]


async def day_row(world: ReviewWorld):
    rows = [row for row in await world.counters.for_day(world.now.date()) if row.account_id == ACC]
    return rows[0] if rows else None


def next_tasks(world: ReviewWorld) -> List[dict]:
    return [body for task_type, body in world.enqueued if task_type == "fact_review" and body.get("mode") == "next"]


# ------------------------------------------------------------------------------------------------ a digit

async def test_a_digit_on_the_rome_question_records_the_atlas_row_and_closes_the_question():
    world, store = ReviewWorld(), Answers()
    await world.seed_state(account_id=ACC, current_question_id="pq-place")
    await ask(world, "place")
    await world.seed_question(QuestionStatus.QUEUED, question_id="q-next", account_id=ACC, user_id=USER, fact_id="f9",
                              quoted_text="Alex has a trip to Rome")

    text = await reply(world, service(world, store), "1", "pq-place")

    assert text == ack(world, UIMessage.MEMORY_ANSWER_NOTED)
    [answer] = store.recorded
    assert (answer.key, answer.resolution, answer.geonameid) == ("place:rome|", "atlas", "1000")
    done = world.question_docs["pq-place"]
    assert (done["status"], done["closed_by"], done["answer"], done["answer_source"]) == ("closed", "owner", None, "digit")
    assert done["reply_text"] is None
    assert next_tasks(world) == [{"task_type": "fact_review", "user_id": USER, "account_id": ACC, "mode": "next"}]
    assert (await world.review_store.get_state(ACC)).current_question_id is None
    assert (await day_row(world)).profile_answered == 1
    assert await history(world) == [("user", reply_part("pq-place", "1")), ("model", text)]


async def test_a_digit_on_a_person_question_records_the_picked_entry_for_the_lineage():
    world, store = ReviewWorld(), Answers()
    await ask(world, "person")

    await reply(world, service(world, store), "2", "pq-person")

    [answer] = store.recorded
    assert (answer.key, answer.resolution, answer.name) == ("person:lin-1:doe", "entry", "Jamie Doe")


async def test_dont_remember_on_a_date_question_records_unknown_and_closes_it():
    world, store = ReviewWorld(), Answers()
    await ask(world, "date")

    text = await reply(world, service(world, store), "1", "pq-date")

    [answer] = store.recorded
    assert (answer.key, answer.resolution) == ("date:lin-1", "unknown")
    assert text == ack(world, UIMessage.MEMORY_ANSWER_NOTED) and world.question_docs["pq-date"]["status"] == "closed"


async def test_a_second_answer_for_a_key_closes_the_question_and_keeps_the_first():
    world, store = ReviewWorld(), Answers()
    await ask(world, "place")
    await reply(world, service(world, store), "1", "pq-place")
    await ask(world, "place", question_id="pq-again")

    await reply(world, service(world, store), "2", "pq-again")

    assert [a.geonameid for a in store.recorded] == ["1000"]
    assert world.question_docs["pq-again"]["status"] == "closed"


async def test_a_delivered_pending_profile_question_is_completed_then_answered():
    world, store = ReviewWorld(), Answers()
    await ask(world, "place", QuestionStatus.PENDING)

    await reply(world, service(world, store), "2", "pq-place")

    done = world.question_docs["pq-place"]
    assert done["status"] == "closed" and done["asked_at"] is not None and len(store.recorded) == 1


# ------------------------------------------------------------------------------------------------ a button

async def test_the_keep_button_on_a_springfield_like_place_records_a_local_answer():
    world, store = ReviewWorld(), Answers()
    await ask(world, "place", open_point=porto_point(), question_id="pq-porto")

    outcome = await click(world, service(world, store), "keep", "pq-porto")

    [answer] = store.recorded
    assert (answer.resolution, answer.city, answer.country) == ("local", "Porto", "Portugal")
    done = world.question_docs["pq-porto"]
    assert (done["status"], done["closed_by"], done["answer"], done["answer_source"]) == ("closed", "owner", None, "button")
    assert outcome is not None and outcome.note == ack(world, UIMessage.MEMORY_ANSWER_NOTED)
    assert (await day_row(world)).profile_answered == 1


async def test_a_picked_candidate_button_records_the_atlas_row_and_re_renders_the_message():
    world, store = ReviewWorld(), Answers()
    await ask(world, "place")
    ref = ref_of(world, "pq-place")

    await click(world, service(world, store), "pick2", "pq-place")

    assert store.recorded[0].geonameid == "2000"
    assert world.interactive.shown[ref].note == ack(world, UIMessage.MEMORY_ANSWER_NOTED)
    assert not world.interactive.has_controls(ref)


async def test_a_button_that_is_no_option_of_the_question_leaves_it_open():
    world, store = ReviewWorld(), Answers()
    await ask(world, "place")

    assert await click(world, service(world, store), "not_listed", "pq-place") is None

    assert store.recorded == [] and world.question_docs["pq-place"]["status"] == "asked"


async def test_a_click_on_another_message_is_ignored():
    world, store = ReviewWorld(), Answers()
    await ask(world, "place")
    other = MessageRef(platform="slack", channel_id=CHANNEL, message_id="elsewhere")

    assert await click(world, service(world, store), "pick1", "pq-place", ref=other) is None

    assert store.recorded == [] and world.question_docs["pq-place"]["status"] == "asked"


async def test_a_click_on_a_closed_profile_question_shows_it_closed_and_writes_nothing():
    world, store = ReviewWorld(), Answers()
    await ask(world, "place")
    svc = service(world, store)
    await click(world, svc, "pick1", "pq-place")

    outcome = await click(world, svc, "pick2", "pq-place")

    assert outcome is not None and outcome.note == ack(world, UIMessage.MEMORY_QUESTION_CLOSED)
    assert [a.geonameid for a in store.recorded] == ["1000"]


# ------------------------------------------------------------------------------------------------ words

async def test_words_are_not_read_yet_the_question_is_still_asked_and_nothing_is_recorded():
    world, store = ReviewWorld(), Answers()
    await world.seed_state(account_id=ACC, current_question_id="pq-place")
    await ask(world, "place")

    text = await reply(world, service(world, store), "Rome, Italy", "pq-place")

    assert text == ack(world, UIMessage.MEMORY_PROFILE_NOT_READ)
    kept = world.question_docs["pq-place"]
    assert kept["status"] == "asked" and kept["reply_text"] is None and kept["reply_at"] is None
    assert store.recorded == [] and next_tasks(world) == []
    assert (await world.review_store.get_state(ACC)).current_question_id == "pq-place"
    row = await day_row(world)
    assert row.profile_reply_not_read == 1 and row.profile_answered == 0 and row.replies_in_words == 0
    assert await history(world) == [("user", reply_part("pq-place", "Rome, Italy")), ("model", text)]


async def test_a_date_question_tells_the_form_that_is_read():
    world, store = ReviewWorld(), Answers()
    await ask(world, "date")

    text = await reply(world, service(world, store), "October 2026", "pq-date")

    assert text == ack(world, UIMessage.MEMORY_PROFILE_DATE_NOT_READ)
    assert text != ack(world, UIMessage.MEMORY_PROFILE_NOT_READ)


async def test_a_digit_beyond_the_options_is_words_and_not_read():
    world, store = ReviewWorld(), Answers()
    await ask(world, "place")

    text = await reply(world, service(world, store), "9", "pq-place")

    assert text == ack(world, UIMessage.MEMORY_PROFILE_NOT_READ) and store.recorded == []


async def test_the_not_read_reply_never_reaches_the_review_model():
    world, store = ReviewWorld(), Answers()
    await ask(world, "person")

    await reply(world, service(world, store), "the one from Lisbon", "pq-person")

    unread = [q for q in await world.review_store.questions_for_account(ACC, scope="all") if q.reply_not_shown]
    assert unread == []


# ------------------------------------------------------------------------------------------------ the other doors

async def test_a_reply_to_a_closed_profile_question_is_already_answered():
    world, store = ReviewWorld(), Answers()
    await ask(world, "place")
    svc = service(world, store)
    await reply(world, svc, "1", "pq-place")

    text = await reply(world, svc, "2", "pq-place")

    assert text == ack(world, UIMessage.MEMORY_ALREADY_ANSWERED)
    assert len(store.recorded) == 1 and (await day_row(world)).already_answered == 1


async def test_a_profile_question_never_reaches_apply():
    world, store = ReviewWorld(), Answers()
    await ask(world, "place")

    result = await service(world, store).apply(ACC, USER, "pq-place", AnswerKind.STILL_TRUE, "digit", NOW)

    assert result is AnswerResult.NOT_APPLIED
    assert world.question_docs["pq-place"]["status"] == "asked" and store.recorded == []
    assert (await day_row(world)).answers_not_applied == 1


async def test_without_the_answers_service_a_profile_reply_is_left_alone(caplog):
    world, store = ReviewWorld(), Answers()
    await ask(world, "place")

    with caplog.at_level(logging.WARNING):
        text = await reply(world, service(world, store, wired=False), "1", "pq-place")

    assert text is None and world.question_docs["pq-place"]["status"] == "asked" and store.recorded == []
    assert any("no profile answers are wired" in r.getMessage() for r in caplog.records)


async def test_an_open_profile_question_that_was_never_delivered_is_left_alone():
    world, store = ReviewWorld(), Answers()
    await world.put_question(profile_question("place", QuestionStatus.PENDING, session_id=SESSION))

    assert await service(world, store).handle(
        InteractiveChoice(binding=InteractiveBinding(kind=FACT_QUESTION_KIND, id="pq-place"), option_id="pick1",
                          platform="slack", platform_user_id="U", message_ref=MessageRef("slack", CHANNEL, "m-x"),
                          at=NOW), USER, ACC) is None
    assert store.recorded == []
