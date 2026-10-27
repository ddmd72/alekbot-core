"""The consolidation agent's typed write port is its own constructor dependency (Facts S4 T6b, T3c re-review L6).

An agent built without it is refused at construction; the run's tracker forwards typed updates to it; the production
graph wires it to the same `FactManagementAdapter` that serves the model's writes."""

from unittest.mock import AsyncMock, MagicMock

import pytest

from src.adapters.fact_management_adapter import FactManagementAdapter
from src.adapters.in_memory_provider_resilience import InMemoryProviderResilience
from src.agents.consolidation_agent import ConsolidationAgent, _TrackingFactUpdates
from src.domain.agent import AgentConfig
from src.domain.fact_update import ParsedFactUpdate
from src.domain.fact_update_outcome import FactUpdateOutcome
from src.domain.request_context import RequestContext
from src.domain.user import PerformanceTier
from src.ports.fact_update_port import FactUpdatePort
from src.ports.llm_port import AgentExecutionContext, LLMPort, ProviderCapabilities
from tests.support.fact_write import ctx
from tests.support.prod_graph import USER_ID, ProdGraph, prod_graph  # noqa: F401  (the fixture)


class _Typed(FactUpdatePort):
    """A typed write path that records what reached it and answers as a written update."""

    def __init__(self) -> None:
        self.calls = []

    async def apply_fact_update(self, fact_id, update, account_id, *, context, evidence_at=None, reason=None, history=None, expected_id=None):
        self.calls.append((fact_id, update, account_id, evidence_at, reason, history))
        # Reviewer edit (S4 T8e): a typed outcome (T3c review M2), not the model's dict: a new version written.
        return FactUpdateOutcome(fact_id=fact_id, written_id=f"{fact_id}-v2", version=2, current_id=fact_id)


def _kwargs(**over):
    provider = MagicMock(spec=LLMPort)
    base = dict(
        config=AgentConfig(agent_id="consolidation_agent", agent_type="consolidation"),
        execution_context=AgentExecutionContext(
            agent_type="consolidation", provider=provider, model_name="m", tier=PerformanceTier.PERFORMANCE,
            capabilities=ProviderCapabilities(), resilience_port=InMemoryProviderResilience(),
        ),
        repository=MagicMock(),
        embedding_service=MagicMock(),
        fact_write_service=MagicMock(),
        fact_management_port=MagicMock(),
    )
    base.update(over)
    return base


def test_an_agent_built_without_the_typed_port_is_refused():
    with pytest.raises(TypeError):
        ConsolidationAgent(**_kwargs())
    with pytest.raises(TypeError):
        ConsolidationAgent(**_kwargs(fact_updates=None))


def test_an_agent_built_with_the_typed_port_holds_it():
    typed = _Typed()
    agent = ConsolidationAgent(**_kwargs(fact_updates=typed))
    assert agent._fact_updates is typed


async def test_the_tracker_sends_a_typed_update_to_the_typed_port_and_records_it():
    model, typed = MagicMock(), _Typed()
    tracker = _TrackingFactUpdates(model, typed)

    result = await tracker.apply_fact_update(
        "f1", ParsedFactUpdate(content="new text"), "acc", context=ctx(), reason="because", history="it ended",
    )

    assert result.written and typed.calls[0][0] == "f1" and typed.calls[0][4:] == ("because", "it ended")
    assert tracker.wrote and tracker.changed == [("f1-v2", "new text")]
    assert not model.method_calls


async def test_every_stage_of_a_run_gets_one_tracker_over_the_agents_two_ports():
    model, typed = MagicMock(), _Typed()
    agent = ConsolidationAgent(**_kwargs(fact_management_port=model, fact_updates=typed))
    agent.prompt_builder = MagicMock()
    agent.prompt_builder.build_for_agent = AsyncMock(return_value="SYS")
    agent._repo.get_active_facts_ordered = AsyncMock(return_value=[MagicMock(id="d1", text="rule")])
    seen = []

    async def spy_loop(**kw):
        seen.append(kw["fact_management"])
        return []

    agent._run_consolidation_loop = spy_loop
    with RequestContext(user_id="u", account_id="acc"):
        await agent._execute_deliberate_process_v3([{"role": "user", "text": "hi"}], "u", "acc", [])

    assert seen and all(t is seen[0] for t in seen)
    assert isinstance(seen[0], _TrackingFactUpdates) and seen[0]._real is model and seen[0]._typed is typed


@pytest.mark.slow
async def test_the_production_graph_builds_the_agent_with_the_adapter_as_both_ports(prod_graph: ProdGraph):
    agent = prod_graph.app.coordinator.get_agent(f"consolidation_agent_{USER_ID}")

    assert agent is not None
    assert isinstance(agent._fact_updates, FactManagementAdapter)
    assert agent._fact_updates is agent._fact_management
