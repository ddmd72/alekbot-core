"""
Integration test for SmartResponseAgent concurrent execution.

Wall-clock proof that ``execute()`` no longer serializes under
``_execute_lock``: two concurrent ``process()`` calls on the SAME
SmartResponseAgent instance with DIFFERENT ``ExecutionOverride`` complete
in parallel, each using its own provider/model/thinking effort, with no
state leak between them.

This is the regression test for the lock removal in Step #2 of
NOTIFICATION_DELIVERY_REFACTOR_RFC. Before the refactor, two concurrent
notify() calls (e.g. email_review + reminder fire) would queue on
``self._execute_lock`` and the waiter would burn its 300s ``wait_for``
budget while blocked — producing the cascading instant-timeout pattern
observed in production on 2026-04-30.

Per:
  docs/10_rfcs/NOTIFICATION_DELIVERY_REFACTOR_RFC.md § 4 / § 8.2
"""

from __future__ import annotations

import asyncio
import time
from typing import Any, List
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.agents.core.smart_response_agent import SmartResponseAgent
from src.domain.agent import (
    AgentConfig,
    AgentIntent,
    AgentMessage,
    AgentStatus,
)
from src.domain.user import PerformanceTier, UserBotConfig
from src.infrastructure.task_execution_resolver import (
    ExecutionOverride,
    TaskExecutionResolver,
)
from src.ports.llm_port import (
    AgentExecutionContext,
    LLMPort,
    LLMRequest,
    LLMResponse,
    Message,
    MessagePart,
    ProviderCapabilities,
    UsageMetadata,
)


# Time each LLM call takes (in the mock). Two concurrent calls on the SAME
# agent should complete in ~CALL_DURATION (parallel) rather than
# ~2 × CALL_DURATION (serialized by lock).
CALL_DURATION = 0.5


def _make_provider(model_label: str) -> MagicMock:
    """Mock LLMPort that records its model_label and sleeps CALL_DURATION."""
    captured: List[LLMRequest] = []

    async def generate_content(request: LLMRequest) -> LLMResponse:
        captured.append(request)
        # Wall-clock work simulation. Two concurrent invocations on the
        # SAME instance must overlap — proven by the test's wall-clock.
        await asyncio.sleep(CALL_DURATION)
        # Build a terminal-tool LLM response so DelegationEngine exits cleanly.
        return LLMResponse(
            text="",
            tool_calls=[],
            usage_metadata=UsageMetadata(
                prompt_tokens=10, completion_tokens=10, total_tokens=20,
            ),
            raw_content=[
                {
                    "type": "tool_use",
                    "name": "deliver_response",
                    "input": {
                        "full_response": f"reply from {model_label}",
                        "response_summary": f"summary {model_label}",
                        "rich_content": None,
                    },
                }
            ],
        )

    provider = MagicMock(spec=LLMPort)
    provider.generate_content = AsyncMock(side_effect=generate_content)
    provider._captured = captured  # for inspection
    provider._model_label = model_label
    return provider


def _make_ctx(model_label: str) -> AgentExecutionContext:
    return AgentExecutionContext(
        agent_type="smart",
        provider=_make_provider(model_label),
        model_name=f"model-{model_label}",
        tier=PerformanceTier.BALANCED,
        capabilities=ProviderCapabilities(),
        provider_name=f"provider-{model_label}",
    )


def _make_terminal_tool_response(model_label: str) -> LLMResponse:
    """Single-turn terminal-tool response — DelegationEngine returns immediately."""
    return LLMResponse(
        text="",
        tool_calls=[],
        usage_metadata=UsageMetadata(
            prompt_tokens=10, completion_tokens=10, total_tokens=20,
        ),
        raw_content=[
            {
                "type": "tool_use",
                "name": "deliver_response",
                "input": {
                    "full_response": f"reply from {model_label}",
                    "response_summary": f"summary {model_label}",
                    "rich_content": None,
                },
            }
        ],
    )


@pytest.fixture
def smart_agent_with_concurrent_providers():
    """Build a Smart agent whose default provider records calls and sleeps."""
    default_ctx = _make_ctx("DEFAULT")

    config = AgentConfig(
        agent_id="smart_response_agent_user-x",
        agent_type="smart_response",
        llm_model="model-DEFAULT",
        timeout_ms=300_000,
        capabilities=["complex_reasoning"],
        metadata={"user_id": "user-x"},
    )

    resolver = MagicMock(spec=TaskExecutionResolver)
    resolver.resolve.return_value = None

    session_store = MagicMock()
    session_store.load_session = AsyncMock(return_value=None)

    prompt_builder = MagicMock()
    prompt_builder.build_for_agent = AsyncMock(return_value="SYSTEM_PROMPT")
    prompt_builder.merge_enriched_context_with_biographical = MagicMock(return_value=[])

    coordinator = MagicMock()
    coordinator.route_message = AsyncMock()
    coordinator.get_available_intents_for = MagicMock(return_value=[])

    return SmartResponseAgent(
        config=config,
        execution_context=default_ctx,
        session_store=session_store,
        prompt_builder=prompt_builder,
        resolver=resolver,
        user_config=UserBotConfig(),
        coordinator=coordinator,
    )


def _make_message(
    *,
    task_id: str,
    execution_override: ExecutionOverride,
) -> AgentMessage:
    """Build a Smart-bound AgentMessage carrying an explicit override."""
    msg = AgentMessage.create(
        sender="notification_service",
        recipient="smart_response_agent_user-x",
        intent=AgentIntent.QUERY,
        payload={"text": "hi"},
        context={
            "session_id": f"session-{task_id}",
            "user_id": "user-x",
            "account_id": "acc-x",
            "current_message_parts": [MessagePart(text="hi")],
            "execution_override": execution_override,
        },
    )
    msg.task_id = task_id
    return msg


# --------------------------------------------------------------------------- #
# The proof                                                                   #
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_concurrent_execute_runs_in_parallel_with_per_call_overrides(
    smart_agent_with_concurrent_providers,
):
    """Two concurrent ``process()`` calls on the SAME instance, with
    different ``ExecutionOverride``, must:

      1. Run in parallel — total wall clock < 2 × CALL_DURATION (no lock).
      2. Each route to its own provider (different LLMPort instances).
      3. Each carry its own model name in the LLMRequest.
      4. Not interfere with each other's state — no leakage of provider /
         model between concurrent calls.
    """
    smart = smart_agent_with_concurrent_providers

    # Two distinct override contexts → distinct providers → distinct models.
    ctx_a = _make_ctx("A")
    ctx_b = _make_ctx("B")
    override_a = ExecutionOverride(execution_context=ctx_a, thinking_effort="low")
    override_b = ExecutionOverride(execution_context=ctx_b, thinking_effort="high")

    msg_a = _make_message(task_id="task-A", execution_override=override_a)
    msg_b = _make_message(task_id="task-B", execution_override=override_b)

    # Capture the default provider's reference BEFORE running. Concurrent
    # runs must NOT mutate self.llm.
    default_provider_before = smart.llm
    default_model_before = smart.model_name

    # Wall-clock measurement: with lock removed, both calls overlap.
    # Generous upper bound: 1.5 × CALL_DURATION leaves room for asyncio
    # scheduling jitter while still proving parallelism (serial would be
    # 2 × CALL_DURATION).
    start = time.perf_counter()
    response_a, response_b = await asyncio.gather(
        smart.process(msg_a),
        smart.process(msg_b),
    )
    elapsed = time.perf_counter() - start

    # 1. Parallelism proven by wall clock.
    assert elapsed < 1.5 * CALL_DURATION, (
        f"Concurrent process() took {elapsed:.2f}s — expected < "
        f"{1.5 * CALL_DURATION:.2f}s. The _execute_lock has likely been "
        f"reintroduced; concurrent calls are serializing."
    )

    # 2. Both succeeded.
    assert response_a.status == AgentStatus.SUCCESS
    assert response_b.status == AgentStatus.SUCCESS

    # 3. Each call routed to its OWN provider.
    provider_a = ctx_a.provider
    provider_b = ctx_b.provider
    assert provider_a.generate_content.await_count == 1
    assert provider_b.generate_content.await_count == 1
    # The default provider must NOT have been called — overrides took over.
    assert smart.llm.generate_content.await_count == 0

    # 4. Each LLMRequest carried the correct model_name.
    captured_a = provider_a._captured
    captured_b = provider_b._captured
    assert len(captured_a) == 1 and captured_a[0].model_name == "model-A"
    assert len(captured_b) == 1 and captured_b[0].model_name == "model-B"

    # 5. NO MUTATION of self.llm / self.model_name during concurrent runs.
    assert smart.llm is default_provider_before
    assert smart.model_name == default_model_before

    # 6. Each call's metadata reports the correct (per-call) model.
    assert response_a.metadata["model"] == "model-A"
    assert response_b.metadata["model"] == "model-B"
