"""The review pass through the agent's loop (Facts S4 T6b): its tools reach `FactReviewScope`, its refusals are counted, an
unanswered born-due or scope refusal posts the 🚨 (a basis or ask refusal leaves the fact kept and posts none), any success
of the fact in the pass answers its refusal, and the day's numbers go to `review_refusals` (F14).

The agent runs its real loop, executor and close-out over a scripted model; the scope, the typed write path, the
repository and the counters are the production classes on the review world's evaluating fake database."""

import json
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.adapters.in_memory_provider_resilience import InMemoryProviderResilience
from src.agents.consolidation_agent import ConsolidationAgent, _TrackingFactUpdates
from src.domain.agent import AgentConfig
from src.domain.consolidation_pass_stats import (
    OUTCOME_ERROR,
    OUTCOME_OK,
    OUTCOME_BORN_DUE,
    OUTCOME_REVIEW_REFUSAL,
    OUTCOME_SCOPE_REFUSAL,
    ConsolidationPassStats,
    PassCall,
    PassTool,
)
from src.domain.entities import ExpiryKind
from src.domain.fact_lifecycle_day import CounterField
from src.domain.fact_review_batch import ReviewBatch
from src.domain.fact_tool_result import outcome_of, refused_result
from src.domain.pass_actor import PassActor
from src.domain.user import PerformanceTier
from src.infrastructure.fact_review_scope import FactReviewScope
from src.ports.llm_port import AgentExecutionContext, LLMPort, LLMResponse, ProviderCapabilities, ToolCall
from tests.support.fact_kit import ACC
from tests.support.fact_lifecycle_world import NOW
from tests.support.fact_review_world import ReviewWorld

TZ = "Europe/Lisbon"
USER = "abcdef1234567890-tail"
HORIZON = {"kind": "horizon", "days": 30}
_REPORT = LLMResponse(text=json.dumps({"operations": []}), tool_calls=[])


@pytest.fixture
def world():
    return ReviewWorld()


def _tools(*calls):
    return LLMResponse(text=None, tool_calls=[ToolCall(name=n, args=a) for n, a in calls])


def _agent(world, responses):
    it = iter(responses)
    provider = MagicMock(spec=LLMPort)
    provider.generate_content = AsyncMock(side_effect=lambda request: next(it))
    repo = MagicMock()
    repo.get_biographical_context_cached = AsyncMock(return_value=[])
    agent = ConsolidationAgent(
        config=AgentConfig(agent_id="consolidation_agent", agent_type="consolidation"),
        execution_context=AgentExecutionContext(
            agent_type="consolidation", provider=provider, model_name="claude-x", tier=PerformanceTier.PERFORMANCE,
            capabilities=ProviderCapabilities(), resilience_port=InMemoryProviderResilience(),
        ),
        repository=repo,
        embedding_service=MagicMock(),
        fact_write_service=MagicMock(),
        fact_management_port=world.store.adapter,
        fact_updates=world.store.adapter,
        alert_sink=world.sink,
        lifecycle_counters=world.counters,
    )
    agent._llm = provider
    return agent


async def _stale(world, text):
    return await world.seed_stale(text, expiry_kind=ExpiryKind.HORIZON, ttl_days=14, reported_date=NOW - timedelta(days=60))


async def _review(world, agent, fact_ids, **loop):
    facts = tuple([await world.repo.get_fact_by_id(fact_id) for fact_id in fact_ids])
    tracker = _TrackingFactUpdates(agent._fact_management, agent._fact_updates)
    scope = FactReviewScope(ReviewBatch(facts, ()), ACC, TZ, world.now, world.repo, tracker)
    ops = await agent._run_consolidation_loop(
        user_message_text="review", system_prompt="SYS", user_id=USER, account_id=ACC,
        write_context=agent._write_context("review", TZ), pass_kind="review", review_scope=scope, **loop,
    )
    return ops, scope, tracker


async def _day(world):
    rows = await world.counters.for_day(datetime.now(timezone.utc).date())
    return next((row for row in rows if row.account_id == ACC), None)


def _revive(fact_id, evidence_id, expiry=HORIZON):
    return ("update_fact", {
        "fact_id": fact_id, "updates": {"state": "current", "expiry": expiry},
        "basis": {"kind": "evidence", "evidence_fact_id": evidence_id},
    })


# --- the stats on their own ------------------------------------------------------------------------------------------


@pytest.mark.parametrize("tag, outcome", [
    ("basis", OUTCOME_REVIEW_REFUSAL), ("ask", OUTCOME_REVIEW_REFUSAL), ("scope", OUTCOME_SCOPE_REFUSAL),
])
def test_a_review_refusal_is_its_own_outcome_and_scope_is_told_apart(tag, outcome):
    assert outcome_of(refused_result("f1", tag, "why")) == outcome


def test_review_refusals_are_counted_and_go_to_the_review_counter_and_only_scope_is_abandoned():
    s = ConsolidationPassStats(PassActor.REVIEW, 15)
    calls = [PassCall(PassTool.UPDATE, "a", key="f1"), PassCall(PassTool.ASK, "b", key="f2"),
             PassCall(PassTool.UPDATE, "c", key="f3"), PassCall(PassTool.COUNT, "d")]
    s.record_turn(calls)
    s.record_outcomes(calls, [OUTCOME_REVIEW_REFUSAL, OUTCOME_REVIEW_REFUSAL, OUTCOME_SCOPE_REFUSAL, OUTCOME_OK])
    assert (s.review_refusals, s.expiry_errors, s.error_turns) == (3, 0, 1)
    assert s.day_counts() == {CounterField.REVIEW_REFUSALS: 3}
    assert [c.key for c in s.abandoned_calls()] == ["f3"]


def test_a_scope_refusal_is_answered_by_a_later_update_or_ask_of_the_fact_closing_included():
    s = ConsolidationPassStats(PassActor.REVIEW, 15)
    refused = [PassCall(PassTool.UPDATE, "a", key="f1"), PassCall(PassTool.UPDATE, "b", key="f2"),
               PassCall(PassTool.ASK, "c", key="f3")]
    s.record_turn(refused)
    s.record_outcomes(refused, [OUTCOME_SCOPE_REFUSAL] * 3)
    later = [PassCall(PassTool.ASK, "d", key="f1"), PassCall(PassTool.UPDATE, "e", key="f2", closing=True),
             PassCall(PassTool.UPDATE, "f", key="f3")]
    s.record_turn(later)
    s.record_outcomes(later, [OUTCOME_OK, OUTCOME_OK, OUTCOME_ERROR])
    assert [c.key for c in s.abandoned_calls()] == ["f3"]


@pytest.mark.parametrize("refusal", [OUTCOME_SCOPE_REFUSAL, OUTCOME_BORN_DUE, "expiry"])
def test_in_the_review_a_success_of_the_fact_before_its_refusal_answers_it(refusal):
    s = ConsolidationPassStats(PassActor.REVIEW, 15)
    archived = [PassCall(PassTool.UPDATE, "a", key="f1", closing=True)]
    s.record_turn(archived)
    s.record_outcomes(archived, [OUTCOME_OK])
    again = [PassCall(PassTool.UPDATE, "a", key="f1", closing=True), PassCall(PassTool.UPDATE, "b", key="f2")]
    s.record_turn(again)
    s.record_outcomes(again, [refusal, refusal])
    assert [c.key for c in s.abandoned_calls()] == ["f2"]


def test_on_a_cleanup_pass_a_success_before_the_refusal_still_answers_nothing():
    s = ConsolidationPassStats(PassActor.STAGE2A, 15)
    first = [PassCall(PassTool.UPDATE, "a", key="f1")]
    s.record_turn(first)
    s.record_outcomes(first, [OUTCOME_OK])
    s.record_turn(first)
    s.record_outcomes(first, ["expiry"])
    assert [c.key for c in s.abandoned_calls()] == ["f1"]


def test_the_conversation_passes_keep_their_counter():
    s = ConsolidationPassStats(PassActor.STAGE2A, 15)
    call = [PassCall(PassTool.CREATE, "a", key="k")]
    s.record_turn(call)
    s.record_outcomes(call, ["expiry"])
    assert s.day_counts() == {CounterField.EXPIRY_TOOL_ERRORS: 1}
    assert ConsolidationPassStats(PassActor.STAGE1, 15).day_counts() == {}


# --- through the loop ------------------------------------------------------------------------------------------------


async def test_an_unanswered_born_due_refusal_in_the_review_posts_the_alert(world):
    fact, evidence = await _stale(world, "plays tennis on Sundays"), await world.seed("still plays tennis weekly")
    agent = _agent(world, [_tools(_revive(fact, evidence, {"kind": "date", "date": "2020-01-01"})), _REPORT])

    await _review(world, agent, [fact])

    alerts = [t for t in world.sink.texts if t.startswith("🚨")]
    assert len(alerts) == 1
    assert "pass `review`" in alerts[0] and f"update fact {fact[:8]}" in alerts[0] and "abandoned 1" in alerts[0]
    assert world.docs[fact]["state"] == "stale"
    day = await _day(world)
    assert day.review_refusals == 1 and day.expiry_tool_errors == 0


async def test_a_born_due_refusal_answered_by_a_later_success_posts_nothing(world):
    fact, evidence = await _stale(world, "plays tennis on Sundays"), await world.seed("still plays tennis weekly")
    agent = _agent(world, [
        _tools(_revive(fact, evidence, {"kind": "date", "date": "2020-01-01"})),
        _tools(_revive(fact, evidence)),
        _REPORT,
    ])

    _, scope, tracker = await _review(world, agent, [fact])

    assert not [t for t in world.sink.texts if t.startswith("🚨")]
    assert [(d.fact_id, d.outcome) for d in scope.decisions()] == [(fact, "revived")]
    assert tracker.wrote
    assert (await _day(world)).review_refusals == 1


async def test_review_refusals_are_counted_and_an_unanswered_one_posts_the_alert(world):
    fact, outside = await _stale(world, "plays tennis on Sundays"), await _stale(world, "owns a red bike")
    agent = _agent(world, [
        _tools(
            ("update_fact", {"fact_id": fact, "updates": {"state": "current", "expiry": HORIZON}}),   # no basis
            ("ask_owner", {"fact_id": outside, "fallback": "revive"}),                               # not in the batch
            ("count_words", {"text": "a b c"}),
        ),
        _tools(("ask_owner", {"fact_id": fact, "fallback": "revive"})),                              # answers the first
        _REPORT,
    ])
    agent._stats_alerts = True

    _, scope, _ = await _review(world, agent, [fact])

    assert [a.fact_id for a in scope.asks()] == [fact]
    summary = next(t for t in world.sink.texts if t.startswith("📊"))
    assert "pass `review`" in summary and "review refusals 2" in summary and "abandoned 1" in summary
    assert "tools: update×1 ask×2 count×1" in summary
    alert = next(t for t in world.sink.texts if t.startswith("🚨"))
    assert alert.startswith("🚨 Consolidation left refusals unanswered") and f"ask fact {outside[:8]}" in alert
    assert fact[:8] not in alert.split("abandoned")[1]
    day = await _day(world)
    assert day.review_refusals == 2 and day.expiry_tool_errors == 0


async def test_the_review_loop_runs_only_the_review_tools(world):
    fact = await _stale(world, "plays tennis on Sundays")
    agent = _agent(world, [_tools(("create_fact", {"content": "x", "fact_attributes": {}})), _REPORT])

    await _review(world, agent, [fact])

    assert not any(doc.get("text") == "x" for doc in world.docs.values())
    requests = [c.args[0] if c.args else next(iter(c.kwargs.values())) for c in agent._llm.generate_content.await_args_list]
    answer = requests[1].messages[-1].parts[0].tool_response
    assert answer["name"] == "create_fact" and "Unknown tool: create_fact" in answer["response"]["result"]
    tools = requests[0].tools
    assert [t["name"] for t in tools] == ["update_fact", "ask_owner", "count_words"]


async def test_max_turns_caps_the_review_loop_and_the_stats(world):
    fact = await _stale(world, "plays tennis on Sundays")
    agent = _agent(world, [_tools(("count_words", {"text": "a"})) for _ in range(3)])

    ops, _, _ = await _review(world, agent, [fact], max_turns=2)

    assert ops == []
    alert = next(t for t in world.sink.texts if "turn limit" in t)
    assert "turns 2/2" in alert and "pass `review`" in alert
    assert agent._llm.generate_content.await_count == 2


async def test_a_review_pass_needs_its_scope_and_no_other_pass_takes_one(world):
    agent = _agent(world, [])
    with pytest.raises(ValueError):
        await agent._run_consolidation_loop(
            user_message_text="r", system_prompt="S", user_id=USER, account_id=ACC,
            write_context=agent._write_context("review", TZ), pass_kind="review",
        )
    fact = await _stale(world, "plays tennis on Sundays")
    scope = FactReviewScope(ReviewBatch((await world.repo.get_fact_by_id(fact),), ()), ACC, TZ, world.now, world.repo,
                            world.store.adapter)
    with pytest.raises(ValueError):
        await agent._run_consolidation_loop(
            user_message_text="r", system_prompt="S", user_id=USER, account_id=ACC,
            write_context=agent._write_context("stage2a", TZ), review_scope=scope,
        )
    assert agent._llm.generate_content.await_count == 0


def _alerts(world):
    return [t for t in world.sink.texts if t.startswith("🚨")]


async def test_an_unanswered_basis_refusal_keeps_the_fact_and_posts_no_alert(world):
    fact = await _stale(world, "plays tennis on Sundays")
    agent = _agent(world, [_tools(("update_fact", {"fact_id": fact, "updates": {"state": "current", "expiry": HORIZON}})), _REPORT])

    _, scope, _ = await _review(world, agent, [fact])

    assert _alerts(world) == []
    assert [f.id for f in scope.kept()] == [fact] and world.docs[fact]["state"] == "stale"
    assert (await _day(world)).review_refusals == 1


async def test_an_unanswered_ask_refusal_keeps_the_fact_and_posts_no_alert(world):
    fact = await _stale(world, "plays tennis on Sundays")
    agent = _agent(world, [_tools(("ask_owner", {"fact_id": fact, "fallback": "archive"})), _REPORT])   # archive: not allowed

    _, scope, _ = await _review(world, agent, [fact])

    assert _alerts(world) == []
    assert scope.asks() == [] and [f.id for f in scope.kept()] == [fact]
    assert (await _day(world)).review_refusals == 1


async def test_an_unanswered_scope_refusal_posts_the_alert(world):
    fact, outside, evidence = await _stale(world, "plays tennis on Sundays"), await _stale(world, "owns a red bike"), \
        await world.seed("still plays tennis weekly")
    agent = _agent(world, [_tools(_revive(outside, evidence)), _REPORT])

    await _review(world, agent, [fact])

    alerts = _alerts(world)
    assert len(alerts) == 1
    assert alerts[0].startswith("🚨 Consolidation left refusals unanswered") and f"update fact {outside[:8]}" in alerts[0]
    assert world.docs[outside]["state"] == "stale"


def _invalidate(fact_id, evidence_id):
    # P2 revised (P2-2): a passed date no longer backs an archive; the closing write is an invalidation on evidence
    return ("update_fact", {
        "fact_id": fact_id, "updates": {"state": "invalidated"},
        "basis": {"kind": "evidence", "evidence_fact_id": evidence_id},
    })


async def test_a_closing_write_sent_again_after_it_was_written_posts_no_alert(world):
    fact, evidence = await _stale(world, "plays tennis on Sundays"), await world.seed("never played tennis")
    agent = _agent(world, [_tools(_invalidate(fact, evidence)), _tools(_invalidate(fact, evidence)), _REPORT])

    _, scope, _ = await _review(world, agent, [fact])

    assert [(d.fact_id, d.outcome) for d in scope.decisions()] == [(fact, "invalidated")]
    assert _alerts(world) == []
    assert (await _day(world)).review_refusals == 1          # the repeat was refused (scope) and is still counted


async def test_an_update_after_the_fact_was_asked_about_posts_no_alert(world):
    fact, evidence = await _stale(world, "plays tennis on Sundays"), await world.seed("still plays tennis weekly")
    agent = _agent(world, [
        _tools(("ask_owner", {"fact_id": fact, "fallback": "revive"})),
        _tools(_revive(fact, evidence)),
        _REPORT,
    ])

    _, scope, _ = await _review(world, agent, [fact])

    assert [a.fact_id for a in scope.asks()] == [fact] and scope.decisions() == []
    assert _alerts(world) == []
    assert world.docs[fact]["state"] == "stale"


@pytest.mark.parametrize("pass_kind, label", [("review", "stage2a"), ("consolidation", "review")])
async def test_the_pass_kind_must_match_the_actor(world, pass_kind, label):
    agent = _agent(world, [])
    fact = await _stale(world, "plays tennis on Sundays")
    scope = FactReviewScope(ReviewBatch((await world.repo.get_fact_by_id(fact),), ()), ACC, TZ, world.now, world.repo,
                            world.store.adapter)
    with pytest.raises(ValueError, match="actor"):
        await agent._run_consolidation_loop(
            user_message_text="r", system_prompt="S", user_id=USER, account_id=ACC,
            write_context=agent._write_context(label, TZ), pass_kind=pass_kind,
            review_scope=scope if pass_kind == "review" else None,
        )
    assert agent._llm.generate_content.await_count == 0
