"""Quote translations in the review store: stored by account, language and the digest of the English text; read back in
one batch; unreadable ones skipped; counted by `purge_overdue` once past their retention."""
import logging
from datetime import datetime, timedelta

import pytest

from src.domain.fact_review_quote_translation import QUOTE_TRANSLATION_RETENTION, QuoteTranslation, quote_digest
from tests.support.fact_review_world import ReviewWorld
from tests.support.review_rule_builders import NOW

TENNIS = "Alex plays tennis on Sundays"
TENNIS_UK = "Алекс грає в теніс по неділях"
GYM = "Alex trains at the FitLife gym"
GYM_UK = "Алекс тренується в спортзалі FitLife"


@pytest.fixture
def world():
    return ReviewWorld()


def record(text=TENNIS, translated=TENNIS_UK, language="uk"):
    return QuoteTranslation(language, quote_digest(text), translated)


async def test_translations_are_stored_by_language_and_text_and_read_back(world):
    await world.review_store.set_quote_translations("acc", [record(), record(GYM, GYM_UK)], NOW)

    doc = world.message_docs[f"acc:quote:uk:{quote_digest(TENNIS)}"]
    assert doc["kind"] == "quote" and doc["account_id"] == "acc" and doc["language"] == "uk"
    assert doc["text"] == TENNIS_UK and doc["written_at"] == NOW
    assert doc["purge_after"] == NOW + QUOTE_TRANSLATION_RETENTION
    found = await world.review_store.get_quote_translations(
        "acc", "uk", [quote_digest(TENNIS), quote_digest(GYM), quote_digest("never translated")],
    )
    assert found == {quote_digest(TENNIS): record(), quote_digest(GYM): record(GYM, GYM_UK)}


async def test_a_later_translation_of_the_same_text_replaces_the_earlier(world):
    await world.review_store.set_quote_translations("acc", [record()], NOW)
    await world.review_store.set_quote_translations("acc", [record(translated="Алекс грає у теніс щонеділі")], NOW + timedelta(days=1))

    found = await world.review_store.get_quote_translations("acc", "uk", [quote_digest(TENNIS)])
    assert found[quote_digest(TENNIS)].text == "Алекс грає у теніс щонеділі"
    assert world.message_docs[f"acc:quote:uk:{quote_digest(TENNIS)}"]["written_at"] == NOW + timedelta(days=1)


async def test_another_language_or_account_is_not_read(world):
    await world.review_store.set_quote_translations("acc", [record()], NOW)
    digests = [quote_digest(TENNIS)]
    assert await world.review_store.get_quote_translations("acc", "ru", digests) == {}
    assert await world.review_store.get_quote_translations("other", "uk", digests) == {}
    assert await world.review_store.get_quote_translations("acc", "uk", []) == {}


async def test_an_unreadable_stored_translation_is_skipped_and_logged(world, caplog):
    digest = quote_digest(TENNIS)
    good = record(GYM, GYM_UK)
    await world.review_store.set_quote_translations("acc", [good], NOW)
    world.message_docs[f"acc:quote:uk:{digest}"] = {
        "account_id": "acc", "kind": "quote", "language": "uk", "source_digest": digest, "text": "   ",
    }
    foreign = quote_digest("Alex keeps a log")
    world.message_docs[f"acc:quote:uk:{foreign}"] = {
        "account_id": "someone-else", "kind": "quote", "language": "uk", "source_digest": foreign, "text": "x",
    }

    with caplog.at_level(logging.ERROR):
        found = await world.review_store.get_quote_translations("acc", "uk", [digest, quote_digest(GYM), foreign])

    assert found == {quote_digest(GYM): good}
    errors = [r.getMessage() for r in caplog.records if r.levelno >= logging.ERROR]
    assert len(errors) == 2 and not any(TENNIS_UK in e or "   " == e for e in errors)


async def test_translations_count_toward_purge_overdue_after_retention(world):
    await world.review_store.set_quote_translations("acc", [record(), record(GYM, GYM_UK)], NOW)
    assert await world.review_store.purge_overdue(NOW + QUOTE_TRANSLATION_RETENTION - timedelta(seconds=1)) == 0
    assert await world.review_store.purge_overdue(NOW + QUOTE_TRANSLATION_RETENTION + timedelta(seconds=1)) == 2


async def test_an_empty_set_writes_nothing_and_a_naive_time_is_refused(world):
    await world.review_store.set_quote_translations("acc", [], NOW)
    assert world.message_docs == {}
    with pytest.raises(ValueError):
        await world.review_store.set_quote_translations("acc", [record()], datetime(2026, 10, 24, 9, 0))
    with pytest.raises(ValueError):
        await world.review_store.set_quote_translations("", [record()], NOW)
    with pytest.raises(ValueError):
        await world.review_store.get_quote_translations("", "uk", [quote_digest(TENNIS)])
    assert world.message_docs == {}
