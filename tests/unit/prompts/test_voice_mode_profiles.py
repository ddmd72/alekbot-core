"""The interpreter and tutor profiles are lelik_xai with exactly two token swaps, minus the plain call's MCP tokens."""
import pytest

from tests.support import prompt_vocabulary as pv

BASE = pv.load_profile("lelik_xai")
# Tokens that serve only the plain call's MCP tools; the modes get no MCP (LELIK_VOICE_MCP_RFC O7).
PLAIN_CALL_ONLY = ("VOICE_TOOL_DATA_LELIK",)


@pytest.mark.parametrize("name,persona", [("lelik_xai_interpreter", "VOICE_PERSONA_INTERPRETER"),
                                          ("lelik_xai_tutor", "VOICE_PERSONA_TUTOR_ES")])
def test_profile_is_lelik_xai_with_the_persona_and_language_swaps(name, persona):
    profile = pv.load_profile(name)
    expected = {k: v for k, v in BASE["tokens"].items()
                if k not in ("VOICE_PERSONA_LELIK", "LANG_MIRROR", *PLAIN_CALL_ONLY)}
    expected[persona] = {"order": 20, "non_overridable": True}
    expected["LANG_ROLE_FIRST"] = {"order": 300, "non_overridable": True}

    assert profile["tokens"] == expected
    assert profile["agent_id"] == name
    assert profile["source_file"] == f"firestore_utils/uploads/{name}.json"
    assert profile["blueprint_id"] == BASE["blueprint_id"]


@pytest.mark.parametrize("token", ["VOICE_PERSONA_INTERPRETER", "VOICE_PERSONA_TUTOR_ES", "LANG_ROLE_FIRST"])
def test_every_swapped_in_token_has_a_file(token):
    assert (pv.SNAPSHOT / "tokens" / "user" / f"{token}.groovy").exists()
