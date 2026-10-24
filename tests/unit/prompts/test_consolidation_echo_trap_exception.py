"""The echo-trap exception for a memory-review reply (Facts S4 §3.10, S4-D28, T7), on the rendered `consolidation` profile.

One definition, `Domain_Scope.memory_review_reply` in `CONSOLIDATION_POLICIES`; every other echo line refers to it. The frames
the prompt quotes are the strings the domain builders render (`review_frame`, `not_delivered_frame`, `reply_part`), with
`<id>` or a real-looking id in the id's place."""
import re

import pytest

from src.domain.fact_review_frame import not_delivered_frame, reply_part, review_frame
from tests.support import prompt_vocabulary as pv
from tests.support.prompt_render import render_static

RENDERED = render_static("consolidation")
LINES = RENDERED.split("\n")
POLICIES = pv.ser.token_from_file(pv.token_path("CONSOLIDATION_POLICIES").read_text(encoding="utf-8"))["content"]
TAXONOMY = pv.ser.token_from_file(pv.token_path("CONSOLIDATION_TAXONOMY").read_text(encoding="utf-8"))["content"]
REFERENCE = "memory_review_reply"
PLACEHOLDER = "QID0"   # a valid question id, swapped for the id the prompt writes


def _shape(builder, qid, *rest):
    """What the builder renders for `qid`, the prompt's own placeholder `<id>` included (the builder refuses `<`)."""
    return builder(PLACEHOLDER, *rest).replace(PLACEHOLDER, qid) if qid == "<id>" else builder(qid, *rest)


def _block(text, name):
    """The lines of the `name: {` / `name {` / `rule name() {` block that opens first in `text`, braces counted."""
    start = re.search(rf"\b{re.escape(name)}\b(?:\(\))?\s*:?\s*\{{", text)
    assert start, f"no block {name!r}"
    depth, out = 0, []
    for line in text[start.start():].split("\n"):
        out.append(line)
        depth += line.count("{") - line.count("}")
        if depth <= 0 and len(out) > 1:
            break
    return out


def _definition():
    line = next(line for line in POLICIES.split("\n") if line.strip().startswith(f"{REFERENCE}:"))
    return line.split(":", 1)[1].strip().strip('"')


DEFINITION = _definition()


def test_the_definition_sits_in_domain_scope_once():
    domain_scope = "\n".join(_block(POLICIES, "Domain_Scope"))
    assert f"    {REFERENCE}: " in domain_scope
    assert sum(line.strip().startswith(f"{REFERENCE}:") for line in RENDERED.split("\n")) == 1


def test_every_echo_line_names_the_frame_or_refers_to_the_one_definition():
    exempt = set(_block(RENDERED, "example_3_discard_echo"))
    echo = [line for line in LINES if re.search(r"RAG echo|echo trap", line, re.IGNORECASE)]
    assert len(echo) >= 4, echo   # Domain_Scope, Trivial_Exclusions, extension_rule, step 4 at least
    offending = [line.strip() for line in echo
                 if line not in exempt and REFERENCE not in line and "[Reply to memory review q=" not in line]
    assert offending == []


def test_example_3_discard_echo_is_unchanged_and_names_no_exception():
    block = "\n".join(_block(RENDERED, "example_3_discard_echo"))
    assert REFERENCE not in block and "memory review" not in block
    assert '"reason": "RAG echo trap - User confirmed existing fact without adding new info"' in block


def test_domain_scope_fallback_refers_to_the_definition():
    fallback = next(line for line in _block(POLICIES, "Domain_Scope") if line.strip().startswith("fallback:"))
    assert "When in doubt, DISCARD with explanation" in fallback and REFERENCE in fallback


def test_the_definition_gives_the_quoted_fact_to_the_review_and_records_only_the_correction():
    assert "The quoted fact belongs to the memory review, which applies the answer" in DEFINITION
    assert "do not update, extend, invalidate or re-create it from this reply" in DEFINITION
    recorded = re.search(r"Record only what the answer adds: ([^;]*);", DEFINITION).group(1)
    assert recorded == "a correction naming the true value -> a fact about that value"
    assert "A digit 1-4, yes, no, stopped, 'don't remember' add nothing." in DEFINITION
    assert "The ASSISTANT part of the pair (the question, the quote, the options) stays an echo: DISCARD." in DEFINITION


def test_the_definition_names_the_three_frames_as_the_builders_render_them():
    assert _shape(reply_part, "<id>", "")[:-1] in DEFINITION
    assert _shape(review_frame, "<id>") in DEFINITION
    assert _shape(not_delivered_frame, "<id>") in DEFINITION
    assert "withdraws that question: the owner never saw it" in DEFINITION


def test_the_negation_rule_scopes_its_memory_review_line_to_stage_1():
    block = _block(TAXONOMY, "Negation_And_Invalidation")
    lines = [line.strip() for line in block if "memory review" in line]
    assert len(lines) == 1
    text = lines[0].split(":", 1)[1].strip().strip('"')
    assert text.startswith("When you consolidate a conversation (Stage 1), ")
    assert f"Follow Domain_Scope.{REFERENCE}." in text


@pytest.mark.parametrize("pattern,builder", [
    (r"\[System: memory review q=([^\s\]]+)[^\]]*\]", None),
    (r"\[Reply to memory review q=([^\]]+)\]", reply_part),
])
def test_every_frame_in_the_rendered_prompt_is_what_a_builder_renders(pattern, builder):
    found = list(re.finditer(pattern, RENDERED))
    assert found, pattern
    for match in found:
        qid, text = match.group(1), match.group(0)
        if builder is reply_part:
            assert text == _shape(reply_part, qid, "")[:-1], text
        else:
            assert text in (_shape(review_frame, qid), _shape(not_delivered_frame, qid)), text


def test_the_not_delivered_frame_is_named():
    assert _shape(not_delivered_frame, "<id>") in RENDERED


def test_the_memory_review_example_follows_example_3_and_has_its_three_cases():
    names = re.findall(r"^\s*(example_\w+): \{", RENDERED, re.MULTILINE)
    assert names[names.index("example_3_discard_echo") + 1] == "example_3b_memory_review_reply"
    block = "\n".join(_block(RENDERED, "example_3b_memory_review_reply"))
    for case in ("case_1_correction", "case_2_digit_only", "case_3_plain_message"):
        assert f"{case}: {{" in block
    correction = "\n".join(_block(block, "case_1_correction"))
    assert correction.count("create_fact(") == 1 and "update_fact(" not in correction
    assert "] 3 — it's Iron Temple now" in correction
    digit = "\n".join(_block(block, "case_2_digit_only"))
    assert "create_fact(" not in digit and "] 2\n" in digit
    plain = "\n".join(_block(block, "case_3_plain_message"))
    assert "create_fact(" not in plain and "the ordinary rules apply" in plain
    assert re.search(r"\n\s*USER: 2\n", plain)


@pytest.mark.parametrize("token", ["CONSOLIDATION_POLICIES", "CONSOLIDATION_TAXONOMY", "CONSOLIDATION_EXAMPLES",
                                   "CONSOLIDATION_COGNITIVE_PROCESS", "CONSOLIDATION_TOOLS"])
def test_each_edited_token_round_trips_through_the_serializer(token):
    text = pv.token_path(token).read_text(encoding="utf-8")
    assert pv.ser.token_to_file(pv.ser.token_from_file(text)) == text
