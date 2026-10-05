"""After LONG_RUNNING_TURNS a timeout is a failure like any other; nothing promises a follow-up."""
import inspect
from unittest.mock import AsyncMock

import pytest

from src.domain.agent import AgentResponse
from src.domain.messaging import MessageContext
from src.services.agent_fallback_service import AgentFallbackService


@pytest.fixture
def make_context():
    def _make():
        return MessageContext(text="q", session_id="s", user_id="u", account_id="a")
    return _make


def test_no_smart_retry_dependency():
    assert "smart_retry" not in inspect.signature(AgentFallbackService.__init__).parameters


async def test_timeout_uses_the_failure_note(make_context):
    coordinator = AsyncMock()
    coordinator.route_message.return_value = AgentResponse.success(task_id="t", agent_id="q", result="ok")
    svc = AgentFallbackService(coordinator=coordinator)
    await svc.try_quick_fallback(AgentResponse.timeout(task_id="t", agent_id="s", error="x"),
                                 make_context(), [])
    parts = coordinator.route_message.call_args.args[0].context["current_message_parts"]
    assert parts[-1].text == AgentFallbackService._FAILURE_NOTE
