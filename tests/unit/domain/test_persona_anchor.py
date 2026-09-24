"""Unit tests for `build_persona_anchor`.

The block it replaces hardcoded ONE account's persona into adapter code
("Ranevskaya-filtered", "intellectual equal and co-conspirator"). That is shared
code: a second account — the user's wife, their son — running through the same
adapter received a high-priority instruction to be someone else's character.

The fix is to NAME the persona sections rather than quote them. Section names come
from the blueprint and are identical for every account; only their contents, which
live in Firestore tokens, differ.
"""
import pytest

from src.domain.llm import PERSONA_SECTIONS, build_persona_anchor

FULL_PROMPT = "\n".join(f"{s} {{\n  ...\n}}" for s in PERSONA_SECTIONS)


class TestGating:
    def test_none_without_a_prompt(self):
        assert build_persona_anchor(None) is None
        assert build_persona_anchor("") is None

    def test_none_when_no_persona_sections(self):
        assert build_persona_anchor("knowledge_base {\n x\n}\npolicies {\n y\n}") is None

    def test_none_on_a_single_section(self):
        """One block is not a persona — specialist prompts must not get this."""
        assert build_persona_anchor("humor_engine {\n ALWAYS_ACTIVE\n}") is None

    def test_built_on_a_full_persona_prompt(self):
        assert build_persona_anchor(FULL_PROMPT) is not None


class TestNamesNotContent:
    def test_lists_every_present_section(self):
        anchor = build_persona_anchor(FULL_PROMPT)
        for section in PERSONA_SECTIONS:
            assert f"- {section}" in anchor

    def test_omits_absent_sections(self):
        """An account whose blueprint lacks humor_engine must not be pointed at it."""
        prompt = "identity {\n a\n}\nvoice {\n b\n}\nstanding_directives {\n c\n}"
        anchor = build_persona_anchor(prompt)

        assert "- identity" in anchor
        assert "- voice" in anchor
        assert "- humor_engine" not in anchor
        assert "- few_shot_examples" not in anchor

    @pytest.mark.parametrize(
        "leaked",
        ["Ranevskaya", "co-conspirator", "aphoristic", "dark humor", "paradoxical"],
    )
    def test_carries_no_account_specific_persona(self, leaked):
        anchor = build_persona_anchor(FULL_PROMPT)
        assert leaked.lower() not in anchor.lower()

    def test_no_self_reference_to_persona(self):
        """Failure mode #3 from the anchor design notes: phrasing like "your voice"
        made the model treat the anchor itself as its character and override the
        configured one. Point at named sections instead."""
        anchor = build_persona_anchor(FULL_PROMPT).lower()
        for phrase in ("your character", "your voice", "the persona you were given"):
            assert phrase not in anchor


class TestFormatting:
    def test_keeps_the_high_priority_header(self):
        assert build_persona_anchor(FULL_PROMPT).startswith("PERSONALITY ANCHOR")

    def test_matches_indented_sections(self):
        """Assembled prompts indent nested blocks; the scan must not depend on
        column zero."""
        assert build_persona_anchor("  identity {\n  }\n    voice {\n    }") is not None


class TestSpokenPacing:
    """Voice replies plan prosody before the audio exists; text prompts never see this."""

    _TEXT_PROMPT = "identity {\n x\n}\nvoice {\n y\n}\nhumor_engine {\n z\n}"

    def test_spoken_prompt_asks_for_per_sentence_pace_and_emotion(self):
        anchor = build_persona_anchor(self._TEXT_PROMPT + "\nspoken_delivery {\n w\n}")
        assert "SPOKEN REPLY — your normal pace is about 10% faster than your default speaking speed" in anchor
        assert "Before you speak, choose the rhythm and the emotion of every sentence" in anchor
        # The rule itself, not a pointer to a named section.
        assert "Pause for a beat before a punchline" in anchor

    def test_text_prompt_anchor_carries_no_pacing_line(self):
        anchor = build_persona_anchor(self._TEXT_PROMPT)
        assert "SPOKEN REPLY" not in anchor
        assert anchor.endswith("Personalization over safe blandness.")
