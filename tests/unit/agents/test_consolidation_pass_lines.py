"""The pass lines the agent renders and the typed calls it builds for the stats (Facts S4 T6b, S1–S3 review L2 and L6).

The strings below were produced by the domain's `format_*` methods before they moved to the agent; the renderer must give
the same bytes for the same pass."""

import json
from unittest.mock import AsyncMock

import pytest

from src.agents.consolidation_agent import (
    ToolResponse,
    _abandoned_alert,
    _limit_alert,
    _pass_call,
    _sequence_text,
    _summary_line,
)
from src.domain.consolidation_pass_stats import (
    OUTCOME_BORN_DUE,
    OUTCOME_ERROR,
    OUTCOME_EXPIRY,
    OUTCOME_OK,
    ConsolidationPassStats,
    PassTool,
)
from src.domain.entities import FactDomain
from src.domain.pass_actor import PassActor
from src.ports.alert_sink import AlertSinkPort
from src.ports.llm_port import LLMResponse, ToolCall

from tests.unit.agents.test_consolidation_agent import _ACCOUNT_ID, _make_agent

USER = "abcdef1234567890-tail"

SEARCH = ("search_existing_facts", {"keywords": ["a"], "primary_query": "a"})
LISBON = ("create_fact", {
    "content": "User flies to Lisbon on 2026-10-10",
    "fact_attributes": {"domain": "location", "expiry": {"kind": "date", "date": "2026-10-10"}},
})
WALKS = ("create_fact", {"content": "User walks daily", "fact_attributes": {"domain": "preference"}})
UPDATE = ("update_fact", {"fact_id": "1234567890abcdef", "updates": {"content": "hidden"}})
MERGE = ("merge_facts", {"fact_ids": ["a", "b"], "merged_content": "Merged text here", "fact_attributes": {"domain": "work"}})
COUNT = ("count_words", {"text": "x"})


def _call(name, args):
    return _pass_call(ToolCall(name=name, args=args), "UTC")


def _stats(label, turns):
    s = ConsolidationPassStats(PassActor(label), 15)
    for calls, outcomes in turns:
        typed = [_call(n, a) for n, a in calls]
        s.record_turn(typed)
        s.record_outcomes(typed, outcomes)
    return s


CLEAN = [([SEARCH, SEARCH, SEARCH], [OUTCOME_OK] * 3), ([LISBON, WALKS, UPDATE], [OUTCOME_OK] * 3), ([COUNT], [OUTCOME_OK])]
REFUSED = [
    ([LISBON], [OUTCOME_BORN_DUE]),
    ([UPDATE, WALKS], [OUTCOME_EXPIRY, OUTCOME_ERROR]),
    ([COUNT], [OUTCOME_OK]),
    ([COUNT], [OUTCOME_OK]),
    ([MERGE], [OUTCOME_BORN_DUE]),
]
EXPIRY_ONLY = [([WALKS], [OUTCOME_EXPIRY]), ([SEARCH], [OUTCOME_OK])]

PINNED = {
    "clean": (
        "stage1", CLEAN,
        "📊 Consolidation pass `stage1` | model claude-x | effort medium | turns 4/15 | tools: search×3 create×2 update×1 "
        "count×1 | repeats 0 | writes 3 | tool-error turns 0 (expiry 0, abandoned 0) | report ops 4 | 12s | user abcdef12\n"
        "t1 search×3 · t2 create×2 update×1 · t3 count×1",
        "🚨 Consolidation hit the turn limit without a final report | pass `stage1` | model claude-x | effort medium | "
        "turns 3/15 | writes so far 3 | repeats 0 | tool-error turns 0 (expiry 0, abandoned 0) | 100s | user abcdef12\n"
        "t1 search×3 · t2 create×2 update×1 · t3 count×1",
        "🚨 Consolidation left expiry refusals unanswered | pass `stage1` | model claude-x | abandoned 0:  | user abcdef12\n"
        "A refused write that is never resent saves nothing; check what the pass retired next to it.",
    ),
    "refused": (
        "stage2a", REFUSED,
        "📊 Consolidation pass `stage2a` | model claude-x | effort medium | turns 6/15 | tools: create×2 update×1 count×2 "
        "merge×1 | repeats 1 | writes 4 | tool-error turns 3 (expiry 3, born due 2, abandoned 3) | report ops 4 | 12s | "
        "user abcdef12\nt1 create×1 · t2 update×1 create×1 · t3 count×1 · t4 count×1 · t5 merge×1",
        "🚨 Consolidation hit the turn limit without a final report | pass `stage2a` | model claude-x | effort medium | "
        "turns 5/15 | writes so far 4 | repeats 1 | tool-error turns 3 (expiry 3, born due 2, abandoned 3) | 100s | "
        "user abcdef12\nt1 create×1 · t2 update×1 create×1 · t3 count×1 · t4 count×1 · t5 merge×1",
        "🚨 Consolidation left expiry refusals unanswered | pass `stage2a` | model claude-x | abandoned 3: create (text 34 "
        "chars), update fact 12345678, merge (text 16 chars) | user abcdef12\n"
        "A refused write that is never resent saves nothing; check what the pass retired next to it.",
    ),
    "expiry only": (
        "cluster", EXPIRY_ONLY,
        "📊 Consolidation pass `cluster` | model claude-x | effort medium | turns 3/15 | tools: create×1 search×1 | "
        "repeats 0 | writes 1 | tool-error turns 1 (expiry 1, abandoned 1) | report ops 4 | 12s | user abcdef12\n"
        "t1 create×1 · t2 search×1",
        "🚨 Consolidation hit the turn limit without a final report | pass `cluster` | model claude-x | effort medium | "
        "turns 2/15 | writes so far 1 | repeats 0 | tool-error turns 1 (expiry 1, abandoned 1) | 100s | user abcdef12\n"
        "t1 create×1 · t2 search×1",
        "🚨 Consolidation left expiry refusals unanswered | pass `cluster` | model claude-x | abandoned 1: create (text 16 "
        "chars) | user abcdef12\n"
        "A refused write that is never resent saves nothing; check what the pass retired next to it.",
    ),
}


@pytest.mark.parametrize("case", sorted(PINNED))
def test_the_rendered_lines_are_the_old_bytes(case):
    label, turns, summary, limit, abandoned = PINNED[case]
    s = _stats(label, turns)
    assert _summary_line(s, "claude-x", "medium", USER, 4, 12.4) == summary
    assert _limit_alert(s, "claude-x", "medium", USER, 99.6) == limit
    assert _abandoned_alert(s, "claude-x", USER) == abandoned


def test_the_lines_carry_no_argument_text_and_only_the_user_prefix():
    s = _stats("stage2a", REFUSED)
    for text in (_summary_line(s, "m", "medium", USER, 0, 1.0), _limit_alert(s, "m", "medium", USER, 1.0),
                 _abandoned_alert(s, "m", USER)):
        assert "abcdef12" in text and "abcdef123" not in text
        assert "Lisbon" not in text and "hidden" not in text and "Merged" not in text
        assert "1234567890abcdef" not in text


def test_the_abandoned_alert_names_at_most_five_calls_and_counts_all():
    s = _stats("stage1", [([("create_fact", {"content": f"fact {i}"})], [OUTCOME_EXPIRY]) for i in range(7)])
    text = _abandoned_alert(s, "m", USER)
    assert "abandoned 7:" in text and text.count("create (text") == 5


def test_the_sequence_of_a_turn_without_calls_is_a_dash():
    s = ConsolidationPassStats(PassActor.STAGE1, 15)
    s.record_turn([])
    assert _sequence_text(s) == "t1 -"


# --- the typed call -------------------------------------------------------------------------------------------------


def test_a_create_is_keyed_by_its_text_with_its_parsed_domain_and_sources():
    call = _call("create_fact", {
        "content": "User flew to Lisbon", "fact_attributes": {"domain": " Location ", "derived_from": [" f1", "f1", "f2 "]},
    })
    other = _call("create_fact", {"content": "User flew to Lisbon", "fact_attributes": {"domain": "work"}})
    assert call.tool is PassTool.CREATE and call.size == len("User flew to Lisbon")
    assert call.key == other.key and call.key != "User flew to Lisbon"
    assert call.domain is FactDomain.LOCATION and other.domain is FactDomain.WORK
    assert call.sources == frozenset({"f1", "f2"})
    assert call.words == other.words and call.words


def test_a_create_the_parser_refuses_keeps_its_text_key_and_nothing_else():
    call = _call("create_fact", {"content": "User walks", "fact_attributes": {"domain": "no-such", "derived_from": 5}})
    assert call.key == _call("create_fact", {"content": "User walks"}).key
    assert call.domain is None and call.sources == frozenset()


def test_a_merge_joins_its_fact_ids_as_the_parser_reads_them():
    call = _call("merge_facts", {"fact_ids": [" a", "b", "a"], "merged_content": "x", "fact_attributes": {}})
    assert call.tool is PassTool.MERGE and call.sources == frozenset({"a", "b"})
    assert _call("merge_facts", {"fact_ids": "bad", "merged_content": "x"}).sources == frozenset({"bad"})
    assert _call("merge_facts", {"fact_ids": [1], "merged_content": "x"}).sources == frozenset()


@pytest.mark.parametrize("state", ["invalidated", "ARCHIVED", " Superseded "])
def test_an_update_to_a_closing_state_is_closing_whatever_else_it_carries(state):
    call = _call("update_fact", {"fact_id": "f1", "updates": {"state": state}})
    assert call.tool is PassTool.UPDATE and call.key == "f1" and call.closing


@pytest.mark.parametrize("updates", [{"content": "x"}, {"state": "current"}, {"state": "nonsense"}, {"state": 3}, "bad"])
def test_an_update_that_names_no_closing_state_is_not_closing(updates):
    assert not _call("update_fact", {"fact_id": "f1", "updates": updates}).closing


def test_the_fingerprint_is_the_name_and_arguments_in_any_key_order():
    a = _call("count_words", {"text": "x", "n": 1})
    assert a.fingerprint == _call("count_words", {"n": 1, "text": "x"}).fingerprint
    assert a.fingerprint != _call("count_words", {"text": "y", "n": 1}).fingerprint
    assert _call("foo", {}).fingerprint != _call("bar", {}).fingerprint
    assert len(a.fingerprint) == 64 and int(a.fingerprint, 16) >= 0   # a hash: no argument text is kept


def test_a_call_of_no_known_tool_is_other():
    assert _call("discard_candidate", {}).tool is PassTool.OTHER


# --- through the loop: " id" and "id" are one source (outward L5) ----------------------------------------------------


def _loop_agent(responses, outcomes, sink):
    it = iter(responses)

    async def _llm(request):
        return next(it)

    agent, _, _, _ = _make_agent(mock_llm=AsyncMock(side_effect=_llm))
    agent._alert_sink = sink
    agent._stats_alerts = False
    turns = iter(outcomes)

    async def _execute(tool_calls, **kwargs):
        return [ToolResponse(name=tc.name, result_str="{}", outcome=o) for tc, o in zip(tool_calls, next(turns))]

    agent._execute_fact_management_tools = _execute
    return agent


def _tools(*calls):
    return LLMResponse(text=None, tool_calls=[ToolCall(name=n, args=a) for n, a in calls])


_REPORT = LLMResponse(text=json.dumps({"operations": []}), tool_calls=[])


def _split(derived):
    return ("create_fact", {
        "content": "User plans a trip to Lisbon on 2026-10-10",
        "fact_attributes": {"domain": "location", "derived_from": derived},
    })


def _rewrite(derived):
    return ("create_fact", {
        "content": "User took a trip to Lisbon on 2026-10-10",
        "fact_attributes": {"domain": "location", "derived_from": derived},
    })


async def _posted(refused_from, answered_from):
    sink = AsyncMock(spec=AlertSinkPort)
    agent = _loop_agent(
        [_tools(_split(refused_from)), _tools(_rewrite(answered_from)), _REPORT],
        [[OUTCOME_BORN_DUE], [OUTCOME_OK]],
        sink,
    )
    await agent._run_consolidation_loop(
        user_message_text="begin", system_prompt="SYS", user_id=USER, account_id=_ACCOUNT_ID,
        write_context=agent._write_context("stage2a"),
    )
    return [c.args[0] for c in sink.post.await_args_list]


async def test_a_rewrite_naming_the_same_source_with_other_spacing_answers_the_refusal():
    assert await _posted([" f1"], ["f1"]) == []
    assert await _posted(["f1 "], " f1") == []


async def test_a_rewrite_from_another_source_still_posts_the_alert():
    posted = await _posted([" f1"], ["f2"])
    assert len(posted) == 1 and posted[0].startswith("🚨 Consolidation left expiry refusals unanswered")
