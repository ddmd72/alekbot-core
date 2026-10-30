"""
OAuth Web Application (OAuth Multi-Tenant Session 4).

Quart web application for OAuth authentication endpoints.
Provides web UI authentication via Google OAuth / Firebase.

RFC: docs/10_rfcs/MULTI_TENANT_OAUTH_RFC.md

Endpoints:
- GET  /auth/login - Redirect to OAuth provider
- GET  /auth/callback - OAuth callback handler
- POST /auth/refresh - Refresh access token
- POST /auth/logout - Logout (clear cookies)
- GET  /auth/me - Get current user info
"""
import secrets
from dataclasses import replace
from typing import Optional, TYPE_CHECKING
from quart import Blueprint, request, jsonify, redirect, make_response
import jwt

from ..services.authentication_service import AuthenticationService
from ..services.session_service import SessionService
from ..services.auth_provider_registry import AuthProviderRegistry
from ..services.google_oauth_service import GoogleOAuthService
from ..ports.oauth_credentials_port import OAuthCredentialsPort
from ..config.auth import AuthConfig
from ..composition.connector_registry import GMAIL, GOOGLE_TASKS, MICROSOFT_ONEDRIVE, MICROSOFT_TODO
from ..domain.connector_spec import ConnectorSpec
from ..domain.oauth_credential import OAuthCredential
from ..domain.provider_account import ProviderAccount
from ..domain.exceptions import AccessDeniedError, OAuthScopeNotGrantedError
from ..domain.reconnect_policy import Integration
from ..domain.safe_redirect import safe_return_path
from ..utils.logger import logger

if TYPE_CHECKING:
    from ..services.invite_code_service import InviteCodeService
    from ..services.integration_reconnect_service import IntegrationReconnectService
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


def create_oauth_blueprint(
    auth_service: AuthenticationService,
    session_service: SessionService,
    auth_registry: AuthProviderRegistry,
    auth_config: AuthConfig,
    invite_service: Optional['InviteCodeService'] = None,
    gmail_oauth_service: Optional[GoogleOAuthService] = None,
    oauth_credentials_port: Optional[OAuthCredentialsPort] = None,
    google_tasks_oauth_service: Optional[GoogleOAuthService] = None,
    ms_todo_client_id: Optional[str] = None,
    ms_todo_client_secret: Optional[str] = None,
    ms_todo_redirect_uri: Optional[str] = None,
    onedrive_redirect_uri: Optional[str] = None,
    task_queue=None,
    reconnect: Optional['IntegrationReconnectService'] = None,
    connectors: Optional['ConnectorRegistry'] = None,
) -> Blueprint:
    """
    Create Quart Blueprint with OAuth endpoints.

    Args:
        auth_service: Authentication service for OAuth flows
        session_service: Session service for JWT management
        auth_registry: OAuth provider registry
        auth_config: OAuth configuration
        invite_service: Optional service to consume invite codes on callback
        connectors: the connector registry; the four connect callbacks build the saved credential's kind and
            integration from its spec (a connect is "not configured" without it)

    Returns:
        Configured Blueprint
    """
    bp = Blueprint("oauth", __name__)

    async def _consented(user_id: str, integration: Integration) -> None:
        """R1 (SECRETS_AT_REST_RFC §6.2): a consent clears the disconnect mark and the notice flag. Best effort: the
        credential is already stored, so a failure here never fails the connect (the mark would only keep the next
        loss of this integration silent)."""
        if reconnect is None:
            return
        try:
            await reconnect.consented(user_id, integration)
        except Exception as exc:
            logger.error(f"💥 Clearing the reconnect state of {integration.value} failed for user={user_id[:8]}: {exc}",
                         exc_info=True)

    def _session_account_id(user_id: str) -> Optional[str]:
        """The account_id of the signed-in session (the `access_token` cookie's verified payload) when it is the
        session of `user_id`; None otherwise. A connect callback stores nothing without it: a credential is never
        saved under a guessed tenant."""
        token = request.cookies.get("access_token")
        if not token:
            return None
        try:
            payload = session_service.verify_access_token(token)
        except (jwt.InvalidTokenError, ValueError):
            return None
        if payload.get("sub") != user_id:
            return None
        account_id = payload.get("account_id")
        return account_id if isinstance(account_id, str) and account_id else None

    async def _save_consent(spec: ConnectorSpec, credential: OAuthCredential) -> None:
        """Store a consent. The store keeps any number of records per kind (MULTI_DRIVE_RFC E15), so for a
        single-credential kind (`spec.multiple` False, every kind in slice 1a) the route keeps today's effect: a
        consent of another account replaces the user's record of that kind (deleted, never revoked at the provider,
        as the store did before). Saved first, so a failure in between leaves the old grant, never none."""
        await oauth_credentials_port.save(credential)
        if spec.multiple:
            return
        account = (credential.account.realm, credential.account.subject)
        for other in await oauth_credentials_port.list_for_user(credential.user_id, spec.kind):
            if (other.account.realm, other.account.subject) != account:
                await oauth_credentials_port.delete(other.user_id, other.credential_id)

    # ========================================================================
    # GET /auth/login - Redirect to OAuth provider
    # ========================================================================
    @bp.route("/auth/login", methods=["GET"])
    async def oauth_login():
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
        4. Create JWT session tokens
        5. Set session cookies
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

            # Create JWT session tokens
            access_token = session_service.create_access_token(user, account)
            refresh_token = session_service.create_refresh_token(user, account)

            logger.info(
                f"✅ OAuth callback success - user: {user.user_id}, "
                f"account: {account.account_id}"
            )

            # Determine redirect target
            next_url = safe_return_path(request.cookies.get("auth_next"))  # a pre-fix cookie may hold anything
            
            # Create redirect response
            response = await make_response(redirect(next_url))

            # Set access token cookie (short-lived)
            response.set_cookie(
                "access_token",
                access_token,
                max_age=auth_config.access_token_ttl,
                httponly=True,
                secure=True,
                samesite="lax",
            )

            # Set refresh token cookie (long-lived)
            response.set_cookie(
                "refresh_token",
                refresh_token,
                max_age=auth_config.refresh_token_ttl,
                httponly=True,
                secure=True,
                samesite="lax",
            )

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
    # POST /auth/refresh - Refresh access token
    # ========================================================================
    @bp.route("/auth/refresh", methods=["POST"])
    async def refresh_token_endpoint():
        """
        Refresh expired access token using refresh token.

        Headers or cookies:
        - refresh_token: JWT refresh token

        Flow:
        1. Verify refresh token
        2. Load user and account from database
        3. Generate new access token
        4. Return new token
        """
        # Get refresh token from cookie or header
        refresh_token = request.cookies.get("refresh_token")
        if not refresh_token:
            auth_header = request.headers.get("Authorization", "")
            if auth_header.startswith("Bearer "):
                refresh_token = auth_header[7:]

        if not refresh_token:
            return jsonify({"error": "Missing refresh token"}), 401

        try:
            # Verify refresh token
            payload = session_service.verify_refresh_token(refresh_token)

            user_id = payload["sub"]
            account_id = payload["account_id"]

            # TODO: Load user and account from database
            # For now, we trust the refresh token payload
            # In production, fetch from database to ensure user is still active

            # Create new access token
            # Note: This is a simplified version - should load from DB
            from ..domain.user import UserProfile
            from ..domain.billing import BillingAccount, AccountTier

            user = UserProfile(user_id=user_id, account_id=account_id)
            account = BillingAccount(account_id=account_id, tier=AccountTier.FREE)

            new_access_token = session_service.create_access_token(user, account)

            logger.info(f"🔄 Access token refreshed - user: {user_id}")

            response = await make_response(
                jsonify({
                    "success": True,
                    "access_token": new_access_token,
                })
            )

            # Update access token cookie
            response.set_cookie(
                "access_token",
                new_access_token,
                max_age=auth_config.access_token_ttl,
                httponly=True,
                secure=True,
                samesite="lax",
            )

            return response

        except jwt.ExpiredSignatureError:
            logger.warning("⏰ Refresh token expired")
            return jsonify({"error": "Refresh token expired"}), 401
        except jwt.InvalidTokenError as e:
            logger.warning(f"❌ Invalid refresh token: {e}")
            return jsonify({"error": "Invalid refresh token"}), 401
        except Exception as e:
            # Log detail server-side; never leak exception internals to the client.
            logger.error(f"❌ Token refresh failed: {e}")
            return jsonify({"error": "Token refresh failed"}), 500

    # ========================================================================
    # POST /auth/logout - Logout (clear cookies)
    # ========================================================================
    @bp.route("/auth/logout", methods=["POST"])
    async def logout():
        """
        Logout user by clearing session cookies.

        Note: JWT tokens remain valid until expiration.
        For immediate revocation, implement token blacklist (future).
        """
        response = await make_response(
            jsonify({"success": True, "message": "Logged out"})
        )

        # Clear session cookies
        response.delete_cookie("access_token")
        response.delete_cookie("refresh_token")

        logger.info("👋 User logged out")
        return response

    # ========================================================================
    # GET /auth/me - Get current user info
    # ========================================================================
    @bp.route("/auth/me", methods=["GET"])
    async def get_current_user():
        """
        Get current authenticated user information.

        Headers or cookies:
        - access_token: JWT access token

        Returns:
        - User profile (user_id, email, display_name)
        - Account info (account_id, tier, role)
        """
        # Get access token from cookie or Authorization header
        access_token = request.cookies.get("access_token")
        if not access_token:
            auth_header = request.headers.get("Authorization", "")
            if auth_header.startswith("Bearer "):
                access_token = auth_header[7:]

        if not access_token:
            return jsonify({"error": "Not authenticated"}), 401

        try:
            # Verify access token
            payload = session_service.verify_access_token(access_token)

            return jsonify({
                "user": {
                    "user_id": payload["sub"],
                    "email": payload.get("email"),
                    "external_user_id": payload.get("external_user_id"),
                },
                "account": {
                    "account_id": payload["account_id"],
                    "tier": payload.get("tier"),
                    "role": payload.get("role"),
                },
            })

        except jwt.ExpiredSignatureError:
            logger.warning("⏰ Access token expired")
            return jsonify({"error": "Token expired"}), 401
        except jwt.InvalidTokenError as e:
            logger.warning(f"❌ Invalid access token: {e}")
            return jsonify({"error": "Invalid token"}), 401
        except Exception as e:
            logger.error(f"❌ Get current user failed: {e}")
            return jsonify({"error": "Failed to get user info"}), 500

    # ========================================================================
    # POST /auth/link-oauth - Link OAuth to existing user
    # ========================================================================
    @bp.route("/auth/link-oauth", methods=["POST"])
    async def link_oauth_to_existing_user():
        """
        Link Google OAuth identity to existing user (e.g., YOUR_USER_ID).

        Use case: User already logged in via Slack/Telegram, wants to add Google OAuth.

        Request:
        - access_token: JWT access token (cookie or Authorization header)
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
        # Get access token from cookie or Authorization header
        access_token = request.cookies.get("access_token")
        if not access_token:
            auth_header = request.headers.get("Authorization", "")
            if auth_header.startswith("Bearer "):
                access_token = auth_header[7:]

        if not access_token:
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

        try:
            # 1. Verify access token → get current user
            payload = session_service.verify_access_token(access_token)
            user_id = payload["sub"]

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

        except jwt.ExpiredSignatureError:
            logger.warning("⏰ Access token expired")
            return jsonify({"error": "Access token expired. Please login again."}), 401
        except jwt.InvalidTokenError as e:
            logger.warning(f"❌ Invalid access token: {e}")
            return jsonify({"error": "Invalid access token"}), 401
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
    # GET /auth/connect-gmail - Initiate incremental Gmail OAuth
    # ========================================================================
    @bp.route("/auth/connect-gmail", methods=["GET"])
    async def connect_gmail():
        """
        Initiate incremental Gmail OAuth (gmail.readonly scope).

        Requires: authenticated session (access_token cookie).
        Sets cookies: gmail_oauth_state, gmail_connect_user_id.
        Redirects to Google consent page.
        """
        if not gmail_oauth_service or not oauth_credentials_port:
            return jsonify({"error": "Gmail integration not configured"}), 501

        access_token = request.cookies.get("access_token")
        if not access_token:
            return redirect("/auth/login?next=/cabinet")

        try:
            payload = session_service.verify_access_token(access_token)
            user_id = payload["sub"]
        except jwt.InvalidTokenError:
            return redirect("/auth/login?next=/cabinet")

        state = secrets.token_urlsafe(32)
        auth_url = gmail_oauth_service.get_authorization_url(
            state=state,
            redirect_uri=auth_config.gmail_oauth_redirect_uri,
        )
        logger.info(f"📧 Gmail OAuth initiated for user={user_id[:8]}")

        response = await make_response(redirect(auth_url))
        response.set_cookie("gmail_oauth_state", state, max_age=600, httponly=True, secure=True, samesite="lax")
        response.set_cookie("gmail_connect_user_id", user_id, max_age=600, httponly=True, secure=True, samesite="lax")
        return response

    # ========================================================================
    # GET /auth/connect-gmail/callback - Gmail OAuth callback
    # ========================================================================
    @bp.route("/auth/connect-gmail/callback", methods=["GET"])
    async def connect_gmail_callback():
        """
        Handle Gmail OAuth callback: exchange code, persist credentials, redirect.

        Verifies CSRF state cookie and the signed-in session. Saves the credential to Firestore.
        Redirects to /cabinet on success, or /cabinet?gmail_error=1 on failure.
        """
        if not gmail_oauth_service or not oauth_credentials_port or connectors is None:
            return jsonify({"error": "Gmail integration not configured"}), 501
        spec = connectors.get(GMAIL)

        code = request.args.get("code")
        state = request.args.get("state")
        error = request.args.get("error")

        if error:
            logger.warning(f"⚠️ Gmail OAuth denied by user: {error}")
            return redirect("/cabinet?gmail_error=denied")

        stored_state = request.cookies.get("gmail_oauth_state")
        user_id = request.cookies.get("gmail_connect_user_id")

        if not stored_state or stored_state != state or not user_id:
            logger.warning("⚠️ Gmail OAuth callback CSRF validation failed")
            return redirect("/cabinet?gmail_error=state")
        account_id = _session_account_id(user_id)
        if account_id is None:
            logger.warning(f"⚠️ Gmail OAuth callback without the signed-in session of user={user_id[:8]}")
            return redirect("/cabinet?gmail_error=state")

        if not code:
            return redirect("/cabinet?gmail_error=no_code")

        try:
            credentials = await gmail_oauth_service.exchange_code(
                code=code,
                redirect_uri=auth_config.gmail_oauth_redirect_uri,
                user_id=user_id,
            )
            await _save_consent(spec, replace(credentials, kind=spec.kind, integration=spec.integration,
                                              account_id=account_id))
            logger.info(f"✅ Gmail connected for user={user_id[:8]}")
            await _consented(user_id, Integration.GMAIL)
        except OAuthScopeNotGrantedError:
            logger.warning(f"⚠️ Gmail consent without the Gmail scope: user={user_id[:8]}")
            return redirect("/cabinet?gmail_error=scope")
        except Exception as exc:
            logger.error(f"💥 Gmail OAuth callback failed: {exc}")
            return redirect("/cabinet?gmail_error=exchange")

        response = await make_response(redirect("/cabinet?gmail_connected=1"))
        response.delete_cookie("gmail_oauth_state")
        response.delete_cookie("gmail_connect_user_id")
        return response

    # ========================================================================
    # GET /auth/connect-google-tasks - Initiate Google Tasks OAuth
    # ========================================================================
    @bp.route("/auth/connect-google-tasks", methods=["GET"])
    async def connect_google_tasks():
        """
        Initiate incremental Google Tasks OAuth (tasks scope).

        Requires: authenticated session (access_token cookie).
        Sets cookies: tasks_oauth_state, tasks_connect_user_id.
        Redirects to Google consent page.
        """
        if not google_tasks_oauth_service or not oauth_credentials_port:
            return jsonify({"error": "Google Tasks integration not configured"}), 501

        access_token = request.cookies.get("access_token")
        if not access_token:
            return redirect("/auth/login?next=/cabinet")

        try:
            payload = session_service.verify_access_token(access_token)
            user_id = payload["sub"]
        except jwt.InvalidTokenError:
            return redirect("/auth/login?next=/cabinet")

        state = secrets.token_urlsafe(32)
        auth_url = google_tasks_oauth_service.get_authorization_url(
            state=state,
            redirect_uri=auth_config.google_tasks_oauth_redirect_uri,
        )
        logger.info(f"📋 Google Tasks OAuth initiated for user={user_id[:8]}")

        response = await make_response(redirect(auth_url))
        response.set_cookie("tasks_oauth_state", state, max_age=600, httponly=True, secure=True, samesite="lax")
        response.set_cookie("tasks_connect_user_id", user_id, max_age=600, httponly=True, secure=True, samesite="lax")
        return response

    # ========================================================================
    # GET /auth/connect-google-tasks/callback - Google Tasks OAuth callback
    # ========================================================================
    @bp.route("/auth/connect-google-tasks/callback", methods=["GET"])
    async def connect_google_tasks_callback():
        """
        Handle Google Tasks OAuth callback: exchange code, persist credentials, redirect.

        Verifies CSRF state cookie and the signed-in session. Saves the credential (kind google_tasks) to Firestore.
        Redirects to /cabinet on success, or /cabinet?tasks_error=1 on failure.
        """
        if not google_tasks_oauth_service or not oauth_credentials_port or connectors is None:
            return jsonify({"error": "Google Tasks integration not configured"}), 501
        spec = connectors.get(GOOGLE_TASKS)

        code = request.args.get("code")
        state = request.args.get("state")
        error = request.args.get("error")

        if error:
            logger.warning(f"⚠️ Google Tasks OAuth denied by user: {error}")
            return redirect("/cabinet?tasks_error=denied")

        stored_state = request.cookies.get("tasks_oauth_state")
        user_id = request.cookies.get("tasks_connect_user_id")

        if not stored_state or stored_state != state or not user_id:
            logger.warning("⚠️ Google Tasks OAuth callback CSRF validation failed")
            return redirect("/cabinet?tasks_error=state")
        account_id = _session_account_id(user_id)
        if account_id is None:
            logger.warning(f"⚠️ Google Tasks OAuth callback without the signed-in session of user={user_id[:8]}")
            return redirect("/cabinet?tasks_error=state")

        if not code:
            return redirect("/cabinet?tasks_error=no_code")

        try:
            credentials = await google_tasks_oauth_service.exchange_code(
                code=code,
                redirect_uri=auth_config.google_tasks_oauth_redirect_uri,
                user_id=user_id,
                provider_override=spec.kind,
            )
            await _save_consent(spec, replace(credentials, kind=spec.kind, integration=spec.integration,
                                              account_id=account_id))
            logger.info(f"✅ Google Tasks connected for user={user_id[:8]}")
        except OAuthScopeNotGrantedError:
            logger.warning(f"⚠️ Google Tasks consent without the Tasks scope: user={user_id[:8]}")
            return redirect("/cabinet?tasks_error=scope")
        except Exception as exc:
            logger.error(f"💥 Google Tasks OAuth callback failed: {exc}")
            return redirect("/cabinet?tasks_error=exchange")

        response = await make_response(redirect("/cabinet?tasks_connected=1"))
        response.delete_cookie("tasks_oauth_state")
        response.delete_cookie("tasks_connect_user_id")
        return response

    # Microsoft consents request User.Read: the account identity is Graph /me (SECRETS_AT_REST_RFC §2.3, I9).
    _TODO_SCOPE = "Tasks.ReadWrite offline_access User.Read"
    _GRAPH_ME = "https://graph.microsoft.com/v1.0/me"

    async def _read_microsoft_identity(session, access_token: str) -> tuple:
        """(subject, label) of the consenting Microsoft account: Graph `/me` `id`, and `mail` or `userPrincipalName`
        (display only, may be ""). Raises when the read fails or carries no `id`, so the callback stores nothing (I9).
        One value names the account across To Do and OneDrive."""
        import aiohttp

        async with session.get(_GRAPH_ME, headers={"Authorization": f"Bearer {access_token}"},
                               timeout=aiohttp.ClientTimeout(total=10)) as resp:
            if not resp.ok:
                raise RuntimeError(f"Microsoft account identity read failed ({resp.status})")
            body = await resp.json()
        subject = body.get("id") if isinstance(body, dict) else None
        if not isinstance(subject, str) or not subject:
            raise RuntimeError("Microsoft /me carried no id: no account identity")
        label = body.get("mail") or body.get("userPrincipalName") or ""
        return subject, label if isinstance(label, str) else ""

    # ========================================================================
    # GET /auth/connect-microsoft-todo - Initiate MS To Do OAuth
    # ========================================================================
    @bp.route("/auth/connect-microsoft-todo", methods=["GET"])
    async def connect_microsoft_todo():
        """
        Initiate Microsoft To Do OAuth (Tasks.ReadWrite + offline_access + User.Read for the account identity).

        Requires: authenticated session (access_token cookie).
        Sets cookies: microsoft_todo_oauth_state, microsoft_todo_connect_user_id.
        Redirects to Microsoft consent page (consumers tenant).
        """
        if not ms_todo_client_id or not ms_todo_redirect_uri:
            return jsonify({"error": "Microsoft To Do integration not configured"}), 501

        access_token = request.cookies.get("access_token")
        if not access_token:
            return redirect("/auth/login?next=/cabinet")

        try:
            payload = session_service.verify_access_token(access_token)
            user_id = payload["sub"]
        except jwt.InvalidTokenError:
            return redirect("/auth/login?next=/cabinet")

        state = secrets.token_urlsafe(32)
        params = {
            "client_id": ms_todo_client_id,
            "response_type": "code",
            "redirect_uri": ms_todo_redirect_uri,
            "scope": _TODO_SCOPE,
            "state": state,
            "response_mode": "query",
        }
        from urllib.parse import urlencode
        auth_url = (
            "https://login.microsoftonline.com/consumers/oauth2/v2.0/authorize?"
            + urlencode(params)
        )
        logger.info(f"📋 MS To Do OAuth initiated for user={user_id[:8]}")

        response = await make_response(redirect(auth_url))
        response.set_cookie("microsoft_todo_oauth_state", state, max_age=600, httponly=True, secure=True, samesite="lax")
        response.set_cookie("microsoft_todo_connect_user_id", user_id, max_age=600, httponly=True, secure=True, samesite="lax")
        return response

    # ========================================================================
    # GET /auth/connect-microsoft-todo/callback - MS To Do OAuth callback
    # ========================================================================
    @bp.route("/auth/connect-microsoft-todo/callback", methods=["GET"])
    async def connect_microsoft_todo_callback():
        """
        Handle Microsoft To Do OAuth callback.

        Exchanges code for tokens, persists OAuthCredential, enqueues setup task.
        Redirects to /cabinet?microsoft_todo_connected=1 on success.
        """
        if (not ms_todo_client_id or not ms_todo_client_secret or not ms_todo_redirect_uri
                or not oauth_credentials_port or connectors is None):
            return jsonify({"error": "Microsoft To Do integration not configured"}), 501
        spec = connectors.get(MICROSOFT_TODO)

        code = request.args.get("code")
        state = request.args.get("state")
        error = request.args.get("error")

        if error:
            logger.warning(f"⚠️ MS To Do OAuth denied by user: {error}")
            return redirect("/cabinet?microsoft_todo_error=denied")

        stored_state = request.cookies.get("microsoft_todo_oauth_state")
        user_id = request.cookies.get("microsoft_todo_connect_user_id")

        if not stored_state or stored_state != state or not user_id:
            logger.warning("⚠️ MS To Do OAuth callback CSRF validation failed")
            return redirect("/cabinet?microsoft_todo_error=state")
        account_id = _session_account_id(user_id)
        if account_id is None:
            logger.warning(f"⚠️ MS To Do OAuth callback without the signed-in session of user={user_id[:8]}")
            return redirect("/cabinet?microsoft_todo_error=state")

        if not code:
            return redirect("/cabinet?microsoft_todo_error=no_code")

        try:
            import aiohttp
            from datetime import datetime, timedelta, timezone

            token_url = "https://login.microsoftonline.com/consumers/oauth2/v2.0/token"
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    token_url,
                    data={
                        "client_id": ms_todo_client_id,
                        "client_secret": ms_todo_client_secret,
                        "code": code,
                        "redirect_uri": ms_todo_redirect_uri,
                        "grant_type": "authorization_code",
                    },
                ) as resp:
                    if not resp.ok:
                        text = await resp.text()
                        raise RuntimeError(f"Token exchange failed: {resp.status} {text}")
                    token_data = await resp.json()
                # the account identity (I9): a failed read raises, so nothing is stored
                ms_subject, ms_label = await _read_microsoft_identity(session, token_data["access_token"])

            expires_in = int(token_data.get("expires_in", 3600))
            credentials = OAuthCredential(
                user_id=user_id,
                account_id=account_id,
                kind=spec.kind,
                integration=spec.integration,
                account=ProviderAccount(realm=spec.realm, subject=ms_subject, label=ms_label),
                access_token=token_data["access_token"],
                refresh_token=token_data.get("refresh_token"),
                token_expiry=datetime.now(timezone.utc) + timedelta(seconds=expires_in),
                scopes=token_data.get("scope", "").split(),
            )
            await _save_consent(spec, credentials)
            logger.info(f"✅ MS To Do connected for user={user_id[:8]}")
            # before the setup task: its read-modify-save of the config then reads the cleared flag
            await _consented(user_id, Integration.TODO)

            # Enqueue setup (idempotent): ensure_primary_list + register subscriptions
            if task_queue:
                await task_queue.enqueue_worker_task(
                    "setup_microsoft_todo",
                    {"user_id": user_id},
                )

        except Exception as exc:
            logger.error(f"💥 MS To Do OAuth callback failed: {exc}", exc_info=True)
            err_response = await make_response(redirect("/cabinet?microsoft_todo_error=exchange"))
            err_response.delete_cookie("microsoft_todo_oauth_state")
            err_response.delete_cookie("microsoft_todo_connect_user_id")
            return err_response

        response = await make_response(redirect("/cabinet?microsoft_todo_connected=1"))
        response.delete_cookie("microsoft_todo_oauth_state")
        response.delete_cookie("microsoft_todo_connect_user_id")
        return response

    # ========================================================================
    # GET /auth/connect-onedrive — the user's drive (USER_DRIVE_RFC §4.1)
    # Same Microsoft app registration as To Do; its own redirect URI and scope.
    # ========================================================================
    _ONEDRIVE_SCOPE = "Files.ReadWrite.AppFolder offline_access User.Read"
    _ONEDRIVE_APPROOT = "https://graph.microsoft.com/v1.0/me/drive/special/approot"

    async def _read_drive_id(session, access_token: str) -> str:
        """The connected drive's id, for the index's drive-switch check (USER_DRIVE_INDEXING_RFC
        §10.1 step 0). Read from the App Folder item: it answers under Files.ReadWrite.AppFolder
        (spike A6 saw other calls refused with 401), and `parentReference.driveId` is the field the
        indexer's change feed reports (OneDriveAdapter.get_changes, `_APPROOT`; a test pins both), so
        both sides compare the same string. Best effort: "" only
        skips the check, so a failure here never fails the connect."""
        import aiohttp

        try:
            # Its own timeout: the session default (5 min) would hold the OAuth callback on a hung Graph call.
            async with session.get(_ONEDRIVE_APPROOT, headers={"Authorization": f"Bearer {access_token}"},
                                   timeout=aiohttp.ClientTimeout(total=10)) as resp:
                if not resp.ok:
                    logger.warning(f"⚠️ Drive id not read at connect ({resp.status})")
                    return ""
                body = await resp.json()
            drive_id = (body.get("parentReference") or {}).get("driveId") if isinstance(body, dict) else None
            return drive_id if isinstance(drive_id, str) else ""
        except Exception as e:
            logger.warning(f"⚠️ Drive id not read at connect: {e}")
            return ""

    @bp.route("/auth/connect-onedrive", methods=["GET"])
    async def connect_onedrive():
        """
        Initiate the drive OAuth (Files.ReadWrite.AppFolder + offline_access + User.Read for the account identity).

        Requires: authenticated session (access_token cookie).
        Sets cookies: drive_oauth_state, drive_connect_user_id.
        """
        if not ms_todo_client_id or not onedrive_redirect_uri:
            return jsonify({"error": "Drive integration not configured"}), 501

        access_token = request.cookies.get("access_token")
        if not access_token:
            return redirect("/auth/login?next=/cabinet")
        try:
            user_id = session_service.verify_access_token(access_token)["sub"]
        except jwt.InvalidTokenError:
            return redirect("/auth/login?next=/cabinet")

        state = secrets.token_urlsafe(32)
        from urllib.parse import urlencode
        auth_url = "https://login.microsoftonline.com/consumers/oauth2/v2.0/authorize?" + urlencode({
            "client_id": ms_todo_client_id,
            "response_type": "code",
            "redirect_uri": onedrive_redirect_uri,
            "scope": _ONEDRIVE_SCOPE,
            "state": state,
            "response_mode": "query",
        })
        logger.info(f"📁 Drive OAuth initiated for user={user_id[:8]}")
        response = await make_response(redirect(auth_url))
        response.set_cookie("drive_oauth_state", state, max_age=600, httponly=True, secure=True, samesite="lax")
        response.set_cookie("drive_connect_user_id", user_id, max_age=600, httponly=True, secure=True, samesite="lax")
        return response

    @bp.route("/auth/connect-onedrive/callback", methods=["GET"])
    async def connect_onedrive_callback():
        """Exchange the code, persist credentials under the drive provider key."""
        if (not ms_todo_client_id or not ms_todo_client_secret or not onedrive_redirect_uri
                or not oauth_credentials_port or connectors is None):
            return jsonify({"error": "Drive integration not configured"}), 501
        spec = connectors.get(MICROSOFT_ONEDRIVE)
        if request.args.get("error"):
            logger.warning(f"⚠️ Drive OAuth denied by user: {request.args.get('error')}")
            return redirect("/cabinet?drive_error=denied")

        state, code = request.args.get("state"), request.args.get("code")
        user_id = request.cookies.get("drive_connect_user_id")
        stored_state = request.cookies.get("drive_oauth_state")
        # Constant-time compare: the query value is attacker-chosen, the cookie is the secret.
        if (not stored_state or not state or not user_id
                or not secrets.compare_digest(stored_state, state)):
            logger.warning("⚠️ Drive OAuth callback CSRF validation failed")
            return redirect("/cabinet?drive_error=state")
        account_id = _session_account_id(user_id)
        if account_id is None:
            logger.warning(f"⚠️ Drive OAuth callback without the signed-in session of user={user_id[:8]}")
            return redirect("/cabinet?drive_error=state")
        if not code:
            return redirect("/cabinet?drive_error=no_code")

        try:
            import aiohttp
            from datetime import datetime, timedelta, timezone

            async with aiohttp.ClientSession() as session:
                async with session.post(
                    "https://login.microsoftonline.com/consumers/oauth2/v2.0/token",
                    data={
                        "client_id": ms_todo_client_id,
                        "client_secret": ms_todo_client_secret,
                        "code": code,
                        "redirect_uri": onedrive_redirect_uri,
                        "grant_type": "authorization_code",
                        "scope": _ONEDRIVE_SCOPE,
                    },
                ) as resp:
                    if not resp.ok:
                        raise RuntimeError(f"Token exchange failed: {resp.status} {await resp.text()}")
                    token_data = await resp.json()
                # the account identity (I9): a failed read raises, so nothing is stored
                ms_subject, ms_label = await _read_microsoft_identity(session, token_data["access_token"])
                drive_id = await _read_drive_id(session, token_data["access_token"])

            await _save_consent(spec, OAuthCredential(
                user_id=user_id,
                account_id=account_id,
                kind=spec.kind,
                integration=spec.integration,
                account=ProviderAccount(realm=spec.realm, subject=ms_subject, label=ms_label),
                access_token=token_data["access_token"],
                refresh_token=token_data.get("refresh_token"),
                token_expiry=datetime.now(timezone.utc) + timedelta(seconds=int(token_data.get("expires_in", 3600))),
                scopes=token_data.get("scope", "").split(),
                drive_id=drive_id,
            ))
            logger.info(f"✅ Drive connected for user={user_id[:8]}")
            await _consented(user_id, Integration.DRIVE)
        except Exception as exc:
            logger.error(f"💥 Drive OAuth callback failed: {exc}", exc_info=True)
            err_response = await make_response(redirect("/cabinet?drive_error=exchange"))
            err_response.delete_cookie("drive_oauth_state")
            err_response.delete_cookie("drive_connect_user_id")
            return err_response

        response = await make_response(redirect("/cabinet?drive_connected=1"))
        response.delete_cookie("drive_oauth_state")
        response.delete_cookie("drive_connect_user_id")
        return response

    return bp
