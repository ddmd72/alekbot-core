"""OAuthProviderPort contract suite (MULTI_DRIVE_RFC §5.1, §5.2, E16): one set of cases, both realm edges.

The fake sits at the HTTP boundary (`aiohttp.ClientSession`), not at the port. Examples are invented round values.
Collected by `tests/unit/adapters/test_oauth_provider_contract.py`; also runnable directly:
`pytest tests/contracts/oauth_provider_contract.py`.
"""
import base64
import hashlib
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List
from unittest.mock import patch
from urllib.parse import parse_qs, urlparse

import pytest

from src.adapters.google.google_oauth_provider import GoogleOAuthProvider
from src.adapters.microsoft.microsoft_oauth_provider import MicrosoftOAuthProvider
from src.domain.connector_spec import ConnectorSpec
from src.domain.flow_intent import FlowIntent
from src.domain.integration import Integration
from src.domain.oauth_credential import OAuthCredential
from src.domain.oauth_flow import OAuthFlow
from src.domain.provider_account import ProviderAccount
from src.domain.revocation_scope import RevocationScope

REDIRECT = "https://example.test/cb"
VERIFIER = "v" * 43
SECRET_BODY = "SECRET-BODY-TEXT"
_SPEC_SCOPES = {"google": ("https://www.googleapis.com/auth/tasks",), "microsoft": ("Tasks.ReadWrite",)}


class _Resp:
    def __init__(self, status: int = 200, body: Any = None) -> None:
        self.status = status
        self.ok = status < 300
        self._body = body

    async def json(self) -> Any:
        return self._body

    async def text(self) -> str:
        return SECRET_BODY

    async def __aenter__(self) -> "_Resp":
        return self

    async def __aexit__(self, *exc: Any) -> bool:
        return False


class FakeAiohttp:
    """Records every call; `routes` maps (method, url prefix) to a response (or an exception to raise)."""

    def __init__(self, routes: Dict[tuple, Any]) -> None:
        self.routes = routes
        self.calls: List[Dict[str, Any]] = []

    def __call__(self, *args: Any, **kwargs: Any) -> "FakeAiohttp":
        return self

    async def __aenter__(self) -> "FakeAiohttp":
        return self

    async def __aexit__(self, *exc: Any) -> bool:
        return False

    def _do(self, method: str, url: str, **kwargs: Any) -> _Resp:
        self.calls.append({"method": method, "url": url, **kwargs})
        for (m, prefix), resp in self.routes.items():
            if m == method and url.startswith(prefix):
                if isinstance(resp, Exception):
                    raise resp
                return resp
        raise AssertionError(f"unexpected {method} {url}")

    def post(self, url: str, **kwargs: Any) -> _Resp:
        return self._do("POST", url, **kwargs)

    def get(self, url: str, **kwargs: Any) -> _Resp:
        return self._do("GET", url, **kwargs)


def _spec(realm: str) -> ConnectorSpec:
    integration = Integration.TASKS
    return ConnectorSpec(kind=f"{realm}_kind", integration=integration, realm=realm,
                         required_scopes=_SPEC_SCOPES[realm], multiple=False, id_space="", label="Kind")


def _flow(intent: FlowIntent = FlowIntent.CONNECT, hint: str = "") -> OAuthFlow:
    return OAuthFlow(user_id="user-alex", kind="k", intent=intent, target_id="", expected_subject="",
                     hint_label=hint, pkce_verifier=VERIFIER, return_path="/cabinet",
                     expires_at=datetime.now(timezone.utc) + timedelta(minutes=10))


def _credential() -> OAuthCredential:
    return OAuthCredential(user_id="user-alex", account_id="acc-1", kind="k", integration=Integration.TASKS,
                           account=ProviderAccount("x", "sub-1", "alex@example.com"),
                           access_token="tok-access-1", refresh_token="tok-refresh-1",
                           token_expiry=datetime.now(timezone.utc), scopes=[])


class _Case:
    def __init__(self, realm: str, edge: Any, token_url: str, identity_url: str, granted_raw: str,
                 granted_expected: tuple, identity_ok: Dict[str, Any], no_subject: Dict[str, Any]) -> None:
        self.realm, self.edge = realm, edge
        self.token_url, self.identity_url = token_url, identity_url
        self.granted_raw, self.granted_expected = granted_raw, granted_expected
        self.identity_ok, self.no_subject = identity_ok, no_subject
        self.spec = _spec(realm)

    def routes(self, token_status: int = 200, identity_status: int = 200, identity: Any = None) -> Dict[tuple, Any]:
        token = {"access_token": "tok-access-1", "refresh_token": "tok-refresh-1", "expires_in": 1800,
                 "scope": self.granted_raw}
        return {
            ("POST", self.token_url): _Resp(token_status, token),
            ("GET", self.identity_url): _Resp(identity_status, identity if identity is not None else self.identity_ok),
        }


@pytest.fixture(params=["google", "microsoft"])
def case(request: pytest.FixtureRequest) -> _Case:
    if request.param == "google":
        return _Case(
            "google", GoogleOAuthProvider("cid", "csecret"),
            "https://oauth2.googleapis.com/token", "https://www.googleapis.com/oauth2/v3/userinfo",
            "https://www.googleapis.com/auth/tasks openid email",
            ("https://www.googleapis.com/auth/tasks", "openid", "email"),
            {"sub": "sub-1", "email": "alex@example.com"}, {"email": "alex@example.com"})
    return _Case(
        "microsoft", MicrosoftOAuthProvider("cid", "csecret"),
        "https://login.microsoftonline.com/consumers/oauth2/v2.0/token", "https://graph.microsoft.com/v1.0/me",
        "https://graph.microsoft.com/Tasks.ReadWrite offline_access https://graph.microsoft.com/User.Read",
        ("Tasks.ReadWrite", "offline_access", "User.Read"),
        {"id": "sub-1", "mail": "alex@example.com"}, {"mail": "alex@example.com"})


def _query(url: str) -> Dict[str, str]:
    return {k: v[0] for k, v in parse_qs(urlparse(url).query).items()}


def test_edge_identity(case: _Case) -> None:
    assert case.edge.realm == case.realm
    expected = RevocationScope.GRANT_WIDE if case.realm == "google" else RevocationScope.NONE
    assert case.edge.revocation_scope is expected


def test_authorization_url_carries_state_pkce_and_scopes(case: _Case) -> None:
    url = case.edge.authorization_url(_flow(), case.spec, REDIRECT, state="raw-state-1")
    q = _query(url)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(VERIFIER.encode()).digest()).decode().rstrip("=")
    assert q["state"] == "raw-state-1"
    assert q["code_challenge"] == challenge and "=" not in q["code_challenge"]
    assert q["code_challenge_method"] == "S256"
    assert q["redirect_uri"] == REDIRECT and q["response_type"] == "code" and q["client_id"] == "cid"
    scopes = q["scope"].split()
    assert set(case.spec.required_scopes) <= set(scopes)
    identity = ["openid", "email"] if case.realm == "google" else ["offline_access", "User.Read"]
    assert set(identity) <= set(scopes) and len(scopes) == len(set(scopes))
    assert "code_verifier" not in q and VERIFIER not in url


def test_authorization_url_prompt_per_intent(case: _Case) -> None:
    connect = _query(case.edge.authorization_url(_flow(FlowIntent.CONNECT), case.spec, REDIRECT, state="s"))
    reconnect = _query(case.edge.authorization_url(_flow(FlowIntent.RECONNECT), case.spec, REDIRECT, state="s"))
    add = _query(case.edge.authorization_url(_flow(FlowIntent.ADD), case.spec, REDIRECT, state="s"))
    if case.realm == "google":
        assert connect["prompt"] == reconnect["prompt"] == "consent"
        assert add["prompt"] == "consent select_account"
        assert connect["access_type"] == "offline"
    else:
        assert "prompt" not in connect and "prompt" not in reconnect
        assert add["prompt"] == "select_account"
        assert connect["response_mode"] == "query"


def test_login_hint_on_reconnect_only(case: _Case) -> None:
    hint = "alex@example.com"
    for intent in (FlowIntent.CONNECT, FlowIntent.ADD):
        assert "login_hint" not in _query(
            case.edge.authorization_url(_flow(intent, hint), case.spec, REDIRECT, state="s"))
    q = _query(case.edge.authorization_url(_flow(FlowIntent.RECONNECT, hint), case.spec, REDIRECT, state="s"))
    assert q["login_hint"] == hint
    assert "login_hint" not in _query(
        case.edge.authorization_url(_flow(FlowIntent.RECONNECT, ""), case.spec, REDIRECT, state="s"))


async def test_exchange_posts_verifier_and_returns_grant(case: _Case) -> None:
    fake = FakeAiohttp(case.routes())
    with patch("aiohttp.ClientSession", fake):
        grant = await case.edge.exchange("code-1", _flow(), case.spec, REDIRECT)
    post = next(c for c in fake.calls if c["method"] == "POST")
    assert post["data"]["code_verifier"] == VERIFIER and post["data"]["code"] == "code-1"
    assert post["data"]["redirect_uri"] == REDIRECT and post["data"]["grant_type"] == "authorization_code"
    assert post.get("timeout") is not None
    assert next(c for c in fake.calls if c["method"] == "GET").get("timeout") is not None
    assert grant.access_token == "tok-access-1" and grant.refresh_token == "tok-refresh-1"
    assert grant.scopes == case.granted_expected
    assert grant.account == ProviderAccount(case.realm, "sub-1", "alex@example.com")
    remaining = grant.token_expiry - datetime.now(timezone.utc)
    assert timedelta(minutes=29) < remaining <= timedelta(minutes=30)


async def test_microsoft_exchange_posts_scope() -> None:
    edge = MicrosoftOAuthProvider("cid", "csecret")
    spec = _spec("microsoft")
    fake = FakeAiohttp({
        ("POST", "https://login.microsoftonline.com/"): _Resp(200, {"access_token": "t", "scope": "Tasks.ReadWrite"}),
        ("GET", "https://graph.microsoft.com/v1.0/me"): _Resp(200, {"id": "sub-1", "userPrincipalName": "user@example.com"}),
    })
    with patch("aiohttp.ClientSession", fake):
        grant = await edge.exchange("c", _flow(), spec, REDIRECT)
    assert set(fake.calls[0]["data"]["scope"].split()) == {"Tasks.ReadWrite", "offline_access", "User.Read"}
    assert grant.account.label == "user@example.com" and grant.refresh_token == ""


async def test_exchange_missing_subject_raises(case: _Case) -> None:
    fake = FakeAiohttp(case.routes(identity=case.no_subject))
    with patch("aiohttp.ClientSession", fake), pytest.raises(ValueError):
        await case.edge.exchange("c", _flow(), case.spec, REDIRECT)


async def test_exchange_identity_failure_raises_without_body(case: _Case) -> None:
    fake = FakeAiohttp(case.routes(identity_status=401))
    with patch("aiohttp.ClientSession", fake), pytest.raises(ValueError) as err:
        await case.edge.exchange("c", _flow(), case.spec, REDIRECT)
    assert "401" in str(err.value) and SECRET_BODY not in str(err.value)


async def test_exchange_non_200_raises_without_leaking(case: _Case) -> None:
    fake = FakeAiohttp(case.routes(token_status=400))
    with patch("aiohttp.ClientSession", fake), pytest.raises(ValueError) as err:
        await case.edge.exchange("code-secret-1", _flow(), case.spec, REDIRECT)
    message = str(err.value)
    assert "400" in message
    assert SECRET_BODY not in message and "code-secret-1" not in message and VERIFIER not in message


async def test_revoke_honours_scope(case: _Case) -> None:
    fake = FakeAiohttp({("POST", "https://oauth2.googleapis.com/revoke"): _Resp(200, {})})
    with patch("aiohttp.ClientSession", fake):
        await case.edge.revoke(_credential())
    if case.realm == "google":
        assert len(fake.calls) == 1 and fake.calls[0]["params"] == {"token": "tok-access-1"}
    else:
        assert fake.calls == []


async def test_google_revoke_failure_does_not_raise() -> None:
    fake = FakeAiohttp({("POST", "https://oauth2.googleapis.com/revoke"): RuntimeError("boom")})
    with patch("aiohttp.ClientSession", fake):
        await GoogleOAuthProvider("cid", "csecret").revoke(_credential())
