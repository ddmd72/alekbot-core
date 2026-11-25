"""`MCPAuthorizationService.resource_grants` (MCP_MEMORY_SUBMISSIONS_RFC §10.2): the memory inbox's connected apps,
shaped like the task queue's worker grants: the other resource's grants and tokens without the resource's scope are not
listed, the latest token of a grant wins, a gone client shows its id."""
from datetime import datetime, timedelta, timezone

from src.domain.mcp import MCPClient, MCPRefreshToken
from src.domain.mcp_resource import MCPResource
from src.domain.mcp_worker_grant import WorkerGrant
from tests.unit.services.test_mcp_tasks_grant import FAR, FACTS, REDIRECT, TASKS, World

T0 = datetime(2026, 11, 20, 10, 0, tzinfo=timezone.utc)
INBOX = "https://mcp.example.test/memory-inbox-mcp"
SCOPE_OF = {MCPResource.FACTS: "user_context", MCPResource.DRIVE: "drive_read", MCPResource.TASKS: "external_tasks",
            MCPResource.MEMORY_INBOX: "memory_submit"}


def token(n: int, grant_id, client_id="c1", resource=INBOX, scopes=("memory_submit",), granted_at=T0, issued_at=T0,
          **over) -> MCPRefreshToken:
    return MCPRefreshToken(token_hash=f"{n:064d}", client_id=client_id, user_id="usr-1", account_id="acc-1",
                           scopes=list(scopes), resource=resource, expires_at=FAR, grant_id=grant_id,
                           granted_at=granted_at, issued_at=issued_at, **over)


async def test_only_the_inboxs_own_grants_with_its_scope_one_entry_each_oldest_first():
    w = World(scope_of=SCOPE_OF)
    await w.repo.save_client(MCPClient(client_id="c1", client_secret="s", client_name="Desk assistant",
                                       redirect_uris=[REDIRECT], created_at=T0))
    later = T0 + timedelta(hours=2)
    await w.repo.save_refresh_token(token(1, "gb", granted_at=later, issued_at=later))
    await w.repo.save_refresh_token(token(2, "ga", issued_at=T0 + timedelta(hours=1)))
    await w.repo.save_refresh_token(token(3, "ga", issued_at=T0 + timedelta(hours=3)))
    await w.repo.save_refresh_token(token(4, "gc", client_id="gone", granted_at=None, issued_at=None))
    await w.repo.save_refresh_token(token(5, None))                                      # no grant id: not listed
    await w.repo.save_refresh_token(token(6, "gd", resource=TASKS, scopes=("external_tasks",), worker_id="w1"))
    await w.repo.save_refresh_token(token(7, "ge", scopes=("user_context",)))            # not the inbox's scope
    await w.repo.save_refresh_token(token(8, "gf", resource=FACTS, scopes=("user_context",)))

    assert await w.service.resource_grants("usr-1", MCPResource.MEMORY_INBOX) == [
        WorkerGrant(grant_id="gc", client_id="gone", client_name="gone", granted_at=None, last_refresh=None),
        WorkerGrant(grant_id="ga", client_id="c1", client_name="Desk assistant", granted_at=T0,
                    last_refresh=T0 + timedelta(hours=3)),
        WorkerGrant(grant_id="gb", client_id="c1", client_name="Desk assistant", granted_at=later, last_refresh=later),
    ]
    assert await w.service.resource_grants("usr-2", MCPResource.MEMORY_INBOX) == []


async def test_a_resource_the_server_does_not_serve_has_no_grants():
    w = World(scope_of={MCPResource.FACTS: "user_context", MCPResource.DRIVE: "drive_read"})
    await w.repo.save_refresh_token(token(1, "ga"))

    assert await w.service.resource_grants("usr-1", MCPResource.MEMORY_INBOX) == []
