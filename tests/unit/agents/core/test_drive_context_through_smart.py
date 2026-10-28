"""History refs after the move (DRIVE_SPECIALIST_RFC §7.4): the drive specialist returns `drive_context` as one flat,
ordered list (the concatenation of today's per-operation entries, no dedup); Smart's engine wraps each commission one
level — the shape one FileManagement call produced — ConversationHandler writes it into the model message's
full_text, never into its summary, and the next turn's history carries the refs while the turn is recent, so
"delete it" / "the second one" reach the next commission. The specialist itself never reads history."""
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from src.agents.base_agent import BaseAgent
from src.domain.agent import AgentConfig, AgentResponse
from src.domain.llm import LLMRequest, LLMResponse, Message, MessagePart, ToolCall
from src.domain.messaging import MessageContext, SmartResponse
from src.domain.settings import ConsolidationSettings
from src.handlers.conversation_handler import ConversationHandler
from src.infrastructure.agent_coordinator import AgentCoordinator
from src.infrastructure.agent_manifest import Intent
from src.infrastructure.delegation_engine import DelegationEngine

LEASES = [{"path": "Contracts/Lease agreement — Porto.pdf", "ref": "drive:AAA"},
          {"path": "Contracts/Lease Lisbon.pdf", "ref": "drive:BBB"},
          {"path": "Contracts/Lease agreement — Porto.pdf", "ref": "drive:AAA"}]   # a later open: no dedup (§7.4)


def _commission(query: str, n: int) -> ToolCall:
    return ToolCall(name="delegate_to_specialist", args={"intent": Intent.WORK_WITH_DRIVE_FILES, "query": query},
                    thought_signature=f"c{n}")


async def _smart_turn(responses):
    """One Smart loop: one commission per specialist response, then a final text."""
    coordinator = MagicMock(spec=AgentCoordinator)
    coordinator.handle_delegation = AsyncMock(side_effect=responses)
    calls = [LLMResponse(text="", tool_calls=[_commission(f"q{i}", i)]) for i in range(len(responses))]
    call_llm = AsyncMock(side_effect=calls + [LLMResponse(text="1. Porto lease\n2. Lisbon lease")])
    return await DelegationEngine(coordinator=coordinator).execute(
        call_llm=call_llm, base_request=LLMRequest(model_name="m", messages=[
            Message(role="user", parts=[MessagePart(text="find my leases")])]),
        context={"user_id": "u1"}, max_turns=5, calling_agent_id="smart_response_agent_u1")


def _specialist(entries):
    return AgentResponse.success(task_id="t", agent_id="drive_agent_u1", result="Drive result: ok\n…",
                                 history_context={"drive_context": list(entries)},
                                 metadata={"drive_status": "ok"})


async def test_one_commission_is_wrapped_one_level_like_one_file_management_call():
    result = await _smart_turn([_specialist(LEASES)])
    assert result.history_contexts == {"drive_context": [LEASES]}


async def test_two_commissions_in_one_turn_keep_their_order():
    result = await _smart_turn([_specialist(LEASES[:2]), _specialist(LEASES[2:])])
    assert result.history_contexts == {"drive_context": [LEASES[:2], LEASES[2:]]}


def _handler(response):
    session_store = MagicMock()
    session_store.append_messages_batch = AsyncMock()
    session_store.load_session = AsyncMock(return_value=None)
    factory = MagicMock()
    factory.ensure_agents_for_user = AsyncMock()
    factory.get_session_store = MagicMock(return_value=session_store)
    factory.user_repo.get_user = AsyncMock(return_value=None)
    coordinator = MagicMock()
    coordinator.route_message = AsyncMock(return_value=response)
    return ConversationHandler(coordinator=coordinator, agent_factory=factory, file_service=MagicMock(),
                               global_config=ConsolidationSettings(threshold=50, batch_size=30)), session_store


def _channel():
    ch = MagicMock()
    ch.channel_id, ch.platform, ch.max_message_length, ch.supports_message_editing = "C-001", "slack", 4000, True
    for name in ("send_status", "send_message", "send_chunked_message", "update_message", "send_rich_content",
                 "update_status_with_phrase_and_dots"):
        setattr(ch, name, AsyncMock())
    ch.send_status_with_phrase = AsyncMock(return_value=("m1", "thinking"))
    ch.get_status_phrase = AsyncMock(return_value="processing")
    return ch


async def test_the_refs_land_in_full_text_and_never_in_the_summary():
    response = AgentResponse.success(task_id="t", agent_id="smart",
                                     result=SmartResponse(text="1. Porto lease\n2. Lisbon lease"))
    response.metadata["response_summary"] = "Found two leases."
    response.metadata["drive_context"] = [LEASES]
    handler, store = _handler(response)
    ctx = MessageContext(text="find my leases", session_id="s", user_id="u", account_id="a", attachments=[],
                         metadata={})
    with patch.object(handler, "validate_model_output", side_effect=lambda t, u: t):
        await handler.handle_message(ctx, _channel())
    model = next(m for m in store.append_messages_batch.call_args[0][1] if m.role == "model")
    block = json.dumps({"drive_context": [LEASES]}, ensure_ascii=False, separators=(",", ":"))
    assert model.parts[0].full_text.endswith("\n\n" + block)
    assert "drive:" not in model.parts[0].text


class _Agent(BaseAgent):
    async def can_handle(self, message):
        return True

    async def execute(self, message):
        return SimpleNamespace()


def test_the_next_turn_sees_the_refs_while_the_turn_is_recent():
    block = json.dumps({"drive_context": [LEASES[:2]]}, ensure_ascii=False, separators=(",", ":"))
    earlier = [Message(role="user", parts=[MessagePart(text="find my leases")]),
               Message(role="model", parts=[MessagePart(text="Found two leases.",
                                                        full_text="1. Porto lease\n2. Lisbon lease\n\n" + block)])]
    agent = _Agent(AgentConfig(agent_id="smart_response_agent_u1", agent_type="smart_response"))
    recent = agent._apply_history_tier(earlier, max_full_turns=5)
    assert "drive:BBB" in recent[1].parts[0].text and "2. Lisbon lease" in recent[1].parts[0].text
    aged = agent._apply_history_tier(earlier + [Message(role="model", parts=[MessagePart(text="x", full_text="x")])],
                                     max_full_turns=0)
    assert "drive:" not in aged[1].parts[0].text   # aged out: Smart passes the user's words and the path (§7.4)
