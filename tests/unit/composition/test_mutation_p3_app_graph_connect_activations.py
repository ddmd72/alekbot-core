"""The connect activations the production graph hands to the connect service (MULTI_DRIVE_RFC §4.3, §5.2) hold the
collaborators `build_application_graph` names: the reconnect service, the To Do connector, the agent task queue and
the drive activation. Each activation is run and its effect on that collaborator is observed."""
from typing import Any, List, Tuple

import pytest

from src.composition.connector_registry import GOOGLE_DRIVE, GMAIL, MICROSOFT_TODO
from src.domain.exceptions import ConnectActivationFailed
from src.domain.integration import Integration
from tests.support.oauth_credentials_store import creds
from tests.support.prod_graph import USER_ID, ProdGraph, prod_graph  # noqa: F401  (the fixture)

pytestmark = pytest.mark.slow


def _activations(graph: ProdGraph) -> dict:
    return graph.app.connect_service._activations


def _spy_consented(graph: ProdGraph, monkeypatch) -> List[Tuple[str, Integration]]:
    seen: List[Tuple[str, Integration]] = []

    async def consented(user_id: str, integration: Integration) -> None:
        seen.append((user_id, integration))

    monkeypatch.setattr(graph.app.reconnect_service, "consented", consented)
    return seen


async def test_the_mail_activation_clears_the_state_through_the_graphs_reconnect_service(prod_graph, monkeypatch):  # noqa: F811
    seen = _spy_consented(prod_graph, monkeypatch)

    await _activations(prod_graph)[Integration.MAIL](creds(USER_ID, GMAIL))

    assert seen == [(USER_ID, Integration.MAIL)]


async def test_the_tasks_activation_compares_with_the_to_do_connector_and_clears_the_state(prod_graph, monkeypatch):  # noqa: F811
    seen = _spy_consented(prod_graph, monkeypatch)
    enqueued: List[Any] = []

    async def enqueue(*args: Any, **kwargs: Any) -> None:
        enqueued.append(args)

    monkeypatch.setattr(prod_graph.app.agent_task_queue, "enqueue_worker_task", enqueue)

    await _activations(prod_graph)[Integration.TASKS](creds(USER_ID, MICROSOFT_TODO))

    assert seen == [(USER_ID, Integration.TASKS)]
    # the To Do setup goes to the graph's agent task queue
    assert enqueued == [("setup_microsoft_todo", {"user_id": USER_ID})]


async def test_the_drive_activation_is_the_graphs_drive_activation(prod_graph):  # noqa: F811
    assert prod_graph.app.container.drive_connection_service is not None
    # a graph drive activation reads the grant's identity through the offline drive, which cannot: the connect fails.
    # With no activation wired the consent would only be stored (logged) and nothing would raise.
    with pytest.raises(ConnectActivationFailed):
        await _activations(prod_graph)[Integration.DRIVE](creds(USER_ID, GOOGLE_DRIVE, credential_id="cred-1"))
