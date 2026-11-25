"""`FirestoreMCPClientRepository.list_resource_grants` (MCP_MEMORY_SUBMISSIONS_RFC §10.2): the live refresh tokens of
one user's grants of one resource, on the evaluating Firestore fake the other grant-field tests use."""
from datetime import timedelta

from tests.unit.adapters.test_mcp_client_store_grant_fields import NOW, REFRESH, store, tok  # noqa: F401

INBOX = "https://dev.example/memory-inbox-mcp"
TASKS = "https://dev.example/tasks-mcp"


async def test_list_resource_grants_returns_that_users_live_tokens_of_that_resource_only(store):  # noqa: F811
    await store.repo.save_refresh_token(tok(1, resource=INBOX, grant_id="ga", scopes=["memory_submit"]))
    await store.repo.save_refresh_token(tok(2, resource=INBOX, grant_id="gb", scopes=["memory_submit"]))
    await store.repo.save_refresh_token(tok(3, resource=INBOX, grant_id="gc", revoked_at=NOW))
    await store.repo.save_refresh_token(tok(4, resource=INBOX, grant_id="gd", expires_at=NOW - timedelta(seconds=1)))
    await store.repo.save_refresh_token(tok(5, resource=TASKS, grant_id="ge"))
    await store.repo.save_refresh_token(tok(6, resource=INBOX, grant_id="gf", user_id="usr-2", account_id="acc-2"))

    got = await store.repo.list_resource_grants("usr-1", INBOX, NOW)

    assert sorted(t.grant_id for t in got) == ["ga", "gb"]
    assert await store.repo.list_resource_grants("usr-1", "https://dev.example/nothing", NOW) == []
