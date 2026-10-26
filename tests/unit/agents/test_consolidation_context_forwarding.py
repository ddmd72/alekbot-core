"""Facts S3 · T4a: the consolidator hands the write context of its pass to every write and its zone to every search.

The fake port records what it was given. Real contexts per pass arrive in T4b; here the fixed ones are checked: which
passes let the model's dates count, that the tool executor, the tracker and the directive backstop forward them.
"""
from typing import Any, Dict, List, Tuple
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.agents.consolidation_agent import ConsolidationAgent, _TrackingFactManagement
from src.domain.agent import AgentConfig
from src.domain.entities import FactDomain
from src.domain.llm import LLMResponse, ToolCall
from src.domain.user import PerformanceTier
from src.ports.fact_management_port import FactManagementPort
from src.ports.llm_port import AgentExecutionContext, LLMPort, ProviderCapabilities
from src.adapters.in_memory_provider_resilience import InMemoryProviderResilience
from tests.support.fact_kit import ACC, USER, make_fact
from tests.support.typed_writes import typed_port_for


class RecordingPort(FactManagementPort):
    def __init__(self) -> None:
        self.calls: List[Tuple[str, Any]] = []

    async def search_existing_facts(self, keywords, primary_query, alternative_query="", limit=20, *, timezone):
        self.calls.append(("search", timezone))
        return []

    async def create_fact(self, content, metadata, *, context):
        self.calls.append(("create", context))
        return {"fact_id": "new", "status": "created", "message": "ok"}

    async def update_fact(self, fact_id, updates, account_id, *, context):
        self.calls.append(("update", context))
        return {"fact_id": fact_id, "status": "updated", "message": "ok"}

    async def merge_facts(self, fact_ids, merged_content, metadata, *, context):
        self.calls.append(("merge", context))
        return {"new_fact_id": "m", "old_fact_ids": fact_ids, "status": "merged", "message": "ok"}


def make_agent(port=None):
    execution_context = AgentExecutionContext(
        agent_type="consolidation", provider=MagicMock(spec=LLMPort), model_name="gemini-pro",
        tier=PerformanceTier.PERFORMANCE, capabilities=ProviderCapabilities(),
        resilience_port=InMemoryProviderResilience(),
    )
    repo = MagicMock()
    repo.get_biographical_context_cached = AsyncMock(return_value=[])
    repo.refresh_biographical_context_cache = AsyncMock()
    return ConsolidationAgent(
        config=AgentConfig(agent_id="consolidation_agent", agent_type="consolidation"),
        execution_context=execution_context, repository=repo, embedding_service=MagicMock(),
        fact_write_service=MagicMock(), fact_management_port=port, prompt_builder=MagicMock(),
        fact_updates=typed_port_for(port),  # reviewer edit (S4 T6b): the typed port is required
    ), repo


@pytest.mark.parametrize("label,model_dates", [
    ("stage1", True), ("email", True), ("stage2a", False), ("stage2b", False), ("backstop", False), ("cluster", False),
])
def test_only_the_conversation_passes_let_the_models_dates_count(label, model_dates):
    context = ConsolidationAgent._write_context(label)
    assert (context.actor, context.model_dates, context.timezone, context.span) == (label, model_dates, "UTC", None)


async def test_the_tool_executor_gives_every_write_and_search_the_passes_context():
    port = RecordingPort()
    agent, _ = make_agent(port)
    context = agent._write_context("stage2a", "Europe/Lisbon")
    calls = [
        ToolCall(name="search_existing_facts", args={"keywords": [], "primary_query": "q"}),
        ToolCall(name="create_fact", args={"content": "c", "fact_attributes": {}}),
        ToolCall(name="update_fact", args={"fact_id": "f", "updates": {}}),
        ToolCall(name="merge_facts", args={"fact_ids": ["a", "b"], "merged_content": "m", "fact_attributes": {}}),
    ]
    await agent._execute_fact_management_tools(calls, USER, ACC, write_context=context)
    assert port.calls == [("search", "Europe/Lisbon"), ("create", context), ("update", context), ("merge", context)]


async def test_the_tracker_forwards_the_context_and_the_zone():
    real = RecordingPort()
    tracker = _TrackingFactManagement(real)
    context = ConsolidationAgent._write_context("email", "Europe/Lisbon")
    await tracker.search_existing_facts(["k"], "q", timezone="Europe/Lisbon")
    await tracker.create_fact("c", {}, context=context)
    await tracker.update_fact("f", {"content": "x"}, ACC, context=context)
    await tracker.merge_facts(["a"], "m", {}, context=context)
    assert real.calls == [("search", "Europe/Lisbon"), ("create", context), ("update", context), ("merge", context)]
    assert tracker.wrote and tracker.changed == [("new", "c"), ("f", "x"), ("m", "m")]


async def test_the_directive_backstop_writes_under_its_own_actor_and_the_passes_zone_and_span():
    real = RecordingPort()
    agent, repo = make_agent(real)
    directives = [make_fact(f"rule {i}", domain=FactDomain.AGENT_DIRECTIVE) for i in range(agent.DIRECTIVE_HARD_CAP + 2)]
    repo.get_active_facts_ordered = AsyncMock(return_value=directives)
    stage2b = agent._write_context("stage2b", "Europe/Lisbon")
    ops = await agent._enforce_directive_cap(ACC, agent._write_context("backstop", stage2b.timezone, stage2b.span))
    assert len(ops) == 2
    assert [(name, c.actor, c.timezone, c.model_dates) for name, c in real.calls] == [
        ("update", "backstop", "Europe/Lisbon", False)] * 2


async def test_stage_2b_derives_the_backstop_context_from_its_own():
    real = RecordingPort()
    agent, repo = make_agent(real)
    directives = [make_fact(f"rule {i}", domain=FactDomain.AGENT_DIRECTIVE) for i in range(agent.DIRECTIVE_HARD_CAP + 1)]
    repo.get_active_facts_ordered = AsyncMock(return_value=directives)
    agent.prompt_builder.build_for_agent = AsyncMock(return_value="SYS")
    agent._call_llm = AsyncMock(return_value=LLMResponse(text='{"operations": []}'))
    ops = await agent._review_directives(USER, ACC, [], agent._write_context("stage2b", "Europe/Lisbon"))
    assert [op["action"] for op in ops] == ["INVALIDATE"]
    assert [(name, c.actor, c.timezone) for name, c in real.calls] == [("update", "backstop", "Europe/Lisbon")]
