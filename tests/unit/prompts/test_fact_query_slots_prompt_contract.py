"""The query prompt and its response schema are one contract (fact profile RFC §5.1, §9 row 7).

The token is the frozen P1 head (measured on the cheap model) with one clause rewritten, the `latest` intent, then one
paragraph on the owner's location block (OD-14 (b)). The latest clause and the home paragraph are the 2026-11-11
measured draft2 (owner-approved); outside the latest clause the P1 head is byte-identical, and the head pin checks that
by putting the P1 clause back. The LIFE AREAS block is rendered from the domain registry. The pins below change only
with a deliberate re-measurement of the prompt. This file lives in
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
_HEAD_SHA256 = "b39657909aaeb0050e412181b31988e2d0b65c0952effca8c3d301b7e26fb088"   # the P1 head, as measured
_P1_LATEST = "latest: it asks for the current or most recent state of something."
_LATEST_SHA256 = "e37ee53fb5e4b5218d9d273d2416f342914fe8006d47e8622cb74d383dd4552b"  # 2026-11-11 draft2
_HOME_SHA256 = "3749b67f4c4db9d6a52847bb70477a535647551bee1b945a773e9d702525fe4a"    # 2026-11-11 draft2
_TOKEN_SHA256 = "6289d1f40caeb887eb1c8df204a01ea43bd08226791adb09a0b459ac97db1df9"
# The 2026-11-11 measured draft2: the token followed by the rendered LIFE AREAS block.
_WITH_BLOCK_SHA256 = "73f14b43cb2c70474758f034cad00dc2fecb06b9825e18cbb8d350e280ff9d38"
_CYRILLIC = re.compile("[Ѐ-ӿ]")


def _content() -> str:
    return token_from_file(_TOKEN.read_text(encoding="utf-8"))["content"]


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _latest_clause(head: str) -> str:
    """The `latest` clause: the rest of the intent line from `latest: ` on."""
    line = next(row for row in head.split("\n") if row.startswith("- intent says"))
    return line[line.index(" latest: ") + 1:]


def test_the_token_is_the_frozen_p1_head_with_the_latest_clause_and_the_home_paragraph():
    head, _, home = _content().rpartition("\n\n")
    latest = _latest_clause(head)
    assert head.count(latest) == 1
    p1_head = head.replace(latest, _P1_LATEST)
    assert len(p1_head) == 5589 and _sha(p1_head) == _HEAD_SHA256    # the P1 head outside the latest clause, unchanged
    assert _sha(latest) == _LATEST_SHA256
    assert home.startswith("The location block is where the user lives.") and "\n" not in home.rstrip("\n")
    assert _sha(home) == _HOME_SHA256
    assert len(head) == 6380 and len(_content()) == 7164
    assert _sha(_content()) == _TOKEN_SHA256


def test_the_token_with_the_rendered_life_areas_block_is_the_measured_prompt():
    """The prompt the 2026-11-11 draft2 measurement ran: the token, then the LIFE AREAS block."""
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
