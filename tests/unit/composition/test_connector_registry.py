"""ConnectorRegistry (MULTI_DRIVE_RFC §3, §4.2, T-6, E25): the four connector kinds and their specs, kinds per
integration, and the realm's OAuth edge from the mapping it was built with."""
from dataclasses import FrozenInstanceError

import pytest

from src.composition.connector_registry import (
    GMAIL, GOOGLE_TASKS, MICROSOFT_ONEDRIVE, MICROSOFT_TODO, ConnectorRegistry,
)
from src.domain.connector_spec import ConnectorSpec
from src.domain.integration import Integration
from src.ports.oauth_provider_port import OAuthProviderPort


def test_the_four_kinds_and_their_specs():
    registry = ConnectorRegistry()
    got = {kind: registry.get(kind) for kind in ("gmail", "google_tasks", "microsoft_todo", "microsoft_onedrive")}
    assert {k: (s.integration, s.realm, s.multiple, s.label) for k, s in got.items()} == {
        "gmail": (Integration.MAIL, "google", False, "Gmail"),
        "google_tasks": (Integration.TASKS, "google", False, "Google Tasks"),
        "microsoft_todo": (Integration.TASKS, "microsoft", False, "Microsoft To Do"),
        # False until slice 1b: another account's consent replaces the drive credential, as today
        "microsoft_onedrive": (Integration.DRIVE, "microsoft", False, "OneDrive"),
    }
    assert all(spec.kind == kind for kind, spec in got.items())
    assert (GMAIL, GOOGLE_TASKS, MICROSOFT_TODO, MICROSOFT_ONEDRIVE) == tuple(got)


def test_required_scopes_are_the_service_scopes_each_connect_asks_for():
    registry = ConnectorRegistry()
    assert registry.get("gmail").required_scopes == ("https://www.googleapis.com/auth/gmail.readonly",)
    assert registry.get("google_tasks").required_scopes == ("https://www.googleapis.com/auth/tasks",)
    assert registry.get("microsoft_todo").required_scopes == ("Tasks.ReadWrite",)
    assert registry.get("microsoft_onedrive").required_scopes == ("Files.ReadWrite.AppFolder",)


def test_only_the_drive_kind_has_an_id_space_and_it_is_todays_provider_key():
    registry = ConnectorRegistry()
    assert registry.get("microsoft_onedrive").id_space == "microsoft_onedrive"
    assert [registry.get(k).id_space for k in ("gmail", "google_tasks", "microsoft_todo")] == ["", "", ""]


def test_kinds_of_an_integration():
    registry = ConnectorRegistry()
    assert registry.kinds(Integration.TASKS) == ["google_tasks", "microsoft_todo"]
    assert registry.kinds(Integration.MAIL) == ["gmail"]
    assert registry.kinds(Integration.DRIVE) == ["microsoft_onedrive"]


def test_an_unknown_kind_raises():
    with pytest.raises(KeyError):
        ConnectorRegistry().get("dropbox")


def test_a_spec_is_frozen():
    spec = ConnectorRegistry().get("gmail")
    assert isinstance(spec, ConnectorSpec)
    with pytest.raises(FrozenInstanceError):
        spec.multiple = True   # type: ignore[misc]


class _Edge(OAuthProviderPort):
    def __init__(self, realm: str) -> None:
        self._realm = realm

    @property
    def realm(self):
        return self._realm

    @property
    def revocation_scope(self):
        raise NotImplementedError

    def authorization_url(self, flow, spec, redirect_uri, *, state):
        raise NotImplementedError

    async def exchange(self, code, flow, spec, redirect_uri):
        raise NotImplementedError

    async def revoke(self, credential):
        raise NotImplementedError


def test_realm_edge_returns_the_edge_it_was_built_with():
    google = _Edge("google")
    registry = ConnectorRegistry({"google": google})
    assert registry.realm_edge("google") is google
    assert registry.realm_edge(registry.get("gmail").realm) is google


def test_realm_edge_of_an_unknown_realm_raises():
    with pytest.raises(KeyError):
        ConnectorRegistry({"google": _Edge("google")}).realm_edge("microsoft")
    with pytest.raises(KeyError):
        ConnectorRegistry().realm_edge("google")


def test_the_edges_cannot_be_changed_after_construction():
    edges = {"google": _Edge("google")}
    registry = ConnectorRegistry(edges)
    edges["microsoft"] = _Edge("microsoft")
    with pytest.raises(KeyError):
        registry.realm_edge("microsoft")
