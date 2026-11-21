"""The call-opening note's text: fixed mode speaks the language's own text, mirror mode the English text with the
language sentence only when the history holds a message of the caller's (decisions/lelik_call_opening.md)."""
import pytest

from src.domain.language import LanguageCode
from src.domain.voice_opening import MIRROR_TAIL, voice_opening_text

_EN = voice_opening_text(False, LanguageCode.EN, False)


@pytest.mark.parametrize("has_caller_message", [False, True])
@pytest.mark.parametrize("language", list(LanguageCode))
def test_fixed_mode_is_the_languages_text_without_a_language_sentence(language, has_caller_message):
    text = voice_opening_text(False, language, has_caller_message)

    assert MIRROR_TAIL not in text
    assert text == voice_opening_text(False, language, False)


def test_the_four_languages_have_four_different_texts():
    assert len({voice_opening_text(False, language, False) for language in LanguageCode}) == len(LanguageCode)


@pytest.mark.parametrize("language", list(LanguageCode))
def test_mirror_mode_is_english_with_the_tail_only_after_a_caller_message(language):
    assert voice_opening_text(True, language, True) == f"{_EN} {MIRROR_TAIL}"
    assert voice_opening_text(True, language, False) == _EN


@pytest.mark.parametrize("entry,expected", [
    ({"role": "user", "content": "hello"}, True),
    ({"role": "user", "content": "  [System: web call with Lelik]"}, False),
    ({"role": "alek", "content": "hello"}, False),
])
def test_only_a_user_entry_the_caller_wrote_counts_as_a_caller_message(entry, expected):
    from src.domain.voice_opening import is_caller_message

    assert is_caller_message(entry) is expected
