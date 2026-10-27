"""`FactAnswerService.review_queue`, `answer_options` and the two reply counters (T12d: hexagonal review A3, A9, the
never-incremented `replies_in_words` and `already_answered`).

On the review world, as the reply and answer tests: the real repository, typed write path, review store, counters and
localization over one evaluating Firestore fake. Assertions are on what the queue lists and on the stored counters."""
from datetime import timedelta
from typing import Any, Dict, Optional

from src.domain.entities import ExpiryKind
from src.domain.fact_answer_result import AnswerResult
from src.domain.fact_review_answer_kind import AnswerKind
from src.domain.fact_review_language import fact_question_message
from src.domain.fact_review_question import FactReviewQuestion, QuestionStatus
from src.domain.language import LanguageCode
from src.domain.messaging import MessageRef
from src.domain.owner_timezone import ResolvedOwnerZone
from src.services.fact_answer_service import FactAnswerService
from src.services.fact_question_texts import FactQuestionTexts
from tests.support.fact_kit import ACC, OTHER, USER
from tests.support.fact_lifecycle_world import NOW
from tests.support.fact_review_world import ReviewWorld
from tests.support.fact_write import EXPIRY_NONE, ctx

TZ = "Europe/Lisbon"
TEXT = "drinks green tea every morning"
SESSION = f"{USER}:D1"


class FixedZones:
    async def resolve(self, account_id: str) -> ResolvedOwnerZone:
        return ResolvedOwnerZone(USER, TZ)


def _texts(world: ReviewWorld) -> FactQuestionTexts:
    return FactQuestionTexts(world.language, world.localization, world.review_store, world.counters, LanguageCode.EN,
                             clock=world.clock)


def _service(world: ReviewWorld) -> FactAnswerService:
    return FactAnswerService(
        fact_updates=world.store.adapter, repo=world.repo, review_store=world.review_store, counters=world.counters,
        task_queue=world.queue, session_store=world.session_store, texts=_texts(world), interactive=world.interactive,
        owner_zones=FixedZones(),
    )


async def _stale(world: ReviewWorld, text: str = TEXT) -> str:
    return await world.seed_stale(text, expiry_kind=ExpiryKind.HORIZON, ttl_days=14,
                                  reported_date=NOW - timedelta(days=60))


async def _ask(world: ReviewWorld, fact_id: str, question_id: str = "q1",
               status: QuestionStatus = QuestionStatus.ASKED, **over: Any) -> FactReviewQuestion:
    fields: Dict[str, Any] = {"question_id": question_id, "account_id": ACC, "user_id": USER, "fact_id": fact_id,
                              "quoted_text": world.docs[fact_id]["text"], **over}
    if status is not QuestionStatus.QUEUED:
        fields["session_id"] = SESSION
        draft = FactReviewQuestion(
            queue_key=NOW, queued_at=NOW, fallback="revive",
            **{k: fields[k] for k in ("question_id", "account_id", "user_id", "fact_id", "quoted_text")},
        )
        labels = await _texts(world).labels_for(USER, ACC)
        fields["message_ref"] = await world.interactive.post("slack", "D1", fact_question_message(draft, labels))
    return await world.seed_question(status, **fields)


def _ref(world: ReviewWorld, question_id: str = "q1") -> MessageRef:
    doc = world.question_docs[question_id]
    return MessageRef(platform=doc["message_platform"], channel_id=doc["message_channel"], message_id=doc["message_id"])


async def _day_row(world: ReviewWorld):
    rows = [row for row in await world.counters.for_day(world.now.date()) if row.account_id == ACC]
    return rows[0] if rows else None


async def _reply(world: ReviewWorld, text: str, question_id: str = "q1") -> Optional[str]:
    ref = _ref(world, question_id)
    return await _service(world).answer_reply(USER, ACC, ref.platform, ref.channel_id, ref.message_id, text, world.now)


# ------------------------------------------------------------------------------------------------ the queue

async def test_the_queue_lists_the_users_open_questions_with_their_facts_as_they_stand():
    world = ReviewWorld()
    fact = await _stale(world)
    await _ask(world, fact, status=QuestionStatus.QUEUED)

    [entry] = await _service(world).review_queue(ACC, USER)

    assert (entry.question_id, entry.text, entry.place) == ("q1", TEXT, "waiting")


async def test_a_fact_rewritten_since_the_question_shows_the_text_the_question_quoted_and_the_answer_agrees():
    world = ReviewWorld()
    fact = await world.seed(TEXT)
    await _ask(world, fact, status=QuestionStatus.QUEUED)
    await world.store.adapter.update_fact(
        fact, {"content": "drinks black tea every morning", "expiry": EXPIRY_NONE}, ACC, context=ctx(),
    )
    service = _service(world)

    [entry] = await service.review_queue(ACC, USER)
    answered = await service.apply(ACC, USER, "q1", AnswerKind.STILL_TRUE, "cabinet", world.now)

    assert entry.text == TEXT
    assert answered is AnswerResult.CHANGED


async def test_another_users_and_another_accounts_questions_are_not_listed():
    world = ReviewWorld()
    mine, theirs = await _stale(world), await _stale(world, "waters the ferns on Sundays")
    await _ask(world, mine, "q-mine", QuestionStatus.QUEUED)
    await _ask(world, theirs, "q-theirs", QuestionStatus.QUEUED, user_id="u-other")

    assert [e.question_id for e in await _service(world).review_queue(ACC, USER)] == ["q-mine"]
    assert await _service(world).review_queue(OTHER, USER) == []


# ------------------------------------------------------------------------------------------------ the Cabinet's labels

async def test_the_answer_options_are_the_three_deciding_answers_in_the_users_ui_language():
    world = ReviewWorld()
    world.language.preferences[USER] = (LanguageCode.UK, False)

    # P2 revised (C14): the options per question kind asked for, each kind's deciding answers with UI-language labels
    by_kind = await _service(world).answer_options(USER, ["still_true", "paid", "still_true"])

    assert list(by_kind) == ["still_true", "paid"]
    options = by_kind["still_true"]
    assert [kind for kind, _ in options] == [AnswerKind.STILL_TRUE, AnswerKind.ENDED, AnswerKind.NEVER]
    labels = await _texts(world).ui_labels(USER)
    assert [label for _, label in options] == [
        labels["memory_option_still_true"], labels["memory_option_ended"], labels["memory_option_never"],
    ]
    assert all(label != "Yes" for _, label in options)
    assert by_kind["paid"] == [
        (AnswerKind.PAID, labels["memory_option_paid"]), (AnswerKind.NOT_PAID, labels["memory_option_not_paid"]),
    ]


async def test_the_ui_labels_count_no_fallback_and_cover_every_text():
    world = ReviewWorld()
    world.language.preferences[USER] = (LanguageCode.UK, True)

    labels = await _texts(world).ui_labels(USER)

    assert set(labels) == set((await _texts(world).labels_for(USER, ACC)))
    row = await _day_row(world)
    assert row is None or row.labels_fallback == 0


# ------------------------------------------------------------------------------------------------ the reply counters

async def test_a_reply_in_words_is_counted_once():
    world = ReviewWorld()
    await _ask(world, await _stale(world))

    assert await _reply(world, "He stopped drinking tea last spring.") is not None

    row = await _day_row(world)
    assert (row.replies_in_words, row.already_answered) == (1, 0)


async def test_a_reply_in_words_that_is_not_recorded_is_not_counted():
    world = ReviewWorld()
    await _ask(world, await _stale(world))
    await _reply(world, "first words")
    stored = world.question_docs["q1"]
    stored["offered_at"] = world.now  # the review has read them: nothing more is recorded

    assert await _reply(world, "more words") is None

    assert (await _day_row(world)).replies_in_words == 1


async def test_a_reply_to_a_closed_question_is_counted_as_already_answered():
    world = ReviewWorld()
    await _ask(world, await _stale(world), status=QuestionStatus.ASKED)
    await _service(world).apply(ACC, USER, "q1", AnswerKind.STILL_TRUE, "digit", world.now)

    assert await _reply(world, "yes") is not None

    row = await _day_row(world)
    assert (row.already_answered, row.replies_in_words) == (1, 0)
