"""Facts S3 · T8: the production graph hands the container's lifecycle counters to the consolidation agent (R1-F6)."""
import pytest

from src.adapters.firestore_fact_lifecycle_counters import FirestoreFactLifecycleCounters
from tests.support.prod_graph import USER_ID, ProdGraph, prod_graph  # noqa: F401  (the fixture)

pytestmark = pytest.mark.slow


async def test_the_consolidation_agent_holds_the_containers_counters_by_identity(prod_graph: ProdGraph):
    app = prod_graph.app
    counters = app.container.fact_lifecycle_counters
    assert isinstance(counters, FirestoreFactLifecycleCounters)

    assert app.agent_factory.lifecycle_counters is counters
    agent = app.coordinator.get_agent(f"consolidation_agent_{USER_ID}")
    assert agent is not None and agent._lifecycle_counters is counters


def test_agent_services_carries_the_counters_for_every_script_that_unpacks_it(prod_graph: ProdGraph):
    container = prod_graph.app.container

    assert container.agent_services()["lifecycle_counters"] is container.fact_lifecycle_counters
