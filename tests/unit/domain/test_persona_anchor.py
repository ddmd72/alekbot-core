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

    def test_spoken_prompt_asks_for_natural_phone_speech(self):
        anchor = build_persona_anchor(self._TEXT_PROMPT + "\nspoken_delivery {\n w\n}")
        assert "SPOKEN REPLY — speak like a regular person on a spontaneous phone call" in anchor
        # Disfluencies stay occasional: the anchor repeats every turn, a mandatory one is a tic.
        assert "not on every line" in anchor
        # Delivery rules leaked into content ("no pathos, no narrator theatre") — 2026-09-28.
        assert "How you sound is never a topic" in anchor
        # Per-sentence prosody technique read as a narrator on live calls (2026-09-25).
        assert "choose the rhythm and the emotion of every sentence" not in anchor

    def test_spoken_prompt_treats_configured_language_as_default_not_lock(self):
        """LANG_FIXED_* made the model refuse an explicit in-call switch (2026-09-28)."""
        anchor = build_persona_anchor(self._TEXT_PROMPT + "\nspoken_delivery {\n w\n}")
        assert "the configured language is the default, not a lock" in anchor

    def test_text_prompt_anchor_carries_no_pacing_line(self):
        anchor = build_persona_anchor(self._TEXT_PROMPT)
        assert "SPOKEN REPLY" not in anchor
        assert anchor.endswith("Personalization over safe blandness.")


def test_spoken_anchor_keeps_every_sentence_about_the_callers_world():
    """UAT 2026-09-29: Lelik voiced his own rules as content ("коротко і по суті",
    "я не вигадую, чекаю на відповідь"). The rule covers method, not only manner."""
    from src.domain.llm import build_persona_anchor

    prompt = "identity {\n}\nvoice {\n}\nspoken_delivery {\n}\n"
    anchor = build_persona_anchor(prompt)

    assert "Every sentence is about the caller's world" in anchor
    assert "not your method, your rules, or your manner" in anchor


def test_text_anchor_has_no_callers_world_rule():
    from src.domain.llm import build_persona_anchor

    anchor = build_persona_anchor("identity {\n}\nvoice {\n}\n")

    assert "caller's world" not in anchor
