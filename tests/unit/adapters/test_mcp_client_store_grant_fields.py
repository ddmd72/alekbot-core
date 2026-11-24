"""The task queue's grant fields in the MCP OAuth store (EXTERNAL_TASK_QUEUE_RFC §5.4 "OAuth", §5.6): a code's
`worker_id`, and a refresh token's `worker_id`, `granted_at` and `issued_at`, round-trip through
`FirestoreMCPClientRepository`; documents stored before them load with each read as None."""
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from src.adapters.firestore_mcp_client_repository import FirestoreMCPClientRepository
from src.domain.mcp import MCPAuthCode, MCPRefreshToken
from src.domain.secret_digest import keyed_digest
from tests.support.aes_gcm_cipher import AesGcmTestCipher

INDEXES_FILE = Path(__file__).resolve().parents[3] / "config" / "firestore.indexes.json"
CLIENTS, CODES, REFRESH = "eval_mcp_oauth_clients", "eval_mcp_auth_codes", "eval_mcp_refresh_tokens"
NOW = datetime.now(timezone.utc).replace(microsecond=0)
DIGEST_KEY = bytes(range(32))
TASKS = "https://dev.example/tasks-mcp"


@pytest.fixture
def store(kit_db):
    db = kit_db(indexes=INDEXES_FILE, groups={
        CLIENTS: "development_mcp_oauth_clients", CODES: "development_mcp_auth_codes",
        REFRESH: "development_mcp_refresh_tokens"})
    repo = FirestoreMCPClientRepository(db, SimpleNamespace(
        mcp_oauth_clients_collection=CLIENTS, mcp_auth_codes_collection=CODES,
        mcp_refresh_tokens_collection=REFRESH), cipher=AesGcmTestCipher(), digest_key=DIGEST_KEY)
    return SimpleNamespace(db=db, repo=repo)


def code(**over) -> MCPAuthCode:
    fields = dict(code="code-a", client_id="cli-a", user_id="usr-1", account_id="acc-1",
                  redirect_uri="https://claude.ai/api/mcp/auth_callback", code_challenge="c" * 43, resource=TASKS,
                  scopes=["external_tasks"], expires_at=NOW + timedelta(minutes=10))
    fields.update(over)
    return MCPAuthCode(**fields)


def refresh(**over) -> MCPRefreshToken:
    fields = dict(token_hash="h" * 64, client_id="cli-a", user_id="usr-1", account_id="acc-1",
                  scopes=["external_tasks"], resource=TASKS, expires_at=NOW + timedelta(days=30), grant_id="g" * 22)
    fields.update(over)
    return MCPRefreshToken(**fields)


async def test_a_codes_worker_round_trips(store):
    await store.repo.save_auth_code(code(worker_id="w1"))
    doc = store.db.collection(CODES).docs[keyed_digest(DIGEST_KEY, "mcp_code", "code-a")]
    assert doc["worker_id"] == "w1"
    assert (await store.repo.get_auth_code("code-a")).worker_id == "w1"
    assert (await store.repo.consume_auth_code("code-a")).worker_id == "w1"


async def test_a_code_stored_before_the_worker_field_loads_with_none(store):
    await store.repo.save_auth_code(code())
    doc_id = keyed_digest(DIGEST_KEY, "mcp_code", "code-a")
    store.db.collection(CODES).docs[doc_id].pop("worker_id")
    assert (await store.repo.get_auth_code("code-a")).worker_id is None


async def test_a_refresh_tokens_worker_and_times_round_trip(store):
    granted = NOW - timedelta(days=3)
    saved = refresh(worker_id="w1", granted_at=granted, issued_at=NOW)
    await store.repo.save_refresh_token(saved)
    doc = store.db.collection(REFRESH).docs["h" * 64]
    assert (doc["worker_id"], doc["granted_at"], doc["issued_at"]) == ("w1", granted, NOW)
    got = await store.repo.get_refresh_token("h" * 64)
    assert got == saved
    assert got.granted_at.utcoffset() == timedelta(0) and got.issued_at.utcoffset() == timedelta(0)


async def test_a_refresh_token_stored_before_the_new_fields_loads_with_none(store):
    await store.repo.save_refresh_token(refresh())
    doc = store.db.collection(REFRESH).docs["h" * 64]
    for field in ("worker_id", "granted_at", "issued_at"):
        doc.pop(field)
    got = await store.repo.get_refresh_token("h" * 64)
    assert (got.worker_id, got.granted_at, got.issued_at, got.grant_id) == (None, None, None, "g" * 22)
