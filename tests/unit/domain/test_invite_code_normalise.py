"""Invite code input normalisation, grouping and hint (SECRETS_AT_REST_RFC §2.5, S5): every digest is taken over the
normalised form, so typed input that differs in case, spaces or dashes finds the same invite."""
import pytest

from src.domain.invite_code import group_invite_code, invite_hint, normalise_invite_code

PLAIN = "ABCDEFGHIJKLMNOPQRSTUVWXYZ"          # synthetic, 26 characters
GROUPED = "ABCDE-FGHIJ-KLMNO-PQRST-UVWXYZ"


@pytest.mark.parametrize("typed", [
    GROUPED, GROUPED.lower(), GROUPED.replace("-", " "), f"  {GROUPED}\n", "abcde fghij-klmno PQRST uvwxyz", PLAIN,
])
def test_typed_variants_normalise_to_one_form(typed):
    assert normalise_invite_code(typed) == PLAIN


def test_normalise_is_idempotent_and_keeps_other_characters():
    assert normalise_invite_code(normalise_invite_code("a-b c2")) == "ABC2"


def test_grouping_is_5_5_5_5_6():
    assert group_invite_code(PLAIN) == GROUPED
    assert group_invite_code(GROUPED.lower()) == GROUPED


def test_grouping_leaves_a_code_of_another_length_normalised():
    assert group_invite_code("ab-c") == "ABC"


def test_hint_is_the_last_four_normalised_characters():
    assert invite_hint(GROUPED.lower()) == "WXYZ"
    assert invite_hint(PLAIN) == "WXYZ"


def test_hint_of_a_short_value_is_what_there_is():
    assert invite_hint("a-b") == "AB"
