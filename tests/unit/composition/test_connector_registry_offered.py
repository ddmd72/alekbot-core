"""ConnectorRegistry's read-only views for the connect service and the Cabinet (MULTI_DRIVE_RFC §5.2, slice 1a):
`specs()` and `edges()` are what `OAuthConnectService` is built over; `offered()` drops frozen Google Tasks."""
import pytest

from src.adapters.google.google_oauth_provider import GoogleOAuthProvider
from src.composition.connector_registry import ConnectorRegistry


def test_offered_is_every_kind_but_the_frozen_one_in_declaration_order():
    assert [s.kind for s in ConnectorRegistry().offered()] == ["gmail", "microsoft_todo", "microsoft_onedrive"]
    assert ConnectorRegistry().get("google_tasks").offered is False


def test_specs_and_edges_are_the_registrys_own_and_read_only():
    google = GoogleOAuthProvider("g-cid", "g-csecret")
    registry = ConnectorRegistry(edges={"google": google})
    assert registry.specs()["gmail"] is registry.get("gmail")
    assert set(registry.specs()) == {"gmail", "google_tasks", "microsoft_todo", "microsoft_onedrive"}
    assert dict(registry.edges()) == {"google": google}
    with pytest.raises(TypeError):
        registry.specs()["dropbox"] = registry.get("gmail")      # type: ignore[index]
    with pytest.raises(TypeError):
        registry.edges()["microsoft"] = google                   # type: ignore[index]
