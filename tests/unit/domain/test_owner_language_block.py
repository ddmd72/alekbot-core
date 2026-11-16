import pytest

from src.domain.language import LanguageCode
from src.domain.owner_language_block import OWNER_LANGUAGE_BLOCK, render_owner_language_block
from src.domain.voice_mode import CALL_FILE_SUMMARIZER_TYPES, VoiceMode


@pytest.mark.parametrize("language", list(LanguageCode))
def test_the_block_names_the_language_by_its_iso_code(language):
    block = render_owner_language_block(language)
    assert block == f'{OWNER_LANGUAGE_BLOCK} {{\n    iso_639_1: "{language.value}"\n}}'


def test_every_mode_but_the_companion_has_its_own_summarizer_type():
    assert set(CALL_FILE_SUMMARIZER_TYPES) == {VoiceMode.INTERPRETER, VoiceMode.TUTOR}
    assert len(set(CALL_FILE_SUMMARIZER_TYPES.values())) == 2
    assert "voice" not in CALL_FILE_SUMMARIZER_TYPES.values()
