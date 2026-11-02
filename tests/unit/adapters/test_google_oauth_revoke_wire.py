"""GE-28 (GOOGLE_DRIVE_RFC §9, design §1.7, Δ1): Google's revoke posts the refresh token (the one that ends the grant)
and the access token only when there is no refresh token; 200 logs the success line, any other status a WARNING with
the status; a transport error a WARNING; it never raises and never logs a token. Over the HTTP boundary fake."""
import logging
from datetime import datetime, timezone

import pytest

from src.adapters.google.google_oauth_provider import GoogleOAuthProvider
from src.domain.integration import Integration
from src.domain.oauth_credential import OAuthCredential
from src.domain.provider_account import ProviderAccount
from tests.support import http_boundary_fake

REVOKE = "https://oauth2.googleapis.com/revoke"
ACCESS = "tok-access-a1"
REFRESH = "tok-refresh-b2"


def _credential(refresh_token: str = REFRESH) -> OAuthCredential:
    return OAuthCredential(credential_id="cred-1", user_id="usr-1", account_id="acc-1", kind="google_drive",
                           integration=Integration.DRIVE, account=ProviderAccount("google", "sub-1", "alex@example.com"),
                           access_token=ACCESS, refresh_token=refresh_token,
                           token_expiry=datetime(2026, 11, 2, 9, 0, tzinfo=timezone.utc), scopes=[])


@pytest.fixture
def http(monkeypatch):
    return http_boundary_fake.install(monkeypatch)


@pytest.fixture
def logs(caplog):
    caplog.set_level(logging.DEBUG)
    return caplog


def _no_token_in(caplog) -> bool:
    return ACCESS not in caplog.text and REFRESH not in caplog.text


async def test_the_refresh_token_is_posted_when_present(http, logs):
    http.route("POST", REVOKE, status=200, body={})
    await GoogleOAuthProvider("cid", "csecret").revoke(_credential())
    assert [r.data for r in http.requests_to("POST", REVOKE)] == [{"token": REFRESH}]
    assert _no_token_in(logs)


async def test_the_access_token_is_posted_without_a_refresh_token(http, logs):
    http.route("POST", REVOKE, status=200, body={})
    await GoogleOAuthProvider("cid", "csecret").revoke(_credential(refresh_token=""))
    assert [r.data for r in http.requests_to("POST", REVOKE)] == [{"token": ACCESS}]
    assert _no_token_in(logs)


async def test_200_logs_the_success_line(http, logs):
    http.route("POST", REVOKE, status=200, body={})
    await GoogleOAuthProvider("cid", "csecret").revoke(_credential())
    assert [r.levelno for r in logs.records if "revoked at Google" in r.getMessage()] == [logging.INFO]
    assert not [r for r in logs.records if r.levelno >= logging.WARNING]


async def test_400_logs_a_warning_with_the_status_and_does_not_raise(http, logs):
    http.route("POST", REVOKE, status=400, body={"error": "invalid_token"})
    await GoogleOAuthProvider("cid", "csecret").revoke(_credential())
    warnings = [r.getMessage() for r in logs.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1 and "400" in warnings[0]
    assert "revoked at Google" not in logs.text
    assert _no_token_in(logs)


async def test_a_transport_error_logs_a_warning_and_does_not_raise(http, logs):
    await GoogleOAuthProvider("cid", "csecret").revoke(_credential())   # no route: the fake refuses the connection
    assert http.unrouted and [r.levelno for r in logs.records if r.levelno >= logging.WARNING] == [logging.WARNING]
    assert "revoked at Google" not in logs.text
    assert _no_token_in(logs)


async def test_n4_the_token_travels_in_the_form_body_not_the_query_string(http, logs):
    http.route("POST", REVOKE, status=200, body={})
    await GoogleOAuthProvider("cid", "csecret").revoke(_credential())
    [request] = http.requests_to("POST", REVOKE)
    assert request.data == {"token": REFRESH} and not request.params
    assert "token" not in request.url and not (request.headers or {})
