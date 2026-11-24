"""
Composition helper: the external task queue's worker MCP resource `/tasks-mcp` and its six tools
(EXTERNAL_TASK_QUEUE_RFC §5.4, §5.7).

A third FastMCP instance, a resource server only, beside the facts app (the authorization server) and the drive app.
Named `tasks_mcp_setup`, not `mcp_tasks_setup`: the drive-notes isolation test treats `mcp_*.py` as memory-side.

**Credentials.** `TasksTokenVerifier` routes on the credential's shape: one with the worker-key prefix goes to
`ExternalTaskService.resolve_key` (shape, digest, revocation, worker, lock, flag) and then `ExternalWorkerUserGate`
(the access whitelist), the same two checks the REST endpoint runs; anything else goes to the OAuth verifier only
(`MCPAuthorizationService.verify_access_token` with `aud` = the tasks URI), which also requires the `wid` claim (the
worker the grant is bound to; §5.4 "OAuth"). Without the authorization service every OAuth token is refused. A refused
credential is no token, which the SDK answers with `401`: a locked, switched-off, revoked or not-whitelisted key is
refused at the door, where REST answers `403` (plan Δ1). A verified key carries kind `WORKER_KEY`, the worker id, the
scope `external_tasks`, no expiry, and in `token` the key's stored digest, never the key; a verified OAuth token carries
kind `OAUTH`, its client, grant id (`gid`), worker id (`wid`), scope and expiry.

**Tools**, each a thin handler over one service operation: `list_tasks`, `claim_task`, `renew_lease`,
`submit_task_result`, `release_task`, `fail_task`. None takes a user, an account, a worker or a label: the principal is
the verified credential's. Input is liberal (`decisions/mcp_tool_schema_liberal_input.md`): flat strings with defaults,
no schema constraint; an empty `task_id` or `lease_id` answers a correctable line. `result` and `reason` reach the
service as sent (None is ""): the service normalises them. Only `list_tasks` is listed read-only.

**Each call** (`_TaskCaller.run`, the RFC's order):
1. the principal from the verified token;
2. the account lock, read fresh (`ACCOUNT_LOCKED_TOOL_ERROR`);
3. for an OAuth token, the grant: `grant_allowed(user, gid)` (an active refresh token of the grant, the user active
   and whitelisted), so a grant revoked in the Cabinet is refused on its next call, not when its access token expires;
   a refused grant answers the neutral refused line;
4. the flag and the worker: every service operation checks them again (`not_enabled` / `worker_revoked` answer one
   neutral line);
5. this worker's limits, the REST endpoint's own buckets (`calls`, plus `claims` on a claim and `submits` on a submit,
   counted per `user:worker`, the same buckets for a key and for an OAuth grant of that worker): over a limit answers
   the wait in seconds;
6. the service operation; its answer is composed by `external_task_mcp_result` from the result texts.
One span `mcp.tasks.<tool>` and one log line `[TaskMCP] tool=… user=<8> worker=<fingerprint> status=… ms=…` per call;
never a task text, a title, a result, a reason, a key or a header. A refusal is an `isError` result with its line; a
claim that found no task is a success. Any other failure logs its type only and answers a generic tool error.

Texts come from `MCP_TASK_TOOL_TEXTS` and `MCP_TASK_RESULT_TEXTS` (`ConnectorTextService`, as the drive app's): a first
load that fails leaves the app unbuilt, so `/tasks-mcp` answers 404 and the facts and drive apps run on.
"""

import time
from typing import Annotated, Any, Awaitable, Callable, Mapping, Optional, Tuple
from urllib.parse import urlparse

from mcp.server.auth.settings import AuthSettings
from mcp.server.fastmcp import Context, FastMCP
from mcp.server.fastmcp.exceptions import ToolError
from mcp.types import CallToolResult, TextContent
from pydantic import AnyHttpUrl, BeforeValidator

from ..config.auth import AuthConfig
from ..config.environment import EnvironmentConfig
from ..domain import external_task_mcp_contract as contract
from ..domain import external_task_mcp_result as answers
from ..domain.connector_texts import ConnectorTexts
from ..domain.exceptions import ConnectorTextsUnavailable
from ..domain.external_task import CALLS_PER_WINDOW, CLAIMS_PER_WINDOW, SUBMITS_PER_WINDOW, SubmitVia
from ..domain.external_task_mcp_result import TaskToolAnswer
from ..domain.external_task_refusal import ExternalTaskRefused
from ..domain.external_worker_key import WORKER_KEY_PREFIX
from ..domain.external_worker_principal import WorkerPrincipal
from ..domain.mcp import ACCOUNT_LOCKED_TOOL_ERROR, normalize_phrase
from ..domain.mcp_resource import TASKS_PATH, tasks_resource_uri
from ..domain.secret_digest import fingerprint
from ..domain.voice_mcp_token import McpTokenKind
from ..ports.alert_sink import AlertSinkPort
from ..ports.prompt_v3.token_repository import TokenRepository
from ..ports.user_repository import UserRepository
from ..ports.whitelist_repository import WhitelistRepository
from ..services.connector_text_service import ConnectorTextService
from ..services.external_task_rate_limiter import ExternalTaskRateLimiter
from ..services.external_task_service import ExternalTaskService
from ..services.external_worker_user_gate import ExternalWorkerUserGate
from ..services.inactive_user_check import InactiveUserCheck
from ..services.mcp_authorization_service import MCPAuthorizationService
from ..utils.logger import logger
from ..utils.telemetry import start_span
from .external_task_api_setup import build_external_task_api_limiter
from .mcp_sdk_oauth_provider import AlekAccessToken
from .mcp_setup import TASKS_SCOPE, build_transport_security
from .token_text_fastmcp import TokenTextFastMCP

TOOL_TEXTS_TOKEN = "MCP_TASK_TOOL_TEXTS"
RESULT_TEXTS_TOKEN = "MCP_TASK_RESULT_TEXTS"
ALERT_SUBJECT = "Task connector texts unavailable"
# The texts are the tokens'; these two answers are given when no text is at hand.
NO_TEXTS = "The task queue's texts are not available; try again later."
NO_CREDENTIAL = "Authentication failed: no worker credential on this request."

_Scopes = Tuple[Tuple[str, int], ...]
_Op = Callable[[WorkerPrincipal, Mapping[str, str]], Awaitable[TaskToolAnswer]]

CALLS: _Scopes = (("calls", CALLS_PER_WINDOW),)
CLAIMS: _Scopes = CALLS + (("claims", CLAIMS_PER_WINDOW),)
SUBMITS: _Scopes = CALLS + (("submits", SUBMITS_PER_WINDOW),)


def _as_sent(value: Any) -> Any:
    """None is ""; anything else reaches the field as sent (the service normalises a result and a reason)."""
    return "" if value is None else value


_Text = Annotated[str, BeforeValidator(normalize_phrase)]
_Raw = Annotated[str, BeforeValidator(_as_sent)]


class TasksTokenVerifier:
    """Satisfies mcp.server.auth.provider.TokenVerifier for the tasks resource: a worker key, an OAuth access token of
    the tasks resource bound to a worker, or nothing."""

    def __init__(self, external_tasks: ExternalTaskService, gate: ExternalWorkerUserGate, resource_uri: str,
                 authorization_service: Optional[MCPAuthorizationService] = None) -> None:
        self._tasks = external_tasks
        self._gate = gate
        self._resource_uri = resource_uri
        self._authorization = authorization_service

    async def verify_token(self, token: str) -> Optional[AlekAccessToken]:
        if not token.startswith(WORKER_KEY_PREFIX):
            return self._verify_oauth(token)
        try:
            principal = await self._tasks.resolve_key(token)
            await self._gate.check(principal)
        except ExternalTaskRefused as refused:
            logger.info(f"[TaskMCP] key refused: {refused.reason.value}")
            return None
        return AlekAccessToken(
            token=self._tasks.key_digest(token), client_id=McpTokenKind.WORKER_KEY.value, scopes=[TASKS_SCOPE],
            expires_at=None, resource=self._resource_uri, user_id=principal.user_id,
            account_id=principal.account_id, kind=McpTokenKind.WORKER_KEY, worker_id=principal.worker_id)

    def _verify_oauth(self, token: str) -> Optional[AlekAccessToken]:
        """An access token minted for the tasks resource and bound to a worker; anything else is None."""
        if self._authorization is None:
            return None
        verified = self._authorization.verify_access_token(token, resource=self._resource_uri)
        if verified is None:
            return None
        if not verified.worker_id:
            logger.info(f"[TaskMCP] oauth token refused: user={verified.user_id[:8]} reason=no_worker")
            return None
        return AlekAccessToken(
            token=verified.token, client_id=verified.client_id, scopes=list(verified.scopes),
            expires_at=verified.expires_at, resource=verified.resource, user_id=verified.user_id,
            account_id=verified.account_id, grant_id=verified.grant_id, kind=McpTokenKind.OAUTH,
            worker_id=verified.worker_id)


def build_tasks_mcp_app(*, external_tasks: ExternalTaskService, limiter: ExternalTaskRateLimiter,
                        gate: ExternalWorkerUserGate, inactive_users: InactiveUserCheck,
                        texts: ConnectorTextService, initial_texts: ConnectorTexts,
                        auth_config: AuthConfig,
                        authorization_service: Optional[MCPAuthorizationService] = None) -> TokenTextFastMCP:
    """The tasks app with its six tools. Raises when a registered tool strays from the contract. Without
    `authorization_service` (the facts app's, the authorization server) only worker keys are accepted."""
    tasks_uri = tasks_resource_uri(auth_config.mcp_resource_uri)
    issuer = urlparse(auth_config.mcp_resource_uri)    # the facts app is the authorization server
    app = TokenTextFastMCP(
        texts=texts, initial=initial_texts, read_only_tools=contract.READ_ONLY_TOOLS,
        name="alekbot-tasks",
        token_verifier=TasksTokenVerifier(external_tasks, gate, tasks_uri, authorization_service),
        auth=AuthSettings(
            issuer_url=AnyHttpUrl(f"{issuer.scheme}://{issuer.netloc}"),
            resource_server_url=AnyHttpUrl(tasks_uri),
            required_scopes=[TASKS_SCOPE],
            validate_token_resource=False,   # the verifier sets the resource itself
        ),
        streamable_http_path=TASKS_PATH,
        transport_security=build_transport_security(auth_config.mcp_resource_uri),
        stateless_http=True,  # Cloud Run may route subsequent requests to different instances
    )
    _register_tools(app, _TaskCaller(app, limiter, inactive_users, authorization_service), external_tasks)
    _check_contract(app)
    return app


def _register_tools(app: TokenTextFastMCP, call: "_TaskCaller", tasks: ExternalTaskService) -> None:

    @app.tool(name=contract.LIST_TASKS)
    async def list_tasks(ctx: Context) -> CallToolResult:
        async def op(p: WorkerPrincipal, texts: Mapping[str, str]) -> TaskToolAnswer:
            return answers.listed(texts, await tasks.list_for_worker(p))
        return await call.run(contract.LIST_TASKS, ctx, CALLS, op)

    @app.tool(name=contract.CLAIM_TASK)
    async def claim_task(ctx: Context) -> CallToolResult:
        async def op(p: WorkerPrincipal, texts: Mapping[str, str]) -> TaskToolAnswer:
            return answers.claimed(texts, await tasks.claim(p))
        return await call.run(contract.CLAIM_TASK, ctx, CLAIMS, op)

    @app.tool(name=contract.RENEW_LEASE)
    async def renew_lease(ctx: Context, task_id: _Text = "", lease_id: _Text = "") -> CallToolResult:
        async def op(p: WorkerPrincipal, texts: Mapping[str, str]) -> TaskToolAnswer:
            return _missing_id(texts, task_id, lease_id) or answers.renewed(
                texts, await tasks.renew(p, task_id, lease_id))
        return await call.run(contract.RENEW_LEASE, ctx, CALLS, op)

    @app.tool(name=contract.SUBMIT_TASK_RESULT)
    async def submit_task_result(ctx: Context, task_id: _Text = "", lease_id: _Text = "",
                                 result: _Raw = "") -> CallToolResult:
        async def op(p: WorkerPrincipal, texts: Mapping[str, str]) -> TaskToolAnswer:
            return _missing_id(texts, task_id, lease_id) or answers.submitted(
                texts, await tasks.submit(p, task_id, lease_id, result))
        return await call.run(contract.SUBMIT_TASK_RESULT, ctx, SUBMITS, op)

    @app.tool(name=contract.RELEASE_TASK)
    async def release_task(ctx: Context, task_id: _Text = "", lease_id: _Text = "") -> CallToolResult:
        async def op(p: WorkerPrincipal, texts: Mapping[str, str]) -> TaskToolAnswer:
            missing = _missing_id(texts, task_id, lease_id)
            if missing is not None:
                return missing
            await tasks.release(p, task_id, lease_id)
            return answers.released(texts, task_id)
        return await call.run(contract.RELEASE_TASK, ctx, CALLS, op)

    @app.tool(name=contract.FAIL_TASK)
    async def fail_task(ctx: Context, task_id: _Text = "", lease_id: _Text = "", reason: _Raw = "") -> CallToolResult:
        async def op(p: WorkerPrincipal, texts: Mapping[str, str]) -> TaskToolAnswer:
            missing = _missing_id(texts, task_id, lease_id)
            if missing is not None:
                return missing
            await tasks.fail(p, task_id, lease_id, reason or None)
            return answers.failed(texts, task_id)
        return await call.run(contract.FAIL_TASK, ctx, CALLS, op)


def _missing_id(texts: Mapping[str, str], task_id: str, lease_id: str) -> Optional[TaskToolAnswer]:
    """The correctable line of the first empty id, or None."""
    for name, value in (("task_id", task_id), ("lease_id", lease_id)):
        if not value:
            return answers.id_required(texts, name)
    return None


class _TaskCaller:
    """Runs one tool call: principal, lock, texts, limits, operation, span, log."""

    def __init__(self, app: TokenTextFastMCP, limiter: ExternalTaskRateLimiter, inactive_users: InactiveUserCheck,
                 authorization_service: Optional[MCPAuthorizationService] = None) -> None:
        self._app = app
        self._limiter = limiter
        self._inactive_users = inactive_users
        self._authorization = authorization_service

    async def run(self, tool: str, ctx: Context, scopes: _Scopes, op: _Op) -> CallToolResult:
        started = time.monotonic()
        token = _worker_token(ctx, tool)
        oauth = token.kind is McpTokenKind.OAUTH
        principal = WorkerPrincipal(user_id=token.user_id, account_id=token.account_id,
                                    worker_id=token.worker_id or "", via=SubmitVia.OAUTH if oauth else SubmitVia.KEY,
                                    client_id=token.client_id if oauth else None)
        who = f"user={principal.user_id[:8]} worker={fingerprint(principal.worker_id)}"
        # The account lock (CABINET_DEVICE_SESSIONS_RFC §7.8): a lock after the key was verified still refuses here.
        if await self._inactive_users.is_inactive(principal.user_id):
            logger.warning(f"🔒 [TaskMCP] tool={tool} {who} status=account_locked")
            return _answer(TaskToolAnswer(text=ACCOUNT_LOCKED_TOOL_ERROR, is_error=True, status="account_locked"))
        with start_span(f"mcp.tasks.{tool}", {"tool": tool}) as span:
            try:
                answer = await self._answer(tool, token, principal, scopes, op)
            except Exception as exc:
                logger.error(f"❌ [TaskMCP] tool={tool} {who} failed: {type(exc).__name__}")
                span.set_attribute("status", "failed")
                raise ToolError(f"{tool} failed: {type(exc).__name__}; try again later.") from None
            ms = int((time.monotonic() - started) * 1000)
            span.set_attribute("status", answer.status)
            span.set_attribute("ms", ms)
        logger.info(f"[TaskMCP] tool={tool} {who} status={answer.status} ms={ms}")
        return _answer(answer)

    async def _answer(self, tool: str, token: AlekAccessToken, principal: WorkerPrincipal, scopes: _Scopes,
                      op: _Op) -> TaskToolAnswer:
        try:
            texts = (await self._app.current_texts()).results
        except ConnectorTextsUnavailable:
            return TaskToolAnswer(text=NO_TEXTS, is_error=True, status="no_texts")
        key_digest: Optional[str] = token.token     # a key's stored digest; an OAuth token is never remembered
        if principal.via is SubmitVia.OAUTH:
            key_digest = None
            # The grant is re-checked on every call: a grant revoked in the Cabinet, or a user taken off the
            # whitelist, is refused now, not when the access token expires (EXTERNAL_TASK_QUEUE_RFC §5.4 step 3).
            if self._authorization is None or not await self._authorization.grant_allowed(principal.user_id,
                                                                                         token.grant_id):
                return TaskToolAnswer(text=texts[contract.REFUSED], is_error=True, status="grant_refused")
        worker = f"{principal.user_id}:{principal.worker_id}"
        for scope, limit in scopes:
            wait = await self._limiter.hit(scope, worker, limit, key_digest)
            if wait is not None:
                logger.warning(f"[TaskMCP] rate limited: {scope} tool={tool} worker={fingerprint(principal.worker_id)}")
                return answers.rate_limited(texts, wait)
        try:
            return await op(principal, texts)
        except ExternalTaskRefused as refused:
            return answers.refused(texts, refused.reason)


def _answer(answer: TaskToolAnswer) -> CallToolResult:
    return CallToolResult(content=[TextContent(type="text", text=answer.text)], isError=answer.is_error)


def _worker_token(ctx: Context, tool: str) -> AlekAccessToken:
    """The request's verified worker credential; ToolError when there is none (the SDK's auth middleware runs first,
    so only a request built around it gets here)."""
    request = ctx.request_context.request
    token = getattr(getattr(request, "user", None), "access_token", None)
    if (not isinstance(token, AlekAccessToken) or token.kind not in (McpTokenKind.WORKER_KEY, McpTokenKind.OAUTH)
            or not token.worker_id):
        logger.warning(f"[TaskMCP] tool={tool}: no verified worker credential on the request")
        raise ToolError(NO_CREDENTIAL)
    return token


def _check_contract(app: FastMCP) -> None:
    """Every registered tool is a contract tool with exactly the contract's parameters, in its order."""
    for info in app._tool_manager.list_tools():
        expected = contract.TOOL_PARAMETERS.get(info.name)
        params = tuple(info.parameters.get("properties", {}))
        if expected is None or params != expected:
            raise RuntimeError(f"tasks MCP tool {info.name} strays from the contract: {params} != {expected}")


async def build_tasks_mcp_or_none(*, external_tasks: Optional[ExternalTaskService], db_client: Any,
                                  env_config: EnvironmentConfig, user_repo: UserRepository,
                                  whitelist_repo: WhitelistRepository, token_repo: Optional[TokenRepository],
                                  alert_sink: Optional[AlertSinkPort], inactive_users: InactiveUserCheck,
                                  auth_config: AuthConfig,
                                  authorization_service: Optional[MCPAuthorizationService] = None,
                                  ) -> Optional[TokenTextFastMCP]:
    """The tasks app, or None: no task queue (`external_tasks` None), no prompt token store, or a failed build (the
    texts' first load included, which alerts through `alert_sink`). The facts and drive apps run on either way. The
    limiter is the REST endpoint's kind over the same counters, so both surfaces share each worker's buckets."""
    if external_tasks is None:
        return None
    try:
        if token_repo is None:
            raise RuntimeError("the tasks MCP resource needs the prompt token store, which did not initialize")
        texts = ConnectorTextService(token_repo, tool_token_id=TOOL_TEXTS_TOKEN, result_token_id=RESULT_TEXTS_TOKEN,
                                     required_tool_keys=contract.REQUIRED_TOOL_KEYS,
                                     required_result_keys=contract.REQUIRED_RESULT_KEYS, alert_sink=alert_sink,
                                     alert_subject=ALERT_SUBJECT)
        app = build_tasks_mcp_app(
            external_tasks=external_tasks, limiter=build_external_task_api_limiter(db_client, env_config),
            gate=ExternalWorkerUserGate(user_repo, whitelist_repo), inactive_users=inactive_users, texts=texts,
            initial_texts=await texts.texts(), auth_config=auth_config, authorization_service=authorization_service)
    except Exception as exc:
        logger.error(f"❌ Failed to initialize the tasks MCP resource: {type(exc).__name__}: {exc}", exc_info=True)
        logger.warning("🤖 Bot will continue without /tasks-mcp")
        return None
    logger.info("[TaskMCP] app built: texts loaded")
    return app

