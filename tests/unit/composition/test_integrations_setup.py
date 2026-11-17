"""`build_integrations`: every integration service runs over the container's one credential store and connectors, the
reconnect service and the drive activation reach the connect flow, and each optional part exists only with what it needs."""
import dataclasses
from types import SimpleNamespace

import pytest

from src.composition import integrations_setup
from src.composition.connector_registry import GMAIL, MICROSOFT_TODO, ConnectorRegistry
from src.composition.integrations_setup import build_integrations
from src.composition.oauth_connect_setup import build_oauth_edges

DB, ENV, USERS, NOTIFY = object(), object(), object(), object()
BOOT = SimpleNamespace(cipher=object(), digest_key=b"digest-1", deploy_topology=SimpleNamespace(service_url="https://svc.example.test"),
                       auth_config=SimpleNamespace(oauth_connect_redirect_uri="https://svc.example.test/connect/callback"))
LANGUAGES = SimpleNamespace(localization=object(), resolver=object())


async def ensure_agents(user_id):
    return None


def _container(*, todo=True, drive=True, drive_queue=True):
    return SimpleNamespace(
        oauth_credentials=object(), connectors=ConnectorRegistry(edges={e.realm: e for e in build_oauth_edges({})}),
        ms_todo_adapter=object() if todo else None, task_config_repo=object(), task_search_index=object(),
        indexed_email_repo=object(), drive_note_repo=object(),
        drive_connection_service=object() if drive else None, user_drive=object() if drive else None,
        drive_task_queue=object() if drive_queue else None, drive_index_repo=object() if drive_queue else None,
        drive_connection_repo=object() if drive_queue else None)


@pytest.fixture
def calls(monkeypatch):
    recorded = {}

    def record(name):
        def build(*args, **kwargs):
            recorded[name] = (args, kwargs)
            return f"<{name}>"
        return build

    for name in ("build_drive_activation", "build_oauth_connect_service", "build_connect_activations",
                 "build_drive_sync_scheduler"):
        monkeypatch.setattr(integrations_setup, name, record(name))
    return recorded


def _build(container, *, queue="QUEUE", alert="ALERT"):
    ops = SimpleNamespace(task_queue=queue, alert_sink=alert)
    return build_integrations(DB, ENV, container=container, boot=BOOT, ops=ops, user_repo=USERS,  # type: ignore[arg-type]
                              notification_service=NOTIFY, languages=LANGUAGES, ensure_agents=ensure_agents)


def test_the_connect_flow_gets_the_reconnect_service_the_drive_activation_and_the_boot_secrets(calls):
    container = _container()

    built = _build(container)

    assert built.connect == "<build_oauth_connect_service>"
    args, kwargs = calls["build_oauth_connect_service"]
    assert args == (DB, ENV)
    assert kwargs["connectors"] is container.connectors and kwargs["credentials"] is container.oauth_credentials
    assert kwargs["cipher"] is BOOT.cipher and kwargs["digest_key"] == b"digest-1"
    assert kwargs["revocation"] is built.credential_revocation
    assert kwargs["redirect_uri"] == "https://svc.example.test/connect/callback"
    assert kwargs["activations"] == "<build_connect_activations>"
    _, activations = calls["build_connect_activations"]
    assert activations == {"reconnect": built.reconnect, "todo_connector": container.connectors.get(MICROSOFT_TODO),
                           "task_queue": "QUEUE", "drive_activation": "<build_drive_activation>"}
    assert calls["build_drive_activation"][1] == {"connections": container.drive_connection_service,
                                                   "drive": container.user_drive, "task_queue": container.drive_task_queue}


def test_the_reconnect_services_run_over_the_one_credential_store(calls):
    container = _container()

    built = _build(container)

    reconnect = built.reconnect
    assert reconnect._credentials is container.oauth_credentials and reconnect._users is USERS
    assert (reconnect._notifier, reconnect._email, reconnect._task_config) == (
        NOTIFY, container.indexed_email_repo, container.task_config_repo)
    assert reconnect._drive_connections is container.drive_connection_service and reconnect._alert_sink == "ALERT"
    assert reconnect._ensure_agents is ensure_agents
    assert built.credential_revocation._credentials is container.oauth_credentials
    flag = built.gmail_reconnect_flag
    assert flag._oauth_credentials is container.oauth_credentials and flag._ensure_agents is ensure_agents
    assert flag._mail_connector is container.connectors.get(GMAIL) and flag._notification is NOTIFY


def test_with_a_drive_the_disconnect_service_revokes_through_the_one_revocation(calls):
    container = _container()

    disconnect = _build(container).drive_disconnect

    assert disconnect._connections is container.drive_connection_service and disconnect._drive is container.user_drive
    assert disconnect._notes is container.drive_note_repo and disconnect._queue is container.drive_task_queue
    assert disconnect._revocation is not None


def test_without_a_drive_there_is_no_disconnect_and_no_drive_activation(calls):
    built = _build(_container(drive=False))

    assert built.drive_disconnect is None
    assert "build_drive_activation" not in calls
    assert calls["build_connect_activations"][1]["drive_activation"] is None


def test_the_drive_fan_out_needs_the_drive_queue_and_gets_the_notice_collaborators(calls):
    container = _container()

    built = _build(container)

    assert built.drive_sync_scheduler == "<build_drive_sync_scheduler>"
    assert calls["build_drive_sync_scheduler"][1] == {
        "repo": container.drive_index_repo, "queue": container.drive_task_queue,
        "connections": container.drive_connection_repo, "connection_service": container.drive_connection_service,
        "notifier": NOTIFY, "alert_sink": "ALERT", "ensure_agents": ensure_agents,
        "localization": LANGUAGES.localization, "language": LANGUAGES.resolver}


def test_without_the_drive_queue_there_is_no_fan_out(calls):
    assert _build(_container(drive_queue=False)).drive_sync_scheduler is None
    assert "build_drive_sync_scheduler" not in calls


def test_the_to_do_setup_needs_the_to_do_client_and_the_task_queue(calls):
    container = _container()

    setup = _build(container).task_setup

    assert setup._lifecycle is container.ms_todo_adapter and setup._tasks_provider is container.ms_todo_adapter
    assert setup._oauth_credentials is container.oauth_credentials and setup._task_queue == "QUEUE"
    assert setup._notification_url_base == "https://svc.example.test"
    assert _build(_container(todo=False)).task_setup is None
    assert _build(container, queue=None).task_setup is None


def test_the_bundle_is_frozen(calls):
    with pytest.raises(dataclasses.FrozenInstanceError):
        _build(_container()).connect = None  # type: ignore[misc]
