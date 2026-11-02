"""The `google_drive` spec in the registry (GOOGLE_DRIVE_RFC §4.1, GE-24, GE-31; design §1.8): declared and offered
(GE-24, from slice D); with no adapter registered (its client not configured) a handle of the kind is refused as not
connected; the Google edge asks Drive's full scope plus its identity scopes, never folding in earlier grants."""
from datetime import datetime, timezone
from urllib.parse import parse_qs, urlparse

import pytest

from src.adapters.google.google_oauth_provider import GoogleOAuthProvider
from src.composition.connector_registry import GOOGLE_DRIVE, MICROSOFT_ONEDRIVE, ConnectorRegistry
from src.composition.drive_provider_router import DriveProviderRouter
from src.domain.flow_intent import FlowIntent
from src.domain.integration import Integration
from src.domain.oauth_flow import OAuthFlow
from src.domain.user_drive import DriveNotConnectedError
from tests.support.drive_handles import drive_handle
from tests.support.fake_drive_adapter import FakeDriveAdapter

DRIVE_SCOPE = "https://www.googleapis.com/auth/drive"


def test_the_spec():
    spec = ConnectorRegistry().get(GOOGLE_DRIVE)
    assert GOOGLE_DRIVE == "google_drive"
    assert (spec.kind, spec.integration, spec.realm) == ("google_drive", Integration.DRIVE, "google")
    assert spec.required_scopes == (DRIVE_SCOPE,)
    assert spec.multiple is True
    assert (spec.id_space, spec.label) == ("google_drive", "Google Drive")


def test_it_is_declared_and_offered():
    registry = ConnectorRegistry()
    assert registry.get(GOOGLE_DRIVE).offered is True
    assert GOOGLE_DRIVE in [spec.kind for spec in registry.offered()]
    assert GOOGLE_DRIVE in registry.kinds(Integration.DRIVE)


def test_no_adapter_is_registered_for_it():
    registry = ConnectorRegistry(drive_adapters={MICROSOFT_ONEDRIVE: FakeDriveAdapter(MICROSOFT_ONEDRIVE)})
    with pytest.raises(KeyError):
        registry.drive_adapter(GOOGLE_DRIVE)


async def test_the_router_refuses_a_google_drive_handle_as_not_connected():
    registry = ConnectorRegistry(drive_adapters={MICROSOFT_ONEDRIVE: FakeDriveAdapter(MICROSOFT_ONEDRIVE)})
    router = DriveProviderRouter(registry.drive_adapters())
    with pytest.raises(DriveNotConnectedError):
        await router.get_root(drive_handle("usr-1", "conn-1", "cred-1", GOOGLE_DRIVE))
    with pytest.raises(DriveNotConnectedError):
        await router.outer_folders(drive_handle("usr-1", "conn-1", "cred-1", GOOGLE_DRIVE))


def test_the_consent_asks_drive_and_the_identity_scopes_only():
    flow = OAuthFlow(user_id="usr-1", kind=GOOGLE_DRIVE, intent=FlowIntent.CONNECT, target_id="", expected_subject="",
                     hint_label="", pkce_verifier="v" * 64, return_path="/cabinet",
                     expires_at=datetime(2026, 11, 2, 9, 10, tzinfo=timezone.utc))
    url = GoogleOAuthProvider("cid", "csecret").authorization_url(
        flow, ConnectorRegistry().get(GOOGLE_DRIVE), "https://service.example/auth/connect/callback", state="st-1")
    query = parse_qs(urlparse(url).query)
    assert query["scope"] == [f"{DRIVE_SCOPE} openid email"]
    assert "include_granted_scopes" not in query
