"""`FactQuestionTexts` shows the fact in the review's translation into the message language, else as stored: the
question, the same text under another fact id, English messages, a failing read, one read per message, the digest."""
import logging

import pytest

from src.domain.entities import FactState
from src.domain.fact_review_quote_translation import QuoteTranslation, quote_digest
from src.domain.fact_review_question import QuestionStatus
from src.domain.language import LanguageCode
from src.domain.ui_messages import UIMessage
from src.services.fact_question_texts import FactQuestionTexts
from tests.support.fact_kit import make_fact
from tests.support.fact_review_world import ReviewWorld
from tests.support.review_rule_builders import question as build_question

USER, ACC = "u1", "acc"
QUOTED = "Drinks green tea"
QUOTED_UK = "П'є зелений чай"


@pytest.fixture
def world() -> ReviewWorld:
    return ReviewWorld()


class CountingStore:
    """The world's review store, counting the reads the resolver makes."""

    def __init__(self, inner) -> None:
        self.inner = inner
        self.states = 0
        self.translation_reads = 0

    async def get_state(self, account_id):
        self.states += 1
        return await self.inner.get_state(account_id)

    async def get_quote_translations(self, account_id, language, digests):
        self.translation_reads += 1
        return await self.inner.get_quote_translations(account_id, language, digests)


class FailingTranslations(CountingStore):
    async def get_quote_translations(self, account_id, language, digests):
        raise RuntimeError("firestore unavailable")


def texts(world, store=None) -> FactQuestionTexts:
    return FactQuestionTexts(
        world.language, world.localization, store or world.review_store, world.counters, system_default=LanguageCode.EN,
        clock=world.clock,
    )


async def store_uk(world, text=QUOTED, translated=QUOTED_UK, language="uk"):
    await world.review_store.set_quote_translations(
        ACC, [QuoteTranslation(language, quote_digest(text), translated)], world.now,
    )


def fixed(world, language):
    world.language.preferences[USER] = (language, False)


async def test_the_question_shows_the_translation_in_the_message_language(world):
    fixed(world, LanguageCode.UK)
    await store_uk(world)

    message = await texts(world).message_for(build_question(QuestionStatus.ASKED), USER, ACC)

    assert message.quote == QUOTED_UK
    assert message.prompt == world.localization.get_ui_string(LanguageCode.UK, UIMessage.MEMORY_REVIEW_QUESTION)


async def test_the_same_text_under_another_fact_id_shows_the_translation(world):
    fixed(world, LanguageCode.UK)
    await store_uk(world)
    other = build_question(QuestionStatus.ASKED, question_id="q2", fact_id="another-fact-id")

    assert (await texts(world).message_for(other, USER, ACC)).quote == QUOTED_UK


async def test_a_translation_into_another_language_is_not_shown(world):
    fixed(world, LanguageCode.UK)
    await store_uk(world, language="fr", translated="Boit du thé vert")

    assert (await texts(world).message_for(build_question(QuestionStatus.ASKED), USER, ACC)).quote == QUOTED


async def test_english_messages_read_no_translation(world):
    fixed(world, LanguageCode.EN)
    store = CountingStore(world.review_store)

    message = await texts(world, store).message_for(build_question(QuestionStatus.ASKED), USER, ACC)

    assert message.quote == QUOTED and store.translation_reads == 0


async def test_a_failing_translation_read_shows_the_stored_text(world, caplog):
    fixed(world, LanguageCode.UK)

    with caplog.at_level(logging.WARNING):
        message = await texts(world, FailingTranslations(world.review_store)).message_for(
            build_question(QuestionStatus.ASKED), USER, ACC)

    assert message.quote == QUOTED
    warned = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert any("quote translations" in w and "RuntimeError" in w for w in warned)
    assert not any(QUOTED in w for w in warned)


async def test_a_message_still_counts_one_labels_fallback(world):
    world.language.preferences[USER] = (None, True)      # mirror, nothing reported: the UI-language fallback
    await world.seed_state()
    await world.review_store.mark_enqueued(ACC, world.now)
    await world.review_store.set_language(ACC, "ru", {}, "stale-digest")     # a Russian set that does not hold

    await texts(world).question_and_labels(build_question(QuestionStatus.ASKED), USER, ACC)

    rows = [row for row in await world.counters.for_day(world.now.date()) if row.account_id == ACC]
    assert rows[0].labels_fallback == 1


async def test_question_and_labels_reads_once(world):
    fixed(world, LanguageCode.UK)
    await store_uk(world)
    store = CountingStore(world.review_store)

    message, labels = await texts(world, store).question_and_labels(build_question(QuestionStatus.ASKED), USER, ACC)

    assert store.states == 1 and store.translation_reads == 1
    assert message.quote == QUOTED_UK and message.prompt == labels["memory_review_question"]


async def test_digest_for_shows_each_fact_translated_or_stored(world):
    fixed(world, LanguageCode.UK)
    await store_uk(world)
    translated = make_fact(QUOTED, state=FactState.ARCHIVED)
    plain = make_fact("Keeps a weight log", state=FactState.INVALIDATED)

    text = await texts(world).digest_for(USER, ACC, [translated, plain])

    assert f"«{QUOTED_UK}»" in text and "«Keeps a weight log»" in text and QUOTED not in text


async def test_pass_language_is_the_fixed_language_or_none_and_a_failed_read_is_none(world, caplog):
    fixed(world, LanguageCode.UK)
    assert await texts(world).pass_language(USER) == "uk"
    world.language.preferences[USER] = (None, False)
    assert await texts(world).pass_language(USER) == "en"         # the system default
    world.language.preferences[USER] = (LanguageCode.UK, True)
    assert await texts(world).pass_language(USER) is None          # mirror

    async def broken(user_id):
        raise RuntimeError("user store unavailable")

    world.language.get_preference = broken
    with caplog.at_level(logging.WARNING):
        assert await texts(world).pass_language(USER) is None
    assert any("translates nothing" in r.getMessage() for r in caplog.records)
