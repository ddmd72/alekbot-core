"""
Composition helper: the drive connector's FastMCP, a resource server only, and its read tools.

The facts instance (`mcp_setup.build_mcp_components`) stays the authorization server. This second instance serves
`/drive-mcp` with scope `drive_read` and verifies bearer tokens against the drive resource URI through the shared
`MCPAuthorizationService` (DRIVE_MCP_CONNECTOR_RFC §3.2). The SDK forbids an auth provider and a token verifier on
one instance, hence two. Named `drive_mcp_setup`, not `mcp_drive_setup`: the drive-notes isolation test treats
`mcp_*.py` as memory-side.

Tools (§4.1, slice 2): with `tools` (the `DRIVE_MCP_TOOLS_ENABLED` knob on, `drive_connector_tools`) the app is a
`TokenTextFastMCP` with the six read tools of `drive_connector_contract.TOOL_NAMES` (`get_note` only when the notes
service exists), each a thin handler over `DriveReadOperations` (or `drive_note_tools.read_note_for_connector`); without, a plain FastMCP
that lists no tool and carries no instructions (no inline text, D4). Nothing that writes is registered.

Each call: the user and account from the verified access token (as the facts tool); the context
`{"user_id", "account_id", "run_origin": "mcp:drive"}` and no `native_formats` (§4.4, §6.2); `RequestContext`; a
`CALL_TIMEOUT_S` `wait_for`, an open's `deadline_at` that much minus `OPEN_MARGIN_S` (E8); one result composed by
`compose_connector_result`, cut by `cap_result`, in `content[].text` only (no structured content, §6.2); one span
`mcp.drive.<tool>` and one log line `[DriveMCP] tool=… user=<8> status=… chars=… ms=…`. The query and question are
never logged; the ref or folder label is logged as `DriveOperations._call` logs it in chat (§9). Input is liberal (`src/composition/CLAUDE.md`): flat strings, defaults, normalising
validators; an empty required ref is a correctable line, not a protocol error.

`get_note` in slice 2 (`read_note_for_connector`, the chat's rules and refusal classes): `drive:` refs only, any other
ref answers the neutral refusal; the connector access check of §7.5 comes with slice 3. A ref that is not a drive ref
(on `open_drive_file` too) and an item that does not exist answer the same neutral REFUSED line: no existence oracle.
"""

import asyncio
import time
from typing import Annotated, Any, Awaitable, Callable, Dict, Optional, Tuple
from urllib.parse import urlparse

from mcp.server.auth.settings import AuthSettings
from mcp.server.fastmcp import Context, FastMCP
from mcp.server.fastmcp.exceptions import ToolError
from mcp.types import CallToolResult, TextContent
from pydantic import AnyHttpUrl, BeforeValidator

from ..config.auth import AuthConfig
from ..domain import drive_connector_contract as contract
from ..domain import drive_note_tool_texts as nt
from ..domain.document_read import OPEN_MARGIN_S
from ..domain.drive_connector_result import RESULT_MAX_CHARS, cap_result, compose_connector_result, connector_status
from ..domain.drive_excerpt_request import FILE_REF_REQUIRED
from ..domain.drive_op import DriveOp
from ..domain.drive_outcome import DriveOutcome
from ..domain.drive_outcome_class import OutcomeClass
from ..domain.drive_tool_texts import FIND_PASSAGES, LIST_FOLDER, OPEN_DRIVE_FILE, READ_PASSAGES, SEARCH_DRIVE
from ..domain.exceptions import ConnectorTextsUnavailable
from ..domain.mcp import normalize_phrase
from ..domain.mcp_resource import DRIVE_PATH, drive_resource_uri
from ..domain.request_context import RequestContext
from ..services.mcp_authorization_service import MCPAuthorizationService
from ..utils.logger import logger
from ..utils.telemetry import start_span
from ..infrastructure.drive_note_tools import read_note_for_connector
from .drive_connector_tools import DriveConnectorTools
from .mcp_sdk_oauth_provider import AlekAccessToken
from .mcp_setup import DRIVE_SCOPE, build_transport_security
from .token_text_fastmcp import TokenTextFastMCP

# One tool call's budget: under claude.ai's 240 s per call and Claude Code's 2-minute backgrounding (§4.4, E8).
CALL_TIMEOUT_S = 120.0
RUN_ORIGIN = "mcp:drive"
# Claude Code files a result over 50,000 characters to disk unless the tool declares its own limit (§6.1, E17).
MAX_RESULT_SIZE_META_KEY = "anthropic/maxResultSizeChars"

_Run = Callable[[Dict[str, Any], str, str], Awaitable[DriveOutcome]]


def _joined(value: Any) -> str:
    """A list a client sent where a comma-separated string is advertised becomes that string; anything else is a
    trimmed string (None is "")."""
    if isinstance(value, (list, tuple)):
        return ", ".join(part for part in (normalize_phrase(v) for v in value) if part)
    return normalize_phrase(value)


_Text = Annotated[str, BeforeValidator(normalize_phrase)]
_Joined = Annotated[str, BeforeValidator(_joined)]


class DriveTokenVerifier:
    """Satisfies mcp.server.auth.provider.TokenVerifier for the drive resource."""

    def __init__(self, service: MCPAuthorizationService, resource_uri: str) -> None:
        self._service = service
        self._resource_uri = resource_uri

    async def verify_token(self, token: str) -> Optional[AlekAccessToken]:
        verified = self._service.verify_access_token(token, resource=self._resource_uri)
        if verified is None:
            return None
        return AlekAccessToken(
            token=verified.token,
            client_id=verified.client_id,
            scopes=list(verified.scopes),
            expires_at=verified.expires_at,
            resource=verified.resource,
            user_id=verified.user_id,
            account_id=verified.account_id,
        )


def build_drive_mcp_app(
    *, authorization_service: MCPAuthorizationService, auth_config: AuthConfig,
    tools: Optional[DriveConnectorTools] = None,
) -> FastMCP:
    """The drive app; `tools` None (the knob off) lists no tool. Raises when a registered tool strays from the
    contract (its name, its parameters)."""
    drive_uri = drive_resource_uri(auth_config.mcp_resource_uri)
    parsed = urlparse(auth_config.mcp_resource_uri)
    issuer_root = f"{parsed.scheme}://{parsed.netloc}"
    settings: Dict[str, Any] = dict(
        name="alekbot-drive",
        token_verifier=DriveTokenVerifier(authorization_service, drive_uri),
        auth=AuthSettings(
            issuer_url=AnyHttpUrl(issuer_root),
            resource_server_url=AnyHttpUrl(drive_uri),
            required_scopes=[DRIVE_SCOPE],
        ),
        streamable_http_path=DRIVE_PATH,
        transport_security=build_transport_security(auth_config.mcp_resource_uri),
        stateless_http=True,  # Cloud Run may route subsequent requests to different instances
    )
    if tools is None:
        return FastMCP(**settings)
    app = TokenTextFastMCP(texts=tools.texts, initial=tools.initial_texts, **settings)
    _register_tools(app, tools)
    _check_contract(app)
    return app


def _register_tools(app: TokenTextFastMCP, tools: DriveConnectorTools) -> None:
    ops = tools.operations
    call = _Caller(app)

    @app.tool(name=SEARCH_DRIVE)
    async def search_drive(ctx: Context, query: _Text = "", folder: _Text = "", drives: _Joined = "",
                           kind: _Text = "", date_from: _Text = "", date_to: _Text = "") -> CallToolResult:
        return await call.run(SEARCH_DRIVE, DriveOp.SEARCH, ctx, lambda c, u, _: ops.search(
            c, u, query=query, folder=folder, kind=kind, date_from=date_from, date_to=date_to, drives=drives))

    @app.tool(name=FIND_PASSAGES)
    async def find_passages(ctx: Context, question: _Text, folder: _Text = "", drives: _Joined = "",
                            file_refs: _Joined = "") -> CallToolResult:
        return await call.run(FIND_PASSAGES, DriveOp.PASSAGES, ctx, lambda c, u, _: ops.find_passages(
            c, u, question=question, folder=folder, drives=drives, file_refs=file_refs))

    @app.tool(name=READ_PASSAGES)
    async def read_passages(ctx: Context, file_ref: _Text, passages: _Text) -> CallToolResult:
        return await call.run(READ_PASSAGES, DriveOp.PASSAGES, ctx, lambda c, u, _: ops.read_passages(
            c, u, file_ref=file_ref, passages=passages))

    @app.tool(name=OPEN_DRIVE_FILE, meta={MAX_RESULT_SIZE_META_KEY: RESULT_MAX_CHARS})
    async def open_drive_file(ctx: Context, file_ref: _Text) -> CallToolResult:
        async def run(c: Dict[str, Any], u: str, _: str) -> DriveOutcome:
            if not file_ref:
                return DriveOutcome(op=DriveOp.OPEN, cls=OutcomeClass.REQUEST_ERROR, text=FILE_REF_REQUIRED,
                                    success=False)
            return await ops.open(c, u, file_ref, deadline_at=time.time() + CALL_TIMEOUT_S - OPEN_MARGIN_S)
        return await call.run(OPEN_DRIVE_FILE, DriveOp.OPEN, ctx, run)

    @app.tool(name=LIST_FOLDER)
    async def list_folder(ctx: Context, folder: _Text = "") -> CallToolResult:
        return await call.run(LIST_FOLDER, DriveOp.LIST, ctx, lambda c, u, _: ops.list_folder(c, u, folder or None))

    notes = tools.notes
    if notes is None:
        return

    @app.tool(name=nt.GET_NOTE)
    async def get_note(ctx: Context, item_ref: _Text = "", whole_drive_name: _Text = "") -> CallToolResult:
        async def run(_c: Dict[str, Any], user_id: str, _a: str) -> DriveOutcome:
            return await read_note_for_connector(notes, user_id, item_ref, whole_drive_name)
        return await call.run(nt.GET_NOTE, DriveOp.NOTE_READ, ctx, run)


class _Caller:
    """Runs one tool call: auth, texts, context, timeout, compose, guard, span, log."""

    def __init__(self, app: TokenTextFastMCP) -> None:
        self._app = app

    async def run(self, tool: str, op: DriveOp, ctx: Context, run: _Run) -> CallToolResult:
        started = time.monotonic()
        user_id, account_id = _token_user(ctx, tool)
        with start_span(f"mcp.drive.{tool}", {"tool": tool}) as span:
            try:
                texts = await self._app.current_texts()
            except ConnectorTextsUnavailable:
                logger.error(f"[DriveMCP] tool={tool} user={user_id[:8]} status=no_texts")
                span.set_attribute("status", "no_texts")
                return _error_result("The drive connector's texts are not available; try again later.")
            context = {"user_id": user_id, "account_id": account_id, "run_origin": RUN_ORIGIN}
            with RequestContext(user_id=user_id, account_id=account_id):
                outcome = await _outcome(tool, op, run, context, user_id, account_id)
            text = cap_result(compose_connector_result(outcome, texts), texts.results[contract.OVER_SIZE],
                              limit=RESULT_MAX_CHARS)
            status = _status(outcome)
            ms = int((time.monotonic() - started) * 1000)
            for key, value in (("status", status), ("chars", len(text)), ("ms", ms)):
                span.set_attribute(key, value)
        logger.info(f"[DriveMCP] tool={tool} user={user_id[:8]} status={status} chars={len(text)} ms={ms}")
        return CallToolResult(content=[TextContent(type="text", text=text)], isError=False)


async def _outcome(tool: str, op: DriveOp, run: _Run, context: Dict[str, Any], user_id: str,
                   account_id: str) -> DriveOutcome:
    try:
        return await asyncio.wait_for(run(context, user_id, account_id), timeout=CALL_TIMEOUT_S)
    except asyncio.TimeoutError:
        return DriveOutcome(op=op, cls=OutcomeClass.NOT_FINISHED, text="", success=False)
    except Exception as exc:   # DriveOperations answers its own failures; this is a bug's last line
        # the type only: an exception's message or traceback can carry the user's words or a file's text
        logger.error(f"❌ [DriveMCP] tool={tool} user={user_id[:8]} failed: {type(exc).__name__}")
        return DriveOutcome(op=op, cls=OutcomeClass.FAILED, text=f"Drive operation failed: {type(exc).__name__}.",
                            success=False)


def _status(outcome: DriveOutcome) -> str:
    status = connector_status(outcome).value
    return f"{status}:{outcome.withheld.value}" if outcome.withheld is not None else status


def _token_user(ctx: Context, tool: str) -> Tuple[str, str]:
    """The verified access token's user and account, as the facts tool reads them (`mcp_setup.get_user_context`)."""
    request = ctx.request_context.request
    access_token = getattr(getattr(request, "user", None), "access_token", None)
    if not isinstance(access_token, AlekAccessToken):
        logger.warning(f"[DriveMCP] tool={tool}: no verified access token on the request")
        raise ToolError("Authentication failed: no user context on this request.")
    return access_token.user_id, access_token.account_id


def _error_result(text: str) -> CallToolResult:
    return CallToolResult(content=[TextContent(type="text", text=text)], isError=True)


def _check_contract(app: FastMCP) -> None:
    """Every registered tool is a contract tool with exactly the contract's parameters, in its order."""
    for info in app._tool_manager.list_tools():
        expected = contract.TOOL_PARAMETERS.get(info.name)
        params = tuple(info.parameters.get("properties", {}))
        if expected is None or params != expected:
            raise RuntimeError(f"drive connector tool {info.name} strays from the contract: {params} != {expected}")
