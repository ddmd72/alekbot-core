import pytest
from unittest.mock import AsyncMock, MagicMock

from src.domain.agent import AgentStatus
from src.agents.lelik_agent import LelikAgent


def _build_agent(telephony):
    return LelikAgent(
        config=MagicMock(agent_id="lelik_agent_u1"),
        telephony=telephony,
        from_number="+346002",
        status_callback_url="https://main.example.com/voice/status",
        prompt_builder=AsyncMock(),
        persona=AsyncMock(),
    )


@pytest.mark.asyncio
async def test_execute_originates_call_with_answer_url_carrying_ticket():
    telephony = AsyncMock()
    telephony.originate_call.return_value = "CA123"

    agent = _build_agent(telephony)

    response = await agent.execute(
        purpose="user asked to talk", ticket="t1", answer_url="https://main.example.com/voice/answer",
        to_number="+346001",
    )

    assert response.status == AgentStatus.SUCCESS
    telephony.originate_call.assert_awaited_once()
    call = telephony.originate_call.await_args
    assert call.kwargs["from_"] == "+346002"
    assert call.kwargs["to"] == "+346001"
    assert "ticket=t1" in call.kwargs["answer_url"]
    assert call.kwargs["answer_url"].startswith("https://main.example.com/voice/answer?")
    # FIX I1(b), final whole-branch review: the status callback carries the
    # ticket too. It is the only correlation path from a call-status event back
    # to the ticket/user whose one-call marker must be released when the
    # callback rings out, is busy, or fails at the carrier — in all of which
    # /voice/answer is never reached at all. (Was asserted as the bare base URL
    # before /voice/status existed as a route; the base URL is still asserted.)
    assert "ticket=t1" in call.kwargs["status_callback_url"]
    assert call.kwargs["status_callback_url"].startswith("https://main.example.com/voice/status?")


@pytest.mark.asyncio
async def test_execute_propagates_origination_failure():
    """The auth webhook (src/web/voice_webhook_app.py) relies on execute() raising
    on failure so it can release the ticket/one-call marker (commit 67beca7) — it
    does NOT inspect a returned AgentResponse.failure() for this path."""
    telephony = AsyncMock()
    telephony.originate_call.side_effect = RuntimeError("origination boom")

    agent = _build_agent(telephony)

    with pytest.raises(RuntimeError, match="origination boom"):
        await agent.execute(
            purpose="user asked to talk", ticket="t1", answer_url="https://main.example.com/voice/answer",
            to_number="+346001",
        )


@pytest.mark.asyncio
async def test_can_handle_returns_false_never_routed_via_coordinator():
    """internal=True, no Intent registered (RFC §4.13) — can_handle exists only
    to satisfy BaseAgent's abstract contract; AgentCoordinator never calls it."""
    agent = _build_agent(AsyncMock())
    assert await agent.can_handle(MagicMock()) is False
