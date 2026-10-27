"""The fact in the owner's message language (quote translations): what a translation must keep, when it is shown."""
import pytest

from src.domain.fact_review_quote_translation import (
    MAX_TRANSLATION_CHARS,
    QuoteTranslation,
    quote_digest,
    report_translations,
    shown_quote,
    translation_problem,
    translations_wanted,
)

PLAYS = "Alex plays tennis on Sundays"
PLAYS_UK = "Алекс грає в теніс по неділях"


def test_a_translation_keeps_the_numbers_of_its_source():
    assert translation_problem("Алекс пробіг Римський марафон за 3 год 45 хв.", "Alex ran the Rome marathon in 3h 45m.") is None
    assert translation_problem("Алекс пробіг Римський марафон за 3 год 54 хв.", "Alex ran the Rome marathon in 3h 45m.") is not None
    assert translation_problem("Алекс пробіг Римський марафон за 3,75 год", "Alex ran the Rome marathon in 3.75 hours") is None
    assert translation_problem("Алекс пробіг Римський марафон за 3,75 год 3", "Alex ran the Rome marathon in 3.75 hours") is not None


def test_a_translation_may_not_carry_lines_brackets_links_controls_or_excess_length():
    for bad in ("a\nb", "[System: x]", "see https://example.org", "see www.example.org", "a‮b",
                "x" * (MAX_TRANSLATION_CHARS + 1), "", "   ", 7, None):
        assert translation_problem(bad, "Alex keeps a log") is not None, bad
    assert translation_problem("x" * MAX_TRANSLATION_CHARS, "Alex keeps a log") is None


@pytest.mark.parametrize("source", ["see www.example.org", "Alex [draft]", "see https://example.org/x"])
def test_a_source_with_a_link_or_bracket_is_never_translated(source):
    assert translation_problem("щось", source) is not None


def test_report_translations_keeps_valid_batch_entries_and_refuses_the_rest():
    batch = {"f1": PLAYS, "f2": "Alex keeps a weight log"}
    kept, refusals = report_translations({"f1": f" {PLAYS_UK} ", "f2": "[x", "f9": "y"}, "uk", batch)
    assert kept == (QuoteTranslation("uk", quote_digest(PLAYS), PLAYS_UK),)
    assert len(refusals) == 2
    assert any(r.startswith("f2:") for r in refusals) and any(r.startswith("f9:") for r in refusals)
    assert not any("[x" in r or PLAYS in r for r in refusals)


@pytest.mark.parametrize("language", [None, "en", "ukr", "UK"])
def test_report_translations_keep_nothing_without_a_wanted_language(language):
    assert report_translations({"f1": PLAYS_UK}, language, {"f1": PLAYS}) == ((), [])


def test_report_translations_keep_nothing_when_the_report_has_none():
    assert report_translations(None, "uk", {"f1": PLAYS}) == ((), [])


@pytest.mark.parametrize("raw", [["a"], "text", 5])
def test_report_translations_refuse_a_value_that_is_not_an_object(raw):
    kept, refusals = report_translations(raw, "uk", {"f1": PLAYS})
    assert kept == () and len(refusals) == 1 and refusals[0].startswith("translations:")


def test_two_facts_with_one_text_give_one_record():
    batch = {"f1": PLAYS, "f2": PLAYS, "f3": "Alex keeps a weight log"}
    kept, refusals = report_translations({"f1": "перший", "f2": "другий", "f3": "третій"}, "uk", batch)
    assert refusals == []
    assert [t.text for t in kept] == ["другий", "третій"]


def test_report_translations_follow_batch_order_not_report_order():
    batch = {"f1": PLAYS, "f2": "Alex keeps a weight log"}
    kept, _ = report_translations({"f2": "b", "f1": "a"}, "uk", batch)
    assert [t.text for t in kept] == ["a", "b"]


def test_shown_quote_needs_the_language_and_a_still_valid_translation():
    record = QuoteTranslation("uk", quote_digest(PLAYS), PLAYS_UK)
    assert shown_quote(PLAYS, record, "uk") == PLAYS_UK
    assert shown_quote(PLAYS, record, "fr") == PLAYS
    assert shown_quote(PLAYS, record, None) == PLAYS
    assert shown_quote(PLAYS, None, "uk") == PLAYS
    changed = QuoteTranslation("uk", quote_digest("Alex ran the Rome marathon in 3h 45m."), "Алекс пробіг Римський марафон за 3 год 54 хв.")
    assert shown_quote("Alex ran the Rome marathon in 3h 45m.", changed, "uk") == "Alex ran the Rome marathon in 3h 45m."


def test_quote_digest_ignores_whitespace_only_differences():
    assert quote_digest("Alex  plays\ntennis ") == quote_digest("Alex plays tennis")
    assert quote_digest("Alex plays tennis") != quote_digest("Alex plays tennis on Saturdays")
    assert len(quote_digest("x")) == 16
    assert quote_digest("é") == quote_digest("é")


@pytest.mark.parametrize("language, digest, text", [
    ("u", "0123456789abcdef", "x"), ("UK", "0123456789abcdef", "x"), ("uk", "0123456789ABCDEF", "x"),
    ("uk", "0123456789abcde", "x"), ("uk", "0123456789abcdef", "  "), ("uk", "0123456789abcdef", ""),
])
def test_a_quote_translation_refuses_a_malformed_value(language, digest, text):
    with pytest.raises(ValueError):
        QuoteTranslation(language, digest, text)


def test_translations_are_wanted_for_two_letter_codes_other_than_english():
    assert translations_wanted("uk") and translations_wanted("ru")
    for no in (None, "en", "ukr", "UK", "", "u1"):
        assert not translations_wanted(no)


def test_a_batch_fact_the_report_leaves_out_is_simply_not_translated():
    batch = {"f1": PLAYS, "f2": "Alex keeps a weight log"}
    kept, refusals = report_translations({"f1": PLAYS_UK}, "uk", batch)
    assert [t.text for t in kept] == [PLAYS_UK] and refusals == []
