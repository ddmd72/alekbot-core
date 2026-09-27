"""Unit tests for the "would a live person say this?" gate on spoken replies.

UAT round 1, Task 3: the spoken-delivery paragraph in `build_persona_anchor`
must ask the model to check its own wording against how a real person would
speak on a call, and rephrase if it would not. Text prompts (no
`spoken_delivery` section) never see this gate.
"""
from src.domain.llm import PERSONA_SECTIONS, build_persona_anchor

GATE_SENTENCE = (
    "Before you speak, check the wording and the meaning: would a real person "
    "say exactly this, in these words, in a live conversation? If not, "
    "rephrase until they would."
)

_TEXT_PROMPT = "identity {\n x\n}\nvoice {\n y\n}\nhumor_engine {\n z\n}"


class TestLiveSpeechGate:
    def test_spoken_prompt_includes_the_gate_sentence(self):
        anchor = build_persona_anchor(_TEXT_PROMPT + "\nspoken_delivery {\n w\n}")
        assert GATE_SENTENCE in anchor

    def test_text_prompt_without_spoken_delivery_omits_the_gate_sentence(self):
        anchor = build_persona_anchor(_TEXT_PROMPT)
        assert GATE_SENTENCE not in anchor

    def test_full_persona_prompt_includes_the_gate_sentence(self):
        full_prompt = "\n".join(f"{s} {{\n  ...\n}}" for s in PERSONA_SECTIONS)
        anchor = build_persona_anchor(full_prompt)
        assert GATE_SENTENCE in anchor
