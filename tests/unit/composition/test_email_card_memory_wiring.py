"""Stage 3 reads email cards behind EMAIL_CARD_MEMORY (email index v2 §6.7, slice 9): under the deploy's environment the
consolidation agent's batch port is the graph's own indexed-email repository and its people list the graph's own; with
the switch unset or off the agent has no batch port and Stage 3 is the v1 one-line form. The switch is read in code with
default off."""
import pytest

from src.composition.email_card_setup import MEMORY_SWITCH, email_card_memory_enabled
from tests.support import prod_env
from tests.support.prod_graph import USER_ID, ProdGraph, prod_graph, seed  # noqa: F401  (the fixture)


def _agent(app):
    return app.coordinator.get_agent(f"consolidation_agent_{USER_ID}")


@pytest.mark.slow
async def test_the_deploy_environment_switches_it_on_with_the_graphs_own_repositories(prod_graph: ProdGraph):
    assert prod_env.deploy_env()[MEMORY_SWITCH] == "1"
    agent = _agent(prod_graph.app)
    factory = prod_graph.app.agent_factory

    assert agent._email_memory_batches is factory.indexed_email_repo
    assert agent._email_memory_batches is agent._indexed_email_repo
    assert agent._confirmed_people is factory.confirmed_people and agent._confirmed_people is not None


@pytest.mark.slow
@pytest.mark.parametrize("value", [None, "0", "off", ""])
async def test_unset_or_off_gives_the_agent_no_batch_port(monkeypatch, value):
    from src.composition.application_graph import build_application_graph
    from src.config.settings import load_settings

    offline = prod_env.install_offline_services(monkeypatch)
    if value is None:
        monkeypatch.delenv(MEMORY_SWITCH, raising=False)
    else:
        monkeypatch.setenv(MEMORY_SWITCH, value)
    app = build_application_graph(load_settings(), offline.db)
    await app.start()
    try:
        await seed(app)
        await app.agent_factory.ensure_agents_for_user(USER_ID)
        agent = _agent(app)
        assert agent._email_memory_batches is None
        assert agent._indexed_email_repo is app.agent_factory.indexed_email_repo
    finally:
        await app.shutdown()


def test_the_switch_reads_the_environment_at_call_time_with_default_off(monkeypatch):
    monkeypatch.delenv(MEMORY_SWITCH, raising=False)
    assert email_card_memory_enabled() is False
    for on in ("1", "true", "on", " YES "):
        monkeypatch.setenv(MEMORY_SWITCH, on)
        assert email_card_memory_enabled() is True
    monkeypatch.setenv(MEMORY_SWITCH, "0")
    assert email_card_memory_enabled() is False
