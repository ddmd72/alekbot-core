"""Facts S3 · T8: the directive-cap backstop reports an op only for an invalidation that was applied (plan §3.20)."""
import logging
from typing import Any, Dict
from unittest.mock import AsyncMock

from src.domain.entities import FactDomain
from src.ports.alert_sink import AlertSinkPort
from src.ports.fact_management_port import FactManagementPort
from tests.support.fact_kit import make_fact
from tests.unit.agents.test_consolidation_agent import _ACCOUNT_ID, _make_agent


def directive(fact_id: str):
    return make_fact(f"rule {fact_id}", id=fact_id, domain=FactDomain.AGENT_DIRECTIVE)


def build(statuses: Dict[str, Any]):
    """An agent whose port answers each fact id with its scripted result; the rulebook is two over the cap."""
    port = AsyncMock(spec=FactManagementPort)

    async def update_fact(fact_id, updates, account_id=None, context=None):
        return statuses[fact_id]

    port.update_fact.side_effect = update_fact
    agent, repo, _, _ = _make_agent(fact_management=port)
    cap = agent.DIRECTIVE_HARD_CAP
    repo.get_active_facts_ordered = AsyncMock(return_value=[directive(f"d{i}") for i in range(cap + 2)])
    sink = AsyncMock(spec=AlertSinkPort)
    agent._alert_sink = sink
    return agent, sink, cap


async def test_an_applied_invalidation_is_reported_and_posts_nothing():
    agent, sink, cap = build({"d15": {"status": "updated"}, "d16": {"status": "updated"}})

    ops = await agent._enforce_directive_cap(_ACCOUNT_ID, agent._write_context("backstop"))

    assert [o["fact_id"] for o in ops] == [f"d{cap}", f"d{cap + 1}"]
    sink.post.assert_not_awaited()


async def test_a_refused_or_failed_result_is_not_reported_logs_an_error_and_posts_one_alert(caplog):
    agent, sink, cap = build({
        "d15": {"status": "failed", "refused": "expiry", "message": "nope"},
        "d16": {"status": "updated"},
    })

    with caplog.at_level(logging.ERROR):
        ops = await agent._enforce_directive_cap(_ACCOUNT_ID, agent._write_context("backstop"))

    assert [o["fact_id"] for o in ops] == ["d16"]
    assert any(r.levelno == logging.ERROR and "d15" in r.getMessage() for r in caplog.records)
    sink.post.assert_awaited_once()
    text = sink.post.await_args.args[0]
    assert text.startswith("🚨") and "1 of 2" in text and f"cap of {cap}" in text
    assert _ACCOUNT_ID[:8] in text and "rule d15" not in text


async def test_every_invalidation_failing_reports_no_op_and_one_alert_for_all():
    agent, sink, _ = build({"d15": {"status": "not_found"}, "d16": {"status": "failed"}})

    ops = await agent._enforce_directive_cap(_ACCOUNT_ID, agent._write_context("backstop"))

    assert ops == []
    sink.post.assert_awaited_once()
    assert "2 of 2" in sink.post.await_args.args[0]


async def test_a_result_that_is_not_a_dict_counts_as_not_applied():
    agent, sink, _ = build({"d15": None, "d16": {"status": "updated"}})

    ops = await agent._enforce_directive_cap(_ACCOUNT_ID, agent._write_context("backstop"))

    assert [o["fact_id"] for o in ops] == ["d16"]
    sink.post.assert_awaited_once()


async def test_a_failing_sink_does_not_change_the_ops():
    agent, sink, _ = build({"d15": {"status": "failed"}, "d16": {"status": "updated"}})
    sink.post.side_effect = RuntimeError("slack down")

    ops = await agent._enforce_directive_cap(_ACCOUNT_ID, agent._write_context("backstop"))

    assert [o["fact_id"] for o in ops] == ["d16"]
