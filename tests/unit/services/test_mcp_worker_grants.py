"""`MCPAuthorizationService.worker_grants`, `revoke_grant`, `revoke_worker_grants` (EXTERNAL_TASK_QUEUE_RFC §5.6): one
entry per live grant of the worker, with the registered client's name, the grant time and the live token's issue time."""
from datetime import datetime, timedelta, timezone

from src.domain.mcp import MCPClient, MCPRefreshToken
from src.domain.mcp_worker_grant import WorkerGrant
from tests.unit.services.test_mcp_tasks_grant import FAR, REDIRECT, TASKS, World

T0 = datetime(2026, 11, 20, 10, 0, tzinfo=timezone.utc)


def token(n: int, grant_id, client_id="c1", granted_at=T0, issued_at=T0, **over) -> MCPRefreshToken:
    return MCPRefreshToken(token_hash=f"{n:064d}", client_id=client_id, user_id="usr-1", account_id="acc-1",
                           scopes=["external_tasks"], resource=TASKS, expires_at=FAR, grant_id=grant_id,
                           worker_id="w1", granted_at=granted_at, issued_at=issued_at, **over)


async def test_one_entry_per_grant_with_the_client_name_oldest_first():
    w = World()
    await w.repo.save_client(MCPClient(client_id="c1", client_secret="s", client_name="Claude",
                                       redirect_uris=[REDIRECT], created_at=T0))
    later = T0 + timedelta(hours=2)
    await w.repo.save_refresh_token(token(1, "gb", granted_at=later, issued_at=later))
    await w.repo.save_refresh_token(token(2, "ga", issued_at=T0 + timedelta(hours=1)))
    await w.repo.save_refresh_token(token(3, "ga", issued_at=T0 + timedelta(hours=3)))   # a race left two live
    await w.repo.save_refresh_token(token(6, "ga", issued_at=T0 + timedelta(hours=2)))   # read after the newest
    await w.repo.save_refresh_token(token(4, "gc", client_id="gone", granted_at=None, issued_at=None))
    await w.repo.save_refresh_token(token(5, None))                                       # no grant id: not listed
    assert await w.service.worker_grants("usr-1", "w1") == [
        WorkerGrant(grant_id="gc", client_id="gone", client_name="gone", granted_at=None, last_refresh=None),
        WorkerGrant(grant_id="ga", client_id="c1", client_name="Claude", granted_at=T0,
                    last_refresh=T0 + timedelta(hours=3)),
        WorkerGrant(grant_id="gb", client_id="c1", client_name="Claude", granted_at=later, last_refresh=later),
    ]


async def test_revoke_grant_and_revoke_worker_grants_count_the_tokens_they_revoked():
    w = World()
    await w.repo.save_refresh_token(token(1, "ga"))
    await w.repo.save_refresh_token(token(2, "gb"))
    await w.repo.save_refresh_token(token(3, "gc"))
    assert await w.service.revoke_grant("usr-1", "ga") == 1
    assert [g.grant_id for g in await w.service.worker_grants("usr-1", "w1")] == ["gb", "gc"]
    assert await w.service.revoke_worker_grants("usr-1", "w1") == 2
    assert await w.service.worker_grants("usr-1", "w1") == []
