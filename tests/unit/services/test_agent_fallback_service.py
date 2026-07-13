"""Unit tests for AgentFallbackService — the ops alert on primary (Smart) failure.

Incident 2026-07-13: a mis-cased provider made Smart fail 100%; the Quick fallback masked it
so all traffic silently looked like it was on Quick. The alert makes that failure visible.
"""
import pytest
from unittest.mock import AsyncMock, MagicMock

from src.services.agent_fallback_service import AgentFallbackService
from src.domain.agent import AgentResponse, AgentStatus
from src.domain.messaging import MessageContext


def _failed(error="Provider 'openAI' not registered"):
    return AgentResponse(
        task_id="t", agent_id="smart_response_agent_u",
        status=AgentStatus.FAILED, result=None, confidence=0.0, error=error,
    )


def _ctx():
    return MessageContext(text="hi", session_id="s", user_id="user-1234", account_id="a")


def _coordinator_returns_quick_ok():
    coordinator = MagicMock()
    coordinator.route_message = AsyncMock(
        return_value=AgentResponse.success(task_id="t", agent_id="quick", result="ok")
    )
    return coordinator


@pytest.mark.asyncio
async def test_primary_failure_fires_ops_alert_with_detail():
    alert = MagicMock()
    alert.post = AsyncMock()
    svc = AgentFallbackService(_coordinator_returns_quick_ok(), alert_webhook=alert)

    await svc.try_quick_fallback(_failed(), _ctx(), [])

    alert.post.assert_awaited_once()
    msg = alert.post.await_args.args[0]
    assert "Smart primary FAILED" in msg
    assert "openAI" in msg  # the underlying failure detail is surfaced


@pytest.mark.asyncio
async def test_no_alert_when_primary_succeeds():
    alert = MagicMock()
    alert.post = AsyncMock()
    svc = AgentFallbackService(MagicMock(), alert_webhook=alert)

    ok = AgentResponse.success(task_id="t", agent_id="smart", result="ok")
    result = await svc.try_quick_fallback(ok, _ctx(), [])

    assert result is ok
    alert.post.assert_not_awaited()


@pytest.mark.asyncio
async def test_alert_failure_does_not_break_fallback():
    alert = MagicMock()
    alert.post = AsyncMock(side_effect=RuntimeError("slack down"))
    svc = AgentFallbackService(_coordinator_returns_quick_ok(), alert_webhook=alert)

    resp = await svc.try_quick_fallback(_failed(), _ctx(), [])

    assert resp.status == AgentStatus.SUCCESS  # Quick fallback still delivered


@pytest.mark.asyncio
async def test_no_webhook_configured_is_safe():
    svc = AgentFallbackService(_coordinator_returns_quick_ok())  # no alert_webhook

    resp = await svc.try_quick_fallback(_failed(), _ctx(), [])

    assert resp.status == AgentStatus.SUCCESS
