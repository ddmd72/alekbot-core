"""Facts S3 · T8: tool-error turns, born-due vs missing expiry, the abandoned-refusal 🚨 and the daily counters.

A scripted LLM drives the real loop, tool executor, adapter, write service and repository on the evaluating Firestore
fake; the sink records posts and the counters are the real adapter on the evaluating Firestore fake.
"""
import json
from datetime import datetime, timezone
from typing import Any, List
from unittest.mock import AsyncMock

import pytest

from src.adapters.firestore_fact_lifecycle_counters import FirestoreFactLifecycleCounters
from src.agents.consolidation_agent import ConsolidationAgent
from src.config.environment import EnvironmentConfig
from src.domain.consolidation_pass_stats import OUTCOME_BORN_DUE, OUTCOME_ERROR, OUTCOME_EXPIRY, OUTCOME_OK
from src.domain.llm import LLMResponse, ToolCall
from src.domain.request_context import RequestContext
from src.ports.alert_sink import AlertSinkPort
from tests.support.fact_kit import ACC, USER
from tests.support.fact_store import INDEXES_FILE, FactStore
from tests.support.fact_write import EXPIRY_NONE, ctx, horizon, on
from tests.support.firestore_eval import EvalDb
from tests.unit.agents.test_consolidation_write_context import OMIT, REPORT, attrs, by_text, calls, create, make_agent


@pytest.fixture
def store():
    return FactStore(indexes=INDEXES_FILE)


class Sink(AlertSinkPort):
    def __init__(self) -> None:
        self.posts: List[str] = []

    async def post(self, text: str) -> None:
        self.posts.append(text)


def scripted(*responses: LLMResponse):
    """The n-th model call answers `responses[n]`; the last one repeats."""
    seen = {"n": 0}

    def script(first: str, turns: str) -> LLMResponse:
        index = min(seen["n"], len(responses) - 1)
        seen["n"] += 1
        return responses[index]

    return script


def build(store: FactStore, *responses: LLMResponse, stats: bool = False):
    agent = make_agent(store, scripted(*responses))
    agent._alert_sink = Sink()
    agent._stats_alerts = stats
    db = EvalDb()
    agent._lifecycle_counters = FirestoreFactLifecycleCounters(db, EnvironmentConfig())
    agent.db = db
    return agent


async def run(agent: ConsolidationAgent, label: str = "stage1"):
    return await agent._run_consolidation_loop(
        user_message_text="begin", system_prompt="SYS", user_id=USER, account_id=ACC, pass_label=label,
        write_context=ctx(actor=label, timezone="UTC"),
    )


async def today_errors(agent) -> int:
    rows = await agent._lifecycle_counters.for_day(datetime.now(timezone.utc).date())
    return sum(r.expiry_tool_errors for r in rows)


def missing_expiry(text: str = "walks daily") -> LLMResponse:
    return calls(create(text, expiry=OMIT))


def with_expiry(text: str = "walks daily") -> LLMResponse:
    return calls(create(text, expiry=horizon(30)))


@pytest.fixture
def limited_turns(monkeypatch):
    monkeypatch.setattr(ConsolidationAgent, "MAX_CONSOLIDATION_TURNS", 3)


# ----------------------------------------------------------------------------------------------- outcomes per call


async def test_the_executor_tags_each_result_with_its_outcome(store):
    agent = build(store, REPORT)
    seeded = await agent._execute_fact_management_tools([create("seed", expiry=EXPIRY_NONE)], USER, ACC, write_context=ctx())
    seed_id = json.loads(seeded[0].result_str)["fact_id"]

    batch = [
        ToolCall(name="count_words", args={"text": "a b"}),
        create("no expiry", expiry=OMIT),
        create("a past plan", expiry=on("2020-01-01")),
        ToolCall(name="update_fact", args={"fact_id": "nope", "updates": {"tags": ["x"]}}),
        ToolCall(name="mystery", args={}),
        ToolCall(name="search_existing_facts", args={"keywords": ["seed"], "primary_query": "seed"}),
        ToolCall(name="update_fact", args={"fact_id": seed_id, "updates": {"state": "archived"}}),
    ]
    with RequestContext(user_id=USER, account_id=ACC):
        responses = await agent._execute_fact_management_tools(batch, USER, ACC, write_context=ctx())

    assert [r.outcome for r in responses] == [
        OUTCOME_OK, OUTCOME_EXPIRY, OUTCOME_BORN_DUE, OUTCOME_ERROR, OUTCOME_ERROR, OUTCOME_OK, OUTCOME_OK]


async def test_only_a_born_due_refusal_carries_the_born_due_tag(store):
    agent = build(store, REPORT)
    responses = await agent._execute_fact_management_tools(
        [create("no expiry", expiry=OMIT), create("a past plan", expiry=on("2020-01-01")),
         create("malformed", expiry={"kind": "sometimes"})],
        USER, ACC, write_context=ctx())

    results = [json.loads(r.result_str) for r in responses]
    assert [r["refused"] for r in results] == ["expiry"] * 3
    assert [r.get("born_due") for r in results] == [None, True, None]
    assert [r.outcome for r in responses] == [OUTCOME_EXPIRY, OUTCOME_BORN_DUE, OUTCOME_EXPIRY]
    assert not store.docs


async def test_a_raising_tool_is_an_error_outcome(store):
    agent = build(store, REPORT)
    agent._fact_management.create_fact = AsyncMock(side_effect=RuntimeError("boom"))

    (response,) = await agent._execute_fact_management_tools([create("x")], USER, ACC, write_context=ctx())

    assert response.outcome == OUTCOME_ERROR and "boom" in response.result_str


# ----------------------------------------------------------------------------------------------- the abandoned alert


async def test_a_refused_create_never_resent_posts_one_alert_even_with_stats_off_and_counts_the_error(store):
    agent = build(store, missing_expiry(), REPORT, stats=False)

    ops = await run(agent)

    assert ops == []
    assert not store.docs                                           # nothing was written
    (post,) = agent._alert_sink.posts
    assert post.startswith("🚨") and "abandoned 1" in post and "`stage1`" in post
    assert f"create (text {len('walks daily')} chars)" in post and "walks daily" not in post
    assert await today_errors(agent) == 1


async def test_a_refusal_followed_by_a_valid_resend_posts_nothing_and_is_still_counted(store):
    agent = build(store, missing_expiry(), with_expiry(), REPORT, stats=False)

    await run(agent)

    assert agent._alert_sink.posts == []
    assert by_text(store, "walks daily")["expiry_kind"] == "horizon"
    assert await today_errors(agent) == 1


async def test_with_stats_on_the_summary_shows_the_counts_and_the_alert_is_a_second_post(store):
    agent = build(store, missing_expiry(), REPORT, stats=True)

    await run(agent)

    summary, alert = agent._alert_sink.posts
    assert summary.startswith("📊") and "tool-error turns 1 (expiry 1, abandoned 1)" in summary
    assert alert.startswith("🚨") and "abandoned 1" in alert


async def test_a_resent_refusal_shows_abandoned_zero_in_the_summary(store):
    agent = build(store, missing_expiry(), with_expiry(), REPORT, stats=True)

    await run(agent)

    (summary,) = agent._alert_sink.posts
    assert "tool-error turns 1 (expiry 1, abandoned 0)" in summary


async def test_a_born_due_refusal_the_model_discards_is_not_an_abandoned_refusal(store):
    past = calls(create("trip on the 3rd", expiry=on("2020-01-01")))
    agent = build(store, past, REPORT, stats=True)              # the model reads "DISCARD it" and reports

    await run(agent)

    (summary,) = agent._alert_sink.posts                         # no 🚨
    assert "tool-error turns 1 (expiry 1, born due 1, abandoned 0)" in summary
    assert await today_errors(agent) == 1                        # still an expiry tool error for the daily line


async def test_a_born_due_refusal_rewritten_as_history_is_not_abandoned_either(store):
    agent = build(store, calls(create("trip on the 3rd", expiry=on("2020-01-01"))),
                  calls(create("trip on the 3rd happened", expiry=EXPIRY_NONE)), REPORT)

    await run(agent)

    assert agent._alert_sink.posts == []
    assert by_text(store, "trip on the 3rd happened")["expiry_kind"] == "none"


async def test_a_refused_update_never_resent_is_abandoned(store):
    agent = build(store, REPORT)
    seeded = await agent._execute_fact_management_tools([create("seed", expiry=EXPIRY_NONE)], USER, ACC, write_context=ctx())
    seed_id = json.loads(seeded[0].result_str)["fact_id"]
    agent = build(store, calls(ToolCall(name="update_fact", args={"fact_id": seed_id, "updates": {"content": "seed v2"}})), REPORT)

    await run(agent)

    (post,) = agent._alert_sink.posts
    assert post.startswith("🚨") and f"update fact {seed_id[:8]}" in post and "seed v2" not in post
    assert [d["text"] for d in store.docs.values()] == ["seed"]            # nothing was written


async def test_a_generic_tool_error_makes_an_error_turn_but_no_alert_and_no_counter(store):
    agent = build(store, calls(ToolCall(name="update_fact", args={"fact_id": "nope", "updates": {"tags": ["x"]}})),
                  REPORT, stats=True)

    await run(agent)

    (summary,) = agent._alert_sink.posts
    assert "tool-error turns 1 (expiry 0, abandoned 0)" in summary
    assert await today_errors(agent) == 0


async def test_the_turn_limit_pass_posts_the_limit_alert_and_then_the_abandoned_one(store, limited_turns):
    agent = build(store, missing_expiry("first"), calls(ToolCall(name="count_words", args={"text": "x"})))

    assert await run(agent) == []

    limit, abandoned = agent._alert_sink.posts
    assert "turn limit" in limit and "tool-error turns 1 (expiry 1, abandoned 1)" in limit
    assert abandoned.startswith("🚨") and "abandoned 1" in abandoned
    assert await today_errors(agent) == 1


async def test_a_failing_counters_store_or_sink_never_changes_the_pass_result(store, caplog):
    agent = build(store, missing_expiry(), REPORT)
    agent._lifecycle_counters = AsyncMock()
    agent._lifecycle_counters.add.side_effect = RuntimeError("firestore down")
    agent._alert_sink = AsyncMock(spec=AlertSinkPort)
    agent._alert_sink.post.side_effect = RuntimeError("slack down")

    with caplog.at_level("WARNING"):
        assert await run(agent) == []

    assert "Lifecycle counters not updated" in caplog.text and "firestore down" not in caplog.text


async def test_without_a_counters_store_the_pass_still_runs(store):
    agent = build(store, missing_expiry(), REPORT)
    agent._lifecycle_counters = None

    assert await run(agent) == []


async def test_a_pass_without_expiry_errors_writes_no_counter_row(store):
    agent = build(store, with_expiry(), REPORT)

    await run(agent)

    assert agent.db.collection(EnvironmentConfig().fact_lifecycle_daily_collection).docs == {}
