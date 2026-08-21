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


def _timed_out(error="Agent failed. Last error: Task execution timeout"):
    return AgentResponse.timeout(task_id="t", agent_id="smart_response_agent_u", error=error)


@pytest.mark.asyncio
async def test_timeout_injects_fast_lane_note_not_apology():
    svc = AgentFallbackService(_coordinator_returns_quick_ok())

    await svc.try_quick_fallback(_timed_out(), _ctx(), [])

    sent_message = svc._coordinator.route_message.await_args.args[0]
    notes = [p.text for p in sent_message.context["current_message_parts"] if p.text]
    joined = " ".join(notes)
    assert "still thinking" in joined or "thinking it through" in joined
    assert "apolog" not in joined.lower()


@pytest.mark.asyncio
async def test_non_timeout_failure_keeps_apology_note():
    svc = AgentFallbackService(_coordinator_returns_quick_ok())

    await svc.try_quick_fallback(_failed(), _ctx(), [])

    sent_message = svc._coordinator.route_message.await_args.args[0]
    notes = [p.text for p in sent_message.context["current_message_parts"] if p.text]
    assert any("apolog" in n.lower() for n in notes)


@pytest.mark.asyncio
async def test_timeout_calls_smart_retry_schedule():
    smart_retry = MagicMock()
    smart_retry.schedule = AsyncMock()
    svc = AgentFallbackService(_coordinator_returns_quick_ok(), smart_retry=smart_retry)
    ctx = _ctx()

    await svc.try_quick_fallback(_timed_out(), ctx, [])

    smart_retry.schedule.assert_awaited_once_with(ctx, [])


@pytest.mark.asyncio
async def test_non_timeout_failure_does_not_call_smart_retry():
    smart_retry = MagicMock()
    smart_retry.schedule = AsyncMock()
    svc = AgentFallbackService(_coordinator_returns_quick_ok(), smart_retry=smart_retry)

    await svc.try_quick_fallback(_failed(), _ctx(), [])

    smart_retry.schedule.assert_not_awaited()


@pytest.mark.asyncio
async def test_no_smart_retry_configured_does_not_raise():
    svc = AgentFallbackService(_coordinator_returns_quick_ok(), smart_retry=None)

    response = await svc.try_quick_fallback(_timed_out(), _ctx(), [])

    assert response.status == AgentStatus.SUCCESS  # Quick fallback still worked
