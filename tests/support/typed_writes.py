"""The consolidation agent's typed write port (`fact_updates`, Facts S4 T6b) for agent tests.

Production passes the one `FactManagementAdapter` as both the model's port and the typed port. A test port that has the
typed path is passed the same way; a model-only one gets `NoTypedWrites`, whose typed update fails loudly, as the run's
tracker over a model-only port did before the typed port became the agent's own dependency."""
from typing import Any

from src.ports.fact_update_port import FactUpdatePort

__all__ = ["NoTypedWrites", "build_consolidation_agent", "typed_port_for"]


class NoTypedWrites(FactUpdatePort):
    """A typed write path for a run that makes no typed write: one would be a test bug, so it raises."""

    async def apply_fact_update(self, fact_id, update, account_id, *, context, evidence_at=None, reason=None,
                                history=None, expected_id=None):
        raise AssertionError(f"no typed update expected in this test (fact {fact_id})")


def typed_port_for(port: Any) -> FactUpdatePort:
    """The typed port production would pass beside `port`: `port` itself when it has the typed path, else
    `NoTypedWrites`."""
    return port if isinstance(port, FactUpdatePort) else NoTypedWrites()


def build_consolidation_agent(*, fact_updates: Any, fact_management_port: Any = None) -> Any:
    """A `ConsolidationAgent` with stub collaborators, built with `fact_updates` as its typed port (to check what its
    constructor accepts)."""
    from unittest.mock import MagicMock

    from src.adapters.in_memory_provider_resilience import InMemoryProviderResilience
    from src.agents.consolidation_agent import ConsolidationAgent
    from src.domain.agent import AgentConfig
    from src.domain.user import PerformanceTier
    from src.ports.llm_port import AgentExecutionContext, LLMPort, ProviderCapabilities

    return ConsolidationAgent(
        config=AgentConfig(agent_id="consolidation_agent", agent_type="consolidation"),
        execution_context=AgentExecutionContext(
            agent_type="consolidation", provider=MagicMock(spec=LLMPort), model_name="m",
            tier=PerformanceTier.PERFORMANCE, capabilities=ProviderCapabilities(),
            resilience_port=InMemoryProviderResilience(),
        ),
        repository=MagicMock(), embedding_service=MagicMock(), fact_write_service=MagicMock(),
        fact_management_port=fact_management_port if fact_management_port is not None else MagicMock(),
        fact_updates=fact_updates,
    )
