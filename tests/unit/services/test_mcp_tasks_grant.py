"""The authorization server's side of the task queue's worker resource (EXTERNAL_TASK_QUEUE_RFC §5.4 "OAuth", §9.4):
the tasks scope binds to `/tasks-mcp` only when the service serves it, a grant of it is bound to one worker (`worker_id`
on the code and the refresh token, `wid` in the access token), the user's flag is checked at consent, at code exchange
and at refresh rotation, and every refresh token records when its grant was made and when it was issued."""

import logging
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import jwt
import pytest

from src.adapters.firestore_mcp_client_repository import FirestoreMCPClientRepository
from src.composition.mcp_setup import SCOPE_OF, TASKS_SCOPE
from src.domain.mcp import MCPAuthCode, MCPClient, MCPRefreshToken
from src.domain.mcp_resource import MCPResource
from src.services.mcp_authorization_service import (
    AuthorizationParams,
    MCPAuthError,
    MCPAuthorizationService,
    MCPInvalidGrant,
    MCPInvalidResource,
    MCPInvalidScope,
)
from tests.support.aes_gcm_cipher import AesGcmTestCipher
from tests.support.firestore_eval import EvalDb
from tests.support.mcp_gate_fakes import FakeUserRepo, FakeWhitelistRepo, ALLOWED_EMAIL

FACTS = "https://mcp.example.test/mcp"
DRIVE = "https://mcp.example.test/drive-mcp"
TASKS = "https://mcp.example.test/tasks-mcp"
REDIRECT = "https://claude.ai/api/mcp/auth_callback"
SECRET = "tasks-grant-jwt-secret-0123456789abcdef-xyz"
ENV = SimpleNamespace(mcp_oauth_clients_collection="c", mcp_auth_codes_collection="a", mcp_refresh_tokens_collection="r")
FAR = datetime(2099, 1, 1, tzinfo=timezone.utc)
TWO_SCOPES = {MCPResource.FACTS: "user_context", MCPResource.DRIVE: "drive_read"}


class World:
    def __init__(self, *, flag: bool = True, scope_of=SCOPE_OF, client_scope="user_context drive_read external_tasks"):
        self.db = EvalDb()
        self.repo = FirestoreMCPClientRepository(self.db, ENV, cipher=AesGcmTestCipher(), digest_key=bytes(range(32)))
        self.users = FakeUserRepo({"usr-1": ALLOWED_EMAIL})
        self.users.users["usr-1"].config.external_tasks_enabled = flag
        self.whitelist = FakeWhitelistRepo()
        self.service = MCPAuthorizationService(
            self.repo, jwt_secret=SECRET, mcp_resource_uri=FACTS, drive_resource_uri=DRIVE, scope_of=scope_of,
            consent_base_url="https://mcp.example.test/mcp/consent", allowed_redirect_hosts=("claude.ai",),
            user_repo=self.users, whitelist_repo=self.whitelist)
        self.client = MCPClient(client_id="c1", client_secret="s", redirect_uris=[REDIRECT], scope=client_scope,
                                created_at=datetime.now(timezone.utc))

    def set_flag(self, on: bool) -> None:
        self.users.users["usr-1"].config.external_tasks_enabled = on

    async def consent(self, resource=TASKS, scopes=()) -> str:
        url = await self.service.build_consent_redirect(self.client, AuthorizationParams(
            redirect_uri=REDIRECT, code_challenge="c" * 43, scopes=list(scopes), state="x", resource=resource))
        return url.split("req=", 1)[1]

    async def code(self, resource=TASKS, scopes=(), worker_id="w1") -> str:
        code, _ = await self.service.issue_auth_code_for_consent(await self.consent(resource, scopes), user_id="usr-1",
                                                                 account_id="acc-1", worker_id=worker_id)
        return code

    async def stored(self, refresh_value: str) -> MCPRefreshToken:
        token = await self.repo.get_refresh_token(self.service._hash_token(refresh_value))
        assert token is not None
        return token


def claims(access_token: str, audience: str = TASKS) -> dict:
    return jwt.decode(access_token, SECRET, algorithms=["HS256"], audience=audience)


# ------------------------------------------------------------------------------------------------ binding

async def test_the_tasks_scope_alone_binds_the_tasks_resource():
    w = World()
    for resource, scopes in ((None, [TASKS_SCOPE]), (TASKS, []), (TASKS + "/", [TASKS_SCOPE])):
        req = w.service.verify_consent_request(await w.consent(resource, scopes))
        assert (req.resource, req.scopes) == (TASKS, [TASKS_SCOPE])


@pytest.mark.parametrize("resource, scopes", [
    (None, [TASKS_SCOPE, "user_context"]),
    (None, [TASKS_SCOPE, "drive_read"]),
    (TASKS, ["user_context"]),
    (TASKS, ["drive_read"]),
    (FACTS, [TASKS_SCOPE]),
    (DRIVE, [TASKS_SCOPE]),
])
async def test_a_mix_or_a_scope_of_another_resource_is_refused(resource, scopes):
    with pytest.raises(MCPInvalidScope):
        await World().consent(resource, scopes)


async def test_an_empty_request_still_binds_the_facts_resource():
    req = World().service.verify_consent_request(await World().consent(None, []))
    assert (req.resource, req.scopes) == (FACTS, ["user_context"])


async def test_a_client_registered_without_the_tasks_scope_is_refused_it():
    w = World(client_scope="user_context drive_read")
    with pytest.raises(MCPInvalidScope, match="not registered"):
        await w.consent(None, [TASKS_SCOPE])
    with pytest.raises(MCPInvalidScope, match="not registered"):
        await w.consent(TASKS, [])


async def test_a_service_without_the_tasks_scope_does_not_serve_the_resource():
    w = World(scope_of=TWO_SCOPES)
    with pytest.raises(MCPInvalidResource):
        await w.consent(TASKS, [])
    with pytest.raises(MCPInvalidScope):
        await w.consent(None, [TASKS_SCOPE])


# ------------------------------------------------------------------------------------------------ consent

async def test_a_tasks_consent_without_a_worker_is_refused():
    w = World()
    req = await w.consent()
    for worker_id in (None, ""):
        with pytest.raises(MCPAuthError, match="worker is required"):
            await w.service.issue_auth_code_for_consent(req, user_id="usr-1", account_id="acc-1", worker_id=worker_id)
    assert w.db.collection("a").docs == {}


@pytest.mark.parametrize("resource, scopes", [(FACTS, []), (DRIVE, []), (None, [])])
async def test_a_worker_on_a_facts_or_drive_consent_is_refused(resource, scopes):
    w = World()
    with pytest.raises(MCPAuthError, match="only bound to a grant of the task queue"):
        await w.code(resource, scopes, worker_id="w1")
    assert w.db.collection("a").docs == {}


async def test_a_tasks_consent_without_the_flag_is_refused(caplog):
    w = World(flag=False)
    with caplog.at_level(logging.WARNING), pytest.raises(MCPAuthError, match="grant not allowed"):
        await w.code()
    assert w.db.collection("a").docs == {}
    assert "MCP consent refused: user=usr-1 resource=tasks reason=not_enabled" in caplog.text


async def test_a_tasks_consent_of_a_user_off_the_whitelist_is_refused():
    w = World()
    w.whitelist.entry.allowed_emails.clear()
    with pytest.raises(MCPAuthError, match="grant not allowed"):
        await w.code()


async def test_the_code_carries_the_worker_and_a_facts_code_carries_none():
    w = World()
    tasks_code = await w.code()
    assert (await w.repo.get_auth_code(tasks_code)).worker_id == "w1"
    facts_code = await w.code(FACTS, [], worker_id=None)
    assert (await w.repo.get_auth_code(facts_code)).worker_id is None


async def test_a_facts_grant_needs_no_flag():
    w = World(flag=False)
    issued = await w.service.finalize_auth_code(await w.code(FACTS, [], worker_id=None))
    assert "wid" not in claims(issued.access_token, FACTS)


# ------------------------------------------------------------------------------------------------ exchange

async def test_the_exchange_stamps_the_worker_and_the_grant_times():
    w = World()
    before = datetime.now(timezone.utc).replace(microsecond=0)
    issued = await w.service.finalize_auth_code(await w.code())
    stored = await w.stored(issued.refresh_token)
    assert stored.worker_id == "w1"
    assert stored.granted_at == stored.issued_at and stored.granted_at >= before
    assert claims(issued.access_token)["wid"] == "w1"
    verified = w.service.verify_access_token(issued.access_token, resource=TASKS)
    assert (verified.worker_id, verified.grant_id, verified.scopes) == ("w1", stored.grant_id, [TASKS_SCOPE])


async def test_a_tasks_token_does_not_verify_for_the_facts_or_drive_resource():
    w = World()
    issued = await w.service.finalize_auth_code(await w.code())
    assert w.service.verify_access_token(issued.access_token) is None
    assert w.service.verify_access_token(issued.access_token, resource=DRIVE) is None


async def test_the_flag_switched_off_before_the_exchange_consumes_the_code_and_mints_nothing(caplog):
    w = World()
    code = await w.code()
    w.set_flag(False)
    with caplog.at_level(logging.WARNING), pytest.raises(MCPInvalidGrant, match="no longer allowed"):
        await w.service.finalize_auth_code(code)
    assert await w.repo.get_auth_code(code) is None
    assert w.db.collection("r").docs == {}
    assert "MCP code exchange refused: user=usr-1 resource=tasks reason=not_enabled" in caplog.text


async def test_a_tasks_code_without_a_worker_is_refused_at_the_exchange():
    w = World()
    await w.repo.save_auth_code(MCPAuthCode(
        code="bare", client_id="c1", user_id="usr-1", account_id="acc-1", redirect_uri=REDIRECT,
        code_challenge="c" * 43, resource=TASKS, scopes=[TASKS_SCOPE], expires_at=FAR))
    with pytest.raises(MCPInvalidGrant):
        await w.service.finalize_auth_code("bare")
    assert w.db.collection("r").docs == {}


# ------------------------------------------------------------------------------------------------ rotation

async def test_rotation_keeps_the_worker_and_the_grant_time_and_moves_the_issue_time():
    w = World()
    first = await w.service.finalize_auth_code(await w.code())
    granted = (await w.stored(first.refresh_token)).granted_at
    stored = await w.stored(first.refresh_token)
    await w.repo.save_refresh_token(stored.model_copy(update={"issued_at": granted - timedelta(hours=1),
                                                              "granted_at": granted - timedelta(hours=1)}))
    second = await w.service.rotate_refresh_token(first.refresh_token)
    rotated = await w.stored(second.refresh_token)
    assert (rotated.worker_id, rotated.grant_id) == ("w1", stored.grant_id)
    assert rotated.granted_at == granted - timedelta(hours=1)
    assert rotated.issued_at > rotated.granted_at
    assert claims(second.access_token)["wid"] == "w1"


async def test_the_flag_switched_off_refuses_the_rotation_and_revokes_the_presented_token(caplog):
    w = World()
    issued = await w.service.finalize_auth_code(await w.code())
    w.set_flag(False)
    with caplog.at_level(logging.WARNING), pytest.raises(MCPInvalidGrant):
        await w.service.rotate_refresh_token(issued.refresh_token)
    assert not (await w.stored(issued.refresh_token)).is_active
    assert len(w.db.collection("r").docs) == 1
    assert "MCP refresh refused: user=usr-1 reason=not_enabled" in caplog.text


async def test_a_facts_grant_rotates_without_the_flag():
    w = World(flag=False)
    issued = await w.service.finalize_auth_code(await w.code(FACTS, [], worker_id=None))
    rotated = await w.service.rotate_refresh_token(issued.refresh_token)
    stored = await w.stored(rotated.refresh_token)
    assert stored.worker_id is None and stored.issued_at is not None


async def test_a_legacy_token_rotates_with_no_grant_time_and_an_issue_time():
    w = World()
    await w.repo.save_refresh_token(MCPRefreshToken(
        token_hash=w.service._hash_token("legacy"), client_id="c1", user_id="usr-1", account_id="acc-1",
        scopes=["drive_read"], resource=DRIVE, expires_at=FAR, grant_id="g" * 22))
    rotated = await w.stored((await w.service.rotate_refresh_token("legacy")).refresh_token)
    assert (rotated.granted_at, rotated.worker_id) == (None, None) and rotated.issued_at is not None


async def test_grant_allowed_holds_for_a_live_tasks_grant():
    w = World()
    issued = await w.service.finalize_auth_code(await w.code())
    assert await w.service.grant_allowed("usr-1", claims(issued.access_token)["gid"]) is True
