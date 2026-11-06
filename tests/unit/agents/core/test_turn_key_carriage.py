"""Carriage of the system-turn key (SYSTEM_TURN_FINGERPRINT_RFC §4.8), frozen-core part: every new parameter is
accepted and defaults to None; the orchestrators accept an injected key source. Behaviour comes with later parts."""
import inspect
from unittest.mock import AsyncMock, MagicMock

from src.adapters.in_memory_provider_resilience import InMemoryProviderResilience
from src.agents.core.quick_response_agent import QuickResponseAgent
from src.agents.core.smart_response_agent import SmartResponseAgent
from src.domain.agent import AgentConfig
from src.domain.llm import LLMRequest, Message, MessagePart
from src.domain.system_fingerprint import is_valid_key
from src.domain.user import PerformanceTier, PromptPreferences, UserBotConfig
from src.infrastructure.delegation_engine import DelegationEngine
from src.infrastructure.task_execution_resolver import TaskExecutionResolver
from src.infrastructure.turn_key import draw_turn_key
from src.ports.llm_port import AgentExecutionContext, LLMPort, ProviderCapabilities
from src.ports.prompt_builder_port import PromptBuilderPort
from src.ports.repository import FactRepository
from src.ports.session_store import SessionStore
from src.services.prompt_builder import UserPromptBuilder
from src.services.prompt_v3.prompt_assembly_service import PromptAssemblyService

KEY = "lamp-orbit-fern"


def _context() -> AgentExecutionContext:
    return AgentExecutionContext(
        agent_type="smart", provider=MagicMock(spec=LLMPort), model_name="model-x", tier=PerformanceTier.BALANCED,
        capabilities=ProviderCapabilities(), resilience_port=InMemoryProviderResilience(),
    )


def _config(agent_id: str) -> AgentConfig:
    return AgentConfig(agent_id=agent_id, agent_type=agent_id, llm_model="model-x", timeout_ms=60000)


class TestTurnKeyCarriage:
    def test_llm_request_turn_key_defaults_to_none_and_is_accepted(self):
        messages = [Message(role="user", parts=[MessagePart(text="hi")])]
        assert LLMRequest(model_name="m", messages=messages).turn_key is None
        assert LLMRequest(model_name="m", messages=messages, turn_key=KEY).turn_key == KEY

    def test_new_keywords_default_to_none(self):
        for fn, name in [
            (PromptBuilderPort.build_for_agent, "session_key"),
            (UserPromptBuilder.build_for_agent, "session_key"),
            (PromptAssemblyService.assemble, "session_key"),
            (DelegationEngine.execute, "turn_key"),
        ]:
            assert inspect.signature(fn).parameters[name].default is None, fn

    async def test_the_prompt_builder_passes_the_session_key_to_the_assembly(self):
        repo = MagicMock(spec=FactRepository)
        repo.get_biographical_context_cached = AsyncMock(return_value=[])
        assembly = MagicMock()
        assembly.assemble = AsyncMock(return_value="PROMPT")
        builder = UserPromptBuilder(repo=repo, user_id="u1", config=UserBotConfig(
            prompt_preferences=PromptPreferences()), assembly_service=assembly)
        await builder.build_for_agent(agent_type="smart", user_id="u1", account_id="a1", session_key=KEY)
        assert assembly.assemble.call_args.kwargs["session_key"] == KEY
        await builder.build_for_agent(agent_type="smart", user_id="u1", account_id="a1")
        assert assembly.assemble.call_args.kwargs["session_key"] is None

    def test_the_default_source_draws_a_valid_key(self):
        assert is_valid_key(draw_turn_key())

    def test_smart_and_quick_accept_an_injected_source(self):
        def source() -> str:
            return KEY

        smart = SmartResponseAgent(
            config=_config("smart_response_agent"), execution_context=_context(),
            session_store=MagicMock(spec=SessionStore), prompt_builder=AsyncMock(spec=PromptBuilderPort),
            resolver=MagicMock(spec=TaskExecutionResolver), user_config=UserBotConfig(), turn_key_source=source,
        )
        quick = QuickResponseAgent(
            config=_config("quick_response_agent"), execution_context=_context(),
            session_store=MagicMock(spec=SessionStore), prompt_builder=AsyncMock(spec=PromptBuilderPort),
            turn_key_source=source,
        )
        assert smart._turn_key_source is source
        assert quick._turn_key_source is source

    def test_without_a_source_both_default_to_draw_turn_key(self):
        smart = SmartResponseAgent(
            config=_config("smart_response_agent"), execution_context=_context(),
            session_store=MagicMock(spec=SessionStore), prompt_builder=AsyncMock(spec=PromptBuilderPort),
            resolver=MagicMock(spec=TaskExecutionResolver), user_config=UserBotConfig(),
        )
        quick = QuickResponseAgent(
            config=_config("quick_response_agent"), execution_context=_context(),
            session_store=MagicMock(spec=SessionStore), prompt_builder=AsyncMock(spec=PromptBuilderPort),
        )
        assert smart._turn_key_source is draw_turn_key
        assert quick._turn_key_source is draw_turn_key
