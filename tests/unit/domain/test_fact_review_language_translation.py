"""The translation-related helpers of the review's language module."""
import pytest

from src.domain.fact_review_language import (
    CANONICAL_LABELS,
    check_labels,
    fact_question_message,
    fixed_message_language,
    labels_digest,
    shown_language,
    translation_language,
)
from src.domain.fact_review_quote_translation import QuoteTranslation, quote_digest
from src.domain.fact_review_state import FactReviewState
from src.domain.language import LanguageCode
from tests.support.review_rule_builders import question as build_question
from src.domain.fact_review_question import QuestionStatus


def test_fixed_message_language_is_the_setting_or_default_and_none_when_mirrored():
    assert fixed_message_language(LanguageCode.UK, False, LanguageCode.EN) == "uk"
    assert fixed_message_language(None, False, LanguageCode.FR) == "fr"
    assert fixed_message_language(LanguageCode.UK, True, LanguageCode.EN) is None


@pytest.mark.parametrize("mirror, fixed, reported, expected", [
    (True, None, "RU", "ru"), (True, None, "ru-RU", None), (False, "uk", "ru", "uk"),
    (False, "en", None, None), (True, None, "en", None), (True, "uk", None, None), (False, None, "ru", None),
])
def test_translation_language_follows_the_mode_and_is_none_for_english(mirror, fixed, reported, expected):
    assert translation_language(mirror, fixed, reported) == expected


def _state(**over):
    return FactReviewState(account_id="acc", **over)


def test_shown_language_is_the_strings_language_when_the_labels_fall_back():
    assert shown_language("ru", None, LanguageCode.UK) == "uk"
    ru_labels = {k: "текст " + "".join(chr(1072 + int(c)) for c in str(i)) for i, k in enumerate(CANONICAL_LABELS)}
    state = _state(labels={"ru": ru_labels}, label_digests={"ru": labels_digest()})
    assert shown_language("ru", state, LanguageCode.UK) == "ru"
    assert shown_language("uk", None, LanguageCode.EN) == "uk"


def test_the_question_message_shows_a_matching_translation_and_otherwise_the_stored_text():
    question = build_question(QuestionStatus.ASKED)
    labels = dict(CANONICAL_LABELS)
    record = QuoteTranslation("uk", quote_digest(question.quoted_text), "переклад")
    assert fact_question_message(question, labels).quote == question.quoted_text
    assert fact_question_message(question, labels, record, "uk").quote == "переклад"
    assert fact_question_message(question, labels, record, "fr").quote == question.quoted_text


def test_check_labels_is_unchanged_by_the_moved_constants():
    assert check_labels(dict(CANONICAL_LABELS)) is None
    for bad in ("see www.example.org", "see https://example.org", "[x", "a‮b"):
        assert check_labels({**CANONICAL_LABELS, "memory_digest_footer": bad}) is not None
