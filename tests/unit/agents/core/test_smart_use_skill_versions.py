"""End to end in Smart: a newer saved version is loaded over a stale body still visible in history
(AGENT_WORKSPACE_RFC §11.2)."""
from dataclasses import dataclass, field
from typing import List
from unittest.mock import AsyncMock, MagicMock

from src.adapters.in_memory_provider_resilience import InMemoryProviderResilience
from src.agents.core.smart_response_agent import SmartResponseAgent
from src.domain.agent import AgentConfig, AgentIntent, AgentMessage
from src.domain.skill import Skill
from src.domain.user import PerformanceTier, UserBotConfig
from src.infrastructure.task_execution_resolver import TaskExecutionResolver
from src.ports.llm_port import LLMPort, LLMResponse, Message, MessagePart, ProviderCapabilities, ToolCall, UsageMetadata
from src.services.agent_context_builder import AgentExecutionContext


@dataclass
class _Session:
    history: List[Message] = field(default_factory=list)


def _resp(text="", tool_calls=None):
    return LLMResponse(text=text, tool_calls=tool_calls or [],
                       usage_metadata=UsageMetadata(prompt_tokens=1, completion_tokens=1, total_tokens=2))


async def test_a_newer_saved_version_is_loaded_over_a_stale_visible_body():
    skill = Skill(name="morning-newspaper", description="Use for the newspaper.", body="v2 body", version=2)
    history = [Message(role="user", parts=[MessagePart(text="make the newspaper")]),
               Message(role="model", parts=[MessagePart(text='Issue.\n\n[Skill "morning-newspaper" v1]\nv1 body')])]
    llm = MagicMock(spec=LLMPort)
    llm.generate_content = AsyncMock(side_effect=[
        _resp(tool_calls=[ToolCall(name="use_skill", args={"name": "morning-newspaper"})]), _resp("issue"),
    ])
    store = MagicMock()
    store.load_session = AsyncMock(return_value=_Session(history))
    builder = MagicMock()
    builder.build_for_agent = AsyncMock(return_value="SYSTEM_PROMPT")
    coordinator = MagicMock()
    coordinator.get_available_intents_for = MagicMock(return_value=[])
    resolver = MagicMock(spec=TaskExecutionResolver)
    resolver.resolve.return_value = None
    skills = MagicMock()
    skills.list_skills = AsyncMock(return_value=[skill])
    ctx = AgentExecutionContext(agent_type="smart", provider=llm, model_name="m", tier=PerformanceTier.BALANCED,
                                capabilities=ProviderCapabilities(), resilience_port=InMemoryProviderResilience())
    agent = SmartResponseAgent(
        config=AgentConfig(agent_id="smart_response_agent_u1", agent_type="smart_response", llm_model="m",
                           timeout_ms=60000, metadata={"user_id": "u1"}),
        execution_context=ctx, session_store=store, prompt_builder=builder, resolver=resolver,
        user_config=UserBotConfig(), coordinator=coordinator, skill_service=skills,
    )
    message = AgentMessage.create(
        sender="notification_service", recipient="smart_response_agent_u1", intent=AgentIntent.QUERY,
        payload={"text": ""},
        context={"session_id": "u1:C1", "user_id": "u1", "account_id": "a1",
                 "current_message_parts": [MessagePart(text="[System: [SELF-REMINDER] \"Morning newspaper\"]")]},
    )
    await agent.execute(message)
    second = llm.generate_content.await_args_list[1].kwargs["request"]
    sent = "\n".join(p.model_dump_json() for m in second.messages for p in m.parts)
    assert "v2 body" in sent
