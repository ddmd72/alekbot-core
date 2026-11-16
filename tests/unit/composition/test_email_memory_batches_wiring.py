"""Stage 3 always reads mail with its card (email index v2 §6.7; the EMAIL_CARD_MEMORY switch retired by slice 9b):
under the deploy's environment, which sets no such switch, the consolidation agent's batch port is the graph's own
indexed-email repository and its people list the graph's own."""
import pytest

from tests.support import prod_env
from tests.support.prod_graph import USER_ID, ProdGraph, prod_graph  # noqa: F401  (the fixture)


@pytest.mark.slow
async def test_the_agents_batches_are_the_graphs_own_repository_with_no_switch(prod_graph: ProdGraph):
    assert not any("EMAIL_CARD_MEMORY" in key for key in prod_env.deploy_env())
    agent = prod_graph.app.coordinator.get_agent(f"consolidation_agent_{USER_ID}")
    factory = prod_graph.app.agent_factory

    assert agent._email_memory_batches is factory.indexed_email_repo
    assert agent._email_memory_batches is agent._indexed_email_repo
    assert agent._confirmed_people is factory.confirmed_people and agent._confirmed_people is not None
