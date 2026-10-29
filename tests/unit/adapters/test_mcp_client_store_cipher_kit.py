"""MCP store at rest (SECRETS_AT_REST_RFC §2.2, §2.5, §8 Verify-only, invariants I1, I2, I4): `FirestoreMCPClientRepository`
over the evaluating Firestore fake (`EvalDb`) and the AES-GCM test cipher, never a mock.

Client secrets are ciphertext bound to `mcp_clients/{client_id}`; authorization codes are stored under their keyed digest
(purpose `mcp_code`) with no raw copy in any field; refresh tokens keep their unkeyed sha256 doc id. Assertions are on
the raw stored documents, on what readers return and, for issue + exchange, on the real chain: the `mcp` SDK's
`TokenHandler` -> `MCPSdkOAuthProvider` -> `MCPAuthorizationService` -> the adapter.
"""
import base64
import hashlib
import json
import logging
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any, Iterator
from urllib.parse import parse_qs, urlencode, urlparse

import pytest
from cryptography.exceptions import InvalidTag
from mcp.server.auth.handlers.token import TokenHandler
from mcp.server.auth.middleware.client_auth import ClientAuthenticator
from starlette.requests import Request

from src.adapters.firestore_mcp_client_repository import FirestoreMCPClientRepository
from src.composition.mcp_sdk_oauth_provider import MCPSdkOAuthProvider
from src.domain.mcp import MCPAuthCode, MCPClient
from src.domain.secret_digest import keyed_digest
from src.services.mcp_authorization_service import AuthorizationParams, MCPAuthorizationService
from tests.support.aes_gcm_cipher import AesGcmTestCipher
from tests.support.firestore_eval import EvalDb

CLIENTS, CODES, REFRESH = "eval_mcp_clients", "eval_mcp_auth_codes", "eval_mcp_refresh_tokens"
DIGEST_KEY = bytes.fromhex("0f1e2d3c4b5a69788796a5b4c3d2e1f000112233445566778899aabbccddeeff")
NOW = datetime.now(timezone.utc).replace(microsecond=0)
SOON = NOW + timedelta(minutes=10)
REDIRECT = "https://claude.ai/api/mcp/auth_callback"
RESOURCE = "https://dev.example/mcp"
VERIFIER = "v" * 64
CHALLENGE = base64.urlsafe_b64encode(hashlib.sha256(VERIFIER.encode()).digest()).decode().rstrip("=")
ENV = SimpleNamespace(mcp_oauth_clients_collection=CLIENTS, mcp_auth_codes_collection=CODES,
                      mcp_refresh_tokens_collection=REFRESH)


class BrokenCipher(AesGcmTestCipher):
    """A cipher whose backend fails (a KMS outage): both calls raise."""

    async def encrypt(self, plaintext: str, *, aad: str) -> str:
        raise RuntimeError("cipher backend unavailable")

    async def decrypt(self, ciphertext: str, *, aad: str) -> str:
        raise RuntimeError("cipher backend unavailable")


class Store:
    def __init__(self, cipher: Any = None) -> None:
        self.db = EvalDb()
        self.cipher = cipher or AesGcmTestCipher()
        self.repo = FirestoreMCPClientRepository(self.db, ENV, cipher=self.cipher, digest_key=DIGEST_KEY)
        self.service = MCPAuthorizationService(
            self.repo, jwt_secret="kit-secret-that-is-at-least-32-characters", mcp_resource_uri=RESOURCE,
            consent_base_url="https://dev.example/mcp/consent", allowed_redirect_hosts=("claude.ai",))
        provider = MCPSdkOAuthProvider(self.service)
        self.handler = TokenHandler(provider=provider, client_authenticator=ClientAuthenticator(provider))

    def docs(self, collection: str) -> dict:
        return self.db.collection(collection).docs

    async def token(self, **form: str):
        body = urlencode(form).encode()

        async def receive():
            return {"type": "http.request", "body": body, "more_body": False}

        scope = {"type": "http", "method": "POST", "path": "/token", "query_string": b"",
                 "headers": [(b"content-type", b"application/x-www-form-urlencoded")]}
        response = await self.handler.handle(Request(scope, receive))
        return response.status_code, json.loads(response.body)


@pytest.fixture
def store() -> Store:
    return Store()


def client(client_id: str = "client-a", **over: Any) -> MCPClient:
    fields = dict(client_id=client_id, client_secret=f"secret-{client_id}", client_name="Claude",
                  redirect_uris=[REDIRECT], scope="user_context", created_at=NOW)
    fields.update(over)
    return MCPClient(**fields)


def auth_code(code: str = "code-1", client_id: str = "client-a") -> MCPAuthCode:
    return MCPAuthCode(code=code, client_id=client_id, user_id="user-a", account_id="account-a", redirect_uri=REDIRECT,
                       code_challenge=CHALLENGE, code_challenge_method="S256", resource=RESOURCE,
                       scopes=["user_context"], expires_at=SOON)


def values(doc: Any) -> Iterator[Any]:
    """Every leaf value of a stored document, nested lists and maps included."""
    if isinstance(doc, dict):
        for value in doc.values():
            yield from values(value)
    elif isinstance(doc, (list, tuple)):
        for value in doc:
            yield from values(value)
    else:
        yield doc


def holds(doc: dict, secret: str) -> bool:
    return any(isinstance(v, str) and secret in v for v in values(doc))


# ------------------------------------------------------------------------------------------- client secrets

async def test_no_stored_field_holds_the_client_secret_and_it_reads_back(store):
    await store.repo.save_client(client())

    stored = store.docs(CLIENTS)["client-a"]
    assert not holds(stored, "secret-client-a")
    assert stored["client_secret"] and stored["client_id"] == "client-a"
    assert await store.repo.get_client("client-a") == client()


async def test_the_secret_is_bound_to_its_record(store):
    await store.repo.save_client(client())
    stored = store.docs(CLIENTS)["client-a"]["client_secret"]

    assert await store.cipher.decrypt(stored, aad="mcp_clients/client-a") == "secret-client-a"
    with pytest.raises(InvalidTag):
        await store.cipher.decrypt(stored, aad="mcp_clients/client-b")


async def test_a_ciphertext_copied_to_another_clients_doc_raises(store):
    await store.repo.save_client(client("client-a"))
    await store.repo.save_client(client("client-b"))
    store.docs(CLIENTS)["client-b"]["client_secret"] = store.docs(CLIENTS)["client-a"]["client_secret"]

    with pytest.raises(InvalidTag):
        await store.repo.get_client("client-b")
    assert await store.repo.get_client("client-a") == client("client-a")


async def test_a_whole_doc_copied_under_another_id_raises(store, caplog):
    await store.repo.save_client(client("client-a"))
    await store.repo.save_client(client("client-b"))
    store.docs(CLIENTS)["client-a"] = dict(store.docs(CLIENTS)["client-b"])

    with caplog.at_level(logging.ERROR), pytest.raises(ValueError, match="another client id"):
        await store.repo.get_client("client-a")
    assert any("client-a" in r.getMessage() and "refused" in r.getMessage() for r in caplog.records)
    assert not any("secret-client-b" in r.getMessage() for r in caplog.records)
    assert await store.repo.get_client("client-b") == client("client-b")


async def test_a_whole_doc_copied_with_its_body_id_rewritten_does_not_decrypt(store):
    await store.repo.save_client(client("client-a"))
    await store.repo.save_client(client("client-b"))
    store.docs(CLIENTS)["client-a"] = {**store.docs(CLIENTS)["client-b"], "client_id": "client-a"}

    with pytest.raises(InvalidTag):
        await store.repo.get_client("client-a")


async def test_a_public_client_stores_and_reads_no_secret(store):
    public = client(client_secret=None, token_endpoint_auth_method="none")
    await store.repo.save_client(public)

    assert store.docs(CLIENTS)["client-a"]["client_secret"] is None
    assert await store.repo.get_client("client-a") == public
    assert store.cipher.encrypts == 0 and store.cipher.decrypts == 0


async def test_a_cipher_failure_on_save_propagates_and_stores_nothing():
    broken = Store(BrokenCipher())
    with pytest.raises(RuntimeError, match="cipher backend unavailable"):
        await broken.repo.save_client(client())
    assert broken.docs(CLIENTS) == {}


async def test_a_cipher_failure_on_read_propagates_never_none(store):
    await store.repo.save_client(client())
    reader = FirestoreMCPClientRepository(store.db, ENV, cipher=BrokenCipher(), digest_key=DIGEST_KEY)

    with pytest.raises(RuntimeError, match="cipher backend unavailable"):
        await reader.get_client("client-a")


async def test_another_key_cannot_read_a_stored_secret(store):
    await store.repo.save_client(client())
    reader = FirestoreMCPClientRepository(store.db, ENV, cipher=AesGcmTestCipher(), digest_key=DIGEST_KEY)

    with pytest.raises(InvalidTag):
        await reader.get_client("client-a")


# ------------------------------------------------------------------------------------- authorization codes

async def test_a_code_is_stored_under_its_keyed_digest_with_no_raw_copy(store):
    await store.repo.save_auth_code(auth_code("code-1"))

    digest = keyed_digest(DIGEST_KEY, "mcp_code", "code-1")
    assert set(store.docs(CODES)) == {digest}
    stored = store.docs(CODES)[digest]
    assert "code" not in stored and not holds(stored, "code-1")
    assert stored["client_id"] == "client-a" and stored["user_id"] == "user-a"


async def test_a_code_reads_back_from_its_presented_value(store):
    await store.repo.save_auth_code(auth_code("code-1"))

    assert await store.repo.get_auth_code("code-1") == auth_code("code-1")
    assert await store.repo.get_auth_code("code-2") is None


async def test_a_code_is_consumed_once_by_its_presented_value(store):
    await store.repo.save_auth_code(auth_code("code-1"))
    await store.repo.save_auth_code(auth_code("code-2"))

    assert await store.repo.consume_auth_code("code-1") == auth_code("code-1")
    assert await store.repo.consume_auth_code("code-1") is None
    assert await store.repo.get_auth_code("code-1") is None
    assert set(store.docs(CODES)) == {keyed_digest(DIGEST_KEY, "mcp_code", "code-2")}


async def test_a_raw_code_doc_id_is_never_read(store):
    legacy = {"code": "code-1", "client_id": "client-a", "user_id": "user-a", "account_id": "account-a",
              "redirect_uri": REDIRECT, "code_challenge": CHALLENGE, "code_challenge_method": "S256",
              "resource": RESOURCE, "scopes": ["user_context"], "expires_at": SOON}
    store.docs(CODES)["code-1"] = dict(legacy)

    assert await store.repo.get_auth_code("code-1") is None
    assert await store.repo.consume_auth_code("code-1") is None
    assert store.docs(CODES) == {"code-1": legacy}


async def test_a_code_digested_under_another_key_is_not_found(store):
    await store.repo.save_auth_code(auth_code("code-1"))
    other = FirestoreMCPClientRepository(store.db, ENV, cipher=store.cipher, digest_key=bytes(32))

    assert await other.get_auth_code("code-1") is None


# ------------------------------------------------------------------------------- the real chain, with logs

async def _issue(store: Store) -> str:
    await store.service.save_client(client())
    redirect = await store.service.build_consent_redirect(client(), AuthorizationParams(
        redirect_uri=REDIRECT, code_challenge=CHALLENGE, scopes=["user_context"], state="state-1", resource=RESOURCE))
    consent_jwt = parse_qs(urlparse(redirect).query)["req"][0]
    code, _ = await store.service.issue_auth_code_for_consent(consent_jwt, "user-a", "account-a")
    return code


async def test_issue_and_exchange_never_log_or_store_the_code_and_refresh_keeps_sha256(store, caplog):
    caplog.set_level(logging.DEBUG)
    code = await _issue(store)
    assert not any(holds(doc, code) for doc in store.docs(CODES).values())
    assert set(store.docs(CODES)) == {keyed_digest(DIGEST_KEY, "mcp_code", code)}

    status, body = await store.token(grant_type="authorization_code", client_id="client-a",
                                     client_secret="secret-client-a", code=code, redirect_uri=REDIRECT,
                                     code_verifier=VERIFIER)

    assert status == 200, body
    assert store.docs(CODES) == {}
    assert set(store.docs(REFRESH)) == {hashlib.sha256(body["refresh_token"].encode()).hexdigest()}
    for record in caplog.records:
        assert code not in record.getMessage(), record.getMessage()
        assert "secret-client-a" not in record.getMessage(), record.getMessage()


async def test_a_second_exchange_of_one_code_is_refused(store):
    code = await _issue(store)
    form = dict(grant_type="authorization_code", client_id="client-a", client_secret="secret-client-a", code=code,
                redirect_uri=REDIRECT, code_verifier=VERIFIER)
    assert (await store.token(**form))[0] == 200

    status, body = await store.token(**form)

    assert status == 400 and body["error"] == "invalid_grant"
    assert len(store.docs(REFRESH)) == 1


async def test_the_token_endpoint_authenticates_the_client_against_the_decrypted_secret(store):
    code = await _issue(store)
    status, body = await store.token(grant_type="authorization_code", client_id="client-a",
                                     client_secret="secret-wrong", code=code, redirect_uri=REDIRECT,
                                     code_verifier=VERIFIER)

    assert status == 401, body
    assert store.docs(REFRESH) == {} and len(store.docs(CODES)) == 1
