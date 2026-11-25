"""
Composition helper: the memory inbox's MCP resource `/memory-inbox-mcp` and its one tool, `submit_user_information`
(MCP_MEMORY_SUBMISSIONS_RFC §5).

A fourth FastMCP instance, a resource server only, beside the facts app (the authorization server), the drive app and
the tasks app. Named `memory_inbox_mcp_setup`, not `mcp_memory_inbox_setup`: the drive-notes isolation test treats
`mcp_*.py` as memory-side.

**Credentials.** `MemoryInboxTokenVerifier` takes OAuth access tokens only (`MCPAuthorizationService.verify_access_token`
with `aud` = the inbox URI): a token of another resource (facts, drive, tasks) and a voice token never verify here, so
Lelik has no path to the tool (§3). A verified token carries its user, account, client, grant id and scope, never a
worker. A refused credential is no token, which the SDK answers with `401`.

**The tool** writes and reads nothing back: it is listed with no read-only hint. Input is liberal
(`decisions/mcp_tool_schema_liberal_input.md`): the advertised `items` is an array of one inline object of four strings
(`memory_inbox_mcp_contract.items_schema`, no constraint), and `normalize_items` takes whatever the client sent before
validation (the validator's output is the value: a `NormalizedItems`).

**Each call** (§5.6): the user and account from the verified token, never from an argument; the account lock, read fresh
(`ACCOUNT_LOCKED_TOOL_ERROR`); the texts; the grant, re-checked on every call as the tasks app does
(`grant_allowed(user, gid)`: an active refresh token of the grant, the user active and whitelisted), so a grant revoked
in the Cabinet stores nothing from its next call on, and a token without `gid` stores nothing; a refused grant answers
the neutral `REFUSED` line; no usable item answers `NOTHING_USABLE`; the registered client's name
(`MCPAuthorizationService.get_client`, stored normalised at registration) and `MemoryInboxService.submit`, one
transaction storing the items and counting the owner-local day; a failure there logs its type only and answers
`STORE_FAILED`. One span `mcp.memory_inbox.submit` and one log line `[MemoryInbox] submit user=<8> client=<8> items=…
stored=… cut=… over_day=… ms=…` (§12): counts and ids only, never submitted text. The only error results are these
and authentication (§5.2).

Texts come from `MCP_MEMORY_INBOX_TOOL_TEXTS` and `MCP_MEMORY_INBOX_RESULT_TEXTS` (`ConnectorTextService`): a first load
that fails leaves the app unbuilt, so `/memory-inbox-mcp` answers 404 and the other MCP apps run on.

`build_memory_inbox_mcp_for(app, mcp)` is main.py's one call (it reads the graph's inbox, token store and alert sink and
the facts app's components), so `main()` does not grow; `build_memory_inbox_mcp_or_none` takes each piece by name.
"""

import time
from typing import TYPE_CHECKING, Annotated, Optional
from urllib.parse import urlparse

from mcp.server.auth.settings import AuthSettings
from mcp.server.fastmcp import Context, FastMCP
from mcp.server.fastmcp.exceptions import ToolError
from mcp.types import CallToolResult, TextContent
from pydantic import AnyHttpUrl, BeforeValidator, SkipValidation, WithJsonSchema

from ..config.auth import AuthConfig
from ..domain import memory_inbox_mcp_contract as contract
from ..domain import memory_inbox_mcp_result as answers
from ..domain.exceptions import ConnectorTextsUnavailable
from ..domain.connector_texts import ConnectorTexts
from ..domain.mcp import ACCOUNT_LOCKED_TOOL_ERROR, normalize_client_name
from ..domain.mcp_resource import MEMORY_INBOX_PATH, memory_inbox_resource_uri
from ..domain.memory_inbox import SubmittingClient
from ..domain.memory_inbox_items import NormalizedItems, normalize_items
from ..domain.memory_inbox_mcp_result import InboxToolAnswer
from ..domain.voice_mcp_token import McpTokenKind
from ..ports.alert_sink import AlertSinkPort
from ..ports.prompt_v3.token_repository import TokenRepository
from ..services.connector_text_service import ConnectorTextService
from ..services.inactive_user_check import InactiveUserCheck
from ..services.mcp_authorization_service import MCPAuthorizationService
from ..services.memory_inbox_service import MemoryInboxService
from ..utils.logger import logger
from ..utils.telemetry import start_span
from .mcp_sdk_oauth_provider import AlekAccessToken
from .mcp_setup import MEMORY_INBOX_SCOPE, MCPComponents, build_transport_security
from .token_text_fastmcp import TokenTextFastMCP

if TYPE_CHECKING:
    from .application_graph import ApplicationGraph

TOOL_TEXTS_TOKEN = "MCP_MEMORY_INBOX_TOOL_TEXTS"
RESULT_TEXTS_TOKEN = "MCP_MEMORY_INBOX_RESULT_TEXTS"
ALERT_SUBJECT = "Memory inbox connector texts unavailable"
SPAN = "mcp.memory_inbox.submit"
# The texts are the tokens'; these two answers are given when no text is at hand.
NO_TEXTS = "The memory inbox's texts are not available; try again later."
NO_CREDENTIAL = "Authentication failed: no user context on this request."

# Whatever the client sent, normalised before validation; the advertised schema is the contract's inline item array.
_Items = Annotated[SkipValidation[NormalizedItems], BeforeValidator(normalize_items),
                   WithJsonSchema(contract.items_schema())]


class MemoryInboxTokenVerifier:
    """Satisfies mcp.server.auth.provider.TokenVerifier for the inbox resource: an OAuth access token minted for it,
    or nothing (no voice route, no worker key)."""

    def __init__(self, authorization_service: MCPAuthorizationService, resource_uri: str) -> None:
        self._authorization = authorization_service
        self._resource_uri = resource_uri

    async def verify_token(self, token: str) -> Optional[AlekAccessToken]:
        verified = self._authorization.verify_access_token(token, resource=self._resource_uri)
        if verified is None:
            return None
        return AlekAccessToken(
            token=verified.token, client_id=verified.client_id, scopes=list(verified.scopes),
            expires_at=verified.expires_at, resource=verified.resource, user_id=verified.user_id,
            account_id=verified.account_id, grant_id=verified.grant_id, kind=McpTokenKind.OAUTH)


def build_memory_inbox_mcp_app(*, inbox: MemoryInboxService, inactive_users: InactiveUserCheck,
                               texts: ConnectorTextService, initial_texts: ConnectorTexts, auth_config: AuthConfig,
                               authorization_service: MCPAuthorizationService) -> TokenTextFastMCP:
    """The inbox app with its one tool. Raises when the registered tool strays from the contract."""
    inbox_uri = memory_inbox_resource_uri(auth_config.mcp_resource_uri)
    issuer = urlparse(auth_config.mcp_resource_uri)    # the facts app is the authorization server
    app = TokenTextFastMCP(
        texts=texts, initial=initial_texts, read_only_tools=frozenset(),   # the one tool writes
        name="alekbot-memory-inbox",
        token_verifier=MemoryInboxTokenVerifier(authorization_service, inbox_uri),
        auth=AuthSettings(
            issuer_url=AnyHttpUrl(f"{issuer.scheme}://{issuer.netloc}"),
            resource_server_url=AnyHttpUrl(inbox_uri),
            required_scopes=[MEMORY_INBOX_SCOPE],
            validate_token_resource=False,   # the verifier checks the token's `aud` itself
        ),
        streamable_http_path=MEMORY_INBOX_PATH,
        transport_security=build_transport_security(auth_config.mcp_resource_uri),
        stateless_http=True,  # Cloud Run may route subsequent requests to different instances
    )
    caller = _InboxCaller(app, inbox, inactive_users, authorization_service)

    @app.tool(name=contract.TOOL)
    async def submit_user_information(ctx: Context, items: _Items) -> CallToolResult:
        return await caller.run(ctx, items)

    _check_contract(app)
    return app


class _InboxCaller:
    """Runs one call: the token's user, the lock, the texts, the items, the client, the store, the span, the log."""

    def __init__(self, app: TokenTextFastMCP, inbox: MemoryInboxService, inactive_users: InactiveUserCheck,
                 authorization_service: MCPAuthorizationService) -> None:
        self._app = app
        self._inbox = inbox
        self._inactive_users = inactive_users
        self._authorization = authorization_service

    async def run(self, ctx: Context, items: NormalizedItems) -> CallToolResult:
        started = time.monotonic()
        token = _access_token(ctx)
        user_id, account_id = token.user_id, token.account_id
        who = f"user={user_id[:8]} client={token.client_id[:8]}"
        # The account lock (CABINET_DEVICE_SESSIONS_RFC §7.8): a token issued before it verifies until it expires; the
        # stored user, read fresh, refuses the call before anything is stored.
        if await self._inactive_users.is_inactive(user_id):
            logger.warning(f"🔒 [MemoryInbox] submit {who} status=account_locked")
            return _result(InboxToolAnswer(text=ACCOUNT_LOCKED_TOOL_ERROR, is_error=True, status="account_locked"))
        sent = len(items.drafts) + items.over_call
        cut = items.over_call + items.cut_fields
        stored = over_day = 0
        with start_span(SPAN, {"items": sent}) as span:
            try:
                texts = (await self._app.current_texts()).results
            except ConnectorTextsUnavailable:
                answer = InboxToolAnswer(text=NO_TEXTS, is_error=True, status="no_texts")
            else:
                # The grant is re-checked on every call (as the tasks app, EXTERNAL_TASK_QUEUE_RFC §5.4 step 3): a grant
                # revoked in the Cabinet, or a user taken off the whitelist, is refused now, not when the token expires.
                if not await self._authorization.grant_allowed(user_id, token.grant_id):
                    answer = answers.refused(texts)
                elif not items.drafts:
                    answer = answers.nothing_usable(texts)
                else:
                    try:
                        receipt = await self._inbox.submit(
                            user_id=user_id, account_id=account_id,
                            client=await self._client(token.client_id), drafts=items.drafts)
                    except Exception as exc:
                        # the type only: an exception's message can carry the submitted text
                        logger.error(f"❌ [MemoryInbox] submit {who} store failed: {type(exc).__name__}")
                        answer = answers.store_failed(texts)
                    else:
                        stored, over_day = receipt.stored, receipt.over_day
                        answer = answers.received(texts, receipt, cut)
            ms = int((time.monotonic() - started) * 1000)
            for key, value in (("status", answer.status), ("stored", stored), ("cut", cut), ("over_day", over_day),
                               ("ms", ms)):
                span.set_attribute(key, value)
        empty = f" empty={items.dropped_empty}" if items.dropped_empty else ""
        logger.info(f"[MemoryInbox] submit {who} items={sent} stored={stored} cut={cut} over_day={over_day}{empty} "
                    f"status={answer.status} ms={ms}")
        return _result(answer)

    async def _client(self, client_id: str) -> SubmittingClient:
        """The registered client's id and its stored (normalised) name; an unknown client keeps an empty name."""
        client = await self._authorization.get_client(client_id)
        return SubmittingClient(client_id=client_id,
                                client_name=normalize_client_name(client.client_name if client else None))


def _result(answer: InboxToolAnswer) -> CallToolResult:
    return CallToolResult(content=[TextContent(type="text", text=answer.text)], isError=answer.is_error)


def _access_token(ctx: Context) -> AlekAccessToken:
    """The request's verified access token; ToolError when there is none (the SDK's auth middleware runs first, so
    only a request built around it gets here)."""
    request = ctx.request_context.request
    token = getattr(getattr(request, "user", None), "access_token", None)
    if not isinstance(token, AlekAccessToken) or token.kind is not McpTokenKind.OAUTH:
        logger.warning(f"[MemoryInbox] {contract.TOOL}: no verified access token on the request")
        raise ToolError(NO_CREDENTIAL)
    return token


def _check_contract(app: FastMCP) -> None:
    """The one registered tool is the contract's, with exactly its parameter."""
    for info in app._tool_manager.list_tools():
        expected = contract.TOOL_PARAMETERS.get(info.name)
        params = tuple(info.parameters.get("properties", {}))
        if expected is None or params != expected:
            raise RuntimeError(f"memory inbox MCP tool {info.name} strays from the contract: {params} != {expected}")


async def build_memory_inbox_mcp_or_none(*, inbox: Optional[MemoryInboxService], token_repo: Optional[TokenRepository],
                                         alert_sink: Optional[AlertSinkPort], inactive_users: InactiveUserCheck,
                                         auth_config: AuthConfig, authorization_service: MCPAuthorizationService,
                                         ) -> Optional[TokenTextFastMCP]:
    """The inbox app, or None: no inbox (no task queue), no prompt token store, or a failed build (the texts' first
    load included, which alerts through `alert_sink`). The other MCP apps run on either way."""
    if inbox is None:
        return None
    try:
        if token_repo is None:
            raise RuntimeError("the memory inbox MCP resource needs the prompt token store, which did not initialize")
        texts = ConnectorTextService(token_repo, tool_token_id=TOOL_TEXTS_TOKEN, result_token_id=RESULT_TEXTS_TOKEN,
                                     required_tool_keys=contract.REQUIRED_TOOL_KEYS,
                                     required_result_keys=contract.REQUIRED_RESULT_KEYS, alert_sink=alert_sink,
                                     alert_subject=ALERT_SUBJECT)
        app = build_memory_inbox_mcp_app(inbox=inbox, inactive_users=inactive_users, texts=texts,
                                         initial_texts=await texts.texts(), auth_config=auth_config,
                                         authorization_service=authorization_service)
    except Exception as exc:
        logger.error(f"❌ Failed to initialize the memory inbox MCP resource: {type(exc).__name__}: {exc}",
                     exc_info=True)
        logger.warning("🤖 Bot will continue without /memory-inbox-mcp")
        return None
    logger.info("[MemoryInbox] MCP app built: texts loaded")
    return app


async def build_memory_inbox_mcp_for(app: "ApplicationGraph", mcp: MCPComponents) -> Optional[TokenTextFastMCP]:
    """main.py's call: the inbox app over the graph's inbox service, prompt token store and alert sink, behind the facts
    app's authorization service and its account-lock check; None as `build_memory_inbox_mcp_or_none` answers."""
    assembly = app.container.assembly_service
    return await build_memory_inbox_mcp_or_none(
        inbox=app.memory_inbox.service if app.memory_inbox is not None else None,
        token_repo=assembly.token_repo if assembly is not None else None, alert_sink=app.alert_sink,
        inactive_users=mcp.inactive_users, auth_config=app.auth_config, authorization_service=mcp.authorization_service)
