"""Mutation survivors of ConversationHandler (group ch3): the text handed to the output validator, the account id
in the agent context, and the status animation stopping even when a cancel is swallowed mid-update."""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from src.domain.agent import AgentResponse
from src.domain.messaging import MessageContext, SmartResponse
from src.domain.prompt_v3.security import RiskLevel, TrustZone
from src.domain.settings import ConsolidationSettings
from src.handlers.conversation_handler import ConversationHandler

_REAL_SLEEP = asyncio.sleep


def _handler(security_port=None, route=None):
    response = AgentResponse.success(task_id="t", agent_id="smart", result=SmartResponse(text="Hi."))
    session_store = MagicMock()
    session_store.append_messages_batch = AsyncMock()
    session_store.load_session = AsyncMock(return_value=None)
    factory = MagicMock()
    factory.ensure_agents_for_user = AsyncMock()
    factory.get_session_store = MagicMock(return_value=session_store)
    factory.user_repo.get_user = AsyncMock(return_value=None)
    coordinator = MagicMock()
    coordinator.route_message = AsyncMock(return_value=response) if route is None else route
    handler = ConversationHandler(coordinator=coordinator, agent_factory=factory, file_service=MagicMock(),
                                  global_config=ConsolidationSettings(threshold=50, batch_size=30),
                                  security_port=security_port)
    return handler, coordinator


def _channel(update=None):
    ch = MagicMock()
    ch.channel_id = "C-001"
    ch.platform = "slack"
    for name in ("send_status", "send_message", "send_chunked_message", "update_message", "send_rich_content"):
        setattr(ch, name, AsyncMock())
    ch.update_status_with_phrase_and_dots = update or AsyncMock()
    ch.send_status_with_phrase = AsyncMock(return_value=("m1", "thinking"))
    ch.get_status_phrase = AsyncMock(return_value="processing")
    ch.max_message_length = 4000
    ch.supports_message_editing = True
    return ch


def _ctx(account_id="acct-alpha"):
    return MessageContext(text="hello", session_id="s", user_id="alpha-user", account_id=account_id,
                          attachments=[], metadata={})


class _Port:
    """Validator with the real signature: `text` is required."""

    def __init__(self):
        self.calls = []

    async def validate(self, text, context, zone):
        self.calls.append((text, context, zone))
        return SimpleNamespace(risk_level=RiskLevel.HIGH, patterns_detected=[], sanitized_text=f"clean<{text}>")


async def test_validator_receives_the_model_text_and_its_sanitized_result_is_returned():
    port = _Port()
    handler, _ = _handler(security_port=port)

    result = await handler.validate_model_output("raw answer", "alpha-user")

    assert result == "clean<raw answer>"
    assert port.calls == [("raw answer", "model_output_user_alpha-user", TrustZone.UNTRUSTED)]


async def test_agent_context_carries_the_account_id():
    handler, coordinator = _handler()
    with patch.object(handler, "validate_model_output", side_effect=lambda t, u: t):
        await handler.handle_message(_ctx("acct-alpha"), _channel())
    assert coordinator.route_message.await_args.args[0].context["account_id"] == "acct-alpha"


async def test_stop_signal_ends_the_animation_even_if_the_update_swallows_the_cancel():
    in_update = asyncio.Event()
    calls = []

    async def update(message_id, phrase, dots):
        calls.append(dots)
        if len(calls) == 1:
            in_update.set()
            try:
                await _REAL_SLEEP(3600)
            except asyncio.CancelledError:
                return  # a channel adapter that swallows the cancel

    async def route(message):
        await in_update.wait()
        return AgentResponse.success(task_id="t", agent_id="smart", result=SmartResponse(text="Hi."))

    async def fast_sleep(delay):
        await _REAL_SLEEP(0.001)

    handler, _ = _handler(route=AsyncMock(side_effect=route))
    with patch.object(handler, "validate_model_output", side_effect=lambda t, u: t), \
            patch("src.handlers.conversation_handler.asyncio.sleep", fast_sleep):
        await asyncio.wait_for(handler.handle_message(_ctx(), _channel(update=update)), timeout=3)

    assert len(calls) == 1
