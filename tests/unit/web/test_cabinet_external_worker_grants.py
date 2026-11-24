"""The Cabinet's view of the task queue's OAuth grants (EXTERNAL_TASK_QUEUE_RFC §5.4 "OAuth", §5.6): each worker lists
its live grants (client name, granted time, last refresh), Revoke ends one grant, and removing a worker revokes its
grants. The routes over the real `ExternalTaskService` of `test_cabinet_external_workers.py`'s `Site` and the real
`MCPAuthorizationService` over `FirestoreMCPClientRepository` on `EvalDb`; grants are made through the service's own
consent and code exchange.

Alex Doe is `usr-1` (account `acc-1`); `usr-2` is another user with the flag.
"""
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from quart import Quart

from src.adapters.firestore_mcp_client_repository import FirestoreMCPClientRepository
from src.composition.mcp_setup import SCOPE_OF
from src.domain.mcp import MCPClient
from src.services.mcp_authorization_service import AuthorizationParams, MCPAuthorizationService
from src.web.user_cabinet_app import create_user_cabinet_blueprint
from tests.support.aes_gcm_cipher import AesGcmTestCipher
from tests.support.firestore_eval import EvalDb
from tests.support.mcp_gate_fakes import FakeWhitelistRepo
from tests.unit.web.test_cabinet_external_workers import BODY, Site

FACTS = "https://mcp.example.test/mcp"
TASKS = "https://mcp.example.test/tasks-mcp"
REDIRECT = "https://claude.ai/api/mcp/auth_callback"
ENV = SimpleNamespace(mcp_oauth_clients_collection="c", mcp_auth_codes_collection="a", mcp_refresh_tokens_collection="r")


class GrantSite(Site):
    def __init__(self, with_mcp: bool = True) -> None:
        super().__init__()
        self.mcp_db = EvalDb()
        self.repo = FirestoreMCPClientRepository(self.mcp_db, ENV, cipher=AesGcmTestCipher(),
                                                 digest_key=bytes(range(32)))
        self.mcp = MCPAuthorizationService(
            self.repo, jwt_secret="grants-jwt-secret-0123456789abcdef-xyz", mcp_resource_uri=FACTS,
            drive_resource_uri="https://mcp.example.test/drive-mcp", scope_of=SCOPE_OF,
            consent_base_url="https://mcp.example.test/mcp/consent", allowed_redirect_hosts=("claude.ai",),
            user_repo=self.users, whitelist_repo=FakeWhitelistRepo(emails=("usr-1@example.com", "usr-2@example.com")))
        self.app = Quart("grants")
        self.app.register_blueprint(create_user_cabinet_blueprint(
            invite_service=MagicMock(), cabinet_sessions=self.kit.verifier, user_repo=MagicMock(),
            fact_repo=MagicMock(), embedding_service=MagicMock(), external_tasks=self.service,
            mcp_authorization=self.mcp if with_mcp else None))
        self.http = self.app.test_client()

    async def oauth_client(self, client_id: str, name: str) -> None:
        await self.repo.save_client(MCPClient(client_id=client_id, client_secret="s", client_name=name,
                                              redirect_uris=[REDIRECT], scope="external_tasks",
                                              created_at=datetime.now(timezone.utc)))

    async def grant(self, client_id: str, user_id: str, account_id: str, worker_id: str):
        """A grant made through consent and code exchange: the issued tokens."""
        client = await self.repo.get_client(client_id)
        url = await self.mcp.build_consent_redirect(client, AuthorizationParams(
            redirect_uri=REDIRECT, code_challenge="c" * 43, scopes=[], resource=TASKS))
        code, _ = await self.mcp.issue_auth_code_for_consent(url.split("req=", 1)[1], user_id=user_id,
                                                             account_id=account_id, worker_id=worker_id)
        return await self.mcp.finalize_auth_code(code)

    async def grant_id(self, issued) -> str:
        return self.mcp.verify_access_token(issued.access_token, resource=TASKS).grant_id


@pytest.fixture
async def site():
    s = GrantSite()
    s.alex = await s.user("usr-1", "acc-1")
    s.other = await s.user("usr-2", "acc-2")
    await s.oauth_client("cli-a", "Claude")
    await s.oauth_client("cli-b", "Inspector")
    return s


async def _add(site, auth=None, label="research") -> str:
    status, data = await site.call(auth or site.alex, "POST", "/api/external-workers", dict(BODY, label=label))
    assert status == 200 and data["grants"] == [], data
    return data["worker_id"]


async def test_each_worker_lists_its_own_live_grants_with_client_name_and_times(site):
    research, watch = await _add(site), await _add(site, label="calendar-watch")
    one = await site.grant("cli-a", "usr-1", "acc-1", research)
    two = await site.grant("cli-b", "usr-1", "acc-1", research)
    await site.mcp.rotate_refresh_token(two.refresh_token)
    await site.grant("cli-a", "usr-1", "acc-1", watch)
    status, data = await site.call(site.alex, "GET", "/api/external-workers")
    assert status == 200
    by_label = {w["label"]: w["grants"] for w in data["workers"]}
    assert sorted(g["client_name"] for g in by_label["research"]) == ["Claude", "Inspector"]
    assert [g["client_name"] for g in by_label["calendar-watch"]] == ["Claude"]
    [claude] = [g for g in by_label["research"] if g["client_name"] == "Claude"]
    assert set(claude) == {"grant_id", "client_name", "granted_at", "last_refresh"}
    assert claude["grant_id"] == await site.grant_id(one)
    [inspector] = [g for g in by_label["research"] if g["client_name"] == "Inspector"]
    assert datetime.fromisoformat(inspector["last_refresh"]) >= datetime.fromisoformat(inspector["granted_at"])
    assert one.refresh_token not in "".join(site.bodies) and one.access_token not in "".join(site.bodies)


async def test_a_revoked_or_expired_grant_is_not_listed(site):
    research = await _add(site)
    revoked = await site.grant("cli-a", "usr-1", "acc-1", research)
    await site.mcp.revoke_refresh_token_value(revoked.refresh_token)
    expired = await site.grant("cli-b", "usr-1", "acc-1", research)
    token = await site.repo.get_refresh_token(site.mcp._hash_token(expired.refresh_token))
    await site.repo.save_refresh_token(token.model_copy(
        update={"expires_at": datetime.now(timezone.utc) - timedelta(seconds=1)}))
    _, data = await site.call(site.alex, "GET", "/api/external-workers")
    assert data["workers"][0]["grants"] == []


async def test_revoke_ends_one_grant_and_the_grant_check_refuses_it_at_once(site):
    research = await _add(site)
    kept, gone = await site.grant("cli-a", "usr-1", "acc-1", research), await site.grant("cli-b", "usr-1", "acc-1",
                                                                                          research)
    gone_id, kept_id = await site.grant_id(gone), await site.grant_id(kept)
    assert await site.mcp.grant_allowed("usr-1", gone_id) is True
    status, data = await site.call(site.alex, "DELETE", f"/api/external-workers/{research}/grants/{gone_id}")
    assert (status, data) == (200, {"revoked": True})
    assert await site.mcp.grant_allowed("usr-1", gone_id) is False
    assert await site.mcp.grant_allowed("usr-1", kept_id) is True
    _, listed = await site.call(site.alex, "GET", "/api/external-workers")
    assert [g["grant_id"] for g in listed["workers"][0]["grants"]] == [kept_id]
    again = await site.call(site.alex, "DELETE", f"/api/external-workers/{research}/grants/{gone_id}")
    assert again == (404, {"error": "not_found"})


async def test_a_grant_of_another_worker_or_another_user_or_an_unknown_one_is_not_found(site):
    research, watch = await _add(site), await _add(site, label="calendar-watch")
    theirs_worker = await _add(site, site.other)
    on_watch = await site.grant_id(await site.grant("cli-a", "usr-1", "acc-1", watch))
    theirs = await site.grant_id(await site.grant("cli-a", "usr-2", "acc-2", theirs_worker))
    for worker_id, grant_id in ((research, on_watch), (research, theirs), (theirs_worker, theirs),
                                (research, "unknown-grant-id-000000"), ("wrk-404", on_watch)):
        assert await site.call(site.alex, "DELETE", f"/api/external-workers/{worker_id}/grants/{grant_id}") == \
            (404, {"error": "not_found"}), (worker_id, grant_id)
    assert await site.mcp.grant_allowed("usr-1", on_watch) is True
    assert await site.mcp.grant_allowed("usr-2", theirs) is True


async def test_removing_a_worker_revokes_its_grants_and_only_its(site):
    research, watch = await _add(site), await _add(site, label="calendar-watch")
    gone = await site.grant_id(await site.grant("cli-a", "usr-1", "acc-1", research))
    kept = await site.grant_id(await site.grant("cli-a", "usr-1", "acc-1", watch))
    status, _ = await site.call(site.alex, "DELETE", f"/api/external-workers/{research}")
    assert status == 200
    assert await site.mcp.grant_allowed("usr-1", gone) is False
    assert await site.mcp.grant_allowed("usr-1", kept) is True


async def test_the_flag_off_or_a_locked_user_reaches_no_grant(site):
    research = await _add(site)
    grant_id = await site.grant_id(await site.grant("cli-a", "usr-1", "acc-1", research))
    await site.set_flag("usr-1", False)
    path = f"/api/external-workers/{research}/grants/{grant_id}"
    assert await site.call(site.alex, "DELETE", path) == (404, {"error": "not_found"})
    await site.set_flag("usr-1", True)
    await site.lock("usr-1")
    assert await site.call(site.alex, "DELETE", path) == (423, {"error": "account_locked"})
    assert await site.mcp.grant_allowed("usr-1", grant_id) is False     # locked: refused by the grant check anyway


async def test_without_the_authorization_service_a_worker_has_no_grants_and_revoke_is_not_found():
    s = GrantSite(with_mcp=False)
    s.alex = await s.user("usr-1", "acc-1")
    research = await _add(s)
    _, data = await s.call(s.alex, "GET", "/api/external-workers")
    assert data["workers"][0]["grants"] == []
    assert await s.call(s.alex, "DELETE", f"/api/external-workers/{research}/grants/g") == (404, {"error": "not_found"})
    status, _ = await s.call(s.alex, "DELETE", f"/api/external-workers/{research}")
    assert status == 200


async def test_a_server_without_the_task_queue_answers_revoke_grant_as_a_user_without_the_flag():
    s = GrantSite()
    s.app = Quart("no-queue")
    s.app.register_blueprint(create_user_cabinet_blueprint(
        invite_service=MagicMock(), cabinet_sessions=s.kit.verifier, user_repo=MagicMock(), fact_repo=MagicMock(),
        embedding_service=MagicMock(), external_tasks=None, mcp_authorization=s.mcp))
    s.http = s.app.test_client()
    auth = await s.user("usr-1", "acc-1")
    assert await s.call(auth, "DELETE", "/api/external-workers/wrk-1/grants/g") == (404, {"error": "not_found"})
