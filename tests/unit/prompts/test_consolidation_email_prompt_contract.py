"""Stage 3's email-card prompt (email index v2 §6.7, slice 9): profile `consolidation_email`, blueprint
`consolidation_email_v1`. They are consolidation's profile and blueprint plus one token, `CONSOLIDATION_EMAIL_CANDIDATES`,
which explains the card candidates code sends: every key code can send, every `about` value and every time label is named
in it, and it names no link to the confirmed list. It renders through the production assembler with no placeholder left."""
import re

import pytest

from src.domain.consolidation import EMAIL_PROFILE_ID
from src.domain.drive_labels import DATE_LABELS
from src.domain.email_card import About
from src.domain.email_memory_candidate import CARD_CANDIDATE_KEYS
from tests.support import prompt_render as pr
from tests.support import prompt_vocabulary as pv

TOKEN_ID = "CONSOLIDATION_EMAIL_CANDIDATES"
PROFILE = pv.load_profile(EMAIL_PROFILE_ID)
BASE = pv.load_profile("consolidation")
DOC = pv.ser.token_from_file(pv.token_path(TOKEN_ID).read_text(encoding="utf-8"))
BODY = DOC["content"]


def test_the_profile_is_consolidations_plus_the_one_token_last():
    assert PROFILE["agent_id"] == EMAIL_PROFILE_ID
    extra = {k: v for k, v in PROFILE["tokens"].items() if k not in BASE["tokens"]}
    assert {k: PROFILE["tokens"][k] for k in BASE["tokens"]} == BASE["tokens"]
    assert extra == {TOKEN_ID: {"non_overridable": True, "order": 70}}
    assert max(v["order"] for v in BASE["tokens"].values()) < 70


def test_the_blueprint_is_consolidations_plus_the_tokens_section_last():
    blueprint = pv.load_blueprint(PROFILE["blueprint_id"])
    base = pv.load_blueprint(BASE["blueprint_id"])
    assert PROFILE["blueprint_id"] == "consolidation_email_v1"
    assert blueprint["class_order"] == [*base["class_order"], "email_candidates"]
    assert blueprint["outer_class"] == base["outer_class"] == "ConsolidationAgent extends Agent"
    assert (DOC["class"], DOC["category"]) == ("email_candidates", "consolidation_email")


def test_the_static_prompt_is_consolidations_with_the_section_appended():
    rendered, base = pr.render_static(EMAIL_PROFILE_ID), pr.render_static("consolidation")
    assert pr.rendered_token_ids(EMAIL_PROFILE_ID) == sorted([*pr.rendered_token_ids("consolidation"), TOKEN_ID])
    assert "    email_candidates {" in rendered and "Email_Candidates" in rendered
    assert rendered.index("    output_specification {") < rendered.index("    email_candidates {")
    assert not re.search(r"\{[A-Z][A-Z_]*\}", rendered)
    assert len(rendered) > len(base)


def test_no_placeholder_no_cyrillic_and_no_example_in_the_token():
    assert not re.search(r"\{[A-Z][A-Z_]*\}", BODY)
    assert not re.search("[Ѐ-ӿ]", BODY)
    for phrase in ("e.g.", "for example", "for instance", "such as"):
        assert phrase not in BODY.lower()


@pytest.mark.parametrize("key", CARD_CANDIDATE_KEYS)
def test_every_key_code_can_send_is_named(key):
    assert re.search(rf"\b{key}\b", BODY)


@pytest.mark.parametrize("value", [a.value for a in About])
def test_every_about_value_is_named(value):
    assert re.search(rf"\b{value}\b \(", BODY)


@pytest.mark.parametrize("label", DATE_LABELS)
def test_every_time_label_is_named(label):
    assert re.search(rf"\b{label}\b", BODY)


def test_the_header_form_and_reported_date_are_explained():
    assert "is a header: a mail with no readable card" in BODY
    assert "into reported_date" in BODY and "read false" in BODY and "may be absent" in BODY


def test_the_token_names_no_link_to_the_confirmed_list():
    lowered = BODY.lower()
    for word in ("entry_id", "entry_name", "linked_by", "relation"):
        assert word not in lowered
