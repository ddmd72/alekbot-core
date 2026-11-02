"""G14 (GOOGLE_DRIVE_RFC §5, design §1.7, Δ5): each OAuth edge's `identity_scopes` covers every form in which its own
identity scopes come back among the granted scopes: what the consent URL adds to the spec's scopes, and what the edge
returns from a real exchange (Google reports `email` as the userinfo URL; Microsoft may add the OIDC defaults)."""
from datetime import datetime, timezone
from urllib.parse import parse_qs, urlparse

import pytest

from src.adapters.google.google_oauth_provider import GoogleOAuthProvider
from src.adapters.microsoft.microsoft_oauth_provider import MicrosoftOAuthProvider
from src.composition.connector_registry import (
    GMAIL, GOOGLE_DRIVE, GOOGLE_TASKS, MICROSOFT_ONEDRIVE, MICROSOFT_TODO, ConnectorRegistry,
)
from src.domain.flow_intent import FlowIntent
from src.domain.oauth_flow import OAuthFlow
from tests.support import http_boundary_fake

REDIRECT = "https://service.example/auth/connect/callback"
GOOGLE_TOKEN = "https://oauth2.googleapis.com/token"
GOOGLE_USERINFO = "https://www.googleapis.com/oauth2/v3/userinfo"
MS_TOKEN = "https://login.microsoftonline.com/consumers/oauth2/v2.0/token"
GRAPH_ME = "https://graph.microsoft.com/v1.0/me"
USERINFO_EMAIL = "https://www.googleapis.com/auth/userinfo.email"
# what each token endpoint lists for a consent of the kind (the spec's scopes plus the edge's identity ones)
GRANTED = {
    GMAIL: f"https://www.googleapis.com/auth/gmail.readonly openid {USERINFO_EMAIL}",
    GOOGLE_TASKS: f"https://www.googleapis.com/auth/tasks openid {USERINFO_EMAIL}",
    GOOGLE_DRIVE: f"https://www.googleapis.com/auth/drive openid {USERINFO_EMAIL}",
    MICROSOFT_TODO: "Tasks.ReadWrite offline_access User.Read openid profile email",
    MICROSOFT_ONEDRIVE: ("https://graph.microsoft.com/Files.ReadWrite.AppFolder offline_access "
                         "https://graph.microsoft.com/User.Read openid profile email"),
}
SPECS = ConnectorRegistry().specs()


def _edge(realm: str):
    return GoogleOAuthProvider("cid", "csecret") if realm == "google" else MicrosoftOAuthProvider("cid", "csecret")


def _flow(kind: str) -> OAuthFlow:
    return OAuthFlow(user_id="usr-1", kind=kind, intent=FlowIntent.CONNECT, target_id="", expected_subject="",
                     hint_label="", pkce_verifier="v" * 64, return_path="/cabinet",
                     expires_at=datetime(2026, 11, 2, 9, 10, tzinfo=timezone.utc))


@pytest.mark.parametrize("kind", sorted(GRANTED))
def test_the_scopes_the_consent_adds_are_identity_scopes(kind):
    spec = SPECS[kind]
    edge = _edge(spec.realm)
    url = edge.authorization_url(_flow(kind), spec, REDIRECT, state="st-1")
    asked = parse_qs(urlparse(url).query)["scope"][0].split()
    assert [s for s in asked if s not in spec.required_scopes and s not in edge.identity_scopes] == []


@pytest.mark.parametrize("kind", sorted(GRANTED))
async def test_the_granted_scopes_an_exchange_returns_are_asked_or_identity_scopes(kind, monkeypatch):
    spec = SPECS[kind]
    edge = _edge(spec.realm)
    http = http_boundary_fake.install(monkeypatch)
    token = {"access_token": "tok-a1", "refresh_token": "tok-b2", "expires_in": 3600, "scope": GRANTED[kind]}
    if spec.realm == "google":
        http.route("POST", GOOGLE_TOKEN, body=token)
        http.route("GET", GOOGLE_USERINFO, body={"sub": "sub-1", "email": "alex@example.com"})
    else:
        http.route("POST", MS_TOKEN, body=token)
        http.route("GET", GRAPH_ME, body={"id": "sub-1", "mail": "alex@example.com"})
    grant = await edge.exchange("code-1", _flow(kind), spec, REDIRECT)
    assert set(spec.required_scopes) <= set(grant.scopes)
    assert [s for s in grant.scopes if s not in spec.required_scopes and s not in edge.identity_scopes] == []


def test_google_lists_the_long_form_of_email():
    assert {"openid", "email", USERINFO_EMAIL} <= set(GoogleOAuthProvider("cid", "csecret").identity_scopes)


def test_microsoft_lists_the_short_forms():
    scopes = MicrosoftOAuthProvider("cid", "csecret").identity_scopes
    assert {"offline_access", "User.Read", "openid", "profile", "email"} <= set(scopes)
    assert all(not s.startswith("https://") for s in scopes)
