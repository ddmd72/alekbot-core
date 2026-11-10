"""The query prompt and its response schema are one contract (fact profile RFC §5.1, §9 row 7).

The token is the frozen P1 head (measured on the cheap model) followed by one paragraph on the owner's location block
(OD-14 (b), measured and owner-approved); the LIFE AREAS block is rendered from the domain registry. The pins below
change only with a deliberate re-measurement of the prompt. This file lives in
`tests/unit/prompts` because `make test-unit` and `make ci` do not collect `tests/contracts/`."""
import hashlib
import re
from pathlib import Path

from firestore_utils.snapshot_serializer import token_from_file
from src.agents.fact_query_slots_agent import _RESPONSE_SCHEMA
from src.domain.fact_domains import render_life_areas_block
from src.domain.fact_profile_answer import PROFILE_KINDS
from src.domain.fact_query_parse import QUERY_DOMAINS, QUERY_INTENTS
from tests.support.prompt_render import render_static, rendered_token_ids

_TOKEN = Path(__file__).resolve().parents[3] / "prompts_snapshot" / "tokens" / "system" / "OUTPUT_FORMAT_FACT_QUERY_SLOTS.groovy"
_HEAD_SHA256 = "b39657909aaeb0050e412181b31988e2d0b65c0952effca8c3d301b7e26fb088"
_TOKEN_SHA256 = "533a648693d8385c84327ab0063293694f4177db16e2b8991dc4d0ea35878bf6"
_WITH_BLOCK_SHA256 = "f6cafc0344f59ce0d4bd08eed68e185e3a442057cb089446140ef830de07ee3a"
_CYRILLIC = re.compile("[Ѐ-ӿ]")


def _content() -> str:
    return token_from_file(_TOKEN.read_text(encoding="utf-8"))["content"]


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def test_the_token_is_the_frozen_p1_head_and_the_home_paragraph():
    head, _, home = _content().rpartition("\n\n")
    assert len(head) == 5589 and _sha(head) == _HEAD_SHA256          # the P1 head, unchanged
    assert home.startswith("The location block is where the user lives.") and "\n" not in home.rstrip("\n")
    assert len(_content()) == 6009
    assert _sha(_content()) == _TOKEN_SHA256


def test_the_token_with_the_rendered_life_areas_block_is_the_measured_prompt():
    assert _sha(_content() + "\n\n" + render_life_areas_block() + "\n") == _WITH_BLOCK_SHA256


def test_no_cyrillic_in_the_token_or_the_block():
    assert not _CYRILLIC.search(_content())
    assert not _CYRILLIC.search(render_life_areas_block())


def test_the_token_does_not_carry_the_life_areas_block():
    assert "LIFE AREAS" not in _content()


def test_every_field_the_text_names_is_a_schema_property_and_the_reverse():
    named = set(re.findall(r"^([a-z_]+): ", _content(), flags=re.M))
    assert named == set(_RESPONSE_SCHEMA["properties"])
    assert named == set(_RESPONSE_SCHEMA["required"])
    time_properties = _RESPONSE_SCHEMA["properties"]["time"]["properties"]
    assert set(time_properties) == {"intent", "from", "to"}
    assert "- intent says" in _content() and "- from and to give" in _content()


def test_the_schema_enums_match_the_code_vocabularies():
    properties = _RESPONSE_SCHEMA["properties"]
    assert properties["domains"]["items"]["enum"] == list(QUERY_DOMAINS)
    assert set(properties["kinds"]["items"]["enum"]) == set(PROFILE_KINDS)
    assert len(properties["kinds"]["items"]["enum"]) == len(PROFILE_KINDS)
    assert properties["time"]["properties"]["intent"]["enum"] == list(QUERY_INTENTS) == [
        "none", "future", "past", "period", "latest"]
    assert properties["scope"]["enum"] == ["top", "all"]
    assert properties["domain_link"]["enum"] == ["one_matter", "separate_topics"]


def test_the_kinds_enum_follows_the_order_of_the_text():
    text = _content()
    listed = re.findall(r"(\w+) \((?:what|a |an |the )", text[text.index("kinds: "):text.index("scope: ")])
    assert listed == _RESPONSE_SCHEMA["properties"]["kinds"]["items"]["enum"]


def test_the_text_names_every_intent_and_both_links():
    text = _content()
    for intent in QUERY_INTENTS:
        assert f"{intent}: " in text
    assert "one_matter" in text and "separate_topics" in text


def test_the_rendered_profile_is_the_outer_class_with_one_output_format_section_holding_the_token():
    rendered = render_static("fact_query_slots")
    assert rendered.startswith("class FactQuerySlotsAgent extends Agent {")
    assert re.findall(r"^    ([a-z_]+) \{$", rendered, flags=re.M) == ["output_format"]
    assert _content().split("\n")[0] in rendered
    assert rendered_token_ids("fact_query_slots") == ["OUTPUT_FORMAT_FACT_QUERY_SLOTS"]
