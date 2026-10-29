"""MCPClientRepository kit (P6 · T2): the docstring of `src/ports/mcp_client_repository.py` checked against
`FirestoreMCPClientRepository` on the evaluating Firestore fake (H1), with the index refusal ON. Assertions are on the
raw stored documents and on what readers return.

Refusals are the caller's (the docstring: the caller compares client_id, the SDK token handler refuses an expired code
or token, the service a revoked one), so they are checked through the real chain over the real repository: the `mcp`
SDK's `TokenHandler` and `ClientAuthenticator` -> `MCPSdkOAuthProvider` -> `MCPAuthorizationService` -> the adapter, with
every outcome asserted on the HTTP answer and on the stored documents.

`delete_client` is marked "no caller in src/" and is not tested.

`store` is the one fixture that selects the backend.
"""
import asyncio
import base64
import hashlib
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlencode

import pytest
from mcp.server.auth.handlers.token import TokenHandler
from mcp.server.auth.middleware.client_auth import ClientAuthenticator
from pydantic import ValidationError
from starlette.requests import Request

from src.adapters.firestore_mcp_client_repository import FirestoreMCPClientRepository
from src.composition.mcp_sdk_oauth_provider import MCPSdkOAuthProvider
from src.domain.mcp import MCPAuthCode, MCPClient, MCPRefreshToken
from src.services.mcp_authorization_service import MCPAuthorizationService, MCPInvalidGrant
from tests.support.firestore_eval import EvalDb
from tests.support.aes_gcm_cipher import AesGcmTestCipher

INDEXES_FILE = Path(__file__).resolve().parents[3] / "config" / "firestore.indexes.json"
CLIENTS, CODES, REFRESH = "eval_mcp_oauth_clients", "eval_mcp_auth_codes", "eval_mcp_refresh_tokens"
NOW = datetime.now(timezone.utc).replace(microsecond=0)
SOON = NOW + timedelta(minutes=10)
PAST = NOW - timedelta(seconds=5)
REDIRECT = "https://claude.ai/api/mcp/auth_callback"
VERIFIER = "v" * 64
CHALLENGE = base64.urlsafe_b64encode(hashlib.sha256(VERIFIER.encode()).digest()).decode().rstrip("=")
DROP = object()


def sha(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


class MCPStore:
    """The real repository over one EvalDb with the declared indexes (each collection mapped to its index group), and
    the real token endpoint over it."""

    def __init__(self) -> None:
        self.db = EvalDb(indexes=INDEXES_FILE, groups={
            CLIENTS: "development_mcp_oauth_clients", CODES: "development_mcp_auth_codes",
            REFRESH: "development_mcp_refresh_tokens"})
        self.repo = FirestoreMCPClientRepository(self.db, SimpleNamespace(
            mcp_oauth_clients_collection=CLIENTS, mcp_auth_codes_collection=CODES,
            mcp_refresh_tokens_collection=REFRESH), cipher=AesGcmTestCipher(), digest_key=bytes(range(32)))
        self.service = MCPAuthorizationService(
            self.repo, jwt_secret="kit-secret-that-is-at-least-32-characters", mcp_resource_uri="https://dev.example/mcp",
            consent_base_url="https://dev.example/mcp/consent", allowed_redirect_hosts=("claude.ai",))
        provider = MCPSdkOAuthProvider(self.service)
        self.handler = TokenHandler(provider=provider, client_authenticator=ClientAuthenticator(provider))

    def docs(self, collection: str) -> dict:
        return self.db.collection(collection).docs

    async def token(self, **form):
        """POST /token with `form`; (status, JSON body)."""
        body = urlencode(form).encode()

        async def receive():
            return {"type": "http.request", "body": body, "more_body": False}

        scope = {"type": "http", "method": "POST", "path": "/token", "query_string": b"",
                 "headers": [(b"content-type", b"application/x-www-form-urlencoded")]}
        response = await self.handler.handle(Request(scope, receive))
        return response.status_code, json.loads(response.body)

    async def exchange_code(self, client_id: str, code: str):
        return await self.token(grant_type="authorization_code", client_id=client_id, client_secret=f"secret-{client_id}",
                                code=code, redirect_uri=REDIRECT, code_verifier=VERIFIER)

    async def exchange_refresh(self, client_id: str, refresh_token: str):
        return await self.token(grant_type="refresh_token", client_id=client_id, client_secret=f"secret-{client_id}",
                                refresh_token=refresh_token)


@pytest.fixture
def store():
    return MCPStore()


def client(client_id="cli-a", **over) -> MCPClient:
    fields = dict(client_id=client_id, client_secret=f"secret-{client_id}", client_secret_expires_at=None,
                  client_name="Claude", redirect_uris=[REDIRECT], grant_types=["authorization_code", "refresh_token"],
                  response_types=["code"], scope="user_context", token_endpoint_auth_method="client_secret_post",
                  created_at=NOW)
    fields.update(over)
    return MCPClient(**fields)


def auth_code(code="code-a", client_id="cli-a", **over) -> MCPAuthCode:
    fields = dict(code=code, client_id=client_id, user_id=f"usr-{client_id}", account_id=f"acc-{client_id}",
                  redirect_uri=REDIRECT, code_challenge=CHALLENGE, code_challenge_method="S256",
                  resource="https://dev.example/mcp", scopes=["user_context"], expires_at=SOON)
    fields.update(over)
    return MCPAuthCode(**fields)


def refresh(value="rt-a", client_id="cli-a", **over) -> MCPRefreshToken:
    fields = dict(token_hash=sha(value), client_id=client_id, user_id=f"usr-{client_id}",
                  account_id=f"acc-{client_id}", scopes=["user_context"], resource="https://dev.example/mcp",
                  expires_at=NOW + timedelta(days=30), revoked_at=None)
    fields.update(over)
    return MCPRefreshToken(**fields)


def strip(doc: dict, **over) -> dict:
    out = dict(doc)
    for key, value in over.items():
        if value is DROP:
            out.pop(key)
        else:
            out[key] = value
    return out


# ---------------------------------------------------------------------------------------------------- clients

async def test_save_client_stores_every_field_under_client_id(store):
    await store.repo.save_client(client(client_secret_expires_at=1_900_000_000))
    assert store.docs(CLIENTS) == {"cli-a": {
        "client_id": "cli-a", "client_secret": "secret-cli-a", "client_secret_expires_at": 1_900_000_000,
        "client_name": "Claude", "redirect_uris": [REDIRECT], "grant_types": ["authorization_code", "refresh_token"],
        "response_types": ["code"], "scope": "user_context", "token_endpoint_auth_method": "client_secret_post",
        "created_at": NOW,
    }}


async def test_get_client_returns_every_field_as_saved(store):
    saved = client(client_secret_expires_at=1_900_000_000, client_name="Other", scope="a b",
                   redirect_uris=[REDIRECT, "https://claude.ai/other"], grant_types=["authorization_code"],
                   response_types=["code", "token"], token_endpoint_auth_method="client_secret_basic")
    await store.repo.save_client(saved)
    got = await store.repo.get_client("cli-a")
    assert got == saved
    assert got.created_at.utcoffset() == timedelta(0)


async def test_save_client_replaces_a_client_with_that_id(store):
    await store.repo.save_client(client(client_name="First", scope="first"))
    await store.repo.save_client(client(client_name="Second", client_secret="new"))
    assert await store.repo.get_client("cli-a") == client(client_name="Second", client_secret="new")
    assert len(store.docs(CLIENTS)) == 1


async def test_an_absent_client_is_none_and_the_token_endpoint_refuses_it(store):
    await store.repo.save_client(client("cli-a"))
    assert await store.repo.get_client("cli-x") is None
    await store.repo.save_auth_code(auth_code(client_id="cli-a"))
    status, body = await store.exchange_code("cli-x", "code-a")
    assert (status, body["error"]) == (401, "unauthorized_client")
    assert "code-a" in store.docs(CODES)


async def test_legacy_client_defaults(store):
    store.docs(CLIENTS)["cli-a"] = strip(FirestoreMCPClientRepository._client_to_fs(client()), grant_types=DROP,
                                         response_types=DROP, token_endpoint_auth_method=DROP, scope=DROP,
                                         client_name=DROP, client_secret=DROP, client_secret_expires_at=DROP)
    got = await store.repo.get_client("cli-a")
    assert (got.grant_types, got.response_types, got.token_endpoint_auth_method) == (
        ["authorization_code", "refresh_token"], ["code"], "client_secret_post")
    assert (got.scope, got.client_name, got.client_secret, got.client_secret_expires_at) == ("user_context", "", None, None)


async def test_legacy_client_without_redirect_uris_reads_empty_and_is_refused(store):
    store.docs(CLIENTS)["cli-a"] = strip(FirestoreMCPClientRepository._client_to_fs(client()), redirect_uris=DROP)
    assert (await store.repo.get_client("cli-a")).redirect_uris == []
    await store.repo.save_auth_code(auth_code())
    with pytest.raises(ValidationError):
        await store.exchange_code("cli-a", "code-a")
    assert "code-a" in store.docs(CODES) and store.docs(REFRESH) == {}


@pytest.mark.parametrize("field", ["client_id", "created_at"])
async def test_legacy_client_missing_a_required_field_raises(store, field):
    store.docs(CLIENTS)["cli-a"] = strip(FirestoreMCPClientRepository._client_to_fs(client()), **{field: DROP})
    with pytest.raises(KeyError):
        await store.repo.get_client("cli-a")


# ------------------------------------------------------------------------------------------ authorization codes

async def test_save_auth_code_stores_every_field_under_the_code(store):
    await store.repo.save_auth_code(auth_code())
    assert store.docs(CODES) == {"code-a": {
        "code": "code-a", "client_id": "cli-a", "user_id": "usr-cli-a", "account_id": "acc-cli-a",
        "redirect_uri": REDIRECT, "code_challenge": CHALLENGE, "code_challenge_method": "S256",
        "resource": "https://dev.example/mcp", "scopes": ["user_context"], "expires_at": SOON,
    }}


async def test_get_auth_code_returns_it_as_saved_without_consuming_it(store):
    plus3 = timezone(timedelta(hours=3))
    saved = auth_code(scopes=["a", "b"], resource=None, expires_at=datetime(2030, 1, 1, 13, 0, tzinfo=plus3))
    await store.repo.save_auth_code(saved)
    first, second = await store.repo.get_auth_code("code-a"), await store.repo.get_auth_code("code-a")
    assert first == second == saved
    assert first.expires_at == datetime(2030, 1, 1, 10, 0, tzinfo=timezone.utc)
    assert first.expires_at.utcoffset() == timedelta(0)
    assert "code-a" in store.docs(CODES)


async def test_save_auth_code_replaces_a_code_with_that_value(store):
    await store.repo.save_auth_code(auth_code(client_id="cli-a"))
    await store.repo.save_auth_code(auth_code(client_id="cli-b"))
    assert (await store.repo.get_auth_code("code-a")).client_id == "cli-b"
    assert len(store.docs(CODES)) == 1


async def test_consume_deletes_the_code_and_returns_it_once(store):
    await store.repo.save_auth_code(auth_code())
    await store.repo.save_auth_code(auth_code("code-b", "cli-b"))
    assert await store.repo.consume_auth_code("code-a") == auth_code()
    assert set(store.docs(CODES)) == {"code-b"}
    assert await store.repo.consume_auth_code("code-a") is None
    assert await store.repo.get_auth_code("code-a") is None


async def test_an_absent_code_is_none(store):
    assert await store.repo.get_auth_code("nope") is None
    assert await store.repo.consume_auth_code("nope") is None
    assert store.docs(CODES) == {}


async def test_tenancy_each_code_reads_back_its_own_client_user_and_account(store):
    await store.repo.save_auth_code(auth_code("code-a", "cli-a"))
    await store.repo.save_auth_code(auth_code("code-b", "cli-b", user_id="usr-x", account_id="acc-y"))
    a, b = await store.repo.get_auth_code("code-a"), await store.repo.get_auth_code("code-b")
    assert (a.client_id, a.user_id, a.account_id) == ("cli-a", "usr-cli-a", "acc-cli-a")
    assert (b.client_id, b.user_id, b.account_id) == ("cli-b", "usr-x", "acc-y")


async def test_an_expired_code_is_returned_not_deleted(store):
    await store.repo.save_auth_code(auth_code(expires_at=PAST))
    got = await store.repo.get_auth_code("code-a")
    assert got.expires_at == PAST and got.expires_at.utcoffset() == timedelta(0)
    assert "code-a" in store.docs(CODES)


async def test_legacy_code_defaults(store):
    store.docs(CODES)["code-a"] = strip(FirestoreMCPClientRepository._code_to_fs(auth_code()),
                                        code_challenge_method=DROP, resource=DROP, scopes=DROP)
    got = await store.repo.get_auth_code("code-a")
    assert (got.code_challenge_method, got.resource, got.scopes) == ("S256", None, [])


@pytest.mark.parametrize("field", ["code", "client_id", "user_id", "account_id", "redirect_uri", "code_challenge",
                                   "expires_at"])
async def test_legacy_code_missing_a_required_field_raises(store, field):
    store.docs(CODES)["code-a"] = strip(FirestoreMCPClientRepository._code_to_fs(auth_code()), **{field: DROP})
    with pytest.raises(KeyError):
        await store.repo.get_auth_code("code-a")
    with pytest.raises(KeyError):
        await store.repo.consume_auth_code("code-a")


@pytest.mark.xfail(strict=True, raises=AssertionError, reason=(
    "known defect: consume_auth_code reads, then deletes, outside a transaction, so concurrent calls can each return "
    "the code — src/adapters/firestore_mcp_client_repository.py:133-139 (P6 T1 H1)"))
async def test_of_concurrent_consumers_of_one_code_exactly_one_gets_it(store):
    await store.repo.save_auth_code(auth_code())
    store.db.collection(CODES).yield_on_get = True        # both read the code before either deletes it
    results = await asyncio.gather(store.repo.consume_auth_code("code-a"), store.repo.consume_auth_code("code-a"))
    assert sum(r is not None for r in results) == 1, results


# ------------------------------------------------------------------------------------------- refresh tokens

async def test_save_refresh_token_stores_every_field_under_the_hash(store):
    await store.repo.save_refresh_token(refresh())
    assert store.docs(REFRESH) == {sha("rt-a"): {
        "token_hash": sha("rt-a"), "client_id": "cli-a", "user_id": "usr-cli-a", "account_id": "acc-cli-a",
        "scopes": ["user_context"], "resource": "https://dev.example/mcp", "expires_at": NOW + timedelta(days=30),
        "revoked_at": None,
    }}


async def test_get_refresh_token_returns_it_as_saved(store):
    saved = refresh(scopes=["a", "b"], resource=None)
    await store.repo.save_refresh_token(saved)
    got = await store.repo.get_refresh_token(sha("rt-a"))
    assert got == saved and got.is_active
    assert got.expires_at.utcoffset() == timedelta(0)


async def test_save_refresh_token_replaces_a_token_with_that_hash(store):
    await store.repo.save_refresh_token(refresh(client_id="cli-a"))
    await store.repo.save_refresh_token(refresh(client_id="cli-b", revoked_at=NOW))
    assert await store.repo.get_refresh_token(sha("rt-a")) == refresh(client_id="cli-b", revoked_at=NOW)
    assert len(store.docs(REFRESH)) == 1


async def test_get_refresh_token_returns_a_revoked_one_with_revoked_at(store):
    await store.repo.save_refresh_token(refresh(revoked_at=PAST))
    got = await store.repo.get_refresh_token(sha("rt-a"))
    assert got.revoked_at == PAST and got.revoked_at.utcoffset() == timedelta(0) and not got.is_active


async def test_an_absent_refresh_token_is_none(store):
    assert await store.repo.get_refresh_token(sha("nope")) is None


async def test_tenancy_each_token_reads_back_its_own_client_user_and_account(store):
    await store.repo.save_refresh_token(refresh("rt-a", "cli-a"))
    await store.repo.save_refresh_token(refresh("rt-b", "cli-b", user_id="usr-x", account_id="acc-y"))
    a, b = await store.repo.get_refresh_token(sha("rt-a")), await store.repo.get_refresh_token(sha("rt-b"))
    assert (a.client_id, a.user_id, a.account_id) == ("cli-a", "usr-cli-a", "acc-cli-a")
    assert (b.client_id, b.user_id, b.account_id) == ("cli-b", "usr-x", "acc-y")


async def test_an_expired_refresh_token_is_returned_not_deleted(store):
    await store.repo.save_refresh_token(refresh(expires_at=PAST))
    assert (await store.repo.get_refresh_token(sha("rt-a"))).expires_at == PAST
    assert sha("rt-a") in store.docs(REFRESH)


async def test_legacy_refresh_token_without_revoked_at_is_active(store):
    store.docs(REFRESH)[sha("rt-a")] = strip(FirestoreMCPClientRepository._refresh_to_fs(refresh()), revoked_at=DROP,
                                             scopes=DROP, resource=DROP)
    got = await store.repo.get_refresh_token(sha("rt-a"))
    assert (got.revoked_at, got.is_active, got.scopes, got.resource) == (None, True, [], None)


@pytest.mark.parametrize("field", ["token_hash", "client_id", "user_id", "account_id", "expires_at"])
async def test_legacy_refresh_token_missing_a_required_field_raises(store, field):
    store.docs(REFRESH)[sha("rt-a")] = strip(FirestoreMCPClientRepository._refresh_to_fs(refresh()), **{field: DROP})
    with pytest.raises(KeyError):
        await store.repo.get_refresh_token(sha("rt-a"))


async def test_revoke_sets_revoked_at_to_now_and_keeps_every_other_field(store):
    await store.repo.save_refresh_token(refresh())
    await store.repo.save_refresh_token(refresh("rt-b"))
    before = dict(store.docs(REFRESH)[sha("rt-a")])
    started = datetime.now(timezone.utc)
    await store.repo.revoke_refresh_token(sha("rt-a"))
    doc = store.docs(REFRESH)[sha("rt-a")]
    assert started <= doc["revoked_at"] <= datetime.now(timezone.utc)
    assert {k: v for k, v in doc.items() if k != "revoked_at"} == {k: v for k, v in before.items() if k != "revoked_at"}
    assert not (await store.repo.get_refresh_token(sha("rt-a"))).is_active
    assert (await store.repo.get_refresh_token(sha("rt-b"))).is_active


async def test_revoke_of_an_absent_hash_is_a_no_op(store):
    await store.repo.revoke_refresh_token(sha("nope"))
    assert store.docs(REFRESH) == {}


async def test_revoking_again_moves_revoked_at_to_now(store):
    await store.repo.save_refresh_token(refresh(revoked_at=PAST - timedelta(days=1)))
    started = datetime.now(timezone.utc)
    await store.repo.revoke_refresh_token(sha("rt-a"))
    assert started <= store.docs(REFRESH)[sha("rt-a")]["revoked_at"] <= datetime.now(timezone.utc)


@pytest.mark.xfail(strict=True, raises=AssertionError, reason=(
    "known defect: get and revoke are separate calls and revoke does not say whether it revoked an active token, so "
    "two concurrent exchanges of one refresh token both get new tokens — src/services/mcp_authorization_service.py:"
    "309-332, src/adapters/firestore_mcp_client_repository.py:179-184 (P6 T1 H2)"))
async def test_of_concurrent_exchanges_of_one_refresh_token_exactly_one_succeeds(store):
    await store.repo.save_refresh_token(refresh())
    store.db.collection(REFRESH).yield_on_get = True      # both exchanges read the token before either revokes it
    results = await asyncio.gather(store.service.rotate_refresh_token("rt-a"), store.service.rotate_refresh_token("rt-a"),
                                   return_exceptions=True)
    assert sum(not isinstance(r, Exception) for r in results) == 1, results
    assert sum(isinstance(r, MCPInvalidGrant) for r in results) == 1
    assert len(store.docs(REFRESH)) == 2                  # the old one, revoked, and one new token


# ------------------------------------------------------------------- the token endpoint over the real store

async def test_a_code_exchange_consumes_the_code_and_stores_a_refresh_token_of_the_codes_owner(store):
    await store.repo.save_client(client("cli-a"))
    await store.repo.save_auth_code(auth_code(client_id="cli-a", user_id="usr-1", account_id="acc-1"))
    status, body = await store.exchange_code("cli-a", "code-a")
    assert status == 200 and body["refresh_token"] and body["access_token"]
    assert store.docs(CODES) == {}
    stored = store.docs(REFRESH)[sha(body["refresh_token"])]
    assert (stored["client_id"], stored["user_id"], stored["account_id"], stored["revoked_at"]) == (
        "cli-a", "usr-1", "acc-1", None)
    status, body = await store.exchange_code("cli-a", "code-a")
    assert (status, body["error"]) == (400, "invalid_grant")


async def test_a_code_of_another_client_is_refused_and_stays(store):
    await store.repo.save_client(client("cli-a"))
    await store.repo.save_client(client("cli-b"))
    await store.repo.save_auth_code(auth_code(client_id="cli-a"))
    status, body = await store.exchange_code("cli-b", "code-a")
    assert (status, body["error"]) == (400, "invalid_grant")
    assert "code-a" in store.docs(CODES) and store.docs(REFRESH) == {}


async def test_an_expired_code_is_refused_and_stays(store):
    await store.repo.save_client(client("cli-a"))
    await store.repo.save_auth_code(auth_code(expires_at=PAST))
    status, body = await store.exchange_code("cli-a", "code-a")
    assert (status, body["error"], body["error_description"]) == (400, "invalid_grant", "authorization code has expired")
    assert "code-a" in store.docs(CODES) and store.docs(REFRESH) == {}


async def test_a_refresh_exchange_rotates_the_token(store):
    await store.repo.save_client(client("cli-a"))
    await store.repo.save_refresh_token(refresh(user_id="usr-1", account_id="acc-1"))
    status, body = await store.exchange_refresh("cli-a", "rt-a")
    assert status == 200
    assert store.docs(REFRESH)[sha("rt-a")]["revoked_at"] is not None
    new = store.docs(REFRESH)[sha(body["refresh_token"])]
    assert (new["client_id"], new["user_id"], new["account_id"], new["revoked_at"]) == ("cli-a", "usr-1", "acc-1", None)
    status, body = await store.exchange_refresh("cli-a", "rt-a")
    assert (status, body["error"]) == (400, "invalid_grant")


async def test_a_refresh_token_of_another_client_is_refused(store):
    await store.repo.save_client(client("cli-a"))
    await store.repo.save_client(client("cli-b"))
    await store.repo.save_refresh_token(refresh(client_id="cli-a"))
    status, body = await store.exchange_refresh("cli-b", "rt-a")
    assert (status, body["error"]) == (400, "invalid_grant")
    assert list(store.docs(REFRESH)) == [sha("rt-a")] and store.docs(REFRESH)[sha("rt-a")]["revoked_at"] is None


async def test_an_expired_refresh_token_is_refused(store):
    await store.repo.save_client(client("cli-a"))
    await store.repo.save_refresh_token(refresh(expires_at=PAST))
    status, body = await store.exchange_refresh("cli-a", "rt-a")
    assert (status, body["error"], body["error_description"]) == (400, "invalid_grant", "refresh token has expired")
    assert list(store.docs(REFRESH)) == [sha("rt-a")] and store.docs(REFRESH)[sha("rt-a")]["revoked_at"] is None


async def test_a_revoked_refresh_token_is_refused(store):
    await store.repo.save_client(client("cli-a"))
    await store.repo.save_refresh_token(refresh(revoked_at=PAST))
    status, body = await store.exchange_refresh("cli-a", "rt-a")
    assert (status, body["error"]) == (400, "invalid_grant")
    assert list(store.docs(REFRESH)) == [sha("rt-a")]


async def test_an_unknown_refresh_token_is_refused(store):
    await store.repo.save_client(client("cli-a"))
    status, body = await store.exchange_refresh("cli-a", "rt-unknown")
    assert (status, body["error"]) == (400, "invalid_grant")
    assert store.docs(REFRESH) == {}


# --------------------------------------------------------------------------------------------------- errors

class _Down:
    async def get(self, *a, **kw):
        raise RuntimeError("backend down")

    async def set(self, *a, **kw):
        raise RuntimeError("backend down")

    async def update(self, *a, **kw):
        raise RuntimeError("backend down")

    async def delete(self, *a, **kw):
        raise RuntimeError("backend down")


@pytest.mark.parametrize("collection,call", [
    ("_clients", lambda repo: repo.save_client(client())),
    ("_clients", lambda repo: repo.get_client("cli-a")),
    ("_codes", lambda repo: repo.save_auth_code(auth_code())),
    ("_codes", lambda repo: repo.get_auth_code("code-a")),
    ("_codes", lambda repo: repo.consume_auth_code("code-a")),
    ("_refresh", lambda repo: repo.save_refresh_token(refresh())),
    ("_refresh", lambda repo: repo.get_refresh_token(sha("rt-a"))),
    ("_refresh", lambda repo: repo.revoke_refresh_token(sha("rt-a"))),
])
async def test_a_backend_error_propagates(store, monkeypatch, collection, call):
    await store.repo.save_client(client())
    await store.repo.save_auth_code(auth_code())
    await store.repo.save_refresh_token(refresh())
    monkeypatch.setattr(getattr(store.repo, collection), "document", lambda _doc_id: _Down())
    with pytest.raises(RuntimeError, match="backend down"):
        await call(store.repo)


@pytest.mark.parametrize("collection,exchange", [
    (CODES, lambda store: store.exchange_code("cli-a", "code-a")),
    (REFRESH, lambda store: store.exchange_refresh("cli-a", "rt-a")),
])
async def test_a_backend_error_fails_the_token_endpoint_and_never_grants(store, monkeypatch, collection, exchange):
    await store.repo.save_client(client())
    await store.repo.save_auth_code(auth_code())
    await store.repo.save_refresh_token(refresh())
    before = {c: {k: dict(v) for k, v in store.docs(c).items()} for c in (CODES, REFRESH)}
    monkeypatch.setattr(store.db.collection(collection), "document", lambda _doc_id: _Down())
    with pytest.raises(RuntimeError, match="backend down"):
        await exchange(store)
    assert {c: store.docs(c) for c in (CODES, REFRESH)} == before
