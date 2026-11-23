"""A locked user is refused at every entry point (CABINET_DEVICE_SESSIONS_RFC §7.8 "Entry points", §10 "A check is
forgotten on a new entry point").

`ENTRY_POINTS` is the RFC's table, one row per entry point, each with the production class or route that holds its
check. `test_a_locked_user_is_refused_at_every_entry_point` builds the production graph (`prod_graph`), gives its user
`u1` a whitelisted Google identity, Slack and Telegram identities, a stored channel, a Cabinet session and an MCP grant,
then writes the lock flag the way the facts PIN transaction does (`is_active = false`, `inactive_reason = duress`) and
walks the table: each row's probe drives the production object (or the blueprint main.py builds over the graph's
objects) with the locked user and asserts the refusal. The flag alone is written first, so every check is shown to hold
by itself, without the lock's follow-up (`AccountLockService`: sessions, MCP grants, the notice), whose own row runs
last. The failures of all rows are reported together.

A new entry point cannot slip past: `test_every_route_is_an_entry_point_or_says_why_not` classifies every route the
main service serves (the pinned `main_boots_offline_url_map.txt`), the indexer's and the MCP apps' paths: each is a row of
`ENTRY_POINTS`, behind the Cabinet gate (every `/api/` route), or listed in `NOT_A_USER_ENTRY` with the reason. A new
route fails until it is classified, and classifying it as a row means adding its probe here. The wiring that lives in
the entry modules, outside the graph, is pinned by `test_the_entry_modules_hand_every_holder_its_check`.
"""
import ast
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Awaitable, Callable, Dict, List, Optional, Tuple

import httpx
import pytest
from mcp.server.fastmcp.exceptions import ToolError
from quart import Quart

from src.domain.user import InactiveReason
from tests.support.prod_graph import prod_graph  # noqa: F401  (the fixture)

ROOT = Path(__file__).resolve().parents[2]
URL_MAP = Path(__file__).with_name("main_boots_offline_url_map.txt")
ALEX_EMAIL = "alex@example.com"
USER, ACCOUNT = "u1", "a1"    # the prod_graph user (tests/support/prod_graph.py)

# The RFC §7.8 table: row -> where its check lives (what the probe of the same name drives).
ENTRY_POINTS: Dict[str, str] = {
    "google_sign_in": "AuthenticationService.handle_oauth_callback (GET /auth/callback)",
    "passkey_sign_in_and_refresh": "AuthenticationService.resume_session (POST /auth/refresh, POST /auth/passkey/verify)",
    "chat": "IAMService.authorize, the one IAM of the Slack and Telegram adapters (every message, queued event, "
            "slash command, button press and `$` command)",
    "worker": "create_main_service_blueprint `/worker` (inactive_users): every task type whose payload names a user",
    "proactive_delivery": "UserNotificationService.resolve_channel: every background post",
    "drive_indexer": "create_indexer_worker_blueprint `/worker` (inactive_users): drive_sync",
    "voice_session_config": "create_voice_control_plane_blueprint POST /voice/session-config (web and phone calls)",
    "voice_delegate": "create_voice_control_plane_blueprint POST /voice/delegate",
    "mcp_grants": "MCPAuthorizationService: the code exchange, the refresh rotation, the drive-link grant",
    "mcp_facts_tool": "build_mcp_components get_user_context: a call on an access token already issued",
    "mcp_drive_tools": "build_drive_mcp_or_none (inactive_users): every drive tool call",
    "external_task_api": "create_external_task_api_blueprint (resolve_key, over the service's account-lock check): "
                         "every worker call of /external-tasks/v1",
    "cabinet_sessions": "AccountLockService.lock step 2: every device session revoked (the gate then answers 401)",
}

# Routes that are not an entry point a locked user could use, each with the reason. Keyed by (method, rule).
NOT_A_USER_ENTRY: Dict[Tuple[str, str], str] = {
    **{("GET", rule): "public page or static file: no user data" for rule in (
        "/", "/alek.jpg", "/og.png", "/robots.txt", "/health", "/favicon.ico", "/static/<path:filename>", "/join",
        "/cabinet", "/cabinet/call", "/cabinet/call/icon-<int:size>.png", "/cabinet/call/interpreter",
        "/cabinet/call/interpreter/icon-<int:size>.png", "/cabinet/call/interpreter/manifest.webmanifest",
        "/cabinet/call/manifest.webmanifest", "/cabinet/call/modes", "/cabinet/call/modes/manifest.webmanifest",
        "/cabinet/call/tutor", "/cabinet/call/tutor/icon-<int:size>.png", "/cabinet/call/tutor/manifest.webmanifest",
        "/auth/session.js", "/cabinet/assets/<name>")},
    ("GET", "/auth/login"): "starts a sign-in; the callback (google_sign_in) or the passkey verify decides",
    ("GET", "/auth/login/google"): "starts a Google sign-in; the callback (google_sign_in) decides",
    ("POST", "/auth/passkey/options"): "a random challenge before any user is known; the verify decides",
    ("POST", "/auth/logout"): "only ends the caller's own session",
    ("POST", "/voice/relay-warmup"): "Cloud Scheduler keeps the relay warm; no user",
    ("POST", "/voice/auth"): "Twilio's call webhook; the call reaches the user only through /voice/session-config",
    ("POST", "/voice/answer"): "Twilio's call webhook; the call reaches the user only through /voice/session-config",
    ("POST", "/voice/status"): "Twilio's status callback; no user data out",
    ("POST", "/voice/inbound-status"): "Twilio's status callback; no user data out",
    ("POST", "/voice/submit-transcript"): "the end of a call already connected (runs to its end by decision, §7.8); "
                                          "its summary posts through channel resolution (proactive_delivery)",
    ("POST", "/voice/delegate/abandon"): "a late answer posts through channel resolution (proactive_delivery)",
    ("GET", "/geo/<link_id>"): "a one-time link page; the turn its POST queues re-authorizes through IAM (chat)",
    ("POST", "/geo/<link_id>"): "a one-time link; the turn it queues re-authorizes through IAM (chat, geo_mark_turn)",
    ("GET", "/d/<link_id>"): "a one-time drive link: MCPAuthorizationService.grant_allowed decides (mcp_grants)",
    ("GET", "/f/<token>"): "a capability link to one file, sent to the chat before the lock: not covered by decision "
                           "(it gives what the chat history already showed)",
    ("GET", "/f/<token>/raw"): "a capability link to one file, sent to the chat before the lock: not covered by decision",
    ("GET", "/s/<code>"): "a short link to one file, sent to the chat before the lock: not covered by decision",
    ("POST", "/webhook/microsoft-tasks/<user_id>"): "Microsoft Graph's change notice: keeps the user's task index "
                                                     "current, nothing reaches the user (data is untouched, §7.8)",
}

# Routes that are a row of ENTRY_POINTS.
ROUTE_ROWS: Dict[Tuple[str, str], str] = {
    ("GET", "/auth/callback"): "google_sign_in",
    ("POST", "/auth/refresh"): "passkey_sign_in_and_refresh",
    ("POST", "/auth/passkey/verify"): "passkey_sign_in_and_refresh",
    ("POST", "/slack/events"): "chat",
    ("POST", "/slack/commands"): "chat",
    ("POST", "/slack/interactivity"): "chat",
    ("POST", "/telegram/webhook"): "chat",
    ("POST", "/worker"): "worker",
    ("POST", "/voice/session-config"): "voice_session_config",
    ("POST", "/voice/delegate"): "voice_delegate",
    **{(m, f"/external-tasks/v1{rule}"): "external_task_api" for m, rule in (
        ("GET", "/tasks"), ("POST", "/claim"), ("POST", "/tasks/<task_id>/renew"),
        ("POST", "/tasks/<task_id>/result"), ("POST", "/tasks/<task_id>/release"),
        ("POST", "/tasks/<task_id>/fail"))},
    **{(m, rule): "cabinet_sessions" for m, rule in (
        ("GET", "/auth/me"), ("POST", "/auth/link-oauth"), ("GET", "/auth/connect/<kind>"),
        ("GET", "/auth/connect/callback"), ("GET", "/mcp/consent"), ("POST", "/mcp/consent"), ("GET", "/cabinet/docs"),
        ("GET", "/cabinet/docs/"), ("GET", "/cabinet/docs/<path:filename>"))},
}
CABINET_GATE_PREFIX = "/api/"    # every /api/ route sits behind the Cabinet gate (cabinet_sessions)

# The indexer service's routes and the MCP apps' paths (main.py dispatches the latter before the Quart app).
INDEXER_ROUTES = {("POST", "/worker"): "drive_indexer", ("GET", "/health"): None}
MCP_PATHS = {"/mcp": "mcp_facts_tool", "/token": "mcp_grants", "/authorize": "cabinet_sessions",
             "/register": None, "/revoke": None, "/drive-mcp": "mcp_drive_tools"}


# ---------------------------------------------------------------------------------------------- static checks

def _served_routes() -> List[Tuple[str, str]]:
    lines = [line for line in URL_MAP.read_text().splitlines() if line and not line.startswith("#")]
    return sorted({tuple(line.split(" ", 1)) for line in lines})  # type: ignore[misc]


def _mcp_literals() -> set:
    tree = ast.parse((ROOT / "src/web/mcp_dispatcher.py").read_text())
    fn = next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == "_is_mcp_path")
    found = {n.value for n in ast.walk(fn) if isinstance(n, ast.Constant) and isinstance(n.value, str)}
    return {p.rstrip("/") or "/" for p in found if not p.startswith("/.well-known")}


def test_every_route_is_an_entry_point_or_says_why_not():
    unclassified = [r for r in _served_routes() if r not in ROUTE_ROWS and r not in NOT_A_USER_ENTRY
                    and not r[1].startswith(CABINET_GATE_PREFIX)]
    assert unclassified == [], "classify each new route: a row of ENTRY_POINTS (and its probe) or NOT_A_USER_ENTRY"
    stale = [r for r in [*ROUTE_ROWS, *NOT_A_USER_ENTRY] if r not in _served_routes()]
    assert stale == []
    assert set(ROUTE_ROWS.values()) <= set(ENTRY_POINTS)


def test_the_indexer_and_mcp_paths_are_classified():
    from src.domain.mcp_resource import DRIVE_PATH
    from src.web.indexer_worker_app import create_indexer_worker_blueprint
    app = Quart("indexer")
    app.register_blueprint(create_indexer_worker_blueprint(None, None))   # type: ignore[arg-type]
    served = {(m, r.rule) for r in app.url_map.iter_rules() if r.endpoint != "static"
              for m in r.methods - {"HEAD", "OPTIONS"}}
    assert served == set(INDEXER_ROUTES)
    assert _mcp_literals() | {DRIVE_PATH} == set(MCP_PATHS)
    assert {v for v in [*INDEXER_ROUTES.values(), *MCP_PATHS.values()] if v} <= set(ENTRY_POINTS)


def _calls(path: str, name: str) -> List[ast.Call]:
    tree = ast.parse((ROOT / path).read_text())
    return [n for n in ast.walk(tree) if isinstance(n, ast.Call) and ast.unparse(n.func).endswith(name)]


@pytest.mark.parametrize("path,factory,expected", [
    ("main.py", "create_main_service_blueprint", "app.inactive_users"),
    ("main.py", "create_voice_control_plane_blueprint", "app.inactive_users"),
    ("main.py", "build_drive_mcp_or_none", "mcp_components.inactive_users"),
    ("indexer_main.py", "create_indexer_worker_blueprint", "InactiveUserCheck(users)"),
])
def test_the_entry_modules_hand_every_holder_its_check(path, factory, expected):
    """The holders whose check is optional (None does not check) get it where the entry module builds them; a holder
    built without it would serve a locked user."""
    calls = _calls(path, factory)
    assert calls, f"{path} no longer calls {factory}"
    for call in calls:
        given = {k.arg: ast.unparse(k.value) for k in call.keywords}
        if path == "indexer_main.py" and ast.unparse(call.args[0]) == "None":
            continue   # the first deploy pass: no tick service, /worker answers 503
        assert given.get("inactive_users") == expected, ast.unparse(call)


# ---------------------------------------------------------------------------------------------- the walk

class Recorder:
    """A dependency the refused path must never reach: every call is recorded and answers None."""

    def __init__(self) -> None:
        self.calls: List[str] = []

    def __getattr__(self, name: str):
        if name.startswith("__"):
            raise AttributeError(name)

        async def call(*args: Any, **kwargs: Any) -> None:
            self.calls.append(name)
        return call


class World:
    """The prod graph and what the probes share."""

    def __init__(self, graph: Any, monkeypatch: Any) -> None:
        self.graph, self.app, self.db = graph, graph.app, graph.offline.db
        self.monkeypatch = monkeypatch
        self.mcp: Any = None
        self.codes: List[str] = []
        self.refresh_token = ""
        self.grant_id: Optional[str] = None
        self.posts_before_lock = 0
        self.slack_posts: List[Tuple[str, str]] = []   # (channel, text) of every Slack post, at the SDK boundary

        async def chat_post_message(client: Any, *, channel: str, text: Optional[str] = None, **_: Any) -> dict:
            self.slack_posts.append((channel, text or ""))
            return {"ok": True, "channel": channel, "ts": "1.1"}
        import slack_sdk.web.async_client
        monkeypatch.setattr(slack_sdk.web.async_client.AsyncWebClient, "chat_postMessage", chat_post_message)

    async def prepare(self) -> None:
        """Alex's identities, channel, Cabinet session and MCP grant, all made while the account is open."""
        from src.config.environment import EnvironmentConfig
        from src.composition.mcp_setup import build_mcp_components
        from src.domain.device_session import SessionAuthKind
        from src.domain.mcp import MCPClient
        from src.services.mcp_authorization_service import AuthorizationParams
        app = self.app
        await app.whitelist_repo.collection.document("config").set({"allowed_emails": [ALEX_EMAIL]})
        user = await app.user_repo.get_user(USER)
        user.email, user.external_user_id = ALEX_EMAIL, "firebase|alex"
        user.auth_metadata = {"email": ALEX_EMAIL}
        user.platform_identities = {"slack": "U-alex", "telegram": "7001"}
        await app.user_repo.update_user(user)
        await app.notification_service.save_primary(USER, "slack", "C-alex")
        account = await app.account_repo.get_account(ACCOUNT)
        await app.device_sessions.start(user, account, label="iPhone", platform_hint="iPhone",
                                        auth_kind=SessionAuthKind.GOOGLE)
        self.mcp = build_mcp_components(
            db_client=self.db, env_config=EnvironmentConfig(), auth_config=app.auth_config,
            memory_search=app.agent_factory.get_memory_search, cipher=app.cipher, digest_key=app.digest_key,
            user_repo=app.user_repo, whitelist_repo=app.whitelist_repo)
        auth = self.mcp.authorization_service
        client = MCPClient(client_id="mcp-claude", client_secret="s", client_name="Claude",
                           redirect_uris=["https://claude.ai/api/mcp/auth_callback"],
                           created_at=datetime.now(timezone.utc))
        await auth.save_client(client)
        for _ in range(2):
            url = await auth.build_consent_redirect(client, AuthorizationParams(
                redirect_uri="https://claude.ai/api/mcp/auth_callback", code_challenge="c" * 43,
                scopes=["user_context"]))
            code, _ = await auth.issue_auth_code_for_consent(url.split("req=")[1], USER, ACCOUNT)
            self.codes.append(code)
        issued = await auth.finalize_auth_code(self.codes.pop())
        self.refresh_token = issued.refresh_token
        self.grant_id = auth.verify_access_token(issued.access_token).grant_id
        assert await auth.grant_allowed(USER, self.grant_id) is True

    async def lock(self) -> None:
        """The flag as the facts PIN transaction writes it; nothing else of the lock runs yet."""
        self.posts_before_lock = len(self.slack_posts)   # the new-sign-in notice of `prepare`
        user = await self.app.user_repo.get_user(USER)
        user.is_active, user.inactive_reason = False, InactiveReason.DURESS
        await self.app.user_repo.update_user_fields(user, ("is_active", "inactive_reason"))

    def token_ctx(self) -> Any:
        from src.composition.mcp_sdk_oauth_provider import AlekAccessToken
        token = AlekAccessToken(token="jwt", client_id="mcp-claude", scopes=["user_context"], expires_at=4102444800,
                                resource=self.app.auth_config.mcp_resource_uri, user_id=USER, account_id=ACCOUNT)
        return SimpleNamespace(request_context=SimpleNamespace(request=SimpleNamespace(
            user=SimpleNamespace(access_token=token))))


Probe = Callable[[World], Awaitable[None]]
PROBES: Dict[str, Probe] = {}


def probe(name: str) -> Callable[[Probe], Probe]:
    def register(fn: Probe) -> Probe:
        PROBES[name] = fn
        return fn
    return register


@probe("google_sign_in")
async def _google_sign_in(w: World) -> None:
    from src.domain.exceptions import AccountLockedError
    from src.ports.auth_port import OAuthTokens, OAuthUserInfo, TokenClaims
    provider = w.app.auth_registry.get_provider()

    async def tokens(code: str, redirect_uri: str) -> OAuthTokens:
        return OAuthTokens(access_token="at", id_token="it", expires_in=3600)

    async def claims(id_token: str) -> TokenClaims:
        return TokenClaims(sub="alex", iss="https://accounts.google.com", aud="client", exp=datetime.now(),
                           iat=datetime.now(), email=ALEX_EMAIL, email_verified=True)

    async def info(access_token: str) -> OAuthUserInfo:
        return OAuthUserInfo(sub="alex", email=ALEX_EMAIL, email_verified=True)

    for name, fn in (("exchange_code_for_tokens", tokens), ("verify_token", claims), ("get_user_info", info)):
        w.monkeypatch.setattr(provider, name, fn)
    with pytest.raises(AccountLockedError):
        await w.app.auth_service.handle_oauth_callback(code="c", redirect_uri=w.app.auth_config.oauth_redirect_uri)


@probe("passkey_sign_in_and_refresh")
async def _resume(w: World) -> None:
    from src.domain.exceptions import AccessDeniedError
    with pytest.raises(AccessDeniedError):
        await w.app.auth_service.resume_session(USER)


@probe("chat")
async def _chat(w: World) -> None:
    from src.services.iam_service import ACCOUNT_LOCKED_REPLY
    adapters = [a for a in (w.app.slack_adapter, w.app.telegram_adapter) if a is not None]
    assert w.app.slack_adapter is not None
    for adapter, (platform, platform_id) in zip(adapters, (("slack", "U-alex"), ("telegram", "7001"))):
        decision = await adapter.iam_service.authorize(platform, platform_user_id=platform_id)
        assert (decision.action, decision.user, decision.message) == ("reject", None, ACCOUNT_LOCKED_REPLY), platform


@probe("worker")
async def _worker(w: World) -> None:
    from src.web.main_service_routes import create_main_service_blueprint

    async def open_gate(_: Optional[str]) -> bool:
        return True
    app = Quart("worker")
    app.register_blueprint(create_main_service_blueprint(
        w.app.worker_handler, open_gate, static_dir=str(ROOT / "src/web/static"),
        inactive_users=w.app.inactive_users))
    for payload in ({"task_type": "fact_review", "user_id": USER, "account_id": ACCOUNT},
                    {"task_type": "agent_execution", "context": {"user_id": USER, "account_id": ACCOUNT}}):
        response = await app.test_client().post("/worker", json=payload)
        assert (response.status_code, await response.get_json()) == (200, {"skipped": "user_inactive"}), payload


@probe("proactive_delivery")
async def _delivery(w: World) -> None:
    service = w.app.notification_service
    assert await service.resolve_channel(USER) is None
    assert await service.resolve_channel(USER, "C-origin", "slack") is None
    assert await service.notify_raw(USER, ACCOUNT, "Reminder: water the plants") is False


@probe("drive_indexer")
async def _indexer(w: World) -> None:
    from src.domain.connection_scope import ConnectionScope
    from src.domain.drive_task_keys import scope_payload
    from src.services.inactive_user_check import InactiveUserCheck
    from src.web.indexer_worker_app import create_indexer_worker_blueprint
    indexing = Recorder()

    async def open_gate(_: Optional[str]) -> bool:
        return True
    app = Quart("indexer")
    app.register_blueprint(create_indexer_worker_blueprint(indexing, open_gate,   # type: ignore[arg-type]
                                                           inactive_users=InactiveUserCheck(w.app.user_repo)))
    response = await app.test_client().post("/worker", json={"task_type": "drive_sync",
                                                             **scope_payload(ConnectionScope(USER, "c1"), None)})
    assert (response.status_code, await response.get_json()) == (200, {"skipped": "user_inactive"})
    assert indexing.calls == []


def _voice_app(w: World, lelik: Recorder, tokens: Recorder) -> Tuple[Any, Any]:
    from src.adapters.firestore_ephemeral_store import FirestoreEphemeralStore
    from src.web.voice_control_plane_app import create_voice_control_plane_blueprint
    store = FirestoreEphemeralStore(w.db, "entry_point_voice")

    async def open_gate(_: Optional[str]) -> bool:
        return True

    async def lelik_of(user_id: str) -> None:
        lelik.calls.append(user_id)
    app = Quart("voice")
    app.register_blueprint(create_voice_control_plane_blueprint(
        ephemeral_store=store, quota_service=Recorder(), prompt_content_store=Recorder(), summary_consumer=Recorder(),
        oidc_verifier=open_gate, alert_sink=Recorder(), lelik_agent_provider=lelik_of, digest_key=w.app.digest_key,
        voice_mcp_tokens=tokens, voice_mcp_minting=True, inactive_users=w.app.inactive_users))
    return app.test_client(), store


@probe("voice_session_config")
async def _session_config(w: World) -> None:
    from src.domain.voice_ticket_keys import ticket_key
    lelik, tokens = Recorder(), Recorder()
    client, store = _voice_app(w, lelik, tokens)
    await store.set(ticket_key(w.app.digest_key, "t-alex"), {"user_id": USER, "account_id": ACCOUNT,
                                                            "mode": "companion", "voice": {"provider": "xai"}},
                    ttl_s=300)
    response = await client.post("/voice/session-config", json={"ticket": "t-alex"})
    assert (response.status_code, await response.get_json()) == (403, {"error": "account locked"})
    assert tokens.calls == []


@probe("voice_delegate")
async def _delegate(w: World) -> None:
    lelik = Recorder()
    client, _ = _voice_app(w, lelik, Recorder())
    response = await client.post("/voice/delegate", json={"user_id": USER, "account_id": ACCOUNT,
                                                          "request": "what is on my list"})
    assert (response.status_code, await response.get_json()) == (403, {"error": "account locked"})
    assert lelik.calls == []


@probe("mcp_grants")
async def _mcp_grants(w: World) -> None:
    from src.services.mcp_authorization_service import MCPInvalidGrant
    auth = w.mcp.authorization_service
    with pytest.raises(MCPInvalidGrant):
        await auth.finalize_auth_code(w.codes.pop())
    assert await auth.grant_allowed(USER, w.grant_id) is False
    with pytest.raises(MCPInvalidGrant):
        await auth.rotate_refresh_token(w.refresh_token)


@probe("mcp_facts_tool")
async def _mcp_facts(w: World) -> None:
    from src.composition.mcp_setup import FACTS_TOOL
    from src.domain.mcp import ACCOUNT_LOCKED_TOOL_ERROR
    tool = w.mcp.fastmcp._tool_manager.get_tool(FACTS_TOOL)
    with pytest.raises(ToolError) as err:
        await tool.run({"queries": ["car"]}, context=w.token_ctx())
    assert ACCOUNT_LOCKED_TOOL_ERROR in str(err.value)


@probe("mcp_drive_tools")
async def _mcp_drive(w: World) -> None:
    from src.composition.drive_connector_tools import build_drive_connector_tools
    from src.composition.drive_mcp_setup import build_drive_mcp_or_none
    from src.domain.mcp import ACCOUNT_LOCKED_TOOL_ERROR
    from src.domain.mcp_resource import DRIVE_PATH, drive_resource_uri
    from src.services.mcp_authorization_service import VerifiedAccessToken
    from tests.support.drive_connector_texts import TokenStore
    drive_uri = drive_resource_uri(w.app.auth_config.mcp_resource_uri)
    reads = Recorder()

    class Auth:
        def verify_access_token(self, token: str, *, resource: Optional[str] = None) -> Optional[VerifiedAccessToken]:
            return VerifiedAccessToken(token=token, client_id="mcp-claude", user_id=USER, account_id=ACCOUNT,
                                       scopes=["drive_read"], resource=drive_uri, expires_at=4_102_444_800)

    tools = await build_drive_connector_tools(conversion_service=reads, drive_service=reads,   # type: ignore[arg-type]
                                              drive_search=reads, drive_notes=None, token_repo=TokenStore(),  # type: ignore[arg-type]
                                              alert_sink=None, links=object())  # type: ignore[arg-type]
    drive = build_drive_mcp_or_none(Auth(), w.app.auth_config, tools=tools, inactive_users=w.mcp.inactive_users)
    assert drive is not None
    body = {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
            "params": {"name": "search_drive", "arguments": {"query": "Tom's plan"}}}
    headers = {"Accept": "application/json, text/event-stream", "Authorization": "Bearer drive-token"}
    base = drive_uri[: -len(DRIVE_PATH)]
    transport = httpx.ASGITransport(app=drive.streamable_http_app(), raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url=base) as http:
        async with drive.session_manager.run():
            resp = await http.post(DRIVE_PATH, json=body, headers=headers)
    data = next(line[len("data: "):] for line in resp.text.splitlines() if line.startswith("data: ")) \
        if resp.headers["content-type"].startswith("text/event-stream") else resp.text
    result = json.loads(data)["result"]
    assert result["isError"] is True and result["content"][0]["text"] == ACCOUNT_LOCKED_TOOL_ERROR
    assert reads.calls == []


@probe("external_task_api")
async def _external_task_api(w: World) -> None:
    """A worker with a live key of the locked user: the flag is on and the worker and key are written into the stores
    directly (the service refuses to mint for a locked user), and a claim on the blueprint main.py builds is refused
    with the lock's code."""
    from src.adapters.firestore_external_worker_store import FirestoreExternalWorkerStore
    from src.composition.external_task_api_setup import build_external_task_api_limiter
    from src.config.environment import EnvironmentConfig
    from src.domain.external_worker import ExternalWorker
    from src.domain.external_worker_key import (
        WORKER_KEY_DIGEST_PURPOSE,
        WORKER_KEY_PREFIX,
        ExternalWorkerKey,
        key_hint,
    )
    from src.domain.secret_digest import keyed_digest
    from src.web.external_task_api import create_external_task_api_blueprint
    assert w.app.external_tasks is not None
    env, now = EnvironmentConfig(), datetime.now(timezone.utc)
    await w.db.collection(env.domain_users_collection).document(USER).update({"config.external_tasks_enabled": True})
    raw = WORKER_KEY_PREFIX + "E" * 43
    store = FirestoreExternalWorkerStore(w.db, env)
    assert await store.create_worker(ExternalWorker(worker_id="wrk-1", user_id=USER, account_id=ACCOUNT,
                                                    label="research", created_at=now, updated_at=now), 10) is None
    assert await store.create_key(ExternalWorkerKey(
        key_digest=keyed_digest(w.app.digest_key, WORKER_KEY_DIGEST_PURPOSE, raw), user_id=USER, account_id=ACCOUNT,
        worker_id="wrk-1", hint=key_hint(raw), created_at=now), 3) is None
    app = Quart("external_task_api")
    app.register_blueprint(create_external_task_api_blueprint(
        w.app.external_tasks, limiter=build_external_task_api_limiter(w.db, env), user_repo=w.app.user_repo,
        whitelist_repo=w.app.whitelist_repo))
    response = await app.test_client().post("/external-tasks/v1/claim", headers={"Authorization": f"Bearer {raw}"})
    assert (response.status_code, await response.get_json()) == (403, {"code": "account_locked"})


@probe("cabinet_sessions")
async def _sessions(w: World) -> None:
    """The rest of the lock: every session revoked, and the lock notice, the one post that bypasses channel
    resolution, is the only thing the locked user was ever sent."""
    assert len(await w.app.device_sessions.list_for_user(USER)) == 1
    await w.app.account_lock.lock(USER, InactiveReason.DURESS)
    assert await w.app.device_sessions.list_for_user(USER) == []
    from src.domain.ui_messages import UIMessage
    notice = w.app.localization.get_ui_string(await w.app.language_service.resolve_ui_language(USER),
                                              UIMessage.ACCOUNT_LOCKED_DURESS_NOTICE)
    assert w.slack_posts[w.posts_before_lock:] == [("C-alex", notice)]


def test_every_row_has_a_probe():
    assert list(PROBES) == list(ENTRY_POINTS)


@pytest.mark.slow   # builds and starts the production graph
async def test_a_locked_user_is_refused_at_every_entry_point(prod_graph, monkeypatch):
    world = World(prod_graph, monkeypatch)
    await world.prepare()
    await world.lock()

    failures: Dict[str, str] = {}
    for name in ENTRY_POINTS:   # cabinet_sessions last: it runs the rest of the lock
        try:
            await PROBES[name](world)
        except BaseException as exc:   # noqa: BLE001 - every row is reported, not only the first
            failures[name] = f"{type(exc).__name__}: {exc}"
    assert failures == {}
    assert os.environ.get("K_SERVICE")   # the sealed deploy environment was in force
