"""
OAuth Web Application (OAuth Multi-Tenant Session 4).

Quart web application for OAuth authentication endpoints.
Provides web UI authentication via Google OAuth / Firebase.

RFC: docs/10_rfcs/MULTI_TENANT_OAUTH_RFC.md

Endpoints:
- GET  /auth/login - The sign-in page: passkey first, Google second (CABINET_DEVICE_SESSIONS_RFC §7.3); with `?provider=`,
  the Google redirect itself
- GET  /auth/login/google - Redirect to the OAuth provider (the page's "Sign in with Google" and its fallback)
- GET  /auth/callback - OAuth callback handler
- POST /auth/refresh - Refresh the device session (CABINET_DEVICE_SESSIONS_RFC §7.1)
- POST /auth/logout - Logout (revoke the device session, clear cookies)
- GET  /auth/me - Get current user info
- GET  /auth/connect/<kind> - Start an integration connect (or, with ?credential=<id>, a reconnect)
- GET  /auth/connect/callback - The one integration connect callback (MULTI_DRIVE_RFC §5.2)
"""
import os
import secrets
from typing import Dict, Optional, Tuple, TYPE_CHECKING
from urllib.parse import urlencode
from quart import Blueprint, request, jsonify, redirect, make_response, send_file

from ..services.authentication_service import AuthenticationService
from ..services.device_session_service import DeviceSessionService
from ..services.auth_provider_registry import AuthProviderRegistry
from ..config.auth import AuthConfig
from ..domain.device_label import describe_device
from ..domain.device_session import SessionAuthKind, SessionRevokeReason
from ..domain.device_session_policy import RefreshOutcome
from ..domain.exceptions import AccessDeniedError
from ..domain.safe_redirect import safe_return_path
from ..utils.logger import logger
from .cabinet_auth import (
    REFRESH_COOKIE, CabinetSessionVerifier, Refusal, clear_session_cookies, set_session_cookies,
)

if TYPE_CHECKING:
    from ..services.invite_code_service import InviteCodeService
    from ..services.oauth_connect_service import OAuthConnectService
    from ..composition.connector_registry import ConnectorRegistry


_ACCESS_DENIED_PAGE = (
    "<!doctype html><html><head><meta charset=\"utf-8\">"
    "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">"
    "<title>Access not authorized</title></head>"
    "<body style=\"font-family:system-ui,sans-serif;max-width:32rem;margin:4rem auto;padding:0 1rem\">"
    "<h1>Access not authorized</h1>"
    "<p>This Google account is not allowed to sign in.</p>"
    "</body></html>"
)

_CABINET = "/cabinet"
_LOGIN = "/auth/login?next=/cabinet"


def _with_query(path: str, params: Dict[str, str]) -> str:
    """`path` with `params` appended to its query string (encoded)."""
    return f"{path}{'&' if '?' in path else '?'}{urlencode(params)}"


def create_oauth_blueprint(
    auth_service: AuthenticationService,
    cabinet_sessions: CabinetSessionVerifier,
    device_sessions: DeviceSessionService,
    auth_registry: AuthProviderRegistry,
    auth_config: AuthConfig,
    invite_service: Optional['InviteCodeService'] = None,
    connect_service: Optional['OAuthConnectService'] = None,
    connectors: Optional['ConnectorRegistry'] = None,
) -> Blueprint:
    """
    Create Quart Blueprint with OAuth endpoints.

    Args:
        auth_service: Authentication service for OAuth flows
        cabinet_sessions: the one verifier of the access token (`cabinet_auth.py`)
        device_sessions: the revocable device sessions a sign-in starts, a refresh rotates and a logout revokes
        auth_registry: OAuth provider registry
        auth_config: OAuth configuration
        invite_service: Optional service to consume invite codes on callback
        connect_service: the integration connect flow (`/auth/connect/<kind>`, `/auth/connect/callback`); without it
            both answer 501 (the start also without `connectors`)
        connectors: the connector registry: which kinds exist and which realms have an edge

    Returns:
        Configured Blueprint
    """
    bp = Blueprint("oauth", __name__)

    async def _session() -> Optional[Tuple[str, str]]:
        """(user_id, account_id) of the signed-in Cabinet session, or None without a live one."""
        identity = await cabinet_sessions.identify()
        return None if identity is None else (identity.user_id, identity.account_id)

    async def _signed_out(body: Dict[str, str], status: int):
        """A refusal that also deletes both session cookies: the session behind them can no longer refresh."""
        response = await make_response(jsonify(body), status)
        clear_session_cookies(response)
        return response

    static_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")

    # ========================================================================
    # GET /auth/login - The sign-in page (CABINET_DEVICE_SESSIONS_RFC §7.3)
    # ========================================================================
    @bp.route("/auth/login", methods=["GET"])
    async def oauth_login():
        """
        The sign-in page: "Sign in with Face ID / passkey" first, "Sign in with Google" second. The page is static; it
        reads `next` (and an invite `code`) from its own URL and hands them to `/auth/passkey/verify` or
        `/auth/login/google`, which check them. A browser without WebAuthn, a cancelled sheet, no passkey or a refused
        one fall through to Google. With `?provider=` (a provider chosen explicitly) this is the Google redirect itself.
        """
        if request.args.get("provider"):
            return await _google_login()
        response = await send_file(os.path.join(static_dir, "sign_in.html"), mimetype="text/html")
        response.headers["Cache-Control"] = "no-cache"
        return response

    # ========================================================================
    # GET /auth/login/google - Redirect to OAuth provider
    # ========================================================================
    @bp.route("/auth/login/google", methods=["GET"])
    async def oauth_login_google():
        """The Google redirect of the sign-in page."""
        return await _google_login()

    async def _google_login():
        """
        Initiate OAuth flow by redirecting to OAuth provider.

        Query parameters:
        - provider: OAuth provider name (default: from config)
        - next: URL to redirect after login (e.g. /join?code=...)
        - code: Invite code to carry over

        Flow:
        1. Generate CSRF state token
        2. Store state in session cookie
        3. Generate authorization URL (embed next/code in state if supported, or cookie)
        4. Redirect user to OAuth provider
        """
        provider_name = request.args.get("provider")
        next_url = request.args.get("next")
        if next_url:
            next_url = safe_return_path(next_url)  # same-site relative path only (open redirect)
        invite_code = request.args.get("code")

        try:
            # Get OAuth provider
            auth_provider = auth_registry.get_provider(provider_name)

            # Generate CSRF state token
            state = secrets.token_urlsafe(32)

            # Generate authorization URL
            # Note: We rely on cookies for next_url/invite_code state preservation 
            # across the OAuth redirect dance because state param size is limited
            auth_url = auth_provider.get_authorization_url(
                state=state,
                redirect_uri=auth_config.oauth_redirect_uri,
            )

            logger.info(f"🔐 OAuth login initiated - provider: {auth_provider.get_provider_name()}")

            # Create response with state cookie
            response = await make_response(redirect(auth_url))
            
            # Store auth state params in cookies
            if next_url:
                response.set_cookie("auth_next", next_url, max_age=600, httponly=True, secure=True, samesite="lax")
            if invite_code:
                response.set_cookie("invite_code", invite_code, max_age=600, httponly=True, secure=True, samesite="lax")
            
            response.set_cookie(
                "oauth_state",
                state,
                max_age=600,  # 10 minutes
                httponly=True,
                secure=True,  # HTTPS only in production
                samesite="lax",
            )

            return response

        except Exception as e:
            logger.error(f"❌ OAuth login failed: {e}")
            return jsonify({"error": "OAuth login failed"}), 500

    # ========================================================================
    # GET /auth/callback - OAuth callback handler
    # ========================================================================
    @bp.route("/auth/callback", methods=["GET"])
    async def oauth_callback():
        """
        Handle OAuth callback after user authorization.

        Query parameters:
        - code: Authorization code from OAuth provider
        - state: CSRF protection token

        Flow:
        1. Verify CSRF state token
        2. Exchange code for tokens
        3. Register or authenticate user
        4. Start a device session (label and platform hint from the User-Agent, untrusted)
        5. Set the session cookies
        6. Check for pending invite code -> consume
        7. Redirect to web UI (or next_url)
        """
        code = request.args.get("code")
        state = request.args.get("state")

        if not code:
            logger.warning("⚠️ OAuth callback missing code parameter")
            return jsonify({"error": "Missing authorization code"}), 400

        # Verify CSRF state
        stored_state = request.cookies.get("oauth_state")
        if not stored_state or stored_state != state:
            logger.warning("⚠️ OAuth callback CSRF validation failed")
            return jsonify({"error": "Invalid state parameter"}), 400

        try:
            # Handle OAuth callback
            user, account, oauth_tokens = await auth_service.handle_oauth_callback(
                code=code,
                redirect_uri=auth_config.oauth_redirect_uri,
            )

            # Handle pending invite code
            pending_code = request.cookies.get("invite_code")
            if pending_code and invite_service:
                try:
                    await invite_service.consume_team_invite(pending_code, user.user_id)
                    logger.info(f"✅ Auto-consumed invite code for user {user.user_id}")
                    # Refresh account as it might have changed (e.g. joined team)
                    # For simplicity, we just proceed, user will see updated state on reload
                except Exception as e:
                    logger.error(f"❌ Failed to auto-consume invite code: {e}")

            next_url = safe_return_path(request.cookies.get("auth_next"))  # a pre-fix cookie may hold anything
            label, platform_hint = describe_device(request.headers.get("User-Agent", ""), next_url)
            session, access_token, refresh_secret = await device_sessions.start(
                user, account, label=label, platform_hint=platform_hint, auth_kind=SessionAuthKind.GOOGLE)

            logger.info(
                f"✅ OAuth callback success - user: {user.user_id}, "
                f"account: {account.account_id}, sid: {session.sid[:8]}"
            )

            response = await make_response(redirect(next_url))
            set_session_cookies(response, session, access_token, refresh_secret)

            # Clear CSRF state cookie
            response.delete_cookie("oauth_state")

            return response

        except AccessDeniedError:
            # Whitelist gate: no user created, no session cookies issued.
            # The reason is logged by AuthenticationService, never shown to the caller.
            response = await make_response(_ACCESS_DENIED_PAGE, 403)
            response.headers["Content-Type"] = "text/html; charset=utf-8"
            response.delete_cookie("oauth_state")
            return response
        except Exception as e:
            logger.error(f"❌ OAuth callback failed: {e}")
            return jsonify({"error": "OAuth callback failed"}), 500

    # ========================================================================
    # POST /auth/refresh - Refresh the device session (CABINET_DEVICE_SESSIONS_RFC §7.1 "Refresh")
    # ========================================================================
    @bp.route("/auth/refresh", methods=["POST"])
    async def refresh_token_endpoint():
        """
        Renew the device session of this browser.

        Cookies: the access cookie (its JWT may be expired: only its `sid` and user are taken) and the refresh cookie
        (the session's current secret).

        Flow:
        1. No verifiable access cookie or no refresh cookie: 401, cookies cleared
        2. `AuthenticationService.resume_session` (active user, whitelisted sign-in email, existing account; refused:
           401, cookies cleared). Role and tier of the new token come from the stored user and account
        3. `DeviceSessionService.refresh`: ROTATE sets both cookies, GRACE (a second tab within the rotation grace)
           only the access cookie, REPLAY (the session is now revoked) and DEAD answer 401 and clear the cookies
        4. The body carries `expires_at`, the new access token's expiry (Unix seconds); never the token itself
        """
        presented = cabinet_sessions.presented_session()
        secret = request.cookies.get(REFRESH_COOKIE)
        if presented is None or not secret:
            return await _signed_out({"error": "No session to refresh"}, 401)
        user_id, sid = presented
        try:
            user, account = await auth_service.resume_session(user_id)
            result = await device_sessions.refresh(sid, secret, user, account)
        except AccessDeniedError:
            return await _signed_out({"error": "Session no longer valid"}, 401)
        except Exception as e:
            # Log detail server-side; never leak exception internals to the client.
            logger.error(f"❌ Session refresh failed: sid={sid[:8]}: {e}", exc_info=True)
            return jsonify({"error": "Token refresh failed"}), 500
        if (result.outcome not in (RefreshOutcome.ROTATE, RefreshOutcome.GRACE) or result.session is None
                or result.access_token is None):
            return await _signed_out({"error": "Session no longer valid"}, 401)
        logger.info(f"🔄 Device session refreshed - user: {user_id}, sid: {sid[:8]}, outcome: {result.outcome.value}")
        response = await make_response(jsonify({
            "success": True, "expires_at": cabinet_sessions.access_expiry(result.access_token)}))
        set_session_cookies(response, result.session, result.access_token, result.refresh_secret)
        return response

    # ========================================================================
    # POST /auth/logout - Logout (revoke the device session, clear cookies)
    # ========================================================================
    @bp.route("/auth/logout", methods=["POST"])
    async def logout():
        """
        Revoke this browser's device session (named by the access cookie, expired or not), then clear both cookies.
        A failed revocation still clears the cookies and answers 500: this browser is signed out, the record stays
        live until its idle end, and the failure is logged.
        """
        presented = cabinet_sessions.presented_session()
        revoke_failed = False
        if presented is not None:
            user_id, sid = presented
            try:
                await device_sessions.revoke(user_id, sid, SessionRevokeReason.LOGOUT)
            except Exception as e:
                logger.error(f"💥 Logout could not revoke the device session sid={sid[:8]}: {e}", exc_info=True)
                revoke_failed = True
        if revoke_failed:
            return await _signed_out({"error": "Logout failed"}, 500)
        logger.info("👋 User logged out")
        return await _signed_out({"success": True, "message": "Logged out"}, 200)

    # ========================================================================
    # GET /auth/me - Get current user info
    # ========================================================================
    @bp.route("/auth/me", methods=["GET"])
    async def get_current_user():
        """
        Get current authenticated user information.

        A Bearer header or the access cookie, through the one verifier (a live device session).

        Returns:
        - User profile (user_id, email, external_user_id)
        - Account info (account_id, tier, role)
        - expires_at: the access token's expiry (Unix seconds), so a page can refresh before it
        """
        check = await cabinet_sessions.verify()
        identity = check.identity
        if identity is None:
            error = {Refusal.EXPIRED: "Token expired", Refusal.INVALID: "Invalid token",
                     Refusal.ENDED: "Invalid token"}.get(check.refusal, "Not authenticated")
            return jsonify({"error": error}), 401
        return jsonify({
            "user": {
                "user_id": identity.user_id,
                "email": identity.email,
                "external_user_id": identity.external_user_id,
            },
            "account": {
                "account_id": identity.account_id,
                "tier": identity.tier,
                "role": identity.role,
            },
            "expires_at": identity.expires_at,
        })

    # ========================================================================
    # POST /auth/link-oauth - Link OAuth to existing user
    # ========================================================================
    @bp.route("/auth/link-oauth", methods=["POST"])
    async def link_oauth_to_existing_user():
        """
        Link Google OAuth identity to existing user (e.g., YOUR_USER_ID).

        Use case: User already logged in via Slack/Telegram, wants to add Google OAuth.

        Request:
        - the access token (cookie or Authorization header) of a live device session
        - code: OAuth authorization code (body)
        - state: CSRF token (body)

        Flow:
        1. Verify access token → get current user_id
        2. Verify CSRF state token
        3. Exchange OAuth code for tokens → get external_user_id
        4. Check if external_user_id already linked to another user (error if yes)
        5. Link external_user_id to current user
        6. Return success

        Returns:
        - 200: Success (OAuth linked to user)
        - 400: Missing parameters or invalid state
        - 401: Invalid or expired access token
        - 409: OAuth identity already linked to another user
        - 500: Server error
        """
        if not cabinet_sessions.presents_token():
            return jsonify({"error": "Not authenticated. Please login first."}), 401

        # Get OAuth code and state from request body
        body = await request.get_json()
        code = body.get("code")
        state = body.get("state")

        if not code:
            return jsonify({"error": "Missing OAuth authorization code"}), 400

        # Verify CSRF state
        stored_state = request.cookies.get("oauth_state")
        if not stored_state or stored_state != state:
            logger.warning("⚠️ OAuth link CSRF validation failed")
            return jsonify({"error": "Invalid state parameter"}), 400

        check = await cabinet_sessions.verify()
        identity = check.identity
        if identity is None:
            error = ("Access token expired. Please login again." if check.refusal is Refusal.EXPIRED
                     else "Invalid access token")
            return jsonify({"error": error}), 401
        user_id = identity.user_id

        try:
            logger.info(f"🔗 Linking OAuth to existing user: {user_id}")

            # 2. Link OAuth identity to user (handles all logic internally)
            user = await auth_service.link_oauth_identity(
                user_id=user_id,
                code=code,
                redirect_uri=auth_config.oauth_redirect_uri
            )

            logger.info(f"✅ OAuth linked successfully: user {user_id} → {user.external_user_id}")

            response = await make_response(
                jsonify({
                    "success": True,
                    "message": "Google OAuth linked successfully",
                    "user": {
                        "user_id": user.user_id,
                        "external_user_id": user.external_user_id,
                        "email": user.email,
                    }
                })
            )

            # Clear CSRF state cookie
            response.delete_cookie("oauth_state")

            return response

        except AccessDeniedError:
            return jsonify({"error": "Access not authorized"}), 403
        except ValueError as e:
            logger.error(f"❌ OAuth link validation error: {e}")
            return jsonify({"error": str(e)}), 400
        except Exception as e:
            # Log detail server-side; never leak exception internals to the client.
            logger.error(f"❌ OAuth link failed: {e}", exc_info=True)
            return jsonify({"error": "Failed to link OAuth identity"}), 500

    # ========================================================================
    # GET /auth/connect/<kind> — start an integration connect (MULTI_DRIVE_RFC §5.2)
    # ========================================================================
    @bp.route("/auth/connect/<kind>", methods=["GET"])
    async def connect_start(kind: str):
        """
        Start a connect of the connector `kind`; `?credential=<id>` starts the reconnect of that stored credential.

        Requires the Cabinet session (anonymous → the sign-in). Unknown kind → 404; a kind whose realm has no edge
        on this deployment → 501. The flow is stored server-side by the connect service; no cookie is set here.
        """
        if connect_service is None or connectors is None:
            return jsonify({"error": "Integrations not configured"}), 501
        session = await _session()
        if session is None:
            return redirect(_LOGIN)
        user_id = session[0]
        spec = connectors.specs().get(kind)
        if spec is None:
            return jsonify({"error": "Unknown connector"}), 404
        if not spec.offered:
            return jsonify({"error": "Unknown connector"}), 404
        if spec.realm not in connectors.edges():
            return jsonify({"error": f"{spec.label} integration not configured"}), 501
        credential_id = request.args.get("credential") or None
        try:
            auth_url = await connect_service.start(user_id, spec.kind, credential_id=credential_id,
                                                    add=request.args.get("intent") == "add")
        except ValueError:
            # a reconnect of a credential the user does not hold (gone meanwhile, or not theirs)
            logger.warning(f"⚠️ Reconnect of an unknown credential: user={user_id[:8]} kind={spec.kind}")
            return redirect(_with_query(_CABINET, {"connect_error": "state", "kind": spec.kind}))
        return redirect(auth_url)

    # ========================================================================
    # GET /auth/connect/callback — the one callback of every realm (MULTI_DRIVE_RFC §5.2, E4)
    # ========================================================================
    @bp.route("/auth/connect/callback", methods=["GET"])
    async def connect_callback():
        """
        Finish a connect: the provider's refusal → `?connect_error=denied`; no state / no code → `state` / `no_code`;
        otherwise the connect service consumes the flow against the Cabinet session (none → the sign-in) and the user
        returns to the flow's return path with `?connected=<kind>` or `?connect_error=<status>&kind=<kind>`.
        """
        if connect_service is None:
            return jsonify({"error": "Integrations not configured"}), 501
        error = request.args.get("error")
        if error:
            logger.warning(f"⚠️ Integration consent refused at the provider: {error[:40]}")
            return redirect(_with_query(_CABINET, {"connect_error": "denied"}))
        state, code = request.args.get("state"), request.args.get("code")
        if not state:
            return redirect(_with_query(_CABINET, {"connect_error": "state"}))
        if not code:
            return redirect(_with_query(_CABINET, {"connect_error": "no_code"}))
        session = await _session()
        if session is None or not session[1]:
            return redirect(_LOGIN)
        user_id, account_id = session
        try:
            outcome = await connect_service.complete(state, code, session_user_id=user_id,
                                                     session_account_id=account_id)
        except Exception as exc:
            logger.error(f"💥 Integration connect failed for user={user_id[:8]}: {type(exc).__name__}", exc_info=True)
            return redirect(_with_query(_CABINET, {"connect_error": "exchange"}))
        target = safe_return_path(outcome.return_path)
        if outcome.status == "connected" and outcome.kind:
            return redirect(_with_query(target, {"connected": outcome.kind}))
        params = {"connect_error": outcome.status}
        if outcome.kind:
            params["kind"] = outcome.kind
        return redirect(_with_query(target, params))

    # ========================================================================
    # GET /auth/session.js - the shared silent-refresh script of the Cabinet and the call pages
    # ========================================================================
    @bp.route("/auth/session.js", methods=["GET"])
    async def session_script():
        """Anonymous by design: the script holds no secret, and a page must load it before it has a session.
        The main app has no static folder, so this is an explicit route (CABINET_DEVICE_SESSIONS_RFC §7.4)."""
        response = await send_file(os.path.join(static_dir, "session.js"), mimetype="application/javascript")
        # Revalidate on every load: a deploy must not leave a page running an old refresh protocol.
        response.headers["Cache-Control"] = "no-cache"
        return response

    return bp
