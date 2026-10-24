"""The review pass's prompt (Facts S4 §3.10, T7): profile `consolidation_review`, blueprint `consolidation_review_v1`, tokens
`CONSOLIDATION_TAXONOMY` and `CONSOLIDATION_LIFECYCLE`.

`CONSOLIDATION_LIFECYCLE` names the review's tools in prose; the code declares them (`_get_tool_declarations("review")`) and
the code writes the message (`_REVIEW_HEADER`). Both directions are pinned: every tool, property and basis kind the token
names exists, and every one the schema has is named. The message header and the token give the same outcome order and the
same checklist line."""
import re
from typing import get_args

import pytest

from src.agents.consolidation_agent import _REVIEW_HEADER, ConsolidationAgent
from src.domain.entities import ExpiryKind, FactState
from src.domain.fact_review_basis import BasisKind
from tests.support import prompt_vocabulary as pv
from tests.support.prompt_render import render_static, rendered_token_ids

LIFECYCLE_ID = "CONSOLIDATION_LIFECYCLE"
PROFILE = pv.load_profile("consolidation_review")
BLUEPRINT = pv.load_blueprint(PROFILE["blueprint_id"])
LIFECYCLE_DOC = pv.ser.token_from_file(pv.token_path(LIFECYCLE_ID).read_text(encoding="utf-8"))
LIFECYCLE = LIFECYCLE_DOC["content"]

REVIEW_TOOLS = {t["name"]: t["parameters"] for t in ConsolidationAgent._get_tool_declarations(None, "review")}
UPDATE = REVIEW_TOOLS["update_fact"]["properties"]
BASIS_KINDS = set(get_args(BasisKind))
EXPIRY_KINDS = {k.value for k in ExpiryKind}


def _strings(body):
    """The text of every double-quoted string literal of the token (the prose the model reads)."""
    return [m.group(1).replace('\\"', '"') for m in re.finditer(r'"((?:[^"\\\n]|\\.)*)"', body)]


PROSE = "\n".join(_strings(LIFECYCLE))


def _schema_names():
    """Every tool, property (nested ones included) and basis kind the review's schema has."""
    names = set(REVIEW_TOOLS)
    for params in REVIEW_TOOLS.values():
        stack = [params]
        while stack:
            node = stack.pop()
            for key, sub in node.get("properties", {}).items():
                names.add(key)
                stack.append(sub)
    return names | BASIS_KINDS


SCHEMA_NAMES = _schema_names()
# snake_case words the token may use that are not schema names: the message's own fields (`_review_line`), the checklist
# keys (`_REVIEW_HEADER`), the final report's keys (`_reported_language`), the field the review never sends and the
# token's own `outcome_order` block, which the checklist refers to.
MESSAGE_WORDS = {"stale_since", "timed_out", "reply_at", "asked_on", "neighbour_says", "dated_plan_passed",
                 "message_language", "reported_date", "outcome_order"}


def test_the_profile_uses_the_review_blueprint_and_exactly_the_two_tokens():
    assert PROFILE["agent_id"] == "consolidation_review"
    assert PROFILE["blueprint_id"] == "consolidation_review_v1"
    assert PROFILE["tokens"] == {
        "CONSOLIDATION_TAXONOMY": {"non_overridable": True, "order": 10},
        "CONSOLIDATION_LIFECYCLE": {"non_overridable": True, "order": 20},
    }
    assert "CONSOLIDATION_TOOLS" not in PROFILE["tokens"]
    assert BLUEPRINT["outer_class"] == "ConsolidationReviewAgent extends Agent"
    assert BLUEPRINT["class_order"] == ["taxonomy", "lifecycle"]


def test_each_token_binds_to_one_class_of_the_blueprint():
    classes = {tid: cls for tid, (cls, _) in pv.load_tokens(PROFILE).items()}
    assert classes == {"CONSOLIDATION_TAXONOMY": "taxonomy", LIFECYCLE_ID: "lifecycle"}
    assert LIFECYCLE_DOC["category"] == "lifecycle"


def test_the_profile_renders_with_exactly_those_two_tokens_in_blueprint_order():
    assert rendered_token_ids("consolidation_review") == ["CONSOLIDATION_LIFECYCLE", "CONSOLIDATION_TAXONOMY"]
    text = render_static("consolidation_review")
    assert text.startswith("class ConsolidationReviewAgent extends Agent {")
    assert text.index("    taxonomy {") < text.index("    lifecycle {")
    assert "rule Stale_Fact_Review()" in text and "@tool " not in text


@pytest.mark.parametrize("name", sorted(SCHEMA_NAMES))
def test_every_name_of_the_review_schema_is_named_by_the_lifecycle_token(name):
    assert re.search(rf"(?<![\w-]){re.escape(name)}(?![\w-])", PROSE), f"{LIFECYCLE_ID} never names {name!r}"


def test_every_tool_the_lifecycle_token_calls_is_a_review_tool():
    called = set(re.findall(r"\b([a-z][a-z_]*)\(", PROSE))
    assert called and called <= set(REVIEW_TOOLS), f"calls {sorted(called - set(REVIEW_TOOLS))}"


def test_every_snake_case_name_in_the_lifecycle_token_is_known():
    snake = set(re.findall(r"\b[a-z]+(?:_[a-z]+)+\b", PROSE))
    unknown = snake - SCHEMA_NAMES - MESSAGE_WORDS
    assert not unknown, f"{LIFECYCLE_ID} names {sorted(unknown)}, which neither the schema nor the message has"


def test_every_kind_the_lifecycle_token_writes_is_a_basis_or_an_expiry_kind():
    kinds = set(re.findall(r"\{kind: (\w+)", PROSE))
    assert kinds == BASIS_KINDS | EXPIRY_KINDS


def test_every_key_written_in_updates_or_basis_is_a_schema_property():
    for block in re.findall(r"updates=\{([^}]*)\}", PROSE):
        keys = set(re.findall(r"(\w+):", block))
        assert keys and keys <= set(UPDATE["updates"]["properties"]), block
    for block in re.findall(r"\{kind: [^}]*\}", PROSE):
        for word in re.findall(r"\b(\w+)\b", block)[2:]:
            if not word.isdigit() and word not in ("N", "YYYY", "MM", "DD"):
                assert word in UPDATE["basis"]["properties"] or word in UPDATE["updates"]["properties"]["expiry"]["properties"], (
                    block)


def test_every_state_the_lifecycle_token_sends_is_a_fact_state_the_review_writes():
    states = set(re.findall(r"state: ([A-Z]+)", PROSE))
    assert states == {"CURRENT", "ARCHIVED", "INVALIDATED", "SUPERSEDED"}
    assert states <= {s.name for s in FactState} and "STALE" not in states


@pytest.mark.parametrize("tool", ["search_existing_facts", "create_fact", "merge_facts"])
def test_the_lifecycle_token_names_no_search_create_or_merge_tool(tool):
    assert tool not in LIFECYCLE


def test_no_outcome_falls_back_to_a_revive():
    assert not re.search(r"\belse\b[^.;\n]{0,40}\brevive", LIFECYCLE, re.IGNORECASE)
    assert not re.search(r"\botherwise\b[^.;\n]{0,40}\brevive", LIFECYCLE, re.IGNORECASE)
    assert "Never revive, archive or invalidate a fact nothing in front of you decides." in PROSE


def test_the_lifecycle_token_asks_for_no_question_text():
    assert "Code writes the question" in PROSE and "you write no question" in PROSE
    for match in re.finditer(r"\b(write|writes|compose|draft|phrase|word)\b[^.;\n]{0,15}\bquestion\b", PROSE, re.IGNORECASE):
        before = PROSE[max(0, match.start() - 5):match.start()]
        assert "Code" in before or match.group(0).lower().startswith("write no question"), match.group(0)
    assert "question text" not in PROSE


def _header_order_lines():
    """The header's outcome order: its lead-in, the four numbered steps and the one-outcome line."""
    steps = [line for line in _REVIEW_HEADER.split("\n") if re.match(r"\(\d\) ", line)]
    assert len(steps) == 4
    return ["Decide each one by the first of these that applies:", *steps, "Every listed fact gets exactly one outcome."]


def test_the_header_and_the_lifecycle_token_give_the_same_outcome_order():
    for line in _header_order_lines():
        assert line in _REVIEW_HEADER, line
        assert f'"{line}"' in LIFECYCLE, f"{LIFECYCLE_ID} outcome_order lacks the header's line {line!r}"


def test_the_header_and_the_lifecycle_token_give_the_same_checklist_line():
    header_line = re.search(r"Before any call, write one line per listed fact[^\n]*", _REVIEW_HEADER).group(0)
    assert header_line.endswith("outcome=<…>.")
    assert header_line in PROSE, f"{LIFECYCLE_ID} checklist differs from the header's: {header_line!r}"
    assert "keep it stale" not in _REVIEW_HEADER and "keep it stale" not in LIFECYCLE


def test_the_reply_mapping_archives_an_ended_standing_fact_with_history_and_a_dated_plan_only():
    assert "stopped or ended -> archive" in PROSE
    assert "also send history: one sentence in the past tense saying it ended and when the owner said so" in PROSE
    assert "A dated plan is archived only, with no history." in PROSE
    assert "never true, or a bare no -> invalidate" in PROSE
    assert "a correction -> invalidate, or supersede by the current fact that states the true value" in PROSE
    assert "don't remember -> the question's fallback, now" in PROSE
    assert "a digit never reaches the review" in PROSE


def test_the_checklist_reply_values_are_the_headers():
    values = re.search(r"reply=<([a-z|]+)>", _REVIEW_HEADER).group(1).split("|")
    explained = re.search(r'"reply: ([^"]*)"', LIFECYCLE).group(1)
    assert sorted(re.findall(r"\b(\w+) \(", explained)) == sorted(values)


def test_the_final_report_shape_has_the_keys_the_agent_reads():
    assert '{\\"operations\\": [' in LIFECYCLE
    assert '\\"message_language\\": …, \\"labels\\": …}' in LIFECYCLE


def test_a_none_expiry_needs_evidence_or_a_reply_and_the_fallback_choice_is_stated():
    assert "{kind: none} says the fact holds for good: give it only when evidence or the owner's reply says so." in PROSE
    assert "revive by default; archive only for a fact of expiry kind date" in PROSE
    assert "with a past fallback_date" in PROSE and "or with noise: true" in PROSE


def test_the_lifecycle_token_round_trips_through_the_serializer():
    text = pv.token_path(LIFECYCLE_ID).read_text(encoding="utf-8")
    assert pv.ser.token_to_file(pv.ser.token_from_file(text)) == text
    for kind, name in (("profiles", "consolidation_review"), ("blueprints", "consolidation_review_v1")):
        yaml_text = (pv.SNAPSHOT / kind / f"{name}.yaml").read_text(encoding="utf-8")
        assert pv.ser.doc_to_yaml(pv.ser.doc_from_yaml(yaml_text)) == yaml_text
