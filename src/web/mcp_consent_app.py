"""
MCP OAuth consent blueprint.

Single route pair (`GET /mcp/consent` + `POST /mcp/consent`) that the
`mcp` SDK's /authorize handler redirects to after receiving an
authorization request. The SDK handles the OAuth protocol envelope;
this blueprint owns only the browser-facing consent UX and the
"user says approve → issue auth code" transition.

Flow:
    1. Claude.ai → GET /mcp/authorize?...  (SDK handles)
    2. SDK → provider.authorize() → returns `{consent_base}?req=<jwt>`
    3. Browser → GET /mcp/consent?req=<jwt>
       - Without a live Cabinet session → redirect to /auth/login?next=...
       - Otherwise render the consent page with Approve / Deny buttons
    4. Browser → POST /mcp/consent  (form: req, action)
       - On approve: service mints a one-shot auth code, we 302 to
         client's redirect_uri?code=...&state=...
       - On deny: 302 to redirect_uri?error=access_denied&state=...

The task queue's worker resource (EXTERNAL_TASK_QUEUE_RFC §5.4 "OAuth"): a grant of it is bound to one worker, so the
page lists the user's active workers (`ExternalTaskService.list_workers`, which also checks the lock and the flag) as
radio buttons and Approve needs one picked. With no worker, the flag off, a locked account or no task queue on this
server the page explains why and offers Deny only. The POST reads the workers again and refuses (400, no code) a
`worker_id` that is not one of them; the service then stamps it on the code.
"""

from html import escape
from typing import TYPE_CHECKING, List, Optional, Tuple
from urllib.parse import quote, urlencode

from quart import Blueprint, Response, abort, make_response, redirect, request

from ..domain.external_task_refusal import ExternalTaskRefused, TaskRefusal
from ..domain.external_worker import ExternalWorker
from ..domain.mcp_resource import is_drive_grant, is_tasks_grant
from ..services.mcp_authorization_service import (
    MCPAuthError,
    MCPAuthorizationService,
)
from ..utils.logger import logger
from .cabinet_auth import CabinetSessionVerifier

if TYPE_CHECKING:
    from ..services.external_task_service import ExternalTaskService

# The page has no script and no external resource: only its own inline <style>. It must never be framed (the
# Approve button), and its URL carries the consent request token, so it sends no referrer.
_CONSENT_PAGE_HEADERS = {
    "Content-Security-Policy": (
        "default-src 'none'; style-src 'unsafe-inline'; base-uri 'none'; frame-ancestors 'none'"
    ),
    "X-Frame-Options": "DENY",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
}

# Why a task connector cannot be approved: the page shows this and offers Deny only.
_TASKS_UNAVAILABLE = "The external task queue is not available on this server."
_TASKS_NOT_ENABLED = "The external task queue is not switched on for your account."
_TASKS_LOCKED = "This account is locked: it can connect nothing until the administrator unlocks it."
_TASKS_NO_WORKERS = (
    "You have no workers yet. Add one in the Cabinet (Settings \u2192 Connections \u2192 External workers), "
    "then connect again."
)


def build_mcp_consent_blueprint(
    mcp_service: MCPAuthorizationService,
    cabinet_sessions: CabinetSessionVerifier,
    external_tasks: Optional["ExternalTaskService"] = None,
) -> Blueprint:
    """`external_tasks` lists the workers a task-queue grant may be bound to; without it such a grant offers Deny only."""
    bp = Blueprint("mcp_consent", __name__)

    async def _workers(user_id: str) -> Tuple[List[ExternalWorker], Optional[str]]:
        """The user's active workers, or why there are none to pick (a text for the page)."""
        if external_tasks is None:
            return [], _TASKS_UNAVAILABLE
        try:
            workers = await external_tasks.list_workers(user_id)
        except ExternalTaskRefused as refused:
            return [], _TASKS_LOCKED if refused.reason is TaskRefusal.ACCOUNT_LOCKED else _TASKS_NOT_ENABLED
        return workers, (None if workers else _TASKS_NO_WORKERS)

    async def _require_user() -> tuple[str, str] | Response:
        """
        Return (user_id, account_id) of the live Cabinet session, or a
        redirect Response to the sign-in without one (a revoked device
        grants nothing: the one verifier asks the session record).
        """
        identity = await cabinet_sessions.identify()
        if identity is None:
            return redirect(f"/auth/login?next={quote(request.full_path.rstrip('?'), safe='')}")
        return identity.user_id, identity.account_id

    def _render_consent_html(
        *,
        req_jwt: str,
        client_name: str,
        scopes: list[str],
        resource: str | None,
        error: str | None = None,
        workers: Optional[List[ExternalWorker]] = None,
        blocked: Optional[str] = None,
    ) -> str:
        # Every value is text: the client name comes from anonymous registration (RFC 7591).
        scope_list = "".join(f"<li><code>{escape(s)}</code></li>" for s in scopes) or "<li>(none)</li>"
        error_block = (
            f'<div class="error">{escape(error)}</div>' if error else ""
        )
        client_name = escape(client_name)
        req_jwt = escape(req_jwt, quote=True)
        if is_drive_grant(resource):
            heading = "Authorize drive connector"
            intro_block = (
                f"<p><strong>{client_name}</strong> is asking to read your drives through alekbot. "
                "It can search, open and quote files only in the folders you choose for it in the Cabinet "
                "(Drives \u2192 Connector access); until you choose, it sees nothing. "
                "It cannot change, move or delete anything.</p>"
            )
            closing_block = ""
        elif is_tasks_grant(resource):
            heading = "Authorize task connector"
            intro_block = (
                f"<p><strong>{client_name}</strong> is asking to work on tasks from your alekbot external task queue "
                "as one of your workers. It sees only the tasks handed to the worker you pick here, and its results "
                "come back to you as outside content. It cannot see your memory or your drives.</p>"
            )
            closing_block = _worker_picker(workers or [], blocked)
        else:
            heading = "Authorize MCP connector"
            intro_block = (
                "<p>An external application is asking to access your alekbot\n"
                "       memory via the Model Context Protocol.</p>\n"
                f'    <div class="client"><strong>{client_name}</strong></div>'
            )
            closing_block = (
                "<p>If you approve, this application will be able to retrieve your\n"
                "       biographical facts and memories on your behalf.</p>"
            )
        return f"""<!doctype html>
<html lang="en">
<head>
    <meta charset="utf-8" />
    <title>{heading} · alekbot</title>
    <style>
        body {{ font-family: system-ui, -apple-system, sans-serif; max-width: 520px;
               margin: 60px auto; padding: 0 24px; color: #1a1a1a; }}
        h1 {{ font-size: 22px; margin-bottom: 8px; }}
        p {{ color: #555; line-height: 1.5; }}
        .client {{ padding: 12px 16px; background: #f6f6f6; border-radius: 8px;
                   margin: 16px 0; }}
        .client strong {{ color: #1a1a1a; }}
        .worker {{ display: block; padding: 8px 0; color: #1a1a1a; }}
        ul {{ margin: 8px 0 16px 0; padding-left: 24px; }}
        .actions {{ display: flex; gap: 12px; margin-top: 24px; }}
        button {{ padding: 10px 20px; border-radius: 6px; font-size: 15px;
                  font-weight: 500; border: none; cursor: pointer; }}
        button.approve {{ background: #1a73e8; color: white; }}
        button.deny    {{ background: #f1f1f1; color: #333; }}
        .error {{ background: #fde8e8; color: #a32020; padding: 10px 14px;
                  border-radius: 6px; margin-bottom: 16px; font-size: 14px; }}
        code {{ background: #eee; padding: 1px 6px; border-radius: 4px;
                font-size: 13px; }}
    </style>
</head>
<body>
    <h1>{heading}</h1>
    {error_block}
    {intro_block}
    <p>Requested scopes:</p>
    <ul>{scope_list}</ul>
    {"" if is_tasks_grant(resource) else closing_block}
    <form method="POST" action="/mcp/consent">
        <input type="hidden" name="req" value="{req_jwt}" />
        {closing_block if is_tasks_grant(resource) else ""}
        <div class="actions">
            {"" if blocked else '<button class="approve" name="action" value="approve">Approve</button>'}
            <button class="deny"    name="action" value="deny">Deny</button>
        </div>
    </form>
</body>
</html>"""

    @bp.route("/mcp/consent", methods=["GET"])
    async def get_consent():
        req_jwt = request.args.get("req")
        if not req_jwt:
            abort(400, description="missing req parameter")

        auth_result = await _require_user()
        if isinstance(auth_result, Response):
            return auth_result

        try:
            parsed = mcp_service.verify_consent_request(req_jwt)
        except MCPAuthError as e:
            logger.warning(f"MCP consent: bad consent JWT — {e}")
            abort(400, description=f"invalid consent request: {e}")

        client = await mcp_service.get_client(parsed.client_id)
        client_name = client.client_name if client else parsed.client_id
        workers: List[ExternalWorker] = []
        blocked = None
        if is_tasks_grant(parsed.resource):
            workers, blocked = await _workers(auth_result[0])

        html = _render_consent_html(
            req_jwt=req_jwt,
            client_name=client_name,
            scopes=parsed.scopes,
            resource=parsed.resource,
            workers=workers,
            blocked=blocked,
        )
        resp = await make_response(html)
        resp.headers["Content-Type"] = "text/html; charset=utf-8"
        resp.headers["Cache-Control"] = "no-store"
        resp.headers.update(_CONSENT_PAGE_HEADERS)
        return resp

    @bp.route("/mcp/consent", methods=["POST"])
    async def post_consent():
        form = await request.form
        req_jwt = form.get("req")
        action = form.get("action")
        if not req_jwt or not action:
            abort(400, description="missing form fields")

        auth_result = await _require_user()
        if isinstance(auth_result, Response):
            return auth_result
        user_id, account_id = auth_result

        # Parse the request once so we always have redirect_uri + state
        # — even when the user denies, we must redirect per RFC 6749.
        try:
            parsed = mcp_service.verify_consent_request(req_jwt)
        except MCPAuthError as e:
            logger.warning(f"MCP consent: bad consent JWT (POST) — {e}")
            abort(400, description=f"invalid consent request: {e}")

        if action == "deny":
            qs = {"error": "access_denied"}
            if parsed.state is not None:
                qs["state"] = parsed.state
            return redirect(f"{parsed.redirect_uri}?{urlencode(qs)}")

        if action != "approve":
            abort(400, description="unknown action")

        bound: dict[str, str] = {}    # a task grant's worker; every other grant carries none
        if is_tasks_grant(parsed.resource):
            # Read the workers again: the page may be stale, and the form value is the browser's.
            workers, _ = await _workers(user_id)
            worker_id = form.get("worker_id")
            if not worker_id or worker_id not in {w.worker_id for w in workers}:
                logger.warning(f"MCP consent: task grant refused for user={user_id[:8]}: no active worker picked")
                abort(400, description="pick one of your active workers")
            bound = {"worker_id": worker_id}

        try:
            code, _ = await mcp_service.issue_auth_code_for_consent(
                req_jwt, user_id=user_id, account_id=account_id, **bound
            )
        except MCPAuthError as e:
            logger.warning(f"MCP consent: failed to issue code — {e}")
            abort(400, description=str(e))

        qs = {"code": code}
        if parsed.state is not None:
            qs["state"] = parsed.state
        return redirect(f"{parsed.redirect_uri}?{urlencode(qs)}")

    return bp



def _worker_picker(workers: List[ExternalWorker], blocked: Optional[str]) -> str:
    """The radio list of a task grant's workers (each label and description is text, none picked in advance), or why
    there is none to pick."""
    if blocked:
        return f'<div class="error">{escape(blocked)}</div>'
    options = []
    for worker in workers:
        description = f" \u2014 {escape(worker.description)}" if worker.description else ""
        options.append(
            f'<label class="worker"><input type="radio" name="worker_id" '
            f'value="{escape(worker.worker_id, quote=True)}" required /> '
            f"<strong>{escape(worker.label)}</strong>{description}</label>"
        )
    return '<p>Pick the worker it acts as:</p>\n        <div class="workers">' + "".join(options) + "</div>"
