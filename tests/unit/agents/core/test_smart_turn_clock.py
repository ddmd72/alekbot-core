"""Smart under a TurnClock: per-call timeout, max turns, snapshot time, no rotation after the mark."""
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.adapters.in_memory_provider_resilience import InMemoryProviderResilience
from src.agents.core.smart_response_agent import SmartResponseAgent
from src.domain.agent import AgentConfig, AgentIntent, AgentMessage
from src.domain.exceptions import TranscriptLockedError
from src.domain.long_turn import LongTurnRecord
from src.domain.turn_clock import CURRENT_TURN_CLOCK, TurnClock
from src.domain.user import PerformanceTier, UserBotConfig
from src.infrastructure.delegation_engine import DelegationEngine, DelegationResult
from src.infrastructure.task_execution_resolver import TaskExecutionResolver
from src.ports.llm_port import (
    AgentExecutionContext,
    LLMPort,
    LLMResponse,
    ProviderCapabilities,
    UsageMetadata,
)


# --------------------------------------------------------------------------- #
# Construction helpers — copied from tests/unit/agents/core/test_smart_per_call_execution.py
# --------------------------------------------------------------------------- #


def _make_ctx(
    *,
    model_name: str = "default-model",
    tier: PerformanceTier = PerformanceTier.BALANCED,
) -> AgentExecutionContext:
    return AgentExecutionContext(
        agent_type="smart",
        provider=MagicMock(spec=LLMPort),
        model_name=model_name,
        tier=tier,
        capabilities=ProviderCapabilities(),
        provider_name="stub",
        resilience_port=InMemoryProviderResilience(),
    )


def _make_smart(ctx: AgentExecutionContext) -> SmartResponseAgent:
    config = AgentConfig(
        agent_id="smart_response_agent",
        agent_type="smart_response",
        llm_model="default-model",
        timeout_ms=300_000,
        capabilities=["complex_reasoning"],
        metadata={"user_id": "user-123"},
    )

    resolver = MagicMock(spec=TaskExecutionResolver)
    resolver.resolve.return_value = None

    session_store = MagicMock()
    session_store.load_session = AsyncMock(return_value=None)

    prompt_builder = MagicMock()
    prompt_builder.build_for_agent = AsyncMock(return_value="SYSTEM")

    coordinator = MagicMock()
    coordinator.route_message = AsyncMock()

    return SmartResponseAgent(
        config=config,
        execution_context=ctx,
        session_store=session_store,
        prompt_builder=prompt_builder,
        resolver=resolver,
        user_config=UserBotConfig(),
        coordinator=coordinator,
        thinking_effort="medium",
    )


def _make_message() -> AgentMessage:
    return AgentMessage.create(
        sender="router_agent",
        recipient="smart_response_agent",
        intent=AgentIntent.QUERY,
        payload={"text": "hi"},
        context={
            "session_id": "s",
            "user_id": "user-123",
            "account_id": "acc-123",
            "metadata": {"turn_id": "slack:Own1"},
        },
    )


class _SpyHandle:
    """Supports both ``agent, engine_kwargs, llm_requests = handle`` and ``handle.message``."""

    def __init__(self, agent, engine_kwargs, llm_requests, message):
        self.agent = agent
        self.engine_kwargs = engine_kwargs
        self.llm_requests = llm_requests
        self.message = message

    def __iter__(self):
        return iter((self.agent, self.engine_kwargs, self.llm_requests))


@pytest.fixture
def smart_agent_with_engine_spy():
    """Smart with ``DelegationEngine.execute`` patched to a spy.

    The spy records the kwargs ``engine.execute(...)`` is called with, calls
    ``call_llm(base_request, 1)`` once — so it actually routes through Smart's
    ``call_llm_for_engine`` closure and exercises the timeout injection — and
    returns a terminal ``DelegationResult``. The provider mock records every
    ``LLMRequest`` it receives.
    """
    ctx = _make_ctx()
    agent = _make_smart(ctx)
    message = _make_message()

    engine_kwargs: dict = {}
    llm_requests: list = []

    async def _generate_content(request):
        llm_requests.append(request)
        return LLMResponse(
            text="ok",
            tool_calls=[],
            usage_metadata=UsageMetadata(prompt_tokens=1, completion_tokens=1, total_tokens=2),
        )

    ctx.provider.generate_content = AsyncMock(side_effect=_generate_content)

    async def _execute_spy(self, *, call_llm, base_request, **kwargs):
        engine_kwargs.update(kwargs)
        engine_kwargs["base_request"] = base_request
        await call_llm(base_request, 1)
        return DelegationResult(text="ok", total_tokens=0)

    with patch.object(DelegationEngine, "execute", _execute_spy):
        yield _SpyHandle(agent, engine_kwargs, llm_requests, message)


@pytest.fixture
def smart_agent_failing_with_transcript_lock():
    """Smart whose ``_run`` always raises a terminal ``TranscriptLockedError``."""
    ctx = _make_ctx()
    agent = _make_smart(ctx)
    agent.test_message = _make_message()
    agent.resolver.next_provider_override = MagicMock()

    error = TranscriptLockedError(provider_name="openai", cause=Exception("x"), turn=2)
    agent._run = AsyncMock(side_effect=error)

    return agent


async def test_every_llm_call_gets_the_clock_timeout(smart_agent_with_engine_spy):
    agent, engine_kwargs, llm_requests = smart_agent_with_engine_spy
    clock = TurnClock.start(budget_s=600, wrap_up_reserve_s=100)
    token = CURRENT_TURN_CLOCK.set(clock)
    try:
        await agent.execute(smart_agent_with_engine_spy.message)
    finally:
        CURRENT_TURN_CLOCK.reset(token)
    assert llm_requests and all(400 <= r.timeout <= 500 for r in llm_requests)
    assert engine_kwargs["max_turns"] == clock.max_loop_turns
    assert clock.snapshot_at > 0


async def test_no_clock_leaves_timeout_unset_and_max_turns_default(smart_agent_with_engine_spy):
    agent, engine_kwargs, llm_requests = smart_agent_with_engine_spy
    await agent.execute(smart_agent_with_engine_spy.message)
    assert all(r.timeout is None for r in llm_requests)
    assert engine_kwargs["max_turns"] == agent.MAX_DELEGATION_TURNS


async def test_smart_opts_into_the_turn_clock(smart_agent_with_engine_spy):
    """Only the orchestrator that owns the clock passes use_turn_clock=True — a nested
    DelegationEngine (e.g. a specialist's own, run SYNC from Smart) must not inherit the
    ambient CURRENT_TURN_CLOCK by default (fix round 1, Task 4)."""
    agent, engine_kwargs, llm_requests = smart_agent_with_engine_spy
    await agent.execute(smart_agent_with_engine_spy.message)
    assert engine_kwargs["use_turn_clock"] is True


async def test_no_provider_rotation_after_the_mark(smart_agent_failing_with_transcript_lock):
    agent = smart_agent_failing_with_transcript_lock
    clock = TurnClock.start()
    clock.marked = True
    token = CURRENT_TURN_CLOCK.set(clock)
    try:
        response = await agent.execute(agent.test_message)
    finally:
        CURRENT_TURN_CLOCK.reset(token)
    assert response.status.value == "failed"
    agent.resolver.next_provider_override.assert_not_called()


async def test_running_jobs_note_and_cancel_tool_when_jobs_exist(smart_agent_with_engine_spy):
    agent, engine_kwargs, _ = smart_agent_with_engine_spy
    agent.long_turn_registry = AsyncMock()
    agent.long_turn_registry.list_running.return_value = [LongTurnRecord(
        turn_id="slack:Ev999999", user_id="u1", session_id="s", title="old job",
        started_at=0.0, heartbeat_at=0.0)]
    await agent.execute(smart_agent_with_engine_spy.message)
    last_user = engine_kwargs["base_request"].messages[-1]
    assert any("old job" in (p.text or "") for p in last_user.parts)
    assert "cancel_long_turn" in engine_kwargs["local_tools"]


async def test_own_turn_is_not_listed(smart_agent_with_engine_spy):
    agent, engine_kwargs, _ = smart_agent_with_engine_spy
    agent.long_turn_registry = AsyncMock()
    own = smart_agent_with_engine_spy.message.context["metadata"]["turn_id"]
    agent.long_turn_registry.list_running.return_value = [LongTurnRecord(
        turn_id=own, user_id="u1", session_id="s", title="me", started_at=0.0, heartbeat_at=0.0)]
    await agent.execute(smart_agent_with_engine_spy.message)
    assert not (engine_kwargs.get("local_tools") or {}).get("cancel_long_turn")
