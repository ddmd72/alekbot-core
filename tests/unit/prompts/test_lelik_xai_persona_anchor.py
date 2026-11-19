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


@pytest.mark.asyncio
async def test_the_calm_mentor_replacement_keeps_the_anchor_on_xai_lelik():
    from src.domain.prompt_v3.profile_slot import ProfileToken
    from tests.unit.services.prompt_v3.test_lelik_xai_voice_character import _assemble

    prompt = await _assemble({"VOICE_PERSONA_CALM_MENTOR": ProfileToken(token_id="VOICE_PERSONA_CALM_MENTOR", order=20)})
    anchor = build_persona_anchor(prompt)

    assert "calm and experienced friend" in prompt and "sparring partner" not in prompt
    assert anchor is not None and "- identity" in anchor and "- spoken_delivery" in anchor
