"""The persona anchor reaches Lelik on xAI: his prompt names `identity` and `spoken_delivery` blocks, so
`build_persona_anchor` lists them. The shared voice tokens stay unwrapped, so the tutor, interpreter and summarizer
prompts remain below the two-section threshold."""
import pytest

from src.domain.llm import build_persona_anchor
from tests.support.prompt_render import render_static


def test_the_xai_lelik_prompt_builds_an_anchor_with_identity_spoken_delivery_and_the_language_line():
    anchor = build_persona_anchor(render_static("lelik_xai"))

    assert anchor is not None
    assert "- identity" in anchor and "- spoken_delivery" in anchor
    assert "Language:" in anchor


@pytest.mark.parametrize("profile", ["lelik_xai_tutor", "lelik_xai_interpreter", "lelik_summarizer"])
def test_the_other_voice_prompts_build_no_anchor(profile):
    assert build_persona_anchor(render_static(profile)) is None


def test_the_openai_lelik_prompt_still_builds_an_anchor():
    assert build_persona_anchor(render_static("lelik")) is not None
