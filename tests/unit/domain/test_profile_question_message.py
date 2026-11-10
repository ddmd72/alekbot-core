"""The message of a profile question (fact profile RFC §4.7): the kind's line and the printed words, the quoted fact, the
candidates, then the fixed options; digits number them in that order. The history frame of a profile question, the labels in
every UI language, and the label check over the new options. Invented cast."""
import importlib

import pytest

from src.domain.fact_review_frame import question_text, review_frame
from src.domain.fact_review_language import CANONICAL_LABELS, LABEL_KEYS, check_labels, fact_question_message
from src.domain.fact_review_quote_translation import QuoteTranslation, quote_digest
from src.domain.interactive_message import InteractiveMessage
from src.domain.ui_messages import UIMessage
from tests.support.profile_question_builders import DOE_FACT, ROME_FACT, profile_question

EN = dict(CANONICAL_LABELS)


def message(kind: str, **over) -> InteractiveMessage:
    return fact_question_message(profile_question(kind, **over), EN)


def test_a_place_question_offers_its_candidates_then_keep_and_dont_remember():
    shown = message("place")
    assert shown.prompt == "Place to confirm: Rome" and shown.quote == ROME_FACT
    assert [(o.id, o.label) for o in shown.options] == [
        ("pick1", "Rome, Italy"), ("pick2", "Rome, United States"), ("keep", "Keep as written"),
        ("dont_know", "Don't remember")]
    assert [number for number, _ in shown.numbered_options()] == [1, 2, 3, 4]
    assert shown.option_for_reply("1") == "pick1" and shown.option_for_reply("3") == "keep"
    assert shown.option_for_reply("4") == "dont_know" and shown.option_for_reply("5") is None
    assert shown.binding.kind == "fact_question" and shown.binding.id == "pq-place"
    assert shown.reply_hint == EN[UIMessage.MEMORY_REPLY_HINT.value]


def test_a_person_question_offers_its_entries_then_not_on_my_list_and_dont_remember():
    shown = message("person")
    assert shown.prompt == "Person to confirm: Doe" and shown.quote == DOE_FACT
    assert [(o.id, o.label) for o in shown.options] == [
        ("pick1", "Alex Doe"), ("pick2", "Jamie Doe — daughter"), ("not_listed", "Not on my list"),
        ("dont_know", "Don't remember")]
    assert shown.option_for_reply("2") == "pick2" and shown.option_for_reply("3") == "not_listed"


def test_a_date_question_offers_dont_remember_only():
    shown = message("date")
    assert shown.prompt == "Date to confirm: on 2026-10-01"
    assert [(o.id, o.label) for o in shown.options] == [("dont_know", "Don't remember")]
    assert shown.option_for_reply("1") == "dont_know" and shown.option_for_reply("2") is None


def test_the_printed_words_are_never_translated_and_the_quote_is_when_a_translation_is_stored():
    question = profile_question("place")
    translation = QuoteTranslation(language="pt", source_digest=quote_digest(question.quoted_text),
                                   text="Alex tem uma viagem a Roma")
    shown = fact_question_message(question, EN, translation, "pt")
    assert shown.quote == "Alex tem uma viagem a Roma" and shown.prompt.endswith(": Rome")


def test_a_lifecycle_question_is_built_as_before():
    from tests.support.review_rule_builders import question
    shown = fact_question_message(question(), EN)
    assert shown.prompt == "Is this still true?" and shown.min_options == 2 and len(shown.options) == 4


def test_the_history_text_names_the_candidates_and_claims_no_change():
    text = question_text(message("place"))
    assert text == ("Place to confirm: Rome\n«Alex has a trip to Rome»\n"
                    "1 — Rome, Italy  2 — Rome, United States  3 — Keep as written  4 — Don't remember")


def test_the_history_frame_of_a_profile_question_and_of_a_lifecycle_one():
    assert review_frame("pq1", profile=True) == (
        "[System: memory review q=pq1 — Alek asked the owner which place, person or date a fact means]")
    assert review_frame("q1") == "[System: memory review q=q1 — Alek asked the owner about a fact he could not confirm]"
    assert review_frame("q1", profile=False) == review_frame("q1")


# ------------------------------------------------------------------------------------------------ the labels

NEW_KEYS = {
    UIMessage.MEMORY_PROFILE_PLACE_QUESTION.value, UIMessage.MEMORY_PROFILE_PERSON_QUESTION.value,
    UIMessage.MEMORY_PROFILE_DATE_QUESTION.value, UIMessage.MEMORY_OPTION_KEEP_AS_WRITTEN.value,
    UIMessage.MEMORY_OPTION_NOT_LISTED.value, UIMessage.MEMORY_PROFILE_NOT_READ.value,
    UIMessage.MEMORY_PROFILE_DATE_NOT_READ.value,
}


def test_the_new_texts_are_label_keys_with_canonical_english_texts():
    assert NEW_KEYS <= LABEL_KEYS and NEW_KEYS <= set(CANONICAL_LABELS)
    assert set(CANONICAL_LABELS) == LABEL_KEYS


@pytest.mark.parametrize("language", ["en", "uk", "fr", "es"])
def test_every_ui_language_has_the_new_texts_and_the_whole_set_passes_the_label_check(language):
    module = importlib.import_module(f"src.locales.{language}")
    strings = next(v for v in vars(module).values() if isinstance(v, dict) and "memory_option_paid" in v)
    labels = {key: strings[key] for key in LABEL_KEYS}
    assert check_labels(labels) is None
    if language == "en":
        assert labels == dict(CANONICAL_LABELS)


def test_the_check_refuses_a_set_whose_profile_options_read_alike():
    labels = dict(CANONICAL_LABELS)
    labels[UIMessage.MEMORY_OPTION_KEEP_AS_WRITTEN.value] = labels[UIMessage.MEMORY_OPTION_DONT_KNOW.value]
    assert "place question must differ" in str(check_labels(labels))
    labels = dict(CANONICAL_LABELS)
    labels[UIMessage.MEMORY_OPTION_NOT_LISTED.value] = labels[UIMessage.MEMORY_OPTION_DONT_KNOW.value]
    assert "person question must differ" in str(check_labels(labels))


def test_a_new_option_label_is_held_to_the_option_bounds_and_a_line_to_the_text_bound():
    labels = dict(CANONICAL_LABELS)
    labels[UIMessage.MEMORY_OPTION_NOT_LISTED.value] = "one two three four five"
    assert "more than 4 words" in str(check_labels(labels))
    labels = dict(CANONICAL_LABELS)
    labels[UIMessage.MEMORY_PROFILE_PLACE_QUESTION.value] = "x " * 70
    assert "too long" in str(check_labels(labels))


def test_a_one_option_message_is_a_date_questions_alone():
    from src.domain.interactive_binding import InteractiveBinding
    from src.domain.interactive_option import InteractiveOption
    kwargs = dict(prompt="p", options=(InteractiveOption(id="a", label="a"),),
                  binding=InteractiveBinding(kind="fact_question", id="x"))
    with pytest.raises(ValueError, match="2-5"):
        InteractiveMessage(**kwargs)
    assert InteractiveMessage(**kwargs, min_options=1).options[0].id == "a"
