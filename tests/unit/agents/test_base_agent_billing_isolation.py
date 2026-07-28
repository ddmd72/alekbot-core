"""
Billing isolation between concurrent executions of the SAME agent instance.

Regression suite for the token over-count found 2026-07-28: an agent instance is a
per-user singleton registered in the AgentCoordinator, and DelegationEngine dispatches
a tool batch via ``asyncio.gather`` — so N executions of ONE instance run concurrently.
When the token accumulators lived on ``self``, every concurrent execution reset and
incremented the same four fields, and each ``_flush_billing()`` billed whatever the
running total happened to be. A 22-way ``fetch_url`` batch inflated the daily counter
~3.6x (Firestore reported $9.69 for a day that really cost $3.75).

The fix scopes the accumulators to the execution via a ContextVar-held TokenLedger.
These tests pin the observable contract: one execution bills exactly its own tokens,
regardless of how many siblings share the instance.
"""

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.agents.base_agent import BaseAgent
from src.domain.agent import AgentConfig, AgentIntent, AgentMessage, AgentResponse
from src.ports.llm_port import LLMRequest, LLMResponse, UsageMetadata

# One LLM call per execution, fixed usage → trivially checkable arithmetic.
PROMPT_TOKENS = 10
COMPLETION_TOKENS = 5
CACHE_READ_TOKENS = 4
CACHE_CREATION_TOKENS = 2
TOKENS_PER_EXECUTION = (
    PROMPT_TOKENS + COMPLETION_TOKENS + CACHE_READ_TOKENS + CACHE_CREATION_TOKENS
)


class _BillingAgent(BaseAgent):
    """Minimal agent: one _call_llm per execute(), optional nested delegation."""

    def __init__(self, config, llm_calls: int = 1):
        super().__init__(config)
        self._llm_calls = llm_calls
        self.nested: BaseAgent | None = None

    async def can_handle(self, message: AgentMessage) -> bool:
        return True

    async def execute(self, message: AgentMessage) -> AgentResponse:
        await self._call_llm(LLMRequest(model_name="test", messages=[]), turn=1)
        if self.nested is not None:
            # Direct await, NOT wrapped in a Task — the case where a naive
            # ContextVar.set() in the inner process() would clobber ours.
            await self.nested.process(_message())
        for turn in range(2, self._llm_calls + 1):
            await self._call_llm(LLMRequest(model_name="test", messages=[]), turn=turn)
        return AgentResponse.success(
            task_id=message.task_id, agent_id=self.agent_id, result="ok"
        )


def _message(account_id: str = "acc-1") -> AgentMessage:
    return AgentMessage.create(
        sender="test",
        recipient="agent",
        intent=AgentIntent.QUERY,
        payload={"query": "q"},
        context={"account_id": account_id},
    )


def _slow_llm() -> MagicMock:
    """LLM whose call yields to the loop, forcing concurrent executions to interleave."""

    async def _generate(request):
        await asyncio.sleep(0.01)
        return LLMResponse(
            text="answer",
            usage_metadata=UsageMetadata(
                prompt_tokens=PROMPT_TOKENS,
                completion_tokens=COMPLETION_TOKENS,
                total_tokens=PROMPT_TOKENS + COMPLETION_TOKENS,
                cache_read_tokens=CACHE_READ_TOKENS,
                cache_creation_tokens=CACHE_CREATION_TOKENS,
            ),
        )

    llm = MagicMock()
    llm.generate_content = AsyncMock(side_effect=_generate)
    return llm


def _make_agent(agent_id: str = "billing_agent", llm_calls: int = 1) -> _BillingAgent:
    agent = _BillingAgent(
        AgentConfig(agent_id=agent_id, agent_type="mock", llm_model="claude-sonnet-4-6"),
        llm_calls=llm_calls,
    )
    agent.llm = _slow_llm()
    agent._quota_service = AsyncMock()
    return agent


class TestConcurrentExecutionsBillIndependently:

    @pytest.mark.asyncio
    async def test_ten_concurrent_executions_each_bill_own_tokens(self):
        """The bug: shared accumulators made each flush bill the running total."""
        agent = _make_agent()

        await asyncio.gather(*[agent.process(_message()) for _ in range(10)])

        calls = agent._quota_service.record_usage.await_args_list
        assert len(calls) == 10
        billed = [c.kwargs["tokens"] for c in calls]
        assert billed == [TOKENS_PER_EXECUTION] * 10, (
            f"each execution must bill only its own {TOKENS_PER_EXECUTION} tokens, got {billed}"
        )

    @pytest.mark.asyncio
    async def test_concurrent_total_equals_true_sum(self):
        """Aggregate check — this is the number that reached Firestore."""
        agent = _make_agent()

        await asyncio.gather(*[agent.process(_message()) for _ in range(22)])

        total = sum(
            c.kwargs["tokens"] for c in agent._quota_service.record_usage.await_args_list
        )
        assert total == 22 * TOKENS_PER_EXECUTION

    @pytest.mark.asyncio
    async def test_multi_turn_execution_bills_all_its_turns(self):
        """Scoping must not break the normal case: one execution, several LLM turns."""
        agent = _make_agent(llm_calls=3)

        await agent.process(_message())

        agent._quota_service.record_usage.assert_awaited_once()
        kw = agent._quota_service.record_usage.await_args.kwargs
        assert kw["tokens"] == 3 * TOKENS_PER_EXECUTION

    @pytest.mark.asyncio
    async def test_concurrent_multi_turn_executions_stay_separate(self):
        """Interleaved multi-turn executions must not pool their turns."""
        agent = _make_agent(llm_calls=2)

        await asyncio.gather(*[agent.process(_message()) for _ in range(5)])

        billed = [
            c.kwargs["tokens"]
            for c in agent._quota_service.record_usage.await_args_list
        ]
        assert billed == [2 * TOKENS_PER_EXECUTION] * 5


class TestNestedExecutionDoesNotLeak:

    @pytest.mark.asyncio
    async def test_inner_agent_awaited_directly_does_not_steal_outer_tokens(self):
        """Specialist awaited inline from an orchestrator: each bills its own tokens.

        Without ``ContextVar.reset(token)`` the inner process() would leave its own
        ledger current, so the outer agent's post-delegation turn — and its flush —
        would land on the specialist's ledger.
        """
        outer = _make_agent(agent_id="outer", llm_calls=2)
        inner = _make_agent(agent_id="inner")
        outer.nested = inner

        await outer.process(_message())

        outer._quota_service.record_usage.assert_awaited_once()
        assert (
            outer._quota_service.record_usage.await_args.kwargs["tokens"]
            == 2 * TOKENS_PER_EXECUTION
        )
        inner._quota_service.record_usage.assert_awaited_once()
        assert (
            inner._quota_service.record_usage.await_args.kwargs["tokens"]
            == TOKENS_PER_EXECUTION
        )
