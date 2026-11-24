"""OAuth access tokens at the tasks resource (EXTERNAL_TASK_QUEUE_RFC §5.4 "OAuth", §5.7): an access token minted for
`/tasks-mcp` and bound to a worker (`wid`) acts as that worker only; its grant is re-checked on every call
(`grant_allowed`), so a grant revoked in the Cabinet or a user taken off the whitelist is refused on the next call while
the access token is still valid; the lock, the flag and the worker are checked as for a key; a submit records
`via=oauth` with the client id. A token with no `wid`, or minted for another resource, is `401` here, and a tasks token
is `401` at `/drive-mcp`.

The world is `tests/support/task_mcp_world.py` with the real `MCPAuthorizationService` over
`FirestoreMCPClientRepository` on `EvalDb`, reading the world's own users and whitelist. Alex Doe is `usr-1` with the
workers `research` and `calendar-watch`; `usr-2` is another user.
"""
import logging
from datetime import datetime, timezone
from types import SimpleNamespace

import httpx
import pytest

from src.adapters.firestore_mcp_client_repository import FirestoreMCPClientRepository
from src.composition import tasks_mcp_setup as tms
from src.composition.drive_mcp_setup import build_drive_mcp_app
from src.composition.mcp_setup import SCOPE_OF
from src.domain.external_task import SubmitVia
from src.domain.mcp import ACCOUNT_LOCKED_TOOL_ERROR, MCPClient
from src.domain.user import InactiveReason
from src.services.mcp_authorization_service import AuthorizationParams, MCPAuthorizationService
from tests.support.aes_gcm_cipher import AesGcmTestCipher
from tests.support.firestore_eval import EvalDb
from tests.support.task_mcp_texts import marker
from tests.support.task_mcp_world import BASE, RESULT, TASKS_URI, USERS, World, err, ok

FACTS = f"{BASE}/mcp"
DRIVE = f"{BASE}/drive-mcp"
REDIRECT = "https://claude.ai/api/mcp/auth_callback"
SECRET = "tasks-oauth-jwt-secret-0123456789abcdef-xyz"
AUTH = SimpleNamespace(mcp_resource_uri=FACTS, oauth_session_secret=SECRET, mcp_access_token_ttl=3600,
                       mcp_refresh_token_ttl=2592000, mcp_auth_code_ttl=600, mcp_consent_request_ttl=600)
ENV = SimpleNamespace(mcp_oauth_clients_collection="c", mcp_auth_codes_collection="a", mcp_refresh_tokens_collection="r")
REFUSED = marker("REFUSED")
TOOLS_LIST = {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}


class OAuthWorld(World):
    def __init__(self) -> None:
        super().__init__()
        self.mcp_db = EvalDb()
        self.repo = FirestoreMCPClientRepository(self.mcp_db, ENV, cipher=AesGcmTestCipher(),
                                                 digest_key=bytes(range(32)))
        self.mcp = MCPAuthorizationService(
            self.repo, jwt_secret=SECRET, mcp_resource_uri=FACTS, drive_resource_uri=DRIVE, scope_of=SCOPE_OF,
            consent_base_url=f"{BASE}/mcp/consent", allowed_redirect_hosts=("claude.ai",), user_repo=self.users,
            whitelist_repo=self.whitelist)

    def app(self, inactive_users=None):
        return tms.build_tasks_mcp_app(external_tasks=self.service, limiter=self.limiter, gate=self.gate,
                                       inactive_users=inactive_users or self.inactive,
                                       texts=self.texts_override or self.texts, initial_texts=self.initial,
                                       auth_config=AUTH, authorization_service=self.mcp)

    async def grant(self, worker_id: str, user_id: str = "usr-1", account_id: str = "acc-1"):
        """A grant through consent and code exchange; the issued tokens."""
        client = MCPClient(client_id="cli-a", client_secret="s", client_name="Claude", redirect_uris=[REDIRECT],
                           scope="external_tasks", created_at=datetime.now(timezone.utc))
        await self.repo.save_client(client)
        url = await self.mcp.build_consent_redirect(client, AuthorizationParams(
            redirect_uri=REDIRECT, code_challenge="c" * 43, scopes=["external_tasks"], resource=TASKS_URI))
        code, _ = await self.mcp.issue_auth_code_for_consent(url.split("req=", 1)[1], user_id=user_id,
                                                             account_id=account_id, worker_id=worker_id)
        return await self.mcp.finalize_auth_code(code)

    def grant_id(self, issued) -> str:
        return self.mcp.verify_access_token(issued.access_token, resource=TASKS_URI).grant_id


@pytest.fixture
async def world():
    w = await OAuthWorld().seed()
    w.initial = await w.texts.texts()
    w.token = (await w.grant(w.research.worker_id)).access_token
    return w


async def test_an_oauth_token_lists_the_tools(world):
    assert {t["name"] for t in await world.list_tools(world.token)} == {
        "list_tasks", "claim_task", "renew_lease", "submit_task_result", "release_task", "fail_task"}


async def test_an_oauth_token_works_as_its_worker_only(world):
    await world.create(label="calendar-watch")
    assert ok(await world.call("claim_task", world.token)) == marker("NO_TASK")
    mine = await world.create()
    claimed = await world.claim(world.token)
    assert claimed["task_id"] == mine.task_id


async def test_a_submit_records_via_oauth_and_the_client(world):
    await world.create()
    claimed = await world.claim(world.token)
    ok(await world.call("submit_task_result", world.token, task_id=claimed["task_id"], lease_id=claimed["lease_id"],
                        result=RESULT))
    meta = (await world.tasks.get("usr-1", claimed["task_id"])).result_meta
    assert (meta.submitted_via, meta.client_id) == (SubmitVia.OAUTH, "cli-a")


async def test_a_grant_revoked_in_the_cabinet_is_refused_on_the_next_call(world):
    issued = await world.grant(world.research.worker_id)
    assert ok(await world.call("list_tasks", issued.access_token)) is not None
    await world.mcp.revoke_grant("usr-1", world.grant_id(issued))
    assert world.mcp.verify_access_token(issued.access_token, resource=TASKS_URI) is not None   # still valid
    assert err(await world.call("list_tasks", issued.access_token)) == REFUSED
    assert ok(await world.call("list_tasks", world.token)) is not None                          # other grant lives


async def test_a_user_taken_off_the_whitelist_is_refused(world):
    world.whitelist.entry.allowed_emails.discard("alex@example.com")
    assert err(await world.call("list_tasks", world.token)) == REFUSED


async def test_a_locked_user_gets_the_lock_text(world):
    await world.db.collection(USERS).document("usr-1").update(
        {"is_active": False, "inactive_reason": InactiveReason.DURESS.value})
    assert err(await world.call("list_tasks", world.token)) == ACCOUNT_LOCKED_TOOL_ERROR


async def test_the_flag_off_is_refused(world):
    await world.flag("usr-1", False)
    assert err(await world.call("list_tasks", world.token)) == REFUSED


async def test_a_wid_naming_a_revoked_worker_is_refused(world):
    issued = await world.grant(world.watch.worker_id)
    await world.service.revoke_worker("usr-1", world.watch.worker_id)
    # the worker's removal through the service alone leaves the grant; the queue refuses the worker
    assert err(await world.call("claim_task", issued.access_token)) == REFUSED


async def test_a_wid_of_another_users_worker_is_refused(world):
    issued = await world.grant(world.theirs.worker_id)       # usr-1's grant naming usr-2's worker
    assert err(await world.call("list_tasks", issued.access_token)) == REFUSED


async def test_a_token_without_a_worker_or_for_another_resource_is_401(world):
    bare = world.mcp._mint_access_token(user_id="usr-1", account_id="acc-1", client_id="cli-a",
                                        scopes=["external_tasks"], resource=TASKS_URI, grant_id="g" * 22)
    facts = world.mcp._mint_access_token(user_id="usr-1", account_id="acc-1", client_id="cli-a",
                                         scopes=["user_context"], resource=FACTS, grant_id="g" * 22,
                                         worker_id=world.research.worker_id)
    for token in (bare, facts, "not-a-token"):
        assert (await world.post(TOOLS_LIST, token)).status_code == 401


async def test_without_the_authorization_service_an_oauth_token_is_401(world):
    app = tms.build_tasks_mcp_app(external_tasks=world.service, limiter=world.limiter, gate=world.gate,
                                  inactive_users=world.inactive, texts=world.texts, initial_texts=world.initial,
                                  auth_config=AUTH)
    transport = httpx.ASGITransport(app=app.streamable_http_app(), raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url=BASE) as http, app.session_manager.run():
        resp = await http.post("/tasks-mcp", json=TOOLS_LIST, headers={
            "Accept": "application/json, text/event-stream", "Authorization": f"Bearer {world.token}"})
    assert resp.status_code == 401


async def test_a_tasks_token_is_401_at_the_drive_resource(world):
    drive = build_drive_mcp_app(authorization_service=world.mcp, auth_config=AUTH)
    transport = httpx.ASGITransport(app=drive.streamable_http_app(), raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url=BASE) as http, drive.session_manager.run():
        resp = await http.post("/drive-mcp", json=TOOLS_LIST, headers={
            "Accept": "application/json, text/event-stream", "Authorization": f"Bearer {world.token}"})
    assert resp.status_code == 401


async def test_the_log_line_carries_no_token(world, caplog):
    with caplog.at_level(logging.DEBUG):
        await world.call("list_tasks", world.token)
    assert world.token not in caplog.text
    assert "[TaskMCP] tool=list_tasks" in caplog.text
