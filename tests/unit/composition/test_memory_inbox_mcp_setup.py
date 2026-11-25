"""The memory inbox MCP resource `/memory-inbox-mcp` end to end over the real streamable-HTTP app
(MCP_MEMORY_SUBMISSIONS_RFC §5).

The world: the real `MemoryInboxService` over the real inbox store on the evaluating fake (`memory_inbox_store.py`),
the real `MCPAuthorizationService` over `FirestoreMCPClientRepository` on `EvalDb` minting tokens through consent and
code exchange, the real `InactiveUserCheck`, and the texts loaded by `ConnectorTextService` from marker tokens
(`memory_inbox_mcp_texts.py`). Results are read from `tools/call` over HTTP; assertions are on the stored documents and
on which marker lines were answered, never on the tokens' wording.

Alex Doe is `u1` (account `a1`, Europe/Lisbon); the outside assistant registered as "Desk assistant".
"""
import json
import logging
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any, Dict, List, Optional

import httpx
import jwt
import pytest

from src.adapters.firestore_mcp_client_repository import FirestoreMCPClientRepository
from src.composition import memory_inbox_mcp_setup as ims
from src.composition.mcp_setup import SCOPE_OF
from src.domain import memory_inbox_mcp_contract as c
from src.domain.billing import BillingAccount
from src.domain.exceptions import ConnectorTextsUnavailable
from src.domain.mcp import ACCOUNT_LOCKED_TOOL_ERROR, MCPClient
from src.domain.memory_inbox import MAX_ITEMS_PER_CALL, RECEIVED_PER_DAY, Basis, SubmissionDraft, SubmittingClient
from src.domain.user import UserBotConfig, UserProfile
from src.domain.voice_mcp_token import McpTokenKind
from src.services.connector_text_service import ConnectorTextService
from src.services.inactive_user_check import InactiveUserCheck
from src.services.mcp_authorization_service import AuthorizationParams, MCPAuthorizationService
from src.services.memory_inbox_service import MemoryInboxService
from src.services.owner_timezone_resolver import OwnerTimezoneResolver
from tests.support.aes_gcm_cipher import AesGcmTestCipher
from tests.support.firestore_eval import EvalDb
from tests.support.mcp_gate_fakes import ALLOWED_EMAIL, FakeWhitelistRepo
from tests.support.memory_inbox_mcp_texts import RESULT_TOKEN, TOOL_TOKEN, InboxTokenStore, marker
from tests.support.memory_inbox_store import inbox_store
from tests.support.recording_task_queue import make_task_queue

UTC = timezone.utc
NOW = datetime(2026, 11, 25, 11, 30, tzinfo=UTC)
BASE = "https://mcp.example.test"
FACTS = f"{BASE}/mcp"
DRIVE = f"{BASE}/drive-mcp"
INBOX = f"{BASE}/memory-inbox-mcp"
TASKS = f"{BASE}/tasks-mcp"
REDIRECT = "https://claude.ai/api/mcp/auth_callback"
SECRET = "inbox-oauth-jwt-secret-0123456789abcdef-xyz"
AUTH = SimpleNamespace(mcp_resource_uri=FACTS, oauth_session_secret=SECRET, mcp_access_token_ttl=3600,
                       mcp_refresh_token_ttl=2592000, mcp_auth_code_ttl=600, mcp_consent_request_ttl=600)
ENV = SimpleNamespace(mcp_oauth_clients_collection="c", mcp_auth_codes_collection="a", mcp_refresh_tokens_collection="r")
CLARA = "Clara booked the Coimbra weekend"
ROBIN = "Robin from the Iron Temple gym trains with Alex"
QUILLEN = "Dr. Marta Quillen is Alex's dentist"


class Users:
    """The user repository the authorization service, the lock check and the owner-zone resolver read."""

    def __init__(self) -> None:
        self.profiles = {"u1": UserProfile(user_id="u1", account_id="a1", email=ALLOWED_EMAIL,
                                           config=UserBotConfig(timezone="Europe/Lisbon"))}

    async def get_user(self, user_id: str) -> Optional[UserProfile]:
        return self.profiles.get(user_id)


class Accounts:
    async def get_account(self, account_id: str) -> Optional[BillingAccount]:
        return BillingAccount(account_id=account_id, iam_policy={"u1": "owner"}) if account_id == "a1" else None


class World:
    def __init__(self) -> None:
        self.inbox = inbox_store()
        self.queue, _ = make_task_queue()
        self.users = Users()
        self.now = NOW
        self.service = MemoryInboxService(store=self.inbox.repo, task_queue=self.queue,
                                          owner_zones=OwnerTimezoneResolver(Accounts(), self.users),  # type: ignore[arg-type]
                                          clock=lambda: self.now)
        self.clients = FirestoreMCPClientRepository(EvalDb(), ENV, cipher=AesGcmTestCipher(),
                                                    digest_key=bytes(range(32)))
        self.whitelist = FakeWhitelistRepo()
        self.mcp = MCPAuthorizationService(
            self.clients, jwt_secret=SECRET, mcp_resource_uri=FACTS, drive_resource_uri=DRIVE, scope_of=SCOPE_OF,
            consent_base_url=f"{BASE}/mcp/consent", allowed_redirect_hosts=("claude.ai",),
            user_repo=self.users, whitelist_repo=self.whitelist)  # type: ignore[arg-type]
        self.inactive = InactiveUserCheck(self.users)  # type: ignore[arg-type]
        self.store = InboxTokenStore()
        self.texts: Any = ConnectorTextService(self.store, tool_token_id=TOOL_TOKEN, result_token_id=RESULT_TOKEN,
                                               required_tool_keys=c.REQUIRED_TOOL_KEYS,
                                               required_result_keys=c.REQUIRED_RESULT_KEYS)

    async def start(self) -> "World":
        self.initial = await self.texts.texts()
        self.token = await self.grant("memory_submit", INBOX)
        return self

    async def grant(self, scope: str, resource: str, **bound: str) -> str:
        """An access token through consent and code exchange, for a client registered with `scope`."""
        client = MCPClient(client_id=f"cli-{scope}", client_secret="s", client_name="Desk\nassistant",
                           redirect_uris=[REDIRECT], scope=scope, created_at=NOW)
        await self.mcp.save_client(client)
        url = await self.mcp.build_consent_redirect(client, AuthorizationParams(
            redirect_uri=REDIRECT, code_challenge="c" * 43, scopes=[scope], resource=resource))
        code, _ = await self.mcp.issue_auth_code_for_consent(url.split("req=", 1)[1], user_id="u1", account_id="a1",
                                                             **bound)
        return (await self.mcp.finalize_auth_code(code)).access_token

    def app(self):
        """A fresh app per request: a stateless app's session manager starts once per instance."""
        return ims.build_memory_inbox_mcp_app(inbox=self.service, inactive_users=self.inactive, texts=self.texts,
                                              initial_texts=self.initial, auth_config=AUTH,  # type: ignore[arg-type]
                                              authorization_service=self.mcp)

    async def post(self, body: Dict[str, Any], token: Optional[str]) -> httpx.Response:
        app = self.app()
        headers = {"Accept": "application/json, text/event-stream"}
        if token is not None:
            headers["Authorization"] = f"Bearer {token}"
        transport = httpx.ASGITransport(app=app.streamable_http_app(), raise_app_exceptions=False)
        async with httpx.AsyncClient(transport=transport, base_url=BASE) as http:
            async with app.session_manager.run():
                return await http.post("/memory-inbox-mcp", json=body, headers=headers)

    async def call(self, arguments: Dict[str, Any], token: Optional[str] = None) -> Dict[str, Any]:
        body = {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                "params": {"name": c.TOOL, "arguments": arguments}}
        resp = await self.post(body, self.token if token is None else token)
        assert resp.status_code == 200, resp.text
        data = next(line[len("data: "):] for line in resp.text.splitlines() if line.startswith("data: ")) \
            if resp.headers["content-type"].startswith("text/event-stream") else resp.text
        return json.loads(data)["result"]

    def received(self) -> List[dict]:
        return list(self.inbox.of_status("received").values())


def ok(result: Dict[str, Any]) -> List[str]:
    assert result["isError"] is False, result
    return result["content"][0]["text"].split("\n")


def err(result: Dict[str, Any]) -> str:
    assert result["isError"] is True, result
    return result["content"][0]["text"]


@pytest.fixture
async def world() -> World:
    return await World().start()


# -------------------------------------------------------------------------------------------------- the listing


async def test_the_emitted_schema_is_one_inline_item_object(world):
    [tool] = await world.app().list_tools()
    schema = tool.inputSchema
    dumped = json.dumps(schema)
    assert "$defs" not in schema and "$ref" not in dumped and "anyOf" not in dumped and '"null"' not in dumped
    keys: set = set()

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            keys.update(node)
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)
    walk(schema)
    assert not {"maxItems", "maxLength", "minLength", "pattern"} & keys
    assert '"additionalProperties": false' not in dumped
    assert list(schema["properties"]) == ["items"] and schema["properties"]["items"]["type"] == "array"
    item = schema["properties"]["items"]["items"]
    assert item["type"] == "object" and tuple(item["properties"]) == c.ITEM_FIELDS
    assert item["required"] == ["information"]
    assert {p["type"] for p in item["properties"].values()} == {"string"}


async def test_the_tool_its_items_and_every_field_are_described_by_the_texts(world):
    app = world.app()
    [tool] = await app.list_tools()
    assert (tool.name, tool.description) == (c.TOOL, marker(c.TOOL))
    items = tool.inputSchema["properties"]["items"]
    assert items["description"] == marker(f"{c.TOOL}.items")
    assert {f: p["description"] for f, p in items["items"]["properties"].items()} == {
        f: marker(f"{c.TOOL}.items.{f}") for f in c.ITEM_FIELDS}
    assert tool.annotations is None                                   # it writes: no read-only hint
    assert app._mcp_server.create_initialization_options().instructions == marker("instructions")


# -------------------------------------------------------------------------------------------------- the call


async def test_a_call_stores_received_records_of_the_tokens_user_and_client(world):
    result = await world.call({"items": [
        {"information": CLARA, "basis": "said", "happens_when": "March 6-8"},
        {"information": ROBIN, "basis": "document", "said_when": "last week"},
        QUILLEN]})
    assert ok(result) == ["<RECEIVED 3>"]
    stored = sorted(world.received(), key=lambda d: d["information"])
    assert [(d["user_id"], d["account_id"], d["client_id"], d["client_name"], d["received_at"]) for d in stored] == [
        ("u1", "a1", "cli-memory_submit", "Desk assistant", NOW)] * 3
    assert [(d["information"], d["basis"], d["said_when"], d["happens_when"]) for d in stored] == [
        (CLARA, "said", "", "March 6-8"), (QUILLEN, "inferred", "", ""), (ROBIN, "document", "last week", "")]


async def test_a_json_encoded_array_is_taken(world):
    assert ok(await world.call({"items": json.dumps([{"information": CLARA, "basis": "said"}])})) == ["<RECEIVED 1>"]
    assert [(d["information"], d["basis"]) for d in world.received()] == [(CLARA, Basis.SAID.value)]


async def test_cuts_and_the_day_cap_are_named_not_errors(world):
    already = RECEIVED_PER_DAY - 5
    receipt = await world.service.submit(user_id="u1", account_id="a1", client=SubmittingClient("cli-x", "Other"),
                                         drafts=[SubmissionDraft(f"item {i}", Basis.SAID, "", "")
                                                 for i in range(already)])
    assert receipt.stored == already
    sent = [f"{CLARA} {i}" for i in range(MAX_ITEMS_PER_CALL + 1)]
    assert ok(await world.call({"items": sent})) == ["<RECEIVED 5>", "<CUT 1>", "<OVER_DAY 15>"]
    assert len(world.received()) == RECEIVED_PER_DAY


async def test_a_cut_field_is_counted_with_the_dropped_items(world):
    lines = ok(await world.call({"items": [{"information": "x" * 1001, "said_when": "y" * 201}]}))
    assert lines == ["<RECEIVED 1>", "<CUT 2>"]
    [doc] = world.received()
    assert sorted(doc["cut"]) == ["information", "said_when"] and len(doc["information"]) == 1000


async def test_a_locked_user_is_refused_and_nothing_is_stored(world):
    world.users.profiles["u1"].is_active = False
    assert err(await world.call({"items": [CLARA]})) == ACCOUNT_LOCKED_TOOL_ERROR
    assert world.inbox.docs == {}


@pytest.mark.parametrize("items", [[], [{"information": "  "}, None], "", None, [{"basis": "said"}]])
async def test_no_usable_item_is_the_one_input_error(world, items):
    assert err(await world.call({"items": items})) == marker("NOTHING_USABLE")
    assert world.inbox.docs == {}


async def test_a_store_failure_is_an_error_and_logs_no_text(world, monkeypatch, caplog):
    async def failing(*args: Any, **kwargs: Any):
        raise RuntimeError(f"write refused: {CLARA}")
    monkeypatch.setattr(world.inbox.repo, "add_submissions", failing)
    caplog.set_level(logging.INFO)
    assert err(await world.call({"items": [CLARA]})) == marker("STORE_FAILED")
    assert "Coimbra" not in caplog.text and "RuntimeError" in caplog.text


async def test_the_log_line_carries_counts_only(world, caplog):
    caplog.set_level(logging.INFO)
    ok(await world.call({"items": [CLARA, {"information": ROBIN, "said_when": "last week"}, QUILLEN, ""]}))
    [line] = [r.getMessage() for r in caplog.records if r.getMessage().startswith("[MemoryInbox] submit")]
    assert line.startswith("[MemoryInbox] submit user=u1 client=cli-memo items=3 stored=3 cut=0 over_day=0 empty=1 "
                           "status=ok ms=")
    assert not any(word in caplog.text for word in ("Coimbra", "Iron Temple", "Quillen", "last week"))


async def test_texts_unavailable_after_boot_answer_an_error_and_store_nothing(world):
    class LoadedOnce:
        async def texts(self):
            raise ConnectorTextsUnavailable("connector texts could not be loaded: token missing")
    world.texts = LoadedOnce()
    err(await world.call({"items": [CLARA]}))           # the SDK lists the tools first and answers the error itself
    token = await ims.MemoryInboxTokenVerifier(world.mcp, INBOX).verify_token(world.token)
    ctx = SimpleNamespace(request_context=SimpleNamespace(request=SimpleNamespace(
        user=SimpleNamespace(access_token=token))))
    result = await world.app()._tool_manager.get_tool(c.TOOL).run({"items": [CLARA]}, context=ctx)
    assert (result.isError, result.content[0].text) == (True, ims.NO_TEXTS)
    assert world.inbox.docs == {}


async def test_a_call_without_a_verified_token_is_refused_by_the_handler(world):
    tool = world.app()._tool_manager.get_tool(c.TOOL)
    ctx = SimpleNamespace(request_context=SimpleNamespace(request=SimpleNamespace(user=None)))
    with pytest.raises(Exception, match="Authentication failed"):
        await tool.run({"items": [CLARA]}, context=ctx)
    assert world.inbox.docs == {}


# -------------------------------------------------------------------------------------------------- the grant


def _grant_id(world: World, token: str) -> str:
    return world.mcp.verify_access_token(token, resource=INBOX).grant_id


async def test_an_allowed_grant_stores(world):
    assert await world.mcp.grant_allowed("u1", _grant_id(world, world.token)) is True
    assert ok(await world.call({"items": [CLARA]})) == ["<RECEIVED 1>"]
    assert [d["information"] for d in world.received()] == [CLARA]


async def test_a_grant_revoked_in_the_cabinet_stores_nothing_and_answers_the_neutral_refusal(world, caplog):
    other = await world.grant("memory_submit", INBOX)
    await world.mcp.revoke_grant("u1", _grant_id(world, world.token))
    assert world.mcp.verify_access_token(world.token, resource=INBOX) is not None      # the access token still verifies
    caplog.set_level(logging.INFO)
    assert err(await world.call({"items": [CLARA]})) == marker("REFUSED")
    assert world.inbox.docs == {}
    assert any(r.getMessage().startswith("[MemoryInbox] submit user=u1 ") and "status=grant_refused" in r.getMessage()
               for r in caplog.records)
    assert ok(await world.call({"items": [CLARA]}, other)) == ["<RECEIVED 1>"]           # the other grant lives


async def test_a_user_taken_off_the_whitelist_stores_nothing(world):
    world.whitelist.entry.allowed_emails.discard(ALLOWED_EMAIL)
    assert err(await world.call({"items": [CLARA]})) == marker("REFUSED")
    assert world.inbox.docs == {}


async def test_a_token_without_a_grant_id_stores_nothing(world):
    claims = jwt.decode(world.token, SECRET, algorithms=["HS256"], options={"verify_aud": False})
    del claims["gid"]
    no_gid = jwt.encode(claims, SECRET, algorithm="HS256")
    assert world.mcp.verify_access_token(no_gid, resource=INBOX).grant_id is None
    assert err(await world.call({"items": [CLARA]}, no_gid)) == marker("REFUSED")
    assert world.inbox.docs == {}


# -------------------------------------------------------------------------------------------------- the door


async def test_a_token_of_another_resource_is_refused(world):
    users = world.users.profiles["u1"]
    users.config.external_tasks_enabled = True
    verifier = ims.MemoryInboxTokenVerifier(world.mcp, INBOX)
    facts = await world.grant("user_context", FACTS)
    tasks = await world.grant("external_tasks", TASKS, worker_id="w1")
    voice = jwt.encode({"sub": "u1", "account_id": "a1", "aud": INBOX, "type": "mcp_access", "scope": "memory_submit",
                        "client_id": "voice", "exp": 4_102_444_800}, "voice-signing-key-0123456789abcdef-xyz",
                       algorithm="HS256", headers={"kid": "voice-mcp"})
    assert [await verifier.verify_token(t) for t in (facts, tasks, voice, "not-a-token")] == [None] * 4
    token = await verifier.verify_token(world.token)
    assert (token.user_id, token.account_id, token.client_id, token.kind, token.worker_id, token.scopes) == (
        "u1", "a1", "cli-memory_submit", McpTokenKind.OAUTH, None, ["memory_submit"])
    for other in (facts, tasks, voice, None):
        resp = await world.post({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                                 "params": {"name": c.TOOL, "arguments": {"items": [CLARA]}}}, other)
        assert resp.status_code == 401
    assert world.inbox.docs == {}


async def test_the_app_serves_the_endpoint_and_only_public_metadata_besides(world):
    paths = {route.path for route in world.app().streamable_http_app().routes}
    assert paths == {"/memory-inbox-mcp", "/.well-known/oauth-protected-resource/memory-inbox-mcp"}


# -------------------------------------------------------------------------------------------------- the build


class Sink:
    def __init__(self) -> None:
        self.posts: List[str] = []

    async def post(self, text: str) -> None:
        self.posts.append(text)


async def _built(world: World, **overrides: Any):
    kwargs: Dict[str, Any] = dict(inbox=world.service, token_repo=world.store, alert_sink=None,
                                  inactive_users=world.inactive, auth_config=AUTH, authorization_service=world.mcp)
    return await ims.build_memory_inbox_mcp_or_none(**{**kwargs, **overrides})


async def test_the_build_gives_the_app_or_none(world):
    assert isinstance(await _built(world), ims.TokenTextFastMCP)
    assert await _built(world, inbox=None) is None
    assert await _built(world, token_repo=None) is None


async def test_a_missing_token_leaves_the_app_unbuilt_and_alerts(world):
    del world.store.tokens[RESULT_TOKEN]
    sink = Sink()
    assert await _built(world, alert_sink=sink) is None
    assert [post.split(":")[0] for post in sink.posts] == [ims.ALERT_SUBJECT]
