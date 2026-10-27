"""The language of the review's messages, the label checks and the one fact-question message (Facts S4 · T1d, §3.17)."""
import pytest

from src.domain.fact_review_answer_kind import AnswerKind
from src.domain.fact_review_language import (
    CANONICAL_LABELS,
    LABEL_KEYS,
    MAX_OPTION_WORDS,
    MAX_TEXT_CHARS,
    OPTION_KEYS,
    check_labels,
    fact_question_message,
    label_source,
    message_language,
    parse_message_language,
    ui_strings_language,
)
from src.domain.fact_review_options import OPTIONS
from src.domain.fact_review_question import FACT_QUESTION_KIND
from src.domain.fact_review_state import FactReviewState
from src.domain.interactive_option import MAX_LABEL_CHARS
from src.domain.language import LanguageCode
from src.domain.ui_messages import UIMessage
from src.locales import en, es, fr, uk
from tests.support.review_rule_builders import question, state

UK, EN, FR, ES = LanguageCode.UK, LanguageCode.EN, LanguageCode.FR, LanguageCode.ES
OPTION = UIMessage.MEMORY_OPTION_ENDED.value
QUESTION_LINE = UIMessage.MEMORY_REVIEW_QUESTION.value
FOOTER = UIMessage.MEMORY_DIGEST_FOOTER.value


# ------------------------------------------------------------------------------------------------ message_language

def test_fixed_is_the_owners_language_whatever_the_state_says():
    assert message_language(FR, False, EN, state(message_language="ru"), UK) == "fr"


def test_fixed_without_a_preference_is_the_system_default():
    assert message_language(None, False, ES, state(message_language="ru"), UK) == "es"
    assert message_language(None, False, EN, None, UK) == "en"


def test_mirror_is_the_language_the_last_pass_reported_even_when_it_has_no_ui_strings():
    assert message_language(FR, True, EN, state(message_language="ru"), UK) == "ru"
    assert message_language(None, True, EN, state(message_language="de"), UK) == "de"


@pytest.mark.parametrize("current", [None, state(), state(message_language=None)])
def test_mirror_with_no_reported_language_yet_is_the_ui_language(current):
    assert message_language(FR, True, EN, current, UK) == "uk"


# ------------------------------------------------------------------------------------------------ the label set

def test_the_label_set_is_the_fifteen_ui_strings_of_the_plan():
    names = [
        "REVIEW_QUESTION", "OPTION_STILL_TRUE", "OPTION_ENDED", "OPTION_NEVER", "OPTION_DONT_KNOW", "REPLY_HINT",
        "ANSWER_NOTED", "REPLY_NOTED", "ALREADY_ANSWERED", "FACT_CHANGED", "QUESTION_CLOSED", "DIGEST_HEADER",
        "DIGEST_FOOTER", "DIGEST_ARCHIVED", "DIGEST_INVALIDATED",
        # P2 revised (C11): the 13 new strings, 28 in all
        "REVIEW_QUESTION_HAPPENED", "REVIEW_QUESTION_RENEWED", "REVIEW_QUESTION_PAID", "OPTION_HAPPENED",
        "OPTION_NOT_HAPPENED", "OPTION_RENEWED", "OPTION_NOT_RENEWED", "OPTION_NOT_NEEDED", "OPTION_PAID",
        "OPTION_NOT_PAID", "DIGEST_HISTORY_HEADER", "DIGEST_INVOICES_HEADER", "DIGEST_INVOICES_CABINET",
    ]
    assert len(names) == len(LABEL_KEYS) == 28
    assert LABEL_KEYS == {UIMessage[f"MEMORY_{name}"].value for name in names}
    assert set(CANONICAL_LABELS) == LABEL_KEYS
    assert list(OPTION_KEYS) == list(AnswerKind)


def test_the_canonical_labels_are_the_english_ui_strings():
    assert {key: en.UI_STRINGS[key] for key in LABEL_KEYS} == dict(CANONICAL_LABELS)


@pytest.mark.parametrize("mod", [uk, en, fr, es], ids=["uk", "en", "fr", "es"])
def test_every_locales_strings_pass_the_checks_a_model_written_set_must_pass(mod):
    assert check_labels({key: mod.UI_STRINGS[key] for key in LABEL_KEYS}) is None


def test_the_english_digest_footer_is_the_plans_sentence():
    assert CANONICAL_LABELS[FOOTER] == "To bring one back, tell me in chat that it is still true."


def test_the_four_english_options_read_as_the_plans_line():
    # P2 revised (C11): OPTION_KEYS spans every answer; the still_true question's four options read as S4's line, and
    # the new options as the spec's P2-3 table
    labels = [CANONICAL_LABELS[OPTION_KEYS[kind]] for kind in OPTIONS["still_true"]]
    assert labels == ["Yes", "No longer", "Never", "Don't remember"]
    assert [CANONICAL_LABELS[key] for key in OPTION_KEYS.values()] == [
        "Yes", "No longer", "Never", "Don't remember",
        "It happened", "It did not", "Renewed", "Not renewed", "No longer needed", "Paid", "Not paid",
    ]


# ------------------------------------------------------------------------------------------------ check_labels

def good(**over):
    labels = dict(CANONICAL_LABELS)
    labels.update(over)
    return labels


def test_a_good_set_passes_and_a_translation_passes():
    assert check_labels(dict(CANONICAL_LABELS)) is None
    russian = good(**{
        QUESTION_LINE: "Это всё ещё так?", OPTION_KEYS[AnswerKind.STILL_TRUE]: "Да", OPTION_KEYS[AnswerKind.ENDED]: "Уже нет",
        OPTION_KEYS[AnswerKind.NEVER]: "Никогда", OPTION_KEYS[AnswerKind.DONT_KNOW]: "Не помню",
    })
    assert check_labels(russian) is None


def test_the_keys_must_be_exactly_the_label_keys():
    missing = dict(CANONICAL_LABELS)
    del missing[FOOTER]
    assert check_labels(missing) is not None
    assert check_labels({**CANONICAL_LABELS, "extra": "text"}) is not None
    assert check_labels({}) is not None


@pytest.mark.parametrize("key", sorted(LABEL_KEYS))
@pytest.mark.parametrize("bad", [
    "two\nlines", "two\r\nlines", "a b", "with 1 digit", "digit ٣ arabic", "has [bracket",
    "see https://example.com", "see www.example.com", "ftp://x", "", "   ", "\n",
    "evil\u202etext", "zero\u200bwidth", "joiner\u200d", "bell\x07", "tab\there", "bom\ufeff",
], ids=lambda v: repr(v)[:20])
def test_each_bad_shape_is_refused_for_every_text(key, bad):
    assert check_labels(good(**{key: bad})) is not None


@pytest.mark.parametrize("key", sorted(LABEL_KEYS))
@pytest.mark.parametrize("shape", ["has <!channel>", "closing > sign"], ids=lambda v: repr(v)[:20])
def test_angle_brackets_are_accepted_for_every_text_because_escaping_is_the_platform_adapters(key, shape):
    # T5c (L4): these two shapes were refused here until the Slack adapter took over escaping `&`, `<` and `>`.
    assert check_labels(good(**{key: shape})) is None


@pytest.mark.parametrize("bad", [None, 3, ["Yes"], {"a": "b"}, b"Yes"])
def test_a_value_that_is_not_text_is_refused(bad):
    assert check_labels(good(**{OPTION: bad})) is not None


def test_a_trailing_newline_is_not_a_second_line():
    assert check_labels(good(**{QUESTION_LINE: "Is this still true?\n"})) is None


@pytest.mark.parametrize("key", sorted(OPTION_KEYS.values()))
def test_an_option_label_is_at_most_75_characters_and_four_words(key):
    assert check_labels(good(**{key: "x" * MAX_LABEL_CHARS})) is None
    assert check_labels(good(**{key: "x" * (MAX_LABEL_CHARS + 1)})) is not None
    assert check_labels(good(**{key: "one two three four"})) is None
    assert check_labels(good(**{key: "one two three four five"})) is not None
    assert MAX_OPTION_WORDS == 4 and MAX_LABEL_CHARS == 75


@pytest.mark.parametrize("key", sorted(LABEL_KEYS - set(OPTION_KEYS.values())))
def test_every_other_text_is_at_most_120_characters_and_may_have_many_words(key):
    assert check_labels(good(**{key: "x" * MAX_TEXT_CHARS})) is None
    assert check_labels(good(**{key: "x" * (MAX_TEXT_CHARS + 1)})) is not None
    assert check_labels(good(**{key: "one two three four five six seven"})) is None
    assert MAX_TEXT_CHARS == 120


def test_the_length_is_counted_without_the_padding():
    assert check_labels(good(**{OPTION: " " + "x" * MAX_LABEL_CHARS + " "})) is None


def test_two_options_with_the_same_label_are_refused_whatever_the_case_and_padding():
    yes, ended = OPTION_KEYS[AnswerKind.STILL_TRUE], OPTION_KEYS[AnswerKind.ENDED]
    assert check_labels(good(**{ended: "Yes"})) is not None
    assert check_labels(good(**{ended: " yes "})) is not None
    assert check_labels(good(**{yes: "Ok", ended: "OK"})) is not None
    # the same words in an option and in another text are fine
    assert check_labels(good(**{QUESTION_LINE: "Yes"})) is None


def test_the_refusal_names_the_key_that_failed():
    assert OPTION in check_labels(good(**{OPTION: "has 1 digit"}))


# ------------------------------------------------------------------------------------------------ fact_question_message

def test_the_question_message_is_the_line_the_fact_the_four_options_and_the_hint():
    q = question(question_id="q42", quoted_text="Lives in Lisbon")
    message = fact_question_message(q, CANONICAL_LABELS)
    assert message.prompt == "Is this still true?"
    assert message.quote == "Lives in Lisbon"
    assert [(o.id, o.label) for o in message.options] == [
        ("still_true", "Yes"), ("ended", "No longer"), ("never", "Never"), ("dont_know", "Don't remember"),
    ]
    assert message.reply_hint == "Reply to this message with a digit, or in your own words."
    assert message.binding.kind == FACT_QUESTION_KIND == "fact_question"
    assert message.binding.id == "q42"


def test_the_digits_are_the_answers_in_every_language():
    from src.domain.fact_review_answers import digit_answer
    for mod in (uk, en, fr, es):
        message = fact_question_message(question(), {key: mod.UI_STRINGS[key] for key in LABEL_KEYS})
        # P2 revised (C11): the digits number the options of the question's kind; S4's still_true question keeps 1-4
        assert [digit_answer(message, d) for d in "1234"] == list(OPTIONS["still_true"])
        assert [o.id for o in message.options] == [k.value for k in OPTIONS["still_true"]]


def test_the_fact_is_quoted_as_stored_in_any_language():
    labels = {key: uk.UI_STRINGS[key] for key in LABEL_KEYS}
    message = fact_question_message(question(quoted_text="Живе в Лісабоні — «стара адреса»"), labels)
    assert message.quote == "Живе в Лісабоні — «стара адреса»"
    assert message.prompt == uk.UI_STRINGS[QUESTION_LINE]


def test_a_label_set_that_passes_the_check_can_always_be_posted():
    distinct = good(**{key: chr(ord("a") + i) * MAX_LABEL_CHARS for i, key in enumerate(OPTION_KEYS.values())})
    assert check_labels(distinct) is None
    assert len(fact_question_message(question(), distinct).options) == 4


def test_missing_labels_are_named_and_nothing_is_built():
    partial = dict(CANONICAL_LABELS)
    del partial[QUESTION_LINE]
    with pytest.raises(ValueError, match=QUESTION_LINE):
        fact_question_message(question(), partial)


def test_a_label_the_option_cannot_carry_is_the_messages_own_refusal_not_a_silent_cut():
    with pytest.raises(ValueError):
        fact_question_message(question(), {**CANONICAL_LABELS, OPTION: "x" * (MAX_LABEL_CHARS + 1)})


def test_an_empty_text_is_refused_for_being_no_text_not_for_its_lines():
    for empty in ("", "   ", "\n"):
        assert "not a text" in check_labels(good(**{OPTION: empty}))


def test_a_control_or_format_character_is_named():
    assert "control or format" in check_labels(good(**{OPTION: "evil\u202etext"}))
    assert "control or format" in check_labels(good(**{QUESTION_LINE: "zero\u200bwidth"}))


def test_ordinary_punctuation_and_letters_in_any_script_are_not_control_characters():
    assert check_labels(good(**{QUESTION_LINE: "¿Sigue siendo cierto? — «да», 好吗？"})) is None


# ------------------------------------------------------------------------------------------------ a reported language (M3)

@pytest.mark.parametrize("raw, parsed", [("ru", "ru"), ("RU", "ru"), (" Uk ", "uk"), ("de", "de"), ("\nfr\n", "fr")])
def test_a_reported_language_is_two_ascii_letters_lowercased(raw, parsed):
    assert parse_message_language(raw) == parsed


@pytest.mark.parametrize("raw", ["ru-RU", "Russian", "", "  ", None, 3, ["ru"], "r", "rus", "r1", "р", "рu", "ru\nuk", b"ru"])
def test_anything_else_is_no_language(raw):
    assert parse_message_language(raw) is None


@pytest.mark.parametrize("code, language", [("uk", UK), ("en", EN), ("fr", FR), ("es", ES), ("UK", UK), (" es ", ES)])
def test_four_languages_have_ui_strings(code, language):
    assert ui_strings_language(code) is language


@pytest.mark.parametrize("code", ["ru", "de", "ru-RU", "", "Ukrainian", "xx"])
def test_any_other_language_has_no_ui_strings(code):
    assert ui_strings_language(code) is None


def test_mirror_with_a_stored_code_in_the_wrong_case_reads_it_as_the_code():
    stored = FactReviewState.model_construct(account_id="acc", message_language="UK")  # the state itself refuses this
    assert message_language(None, True, EN, stored, FR) == "uk"
    bad = FactReviewState.model_construct(account_id="acc", message_language="Russian")
    assert message_language(None, True, EN, bad, FR) == "fr"


def test_label_source_is_ui_for_the_four_languages_whatever_is_cached():
    assert label_source("uk", None) == "ui"
    assert label_source("ES", {"junk": "x"}) == "ui"


def test_label_source_is_the_model_set_only_while_it_passes_the_check_now():
    assert label_source("ru", good()) == "model"
    assert label_source("ru", None) == "fallback"
    assert label_source("ru", {}) == "fallback"
    assert label_source("ru", good(**{OPTION: "has 1 digit"})) == "fallback"
    older = dict(CANONICAL_LABELS)
    del older[FOOTER]  # a set written before a key was added
    assert label_source("ru", older) == "fallback"
    assert label_source("ru-RU", good()) == "fallback"
