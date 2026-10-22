"""consolidate_full: Stage 3 (email) is best-effort and runs under the rest of the agent's budget.

A Stage 3 error or a slow Stage 3 must not fail the batch (the whole batch would re-run Stage 1 over facts it
already wrote); the unconsolidated emails simply stay for the next run. Stage 1/2 failure still aborts.
"""
import asyncio
import logging
import time
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

import src.agents.consolidation_agent as consolidation_agent
from src.domain.agent import AgentStatus
from src.domain.request_context import RequestContext
from tests.unit.agents.test_consolidation_agent import _ACCOUNT_ID, _USER_ID, _make_agent, _make_message


class _FakeEmailRepo:
    """Holds unconsolidated emails; mark_consolidated really removes them from the pending set."""

    def __init__(self, n=2):
        self.pending = [
            SimpleNamespace(
                email_id=f"e{i}", text="invoice due", category="finance", tags=[],
                email_date=datetime(2026, 10, 1, tzinfo=timezone.utc), from_address="a@b.c",
                subject="s", attachments=[], metadata={},
            )
            for i in range(n)
        ]
        self.consolidated: list[str] = []

    async def get_unconsolidated_batch(self, user_id, limit):
        return [e for e in self.pending if e.email_id not in self.consolidated][:limit]

    async def mark_consolidated(self, user_id, email_ids, when):
        self.consolidated.extend(email_ids)


def _stage1_ok(agent):
    agent._handle_consolidate = AsyncMock(return_value=MagicMock(
        status=AgentStatus.SUCCESS, result={"operations": 3, "facts_affected": 2}))


async def _run(agent, timeout_ms=None):
    msg = _make_message("consolidate_full", payload_extra={"messages": [{"role": "user", "text": "hi"}]})
    msg.timeout_ms = timeout_ms
    with RequestContext(user_id=_USER_ID, account_id=_ACCOUNT_ID):
        return await agent._handle_consolidate_full(msg, _USER_ID, _ACCOUNT_ID, [], time.time())


async def test_stage3_error_keeps_the_batch_successful_and_stage1_runs_once(caplog):
    emails = _FakeEmailRepo()
    agent, *_ = _make_agent(indexed_email_repo=emails)
    _stage1_ok(agent)
    agent._run_consolidation_loop = AsyncMock(side_effect=RuntimeError("email llm down"))

    with caplog.at_level(logging.ERROR):
        response = await _run(agent)

    assert response.status == AgentStatus.SUCCESS
    assert response.result["stage1_operations"] == 3 and response.result["email_batches"] == 0
    agent._handle_consolidate.assert_awaited_once()
    assert emails.consolidated == []     # not marked: the next run picks them up
    errors = [r for r in caplog.records if r.levelno == logging.ERROR and "Stage 3" in r.getMessage()]
    assert len(errors) == 1 and "email llm down" in errors[0].getMessage()


async def test_execute_level_stage3_error_does_not_fail_the_batch_result():
    emails = _FakeEmailRepo()
    agent, *_ = _make_agent(indexed_email_repo=emails)
    _stage1_ok(agent)
    agent._handle_consolidate_email = AsyncMock(side_effect=ValueError("bad email payload"))
    msg = _make_message("consolidate_full", payload_extra={"messages": [{"role": "user", "text": "hi"}]})

    with RequestContext(user_id=_USER_ID, account_id=_ACCOUNT_ID):
        response = await agent.execute(msg)

    assert response.status == AgentStatus.SUCCESS


async def test_slow_stage3_is_cut_at_the_remaining_budget_and_the_batch_succeeds(monkeypatch, caplog):
    monkeypatch.setattr(consolidation_agent, "_STAGE3_MARGIN_S", 0.0)
    monkeypatch.setattr(consolidation_agent, "_STAGE3_MIN_S", 0.01)
    emails = _FakeEmailRepo()
    agent, *_ = _make_agent(indexed_email_repo=emails)
    _stage1_ok(agent)
    cancelled = []

    async def slow(*_a, **_k):
        try:
            await asyncio.sleep(30)
        except asyncio.CancelledError:
            cancelled.append(True)
            raise

    agent._run_consolidation_loop = slow

    with caplog.at_level(logging.ERROR):
        response = await _run(agent, timeout_ms=200)      # 0.2 s of budget in all

    assert response.status == AgentStatus.SUCCESS
    assert cancelled == [True]                            # the email work was really cut, not abandoned
    assert emails.consolidated == []
    cut = [r.getMessage() for r in caplog.records if "Stage 3" in r.getMessage() and r.levelno == logging.ERROR]
    assert len(cut) == 1 and "timed out" in cut[0]


async def test_stage3_skipped_with_an_info_line_when_too_little_budget_is_left(caplog):
    agent, *_ = _make_agent(indexed_email_repo=_FakeEmailRepo())
    _stage1_ok(agent)
    agent._handle_consolidate_email = AsyncMock()

    with caplog.at_level(logging.INFO):
        response = await _run(agent, timeout_ms=40_000)   # 40 s - margin leaves 10 s < the minimum

    assert response.status == AgentStatus.SUCCESS
    agent._handle_consolidate_email.assert_not_awaited()
    skipped = [r for r in caplog.records if "Stage 3 skipped" in r.getMessage()]
    assert len(skipped) == 1 and skipped[0].levelno == logging.INFO


async def test_stage3_uses_the_agent_config_budget_when_the_message_has_none():
    agent, *_ = _make_agent(indexed_email_repo=_FakeEmailRepo())
    agent.config.timeout_ms = 40_000
    _stage1_ok(agent)
    agent._handle_consolidate_email = AsyncMock()

    await _run(agent, timeout_ms=None)

    agent._handle_consolidate_email.assert_not_awaited()


async def test_stage3_within_budget_runs_and_counts_its_batches():
    emails = _FakeEmailRepo(n=2)
    agent, *_ = _make_agent(indexed_email_repo=emails)
    _stage1_ok(agent)
    agent._run_consolidation_loop = AsyncMock(return_value=[{"action": "CREATE"}])

    response = await _run(agent, timeout_ms=900_000)

    assert response.status == AgentStatus.SUCCESS
    assert response.result["email_batches"] == 1 and response.result["email_facts_affected"] == 1
    assert emails.consolidated == ["e0", "e1"]


async def test_no_deadline_agent_runs_stage3_unbounded():
    emails = _FakeEmailRepo(n=1)
    agent, *_ = _make_agent(indexed_email_repo=emails)
    _stage1_ok(agent)
    agent._run_consolidation_loop = AsyncMock(return_value=[])

    response = await _run(agent, timeout_ms=None)       # AgentConfig default: no timeout

    assert response.result["email_batches"] == 1


async def test_cancellation_during_stage3_is_not_swallowed():
    agent, *_ = _make_agent(indexed_email_repo=_FakeEmailRepo())
    _stage1_ok(agent)
    agent._handle_consolidate_email = AsyncMock(side_effect=asyncio.CancelledError())

    with pytest.raises(asyncio.CancelledError):
        await _run(agent)


async def test_stage1_failure_still_aborts_and_stage3_is_not_attempted():
    agent, *_ = _make_agent(indexed_email_repo=_FakeEmailRepo())
    agent._handle_consolidate = AsyncMock(return_value=MagicMock(status=AgentStatus.FAILED, error="stage1 failed"))
    agent._handle_consolidate_email = AsyncMock()

    response = await _run(agent)

    assert response.status == AgentStatus.FAILED and response.error == "stage1 failed"
    agent._handle_consolidate_email.assert_not_awaited()
