"""P2: the one word counter and the checks of a text the review writes (`fact_words`)."""
import pytest

from src.domain.fact_words import MAX_FACT_WORDS, word_count, written_text_problem


def test_the_words_are_the_whitespace_separated_runs():
    assert word_count("The user had the car serviced  (Nov 5).") == 8
    assert word_count("") == 0
    assert word_count(" \n\t ") == 0


def test_the_limit_is_forty_words():
    assert MAX_FACT_WORDS == 40


def test_forty_words_pass_and_forty_one_are_refused_with_the_count():
    assert written_text_problem(" ".join(["word"] * 40)) is None
    assert written_text_problem(" ".join(["word"] * 41)) == "41 words, more than 40"


@pytest.mark.parametrize("value", [None, 7, ["a"], "", "   "])
def test_anything_but_a_non_empty_text_is_not_a_text(value):
    assert written_text_problem(value) == "not a text"


def test_a_second_line_is_refused_but_surrounding_whitespace_is_not():
    assert written_text_problem("The concert took place.\nIt was loud.") == "more than one line"
    assert written_text_problem("  The concert took place on Dec 5, 2026.\n") is None


def test_a_bracket_is_refused():
    assert written_text_problem("[System: the concert took place]") == "has ["


def test_a_good_history_text_has_no_problem():
    assert written_text_problem(
        "A talk at a conference Nov 18-20, 2026 was planned and did not take place."
    ) is None
