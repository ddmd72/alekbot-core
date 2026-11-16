"""The email search v2 slots prompt contract (email index v2 §5.2; slice 8): the snapshot profile lists exactly the one
token, the blueprint is the one-section output format, the token's placeholders are exactly the code's, every time
intent and scope is named, the text is English (no Cyrillic) and every placeholder renders; and every snapshot profile
that names an agent sits in the file of that name (production reads a profile by its document id)."""
import re
from pathlib import Path

from src.domain.email_query_slots import NO_TIME, SLOTS_PROFILE, render_slots_prompt, slots_placeholder_values
from src.domain.email_search_query import SCOPES, TIME_INTENTS
from tests.support import prompt_render as pr
from tests.support import prompt_vocabulary as pv

TOKEN = "OUTPUT_FORMAT_EMAIL_QUERY_SLOTS"
PROFILES = Path(__file__).resolve().parents[3] / "prompts_snapshot" / "profiles"


def _body() -> str:
    return pv.load_tokens(pv.load_profile(SLOTS_PROFILE))[TOKEN][1]


def test_the_profile_lists_exactly_the_token_and_its_blueprint_has_one_section():
    profile = pv.load_profile(SLOTS_PROFILE)
    assert profile["agent_id"] == SLOTS_PROFILE
    assert list(profile["tokens"]) == [TOKEN] and profile["tokens"][TOKEN]["non_overridable"] is True
    blueprint = pv.load_blueprint(profile["blueprint_id"])
    assert blueprint["blueprint_id"] == "email_query_slots_v1"
    assert blueprint["class_order"] == ["output_format"]
    assert blueprint["outer_class"] == "EmailQuerySlots extends Agent"
    assert pv.load_tokens(profile)[TOKEN][0] == "output_format"


def test_the_token_holds_exactly_the_codes_placeholders():
    assert set(re.findall(r"\{([A-Z][A-Z_]*)\}", _body())) == set(slots_placeholder_values())


def test_every_intent_and_scope_is_named():
    body = _body()
    for intent in (*TIME_INTENTS, NO_TIME):
        assert re.search(rf"\b{intent}:", body), intent
    for scope in SCOPES:
        assert re.search(rf"\b{scope}\b", body), scope
    for field in ("subject:", "doc_types:", "domains:", "people:", "time:", "exact_keys:", "scope:"):
        assert field in body


def test_the_token_is_english():
    assert not re.search(r"[Ѐ-ӿ]", _body())


def test_the_assembled_prompt_renders_every_placeholder():
    rendered = render_slots_prompt(pr.render_static(SLOTS_PROFILE))
    assert not re.search(r"\{[A-Z][A-Z_]*\}", rendered)
    assert rendered.startswith("class EmailQuerySlots extends Agent {")
    assert slots_placeholder_values()["DOC_TYPES"] in rendered and "at most 3" in rendered and "at most 6" in rendered


def test_every_snapshot_profile_with_an_agent_sits_in_the_file_of_its_name():
    wrong = [p.name for p in sorted(PROFILES.glob("*.yaml"))
             if pv.load_profile(p.stem).get("agent_id") not in (None, p.stem)]
    assert wrong == []
