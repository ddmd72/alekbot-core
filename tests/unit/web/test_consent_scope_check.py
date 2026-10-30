"""A Google consent whose granted scopes lack the service scope stores nothing (granular consent: an unticked
Gmail / Tasks checkbox still yields a token with only `openid email`). Same fixtures as test_consent_identity."""
import pytest

from src.domain.exceptions import OAuthScopeNotGrantedError
from src.services.google_oauth_service import GMAIL_SCOPES, GOOGLE_TASKS_SCOPES, GoogleOAuthService
from tests.unit.web.test_consent_identity import (  # noqa: F401
    CALLBACKS, GOOGLE, GOOGLE_TOKEN, GOOGLE_TOKEN_BODY, USERINFO, World, world,
)

GMAIL = "https://www.googleapis.com/auth/gmail.readonly"
TASKS = "https://www.googleapis.com/auth/tasks"
SERVICE_SCOPE = {"gmail": GMAIL, "google_tasks": TASKS}


@pytest.mark.parametrize("which", GOOGLE)
async def test_granted_scopes_are_stored(world, which):
    world.http.route("POST", GOOGLE_TOKEN, body={**GOOGLE_TOKEN_BODY, "scope": f"openid email {SERVICE_SCOPE[which]}"})
    resp = await world.callback(which)
    assert resp.headers["Location"] == CALLBACKS[which][4]
    got = await world.store.get_credentials("user-a", CALLBACKS[which][3])
    assert sorted(got.scopes) == sorted(["openid", "email", SERVICE_SCOPE[which]])


@pytest.mark.parametrize("which", GOOGLE)
@pytest.mark.parametrize("scope", ["openid email", "email openid https://www.googleapis.com/auth/other", None])
async def test_missing_service_scope_stores_nothing_and_redirects(world, which, scope):
    body = dict(GOOGLE_TOKEN_BODY)
    if scope is not None:
        body["scope"] = scope
    world.http.route("POST", GOOGLE_TOKEN, body=body)
    resp = await world.callback(which)
    assert resp.headers["Location"] == CALLBACKS[which][5] + "scope"
    assert world.docs == {}


@pytest.mark.parametrize("scopes,granted_scope", [
    (GMAIL_SCOPES, "openid email"),
    (GOOGLE_TASKS_SCOPES, GMAIL),  # the other service's scope does not count
])
async def test_exchange_code_raises_without_the_service_scope(world, scopes, granted_scope):
    world.http.route("POST", GOOGLE_TOKEN, body={**GOOGLE_TOKEN_BODY, "scope": granted_scope})
    with pytest.raises(OAuthScopeNotGrantedError):
        await GoogleOAuthService("cid", "csecret", scopes=scopes).exchange_code("c", "https://x.test/cb", "user-a")


async def test_exchange_code_raises_when_scope_field_is_absent(world):
    world.http.route("POST", GOOGLE_TOKEN, body=dict(GOOGLE_TOKEN_BODY))  # no `scope` field, routed explicitly
    with pytest.raises(OAuthScopeNotGrantedError):
        await GoogleOAuthService("cid", "csecret", scopes=GMAIL_SCOPES).exchange_code("c", "https://x.test/cb", "user-a")
    assert world.http.requests_to("GET", USERINFO) == []  # refused before the identity read


def test_scopes_without_a_service_scope_fail_fast():
    with pytest.raises(ValueError):
        GoogleOAuthService("cid", "csecret", scopes=["openid", "email"])
