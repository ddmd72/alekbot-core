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
from typing import Optional, TYPE_CHECKING
from quart import Blueprint, request, jsonify, redirect, make_response
import jwt

from ..services.authentication_service import AuthenticationService
from ..services.session_service import SessionService
from ..services.auth_provider_registry import AuthProviderRegistry
from ..services.google_oauth_service import GoogleOAuthService
from ..ports.oauth_credentials_port import OAuthCredentialsPort
from ..config.auth import AuthConfig
from ..utils.logger import logger

if TYPE_CHECKING:
    from ..services.invite_code_service import InviteCodeService


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
    task_queue=None,
) -> Blueprint:
    """
    Create Quart Blueprint with OAuth endpoints.

    Args:
        auth_service: Authentication service for OAuth flows
        session_service: Session service for JWT management
        auth_registry: OAuth provider registry
        auth_config: OAuth configuration
        invite_service: Optional service to consume invite codes on callback

    Returns:
        Configured Blueprint
    """
    bp = Blueprint("oauth", __name__)

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
                    logger.info(f"✅ Auto-consumed invite code {pending_code} for user {user.user_id}")
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
            next_url = request.cookies.get("auth_next", "/cabinet")
            
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

        Verifies CSRF state cookie. Saves OAuthCredentials to Firestore.
        Redirects to /cabinet on success, or /cabinet?gmail_error=1 on failure.
        """
        if not gmail_oauth_service or not oauth_credentials_port:
            return jsonify({"error": "Gmail integration not configured"}), 501

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

        if not code:
            return redirect("/cabinet?gmail_error=no_code")

        try:
            credentials = await gmail_oauth_service.exchange_code(
                code=code,
                redirect_uri=auth_config.gmail_oauth_redirect_uri,
                user_id=user_id,
            )
            await oauth_credentials_port.save_credentials(credentials)
            logger.info(f"✅ Gmail connected for user={user_id[:8]}")
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

        Verifies CSRF state cookie. Saves OAuthCredentials (provider=google_tasks) to Firestore.
        Redirects to /cabinet on success, or /cabinet?tasks_error=1 on failure.
        """
        if not google_tasks_oauth_service or not oauth_credentials_port:
            return jsonify({"error": "Google Tasks integration not configured"}), 501

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

        if not code:
            return redirect("/cabinet?tasks_error=no_code")

        try:
            credentials = await google_tasks_oauth_service.exchange_code(
                code=code,
                redirect_uri=auth_config.google_tasks_oauth_redirect_uri,
                user_id=user_id,
                provider_override="google_tasks",
            )
            await oauth_credentials_port.save_credentials(credentials)
            logger.info(f"✅ Google Tasks connected for user={user_id[:8]}")
        except Exception as exc:
            logger.error(f"💥 Google Tasks OAuth callback failed: {exc}")
            return redirect("/cabinet?tasks_error=exchange")

        response = await make_response(redirect("/cabinet?tasks_connected=1"))
        response.delete_cookie("tasks_oauth_state")
        response.delete_cookie("tasks_connect_user_id")
        return response

    # ========================================================================
    # GET /auth/connect-microsoft-todo - Initiate MS To Do OAuth
    # ========================================================================
    @bp.route("/auth/connect-microsoft-todo", methods=["GET"])
    async def connect_microsoft_todo():
        """
        Initiate Microsoft To Do OAuth (Tasks.ReadWrite + offline_access).

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
            "scope": "Tasks.ReadWrite offline_access",
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

        Exchanges code for tokens, persists OAuthCredentials, enqueues setup task.
        Redirects to /cabinet?microsoft_todo_connected=1 on success.
        """
        if not ms_todo_client_id or not ms_todo_client_secret or not ms_todo_redirect_uri:
            return jsonify({"error": "Microsoft To Do integration not configured"}), 501

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

        if not code:
            return redirect("/cabinet?microsoft_todo_error=no_code")

        try:
            import aiohttp
            from datetime import datetime, timedelta, timezone
            from ..domain.email import OAuthCredentials

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

            # Fetch Microsoft account email for display
            ms_email = ""
            try:
                async with aiohttp.ClientSession() as me_session:
                    async with me_session.get(
                        "https://graph.microsoft.com/v1.0/me",
                        headers={"Authorization": f"Bearer {token_data['access_token']}"},
                    ) as me_resp:
                        if me_resp.ok:
                            me_data = await me_resp.json()
                            ms_email = me_data.get("mail") or me_data.get("userPrincipalName", "")
            except Exception as e:
                logger.warning(f"⚠️ Could not fetch MS user profile: {e}")

            expires_in = int(token_data.get("expires_in", 3600))
            credentials = OAuthCredentials(
                user_id=user_id,
                provider="microsoft_todo",
                access_token=token_data["access_token"],
                refresh_token=token_data.get("refresh_token"),
                token_expiry=datetime.now(timezone.utc) + timedelta(seconds=expires_in),
                scopes=token_data.get("scope", "").split(),
                email_address=ms_email,
            )
            await oauth_credentials_port.save_credentials(credentials)
            logger.info(f"✅ MS To Do connected for user={user_id[:8]}")

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

    return bp
