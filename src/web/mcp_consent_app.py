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
       - If no alekbot Cabinet JWT cookie → redirect to /auth/login?next=...
       - Otherwise render the consent page with Approve / Deny buttons
    4. Browser → POST /mcp/consent  (form: req, action)
       - On approve: service mints a one-shot auth code, we 302 to
         client's redirect_uri?code=...&state=...
       - On deny: 302 to redirect_uri?error=access_denied&state=...
"""

from urllib.parse import urlencode

from quart import Blueprint, Response, abort, make_response, redirect, request

from ..services.mcp_authorization_service import (
    MCPAuthError,
    MCPAuthorizationService,
)
from ..services.session_service import SessionService
from ..utils.logger import logger


def build_mcp_consent_blueprint(
    mcp_service: MCPAuthorizationService,
    session_service: SessionService,
) -> Blueprint:
    bp = Blueprint("mcp_consent", __name__)

    def _require_user() -> tuple[str, str] | Response:
        """
        Return (user_id, account_id) from the Cabinet JWT cookie, or a
        redirect Response if the user is not logged in.
        """
        token = request.cookies.get("access_token")
        if not token:
            next_url = request.url
            return redirect(f"/auth/login?next={next_url}")
        try:
            payload = session_service.verify_access_token(token)
        except Exception as e:  # noqa: BLE001
            logger.warning(f"MCP consent: invalid Cabinet token — {e}")
            next_url = request.url
            return redirect(f"/auth/login?next={next_url}")
        return payload["sub"], payload["account_id"]

    def _render_consent_html(
        *,
        req_jwt: str,
        client_name: str,
        scopes: list[str],
        error: str | None = None,
    ) -> str:
        scope_list = "".join(f"<li><code>{s}</code></li>" for s in scopes) or "<li>(none)</li>"
        error_block = (
            f'<div class="error">{error}</div>' if error else ""
        )
        return f"""<!doctype html>
<html lang="en">
<head>
    <meta charset="utf-8" />
    <title>Authorize MCP connector · alekbot</title>
    <style>
        body {{ font-family: system-ui, -apple-system, sans-serif; max-width: 520px;
               margin: 60px auto; padding: 0 24px; color: #1a1a1a; }}
        h1 {{ font-size: 22px; margin-bottom: 8px; }}
        p {{ color: #555; line-height: 1.5; }}
        .client {{ padding: 12px 16px; background: #f6f6f6; border-radius: 8px;
                   margin: 16px 0; }}
        .client strong {{ color: #1a1a1a; }}
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
    <h1>Authorize MCP connector</h1>
    {error_block}
    <p>An external application is asking to access your alekbot
       memory via the Model Context Protocol.</p>
    <div class="client"><strong>{client_name}</strong></div>
    <p>Requested scopes:</p>
    <ul>{scope_list}</ul>
    <p>If you approve, this application will be able to retrieve your
       biographical facts and memories on your behalf.</p>
    <form method="POST" action="/mcp/consent">
        <input type="hidden" name="req" value="{req_jwt}" />
        <div class="actions">
            <button class="approve" name="action" value="approve">Approve</button>
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

        auth_result = _require_user()
        if isinstance(auth_result, Response):
            return auth_result

        try:
            parsed = mcp_service.verify_consent_request(req_jwt)
        except MCPAuthError as e:
            logger.warning(f"MCP consent: bad consent JWT — {e}")
            abort(400, description=f"invalid consent request: {e}")

        client = await mcp_service.get_client(parsed.client_id)
        client_name = client.client_name if client else parsed.client_id

        html = _render_consent_html(
            req_jwt=req_jwt,
            client_name=client_name,
            scopes=parsed.scopes,
        )
        resp = await make_response(html)
        resp.headers["Content-Type"] = "text/html; charset=utf-8"
        resp.headers["Cache-Control"] = "no-store"
        return resp

    @bp.route("/mcp/consent", methods=["POST"])
    async def post_consent():
        form = await request.form
        req_jwt = form.get("req")
        action = form.get("action")
        if not req_jwt or not action:
            abort(400, description="missing form fields")

        auth_result = _require_user()
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

        try:
            code, _ = await mcp_service.issue_auth_code_for_consent(
                req_jwt, user_id=user_id, account_id=account_id
            )
        except MCPAuthError as e:
            logger.warning(f"MCP consent: failed to issue code — {e}")
            abort(400, description=str(e))

        qs = {"code": code}
        if parsed.state is not None:
            qs["state"] = parsed.state
        return redirect(f"{parsed.redirect_uri}?{urlencode(qs)}")

    return bp
