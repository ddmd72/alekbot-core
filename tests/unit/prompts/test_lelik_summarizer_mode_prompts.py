"""One summarizer, one prompt per call mode (VOICE_CALL_LIVE_TEXT_RFC §4.11): the interpreter and tutor profiles render
their own cognitive text and the shared output format, take the language from the owner_language block and never
mirror the transcript; the companion profile keeps its text, and no shared text speaks of one mode."""
import re
from pathlib import Path

import pytest

from firestore_utils.snapshot_serializer import token_from_file
from src.domain.owner_language_block import OWNER_LANGUAGE_BLOCK
from tests.support import prompt_vocabulary as pv
from tests.support.prompt_render import render_static, rendered_token_ids

_TOKENS = Path(__file__).resolve().parents[3] / "prompts_snapshot" / "tokens" / "system"
_CYRILLIC = re.compile("[Ѐ-ӿ]")
_FILE_MODES = {
    "lelik_summarizer_interpreter": "COGNITIVE_PROCESS_LELIK_SUMMARIZER_INTERPRETER",
    "lelik_summarizer_tutor": "COGNITIVE_PROCESS_LELIK_SUMMARIZER_TUTOR",
}
# The companion's own tokens; the rest of its profile is exactly the live companion call's character
# (`_live_call_character`), derived so that this file names no character token.
_COMPANION_OWN = {"COGNITIVE_PROCESS_LELIK_SUMMARIZER", "LANG_MIRROR", "OUTPUT_FORMAT_LELIK_SUMMARIZER"}
_COMPANION_CHARACTER_COUNT = 4


def _content(token_id: str) -> str:
    return token_from_file((_TOKENS / f"{token_id}.groovy").read_text(encoding="utf-8"))["content"]


@pytest.mark.parametrize("profile,cognitive", sorted(_FILE_MODES.items()))
def test_a_file_mode_renders_its_own_text_and_the_shared_output_format(profile, cognitive):
    assert sorted(pv.load_profile(profile)["tokens"]) == sorted([cognitive, "OUTPUT_FORMAT_LELIK_SUMMARIZER"])
    assert rendered_token_ids(profile) == sorted([cognitive, "OUTPUT_FORMAT_LELIK_SUMMARIZER"])
    prompt = render_static(profile)
    assert "Your note opens that file" in prompt
    assert "Code reads your output, so it is one JSON object and nothing else" in prompt
    assert pv.load_profile(profile)["blueprint_id"] == pv.load_profile("lelik_summarizer")["blueprint_id"]


@pytest.mark.parametrize("profile", sorted(_FILE_MODES))
def test_a_file_mode_has_no_language_slot_and_names_the_owner_language_block(profile):
    """The owner's language reaches a file mode only through the block code renders; no LANG_* token (the mirror
    would follow the two-language transcript) is in the prompt, and a user override has no slot to land in."""
    assert not [t for t in rendered_token_ids(profile) if t.startswith("LANG_")]
    assert "Mirrors the language" not in render_static(profile)
    assert f"the {OWNER_LANGUAGE_BLOCK} block gives" in _content(_FILE_MODES[profile])


@pytest.mark.parametrize("token_id", [*_FILE_MODES.values(), "OUTPUT_FORMAT_LELIK_SUMMARIZER"])
def test_the_new_and_shared_tokens_have_no_cyrillic(token_id):
    assert _CYRILLIC.search((_TOKENS / f"{token_id}.groovy").read_text(encoding="utf-8")) is None


def test_the_interpreter_never_credits_the_owner_with_the_other_persons_words():
    text = _content("COGNITIVE_PROCESS_LELIK_SUMMARIZER_INTERPRETER")
    assert "Say who said something only when the turns make it clear" in text
    assert "never as the owner's own words" in text
    assert "VOICE FIRST" not in text


def test_the_tutor_keeps_the_lesson_material():
    text = _content("COGNITIVE_PROCESS_LELIK_SUMMARIZER_TUTOR")
    assert "the mistakes you corrected, each with its right form" in text
    assert "VOICE FIRST" not in text


def _live_call_character() -> set:
    """The live companion call's character, read from lelik_xai's profile: its `properties` tokens that are Lelik's
    own (id ends `_LELIK`). The call-only manners are not Lelik's own, and the tool-data rule is a policy."""
    tokens = pv.load_tokens(pv.load_profile("lelik_xai"))
    return {token_id for token_id, (cls, _) in tokens.items() if cls == "properties" and token_id.endswith("_LELIK")}


def test_the_companion_profile_is_unchanged():
    character = _live_call_character()
    assert len(character) == _COMPANION_CHARACTER_COUNT
    expected = _COMPANION_OWN | character
    assert set(pv.load_profile("lelik_summarizer")["tokens"]) == expected
    assert set(rendered_token_ids("lelik_summarizer")) == expected


def test_the_companion_text_is_the_chat_note_again_with_one_emoji_clause():
    text = _content("COGNITIVE_PROCESS_LELIK_SUMMARIZER")
    assert "you retell it to them in a chat note" in text
    assert "Alek, the exocortex you are the voice of, reads the note in the history" in text
    assert "It opens with a word, never an emoji.\"" in text
    assert "first word of summary" not in text


@pytest.mark.parametrize("token_id", ["COGNITIVE_PROCESS_LELIK_SUMMARIZER", "OUTPUT_FORMAT_LELIK_SUMMARIZER"])
def test_no_text_the_companion_reads_speaks_of_one_mode(token_id):
    """A sentence that cannot hold for one mode is fixed in code (a text per mode), never with a clause per mode."""
    text = _content(token_id).lower()
    assert "interpreter" not in text and "tutor" not in text
    if token_id.startswith("OUTPUT_FORMAT"):
        assert "companion" not in text
