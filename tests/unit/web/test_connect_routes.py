"""The generic connect routes (MULTI_DRIVE_RFC §5.2, slice 1a): `GET /auth/connect/<kind>` (`?credential=<id>` is a
reconnect) and the one callback `GET /auth/connect/callback`, over the real stack of `connect_world.py`.

Start: the Cabinet session or the sign-in; an unknown kind is 404, a realm without an edge 501; otherwise a redirect to
the realm's consent URL, the flow stored server-side. Callback: the provider's refusal, a missing state or code, and
every outcome of the connect service map to `?connected=<kind>` / `?connect_error=<status>&kind=<kind>`. No route sets
a cookie (the `*_connect_user_id` and `*_oauth_state` cookies are gone)."""
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pytest

from src.domain.flow_intent import FlowIntent
from tests.support.firestore_eval import eval_transactions
from tests.unit.web.connect_world import (
    ALEX, CLARA, GOOGLE_AUTH, MS_AUTH, REDIRECT, ConnectWorld,
)

LOGIN = "/auth/login?next=/cabinet"
SRC = Path(__file__).resolve().parents[3] / "src"


@pytest.fixture
def world(monkeypatch):
    with eval_transactions():
        yield ConnectWorld(monkeypatch)


def _query(location: str) -> dict:
    return {k: v[0] for k, v in parse_qs(urlparse(location).query).items()}


# ------------------------------------------------------------------------------------------------------- start

async def test_an_anonymous_start_goes_to_the_sign_in_and_stores_no_flow(world):
    resp, _ = await world.start("gmail", token=None)
    assert (resp.status_code, resp.headers["Location"]) == (302, LOGIN)
    assert world.flows == {}


async def test_an_invalid_session_goes_to_the_sign_in(world):
    resp, _ = await world.start("gmail", token="not-a-jwt")
    assert resp.headers["Location"] == LOGIN
    assert world.flows == {}


async def test_an_unknown_kind_is_404(world):
    resp, _ = await world.start("dropbox")
    assert resp.status_code == 404
    assert world.flows == {}


async def test_a_kind_whose_realm_has_no_edge_is_501(monkeypatch):
    with eval_transactions():
        world = ConnectWorld(monkeypatch, microsoft=False)
        resp, _ = await world.start("microsoft_todo")
    assert resp.status_code == 501
    assert world.flows == {}


@pytest.mark.parametrize("kind, consent", [
    ("gmail", GOOGLE_AUTH), ("microsoft_todo", MS_AUTH), ("microsoft_onedrive", MS_AUTH), ("google_tasks", GOOGLE_AUTH),
])
async def test_a_start_redirects_to_the_realms_consent_with_the_one_callback(world, kind, consent):
    resp, state = await world.start(kind)
    location = resp.headers["Location"]
    assert resp.status_code == 302 and location.startswith(consent + "?")
    query = _query(location)
    assert query["redirect_uri"] == REDIRECT
    assert set(world.registry.get(kind).required_scopes) <= set(query["scope"].split())
    assert state and len(world.flows) == 1
    assert world.set_cookies() == []


async def test_a_reconnect_names_the_credential_and_hints_its_account(world):
    await world.connect("gmail")
    (held,) = await world.held("gmail")
    resp, _ = await world.start("gmail", query=f"?credential={held.credential_id}")
    assert _query(resp.headers["Location"])["login_hint"] == "alex@example.com"
    (flow,) = [doc["value"] for doc in world.flows.values()]
    assert (flow["intent"], flow["target_id"], flow["expected_subject"]) == (
        FlowIntent.RECONNECT.value, held.credential_id, "sub-1")


async def test_a_reconnect_of_a_credential_the_user_does_not_hold_stores_no_flow(world):
    resp, _ = await world.start("gmail", query="?credential=cred-1")
    assert resp.headers["Location"] == "/cabinet?connect_error=state&kind=gmail"
    assert world.flows == {}


async def test_unwired_routes_are_501(monkeypatch):
    with eval_transactions():
        world = ConnectWorld(monkeypatch, wired=False)
        assert (await world.start("gmail"))[0].status_code == 501
        assert (await world.callback("st-1")).status_code == 501


# ---------------------------------------------------------------------------------------------------- callback

async def test_a_connect_round_trip_stores_the_credential_and_lands_connected(world):
    resp = await world.connect("gmail")
    assert (resp.status_code, resp.headers["Location"]) == (302, "/cabinet?connected=gmail")
    (held,) = await world.held("gmail")
    assert (held.account_id, held.account.subject, held.access_token) == ("acct-1", "sub-1", "at-gmail")
    assert world.flows == {}                                       # single use: consumed
    assert world.set_cookies() == []


async def test_the_provider_refusal_is_denied_and_stores_nothing(world):
    await world.start("gmail")
    resp = await world.get("/auth/connect/callback?error=access_denied&state=x")
    assert resp.headers["Location"] == "/cabinet?connect_error=denied"
    assert await world.held("gmail") == []


@pytest.mark.parametrize("query, error", [("?code=code-1", "state"), ("?state=st-1", "no_code")])
async def test_a_missing_state_or_code_is_refused_before_any_exchange(world, query, error):
    await world.start("gmail")
    resp = await world.get(f"/auth/connect/callback{query}")
    assert resp.headers["Location"] == f"/cabinet?connect_error={error}"
    assert world.http.requests_to("POST", "https://oauth2.googleapis.com/token") == []


@pytest.mark.parametrize("token", [None, "not-a-jwt"], ids=["anonymous", "invalid"])
async def test_a_callback_without_the_session_goes_to_the_sign_in_and_keeps_the_flow(world, token):
    _, state = await world.start("gmail")
    resp = await world.callback(state, token=token)
    assert resp.headers["Location"] == LOGIN
    assert len(world.flows) == 1 and await world.held("gmail") == []


async def test_an_unknown_or_replayed_state_is_a_state_error_without_a_kind(world):
    _, state = await world.start("gmail")
    assert (await world.callback(state)).headers["Location"] == "/cabinet?connected=gmail"
    assert (await world.callback(state)).headers["Location"] == "/cabinet?connect_error=state"
    assert (await world.callback("never-issued")).headers["Location"] == "/cabinet?connect_error=state"


async def test_a_grant_without_the_service_scope_is_a_scope_error(world):
    world.granted = "openid email"
    resp = await world.connect("gmail")
    assert resp.headers["Location"] == "/cabinet?connect_error=scope&kind=gmail"
    assert await world.held("gmail") == []


async def test_a_failed_exchange_is_an_exchange_error(world):
    world.http.route("POST", "https://oauth2.googleapis.com/token", 400, {"error": "invalid_grant"})
    resp = await world.connect("gmail")
    assert resp.headers["Location"] == "/cabinet?connect_error=exchange&kind=gmail"
    assert await world.held("gmail") == []


async def test_another_users_session_is_a_user_error(world):
    _, state = await world.start("gmail")                       # started by Alex
    resp = await world.callback(state, token=world.session(CLARA))
    assert resp.headers["Location"] == "/cabinet?connect_error=user&kind=gmail"
    assert await world.held("gmail", ALEX) == [] and await world.held("gmail", CLARA) == []


async def test_a_reconnect_with_another_account_is_an_account_error_and_keeps_the_old(world):
    await world.connect("gmail")
    (held,) = await world.held("gmail")
    world.signs_in_as("sub-2")
    _, state = await world.start("gmail", query=f"?credential={held.credential_id}")
    resp = await world.callback(state)
    assert resp.headers["Location"] == "/cabinet?connect_error=account&kind=gmail"
    assert [c.account.subject for c in await world.held("gmail")] == ["sub-1"]


async def test_no_route_sets_a_cookie_and_no_connect_cookie_is_left_in_the_code(world):
    await world.connect("gmail")
    await world.connect("microsoft_todo")
    await world.connect("microsoft_onedrive")
    await world.get("/auth/connect/callback?error=access_denied")
    assert world.set_cookies() == []
    web = "\n".join(p.read_text(encoding="utf-8") for p in (SRC / "web").rglob("*") if p.suffix in {".py", ".html"})
    assert "_connect_user_id" not in web and "_oauth_state" not in web
