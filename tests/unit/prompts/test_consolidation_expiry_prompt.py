"""What the consolidation tokens say about expiry (Facts S3, T5, plan §1.11/§3.20, owner Q4).

Reads `prompts_snapshot/` only. The vocabulary tests (`test_consolidation_vocabulary.py`) pin the enum and schema
agreement; this file pins the expiry contract: one vocabulary for the kinds, no TTL, every example call sends `expiry`,
every SUPERSEDED instruction names its successor, and the extension rule is worded for reading a conversation.
"""
import re

import pytest

from tests.support import prompt_vocabulary as pv

PROFILE = pv.load_profile("consolidation")
TOKENS = pv.load_tokens(PROFILE)
FIVE = ["CONSOLIDATION_TAXONOMY", "CONSOLIDATION_TOOLS", "CONSOLIDATION_EXAMPLES", "CONSOLIDATION_COGNITIVE_PROCESS",
        "CONSOLIDATION_POLICIES"]
EXAMPLE_CALL_TOKENS = ["CONSOLIDATION_TOOLS", "CONSOLIDATION_EXAMPLES"]
FIELD_NAMES = re.compile(r"\b(state|temporal[_ ]class|temporal|context_priority)\b", re.IGNORECASE)


def body(token):
    return TOKENS[token][1]


def lines(token):
    return body(token).split("\n")


def call_arguments(text, name):
    """The argument text of each `name(` call in `text`, parentheses inside double-quoted strings ignored."""
    found = []
    for match in re.finditer(rf"(?<!@tool )\b{name}\(", text):
        depth, i, in_string = 1, match.end(), False
        while i < len(text) and depth:
            ch = text[i]
            if ch == "\\" and in_string:
                i += 1
            elif ch == '"':
                in_string = not in_string
            elif not in_string:
                depth += (ch == "(") - (ch == ")")
            i += 1
        found.append(text[match.end():i - 1])
    return found


@pytest.mark.parametrize("token", FIVE)
def test_no_token_offers_ttl_days_or_a_ttl(token):
    assert "ttl_days" not in body(token) and not re.search(r"\bTTL\b", body(token))


def test_no_consolidation_token_offers_ttl_days():
    assert [t for t, (_, text) in TOKENS.items() if "ttl_days" in text] == []


@pytest.mark.parametrize("token", FIVE)
def test_kinds_are_named_only_as_date_horizon_none(token):
    assert not re.search(r"\b(HORIZON|NONE)\b", body(token))
    assert not re.search(r'"kind":\s*"(?!date"|horizon"|none")', body(token))
    assert not re.search(r"kind:\s*(?!date\b|horizon\b|none\b)[A-Za-z]", body(token))


@pytest.mark.parametrize("token", FIVE)
def test_lines_naming_state_class_or_priority_carry_no_iso_utc_yyyy_never(token):
    bad = [line.strip() for line in lines(token) if FIELD_NAMES.search(line)
           and re.search(r"\b(ISO|UTC|YYYY|NEVER)\b", line)]
    assert bad == []


@pytest.mark.parametrize("token", sorted(TOKENS))
def test_no_token_carries_the_old_date_literal(token):
    assert "2026-02-16" not in body(token)


@pytest.mark.parametrize("token", FIVE)
def test_every_superseded_instruction_names_superseded_by(token):
    exempt = re.compile(r"^\s*(guideline|returns):")
    bad = [line.strip() for line in lines(token)
           if "SUPERSEDED" in line and "superseded_by" not in line and not exempt.match(line)]
    assert bad == []


@pytest.mark.parametrize("token,name", [(t, n) for t in EXAMPLE_CALL_TOKENS for n in ("create_fact", "merge_facts")])
def test_every_create_and_merge_example_call_sends_expiry(token, name):
    calls = call_arguments(body(token), name)
    assert calls, f"{token} has no {name} example"
    assert all('"expiry"' in args for args in calls), f"a {name} example in {token} sends no expiry"


@pytest.mark.parametrize("token", EXAMPLE_CALL_TOKENS)
def test_every_content_update_example_sends_expiry(token):
    calls = call_arguments(body(token), "update_fact")
    changing = [c for c in calls if re.search(r'"(content|tags|metadata|temporal_class)"', c)]
    assert changing, f"{token} has no content update example"
    assert all('"expiry"' in c for c in changing)


def test_the_example_calls_checker_sees_a_missing_expiry():
    broken = 'create_fact(content="a (b)", fact_attributes={"domain": "HEALTH", "tags": ["x"]})'
    assert call_arguments(broken, "create_fact") == ['content="a (b)", fact_attributes={"domain": "HEALTH", "tags": ["x"]}']
    assert '"expiry"' not in call_arguments(broken, "create_fact")[0]


@pytest.mark.parametrize("token", ["CONSOLIDATION_TAXONOMY", "CONSOLIDATION_COGNITIVE_PROCESS"])
def test_the_extension_rule_is_worded_for_reading_a_conversation_and_cleanup_never_extends(token):
    text = body(token)
    assert "consolidate a conversation or emails" in text
    assert "held all along" in text and "reported_date" in text
    assert "link is unclear" in text and "leave the old one as it is" in text
    assert "Cluster and rulebook reviews never extend a fact" in text


def test_taxonomy_states_the_kinds_table_and_its_precedence_over_the_class_line():
    text = body("CONSOLIDATION_TAXONOMY")
    assert "The kinds table decides; the class line is only a default when no row applies." in text
    for row in ("Past event", "Planned event", "Validity or deadline", "Current activity", "Changing value",
                "Stable or permanent", "Noise that passed the filter", "Standing directive"):
        assert row in text


def test_taxonomy_date_rules_compare_with_today_and_resolve_ranges_months_and_years():
    text = body("CONSOLIDATION_TAXONOMY")
    assert "Compare every date with Today, not with the message time" in text
    assert "DISCARD it if it no longer matters" in text and "{kind: none}" in text
    assert "A range -> its last day. Only a month -> its last day. Only a year -> none" in text
    assert "then Dec 31 of that year" in text


def test_stale_is_system_only_in_every_token_that_lists_it():
    assert "set by the system only" in body("CONSOLIDATION_TAXONOMY")
    assert "STALE is set by the system only" in body("CONSOLIDATION_POLICIES")
    assert "stale is set by the system" in body("CONSOLIDATION_TOOLS")


def test_tools_names_expiry_on_create_update_and_merge_and_the_new_returns_keys():
    tools = pv.groovy_tools(body("CONSOLIDATION_TOOLS"))
    assert "expiry" in tools["create_fact"]["nested"]["fact_attributes"]
    assert "derived_from" in tools["create_fact"]["nested"]["fact_attributes"]
    assert {"expiry", "superseded_by"} <= tools["update_fact"]["nested"]["updates"]
    merge_line = next(l for l in lines("CONSOLIDATION_TOOLS") if l.strip().startswith("fact_attributes: \"Attributes for new fact"))
    assert "expiry" in merge_line
    assert {"expiry", "due", "reported_date"} <= pv.returns_keys(body("CONSOLIDATION_TOOLS"))["search_existing_facts"]


def test_cognitive_process_decides_expiry_from_the_content_and_decomposition_names_the_successor():
    text = body("CONSOLIDATION_COGNITIVE_PROCESS")
    assert "Decide expiry from the content" in text
    assert "superseded_by=<id of the first new fact>" in text
