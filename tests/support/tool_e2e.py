"""A real `SmartResponseAgent` over a scripted model, for end-to-end tests of its local tools and delegations
(TEST_COVERAGE_SYSTEMIC_RFC §5 P14, card A; P13 card F builds on `smart_rig` too).

`smart_rig(llm, ...)` takes every keyword of Smart's constructor. Each one left at None keeps a stub that touches
nothing: `StubPromptBuilder` (returns a fixed prompt and records its calls), `EmptySessions` (no history), a resolver
that never overrides (`MagicMock(spec=TaskExecutionResolver)`, never asserted on: the one internal mock the package
allows), `UserBotConfig()`, no repository (no biographical read) and a registry-less `AgentCoordinator()` (no delegate
intents). A given object is passed through unchanged.

`smart_message(text, ...)` is the message a chat turn delivers: `interactive_delivery` is set only when `interactive`
(ConversationHandler sets it; every background path leaves it out), which gates `draft_skill` and
`propose_memory_facts`.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional
from unittest.mock import MagicMock

from src.agents.core.smart_response_agent import SmartResponseAgent
from src.domain.agent import AgentConfig, AgentIntent, AgentMessage
from src.domain.llm import Message, MessagePart
from src.domain.user import UserBotConfig
from src.infrastructure.agent_coordinator import AgentCoordinator
from src.infrastructure.task_execution_resolver import TaskExecutionResolver
from src.ports.llm_port import AgentExecutionContext, LLMPort
from src.ports.prompt_builder_port import PromptBuilderPort
from tests.support.scripted_llm import execution_context as _execution_context


class StubPromptBuilder(PromptBuilderPort):
    """Serves `prompt` for every agent and records each `build_for_agent` call's keywords in `calls`."""

    def __init__(self, prompt: str = "SYSTEM") -> None:
        self.prompt = prompt
        self.calls: List[Dict[str, Any]] = []

    async def preload_components(self) -> None:
        return None

    async def build_for_agent(self, **kwargs: Any) -> str:  # type: ignore[override]
        self.calls.append(kwargs)
        return self.prompt

    def merge_enriched_context_with_biographical(
        self, enriched_context: Optional[Dict[str, Any]], cached_biographical: Optional[List[Dict[str, Any]]] = None,
    ) -> List[Dict[str, Any]]:
        return list(cached_biographical or [])

    def invalidate_cache(self, component_key: Optional[str] = None) -> None:
        return None

    def invalidate_biographical_cache(self, user_id: str) -> None:
        return None

    def get_cache_stats(self) -> Dict[str, Any]:
        return {}


@dataclass
class _EmptySession:
    history: List[Message] = field(default_factory=list)


class EmptySessions:
    """A session store with no history (Smart reads only `load_session(...).history`)."""

    async def load_session(self, session_id: str) -> _EmptySession:
        return _EmptySession()


def _resolver() -> Any:
    resolver = MagicMock(spec=TaskExecutionResolver)
    resolver.resolve.return_value = None
    resolver.next_provider_override.return_value = None
    return resolver


def smart_rig(
    llm: LLMPort, *,
    user_id: str = "u1",
    execution_context: Optional[AgentExecutionContext] = None,
    session_store: Optional[Any] = None,
    prompt_builder: Optional[PromptBuilderPort] = None,
    resolver: Optional[Any] = None,
    user_config: Optional[UserBotConfig] = None,
    repository: Optional[Any] = None,
    embedding_service: Optional[Any] = None,
    coordinator: Optional[Any] = None,
    model_name: Optional[str] = None,
    history_recent_full_turns: int = 5,
    history_summary_service: Optional[Any] = None,
    user_timezone: str = "UTC",
    thinking_effort: Optional[str] = None,
    skill_service: Optional[Any] = None,
    long_turn_registry: Optional[Any] = None,
    memory_fact_service: Optional[Any] = None,
    workspace_service: Optional[Any] = None,
) -> SmartResponseAgent:
    """A real Smart over `llm`; see the module docstring for what each None means."""
    ctx = execution_context if execution_context is not None else _execution_context(llm, "smart")
    return SmartResponseAgent(
        config=AgentConfig(agent_id=f"smart_response_agent_{user_id}", agent_type="smart_response",
                           llm_model=model_name or ctx.model_name, timeout_ms=60000, metadata={"user_id": user_id}),
        execution_context=ctx,
        session_store=session_store if session_store is not None else EmptySessions(),
        prompt_builder=prompt_builder if prompt_builder is not None else StubPromptBuilder(),
        resolver=resolver if resolver is not None else _resolver(),
        user_config=user_config if user_config is not None else UserBotConfig(),
        repository=repository,
        embedding_service=embedding_service,
        coordinator=coordinator if coordinator is not None else AgentCoordinator(),
        model_name=model_name,
        history_recent_full_turns=history_recent_full_turns,
        history_summary_service=history_summary_service,
        user_timezone=user_timezone,
        thinking_effort=thinking_effort,
        skill_service=skill_service,
        long_turn_registry=long_turn_registry,
        memory_fact_service=memory_fact_service,
        workspace_service=workspace_service,
    )


def smart_message(text: str, *, interactive: bool = True, session_id: str = "u1:C1", user_id: str = "u1",
                  account_id: str = "a1", **context: Any) -> AgentMessage:
    """The message a chat turn delivers to Smart; `**context` is merged into its context."""
    ctx: Dict[str, Any] = {"session_id": session_id, "user_id": user_id, "account_id": account_id,
                           "current_message_parts": [MessagePart(text=text)]}
    if interactive:
        ctx["interactive_delivery"] = True
    ctx.update(context)
    return AgentMessage.create(sender="router", recipient=f"smart_response_agent_{user_id}",
                               intent=AgentIntent.QUERY, payload={"text": text}, context=ctx)
