"""The Quick fallback note travels as a context field, not as a message part (SYSTEM_TURN_FINGERPRINT_RFC §4.3)."""
from src.domain.agent import AgentResponse, AgentStatus
from src.domain.llm import MessagePart
from src.domain.messaging import MessageContext
from src.domain.system_fingerprint import wrap_system
from src.services.agent_fallback_service import AgentFallbackService
from tests.unit.agents.core.test_quick_turn_key import _quick
from tests.unit.agents.core.turn_key_world import KEY, KeySource, last_user_parts
from tests.support.scripted_llm import ScriptedLLM, tools


def _context() -> MessageContext:
    return MessageContext(user_id="u1", account_id="a1", session_id="u1:C1", thread_id="t1", text="What is Tom's plan?")


def _failed() -> AgentResponse:
    return AgentResponse.failure(task_id="t", agent_id="smart_response_agent_u1", error="boom")


class _Recorder:
    def __init__(self) -> None:
        self.messages = []

    async def route_message(self, message):
        self.messages.append(message)
        return AgentResponse.success(task_id="t", agent_id="quick", result="sorry")


class _ToQuick:
    def __init__(self, quick) -> None:
        self.quick = quick

    async def route_message(self, message):
        return await self.quick.execute(message)


async def test_the_note_is_a_context_field_and_the_parts_are_the_originals():
    parts = [MessagePart(text="What is Tom's plan?")]
    coordinator = _Recorder()
    await AgentFallbackService(coordinator).try_quick_fallback(_failed(), _context(), parts)
    sent = coordinator.messages[0]
    assert sent.context["current_message_parts"] == parts
    assert sent.context["fallback_note"] == AgentFallbackService._FAILURE_NOTE


async def test_end_to_end_the_model_sees_the_keyed_note():
    llm = ScriptedLLM([tools(("deliver_response", {"response": "Sorry about that."}))])
    quick = _quick(llm, KeySource(KEY))
    response = await AgentFallbackService(_ToQuick(quick)).try_quick_fallback(
        _failed(), _context(), [MessagePart(text="What is Tom's plan?")])
    assert response.status == AgentStatus.SUCCESS
    assert last_user_parts(llm.requests[0])[-1] == wrap_system(AgentFallbackService._FAILURE_NOTE, KEY)
