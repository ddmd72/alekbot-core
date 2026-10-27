"""Facts S4 · T7b: `FactQuestionTexts`, the one resolver of the memory review's language and texts (plan §3.17, R44-4).

On `ReviewWorld`: the real review store and counters on the evaluating Firestore fake, the real file localization and
the fake language service with the real chain (a preferred language is also the UI language, else the account default,
else English).
"""
import logging
from types import MappingProxyType
from typing import Dict, List, Optional

import pytest

from src.domain.fact_lifecycle_day import CounterField
from src.domain.fact_review_language import LABEL_KEYS, fact_question_message, labels_digest
from src.domain.fact_review_question import QuestionStatus
from src.domain.language import LanguageCode
from src.domain.ui_messages import UIMessage
from src.services.fact_question_texts import FactQuestionTexts
from tests.support.fact_review_world import ReviewWorld
from tests.support.review_p2_labels import RU_P2_LABELS

USER, ACC = "u1", "acc"
ACKS = (UIMessage.MEMORY_ANSWER_NOTED, UIMessage.MEMORY_REPLY_NOTED, UIMessage.MEMORY_ALREADY_ANSWERED,
        UIMessage.MEMORY_FACT_CHANGED, UIMessage.MEMORY_QUESTION_CLOSED)

RU: Dict[str, str] = {
    UIMessage.MEMORY_REVIEW_QUESTION.value: "Это всё ещё правда?",
    UIMessage.MEMORY_OPTION_STILL_TRUE.value: "Да",
    UIMessage.MEMORY_OPTION_ENDED.value: "Уже нет",
    UIMessage.MEMORY_OPTION_NEVER.value: "Никогда",
    UIMessage.MEMORY_OPTION_DONT_KNOW.value: "Не помню",
    UIMessage.MEMORY_REPLY_HINT.value: "Ответьте на это сообщение цифрой или своими словами.",
    UIMessage.MEMORY_ANSWER_NOTED.value: "Спасибо, записал.",
    UIMessage.MEMORY_REPLY_NOTED.value: "Понял, учту.",
    UIMessage.MEMORY_ALREADY_ANSWERED.value: "Уже отвечено, спасибо.",
    UIMessage.MEMORY_FACT_CHANGED.value: "Этот факт тем временем изменился, поэтому ответ не применён; я к нему вернусь.",
    UIMessage.MEMORY_QUESTION_CLOSED.value: "Этот вопрос закрыт.",
    UIMessage.MEMORY_DIGEST_HEADER.value: "Факты, которые я отложил на этой неделе:",
    UIMessage.MEMORY_DIGEST_FOOTER.value: "Чтобы вернуть один, скажите в чате, что он всё ещё верен.",
    UIMessage.MEMORY_DIGEST_ARCHIVED.value: "это уже не так",
    UIMessage.MEMORY_DIGEST_INVALIDATED.value: "этого никогда не было",
    **RU_P2_LABELS,  # P2 revised (C11): the set is the 28 label keys
}
BAD_RU = {**RU, UIMessage.MEMORY_OPTION_STILL_TRUE.value: "Да 1"}       # a digit: refused by check_labels


class RaisingCounters:
    async def add(self, account_id, day, counts) -> None:
        raise RuntimeError("counters unavailable")


class RaisingStates:
    """A review store whose state read fails (the only method the resolver calls)."""

    async def get_state(self, account_id: str):
        raise RuntimeError("firestore unavailable")


@pytest.fixture
def world() -> ReviewWorld:
    return ReviewWorld()


def texts(world: ReviewWorld, counters=None, review_store=None) -> FactQuestionTexts:
    return FactQuestionTexts(
        language=world.language, localization=world.localization, review_store=review_store or world.review_store,
        counters=counters or world.counters, system_default=LanguageCode.EN, clock=world.clock,
    )


def ui(world: ReviewWorld, language: LanguageCode) -> Dict[str, str]:
    return {key: world.localization.get_ui_string(language, UIMessage(key)) for key in LABEL_KEYS}


async def fallbacks(world: ReviewWorld) -> int:
    rows = [row for row in await world.counters.for_day(world.now.date()) if row.account_id == ACC]
    return rows[0].labels_fallback if rows else 0


async def with_russian(world: ReviewWorld, labels: Dict[str, str], digest: Optional[str] = None) -> None:
    """The state a review pass leaves after reporting `ru`: the production write path, `set_language`."""
    await world.review_store.mark_enqueued(ACC, world.now)
    assert await world.review_store.set_language(ACC, "ru", labels, digest or labels_digest())


# ------------------------------------------------------------------------------------------------ the cases

async def fixed_uk(world: ReviewWorld) -> None:
    world.language.preferences[USER] = (LanguageCode.UK, False)
    await with_russian(world, RU)                      # a reported language does not move a fixed setting


async def mirror_ru_cached(world: ReviewWorld) -> None:
    world.language.preferences[USER] = (LanguageCode.UK, True)
    await with_russian(world, RU)


async def mirror_ru_bad(world: ReviewWorld) -> None:
    world.language.preferences[USER] = (LanguageCode.UK, True)
    await with_russian(world, BAD_RU)


async def mirror_ru_old_digest(world: ReviewWorld) -> None:
    world.language.preferences[USER] = (LanguageCode.UK, True)
    await with_russian(world, RU, digest="0123456789abcdef")


async def mirror_ru_without_labels(world: ReviewWorld) -> None:
    world.language.preferences[USER] = (LanguageCode.UK, True)
    await world.seed_state(message_language="ru")


async def mirror_unreported(world: ReviewWorld) -> None:
    world.language.account_default = LanguageCode.FR   # no preference of the user's: mirror, UI language from the account
    await world.seed_state()


# name -> (set up, the message language, the language of the texts (None: the Russian labels), fallbacks counted)
CASES = {
    "fixed-uk": (fixed_uk, "uk", LanguageCode.UK, 0),
    "mirror-ru-cached": (mirror_ru_cached, "ru", None, 0),
    "mirror-ru-bad-labels": (mirror_ru_bad, "ru", LanguageCode.UK, 1),
    "mirror-ru-old-digest": (mirror_ru_old_digest, "ru", LanguageCode.UK, 1),
    "mirror-ru-no-labels": (mirror_ru_without_labels, "ru", LanguageCode.UK, 1),
    "mirror-unreported": (mirror_unreported, "fr", LanguageCode.FR, 0),
}


def expected(world: ReviewWorld, language: Optional[LanguageCode]) -> Dict[str, str]:
    return RU if language is None else ui(world, language)


@pytest.mark.parametrize("name", list(CASES))
async def test_the_language_and_the_labels_of_each_case(world, name):
    setup, code, language, counted = CASES[name]
    await setup(world)
    resolver = texts(world)

    assert await resolver.language_for(USER, ACC) == code
    assert await fallbacks(world) == 0, "the language alone counts nothing"
    assert dict(await resolver.labels_for(USER, ACC)) == expected(world, language)
    assert await fallbacks(world) == counted


@pytest.mark.parametrize("ack", ACKS, ids=lambda m: m.value)
@pytest.mark.parametrize("name", list(CASES))
async def test_each_acknowledgement_comes_from_the_source_of_its_question(world, name, ack):
    setup, _, language, counted = CASES[name]
    await setup(world)

    text = await texts(world).text(USER, ACC, ack)

    assert text == expected(world, language)[ack.value]
    assert await fallbacks(world) == counted


async def test_the_fallback_is_the_ui_language_not_ukrainian_by_default(world):
    world.language.preferences[USER] = (LanguageCode.ES, True)
    await with_russian(world, BAD_RU)

    assert dict(await texts(world).labels_for(USER, ACC)) == ui(world, LanguageCode.ES)
    assert await fallbacks(world) == 1


async def test_each_call_that_falls_back_counts_once(world):
    await mirror_ru_bad(world)
    resolver = texts(world)

    await resolver.labels_for(USER, ACC)
    await resolver.text(USER, ACC, UIMessage.MEMORY_ANSWER_NOTED)

    assert await fallbacks(world) == 2


async def test_a_new_label_set_under_the_current_digest_is_used_at_once(world):
    await mirror_ru_old_digest(world)
    resolver = texts(world)
    assert dict(await resolver.labels_for(USER, ACC)) == ui(world, LanguageCode.UK)

    assert await world.review_store.set_language(ACC, "ru", RU, labels_digest())

    assert dict(await resolver.labels_for(USER, ACC)) == RU


async def test_a_fixed_language_with_no_preference_is_the_system_default(world):
    world.language.preferences[USER] = (None, False)
    world.language.account_default = LanguageCode.FR
    await world.seed_state(message_language="ru")
    resolver = FactQuestionTexts(world.language, world.localization, world.review_store, world.counters,
                                 system_default=LanguageCode.ES, clock=world.clock)

    assert await resolver.language_for(USER, ACC) == "es"
    assert dict(await resolver.labels_for(USER, ACC)) == ui(world, LanguageCode.ES)


async def test_an_account_without_a_state_mirrors_into_the_ui_language(world):
    world.language.preferences[USER] = (LanguageCode.UK, True)

    assert await texts(world).language_for(USER, ACC) == "uk"
    assert dict(await texts(world).labels_for(USER, ACC)) == ui(world, LanguageCode.UK)


# ------------------------------------------------------------------------------------------------ message_for

@pytest.mark.parametrize("name", ["mirror-ru-cached", "mirror-ru-bad-labels", "fixed-uk"])
async def test_message_for_builds_the_domain_message_with_the_chosen_labels(world, name):
    setup, _, language, _ = CASES[name]
    await setup(world)
    question = await world.seed_question(QuestionStatus.QUEUED)

    message = await texts(world).message_for(question, USER, ACC)

    assert message == fact_question_message(question, expected(world, language))
    assert message.quote == question.quoted_text
    labels = expected(world, language)
    assert [o.label for o in message.options] == [
        labels[UIMessage.MEMORY_OPTION_STILL_TRUE.value], labels[UIMessage.MEMORY_OPTION_ENDED.value],
        labels[UIMessage.MEMORY_OPTION_NEVER.value], labels[UIMessage.MEMORY_OPTION_DONT_KNOW.value],
    ]


# ------------------------------------------------------------------------------------------------ edges

async def test_a_text_that_is_not_a_review_text_is_refused(world):
    with pytest.raises(ValueError, match="not a memory review text"):
        await texts(world).text(USER, ACC, UIMessage.RESPONSE_READY)


async def test_the_labels_returned_are_read_only(world):
    await mirror_ru_cached(world)

    labels = await texts(world).labels_for(USER, ACC)

    assert isinstance(labels, MappingProxyType)


async def test_a_failing_state_read_is_logged_and_raised(world, caplog):
    world.language.preferences[USER] = (LanguageCode.UK, True)
    resolver = texts(world, review_store=RaisingStates())

    with caplog.at_level(logging.ERROR), pytest.raises(RuntimeError, match="firestore unavailable"):
        await resolver.labels_for(USER, ACC)

    errors: List[str] = [r.getMessage() for r in caplog.records if r.levelno == logging.ERROR]
    assert len(errors) == 1 and ACC[:8] in errors[0] and "RuntimeError" in errors[0]


async def test_a_failing_counter_is_a_warning_and_the_fallback_texts_still_come(world, caplog):
    await mirror_ru_bad(world)
    resolver = texts(world, counters=RaisingCounters())

    with caplog.at_level(logging.WARNING):
        labels = await resolver.labels_for(USER, ACC)

    assert dict(labels) == ui(world, LanguageCode.UK)
    warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1 and CounterField.LABELS_FALLBACK.value in warnings[0]
