"""
Firebase Auth Adapter (OAuth Multi-Tenant Session 3).

Implements AuthPort using Firebase Authentication and Google Identity Platform.
Uses Firebase Admin SDK for token verification and Firebase Auth REST API for OAuth flows.

RFC: docs/10_rfcs/MULTI_TENANT_OAUTH_RFC.md
Architecture: Provider-agnostic adapter implementing OIDC/OAuth 2.0 via Firebase
"""
from typing import Optional
from datetime import datetime
from urllib.parse import urlencode

import aiohttp
from firebase_admin import auth as firebase_auth, credentials, initialize_app
from firebase_admin.exceptions import FirebaseError

from ..ports.auth_port import (
    AuthPort,
    TokenClaims,
    OAuthTokens,
    OAuthUserInfo,
)
from ..utils.logger import logger


class FirebaseAuthAdapter(AuthPort):
    """
    Firebase Authentication adapter for OAuth 2.0 / OIDC flows.

    Features:
    - Google OAuth via Firebase Authentication
    - ID token verification using Firebase Admin SDK
    - UserInfo retrieval from Google's OIDC endpoint
    - Token refresh via Firebase Auth REST API

    Configuration:
    - FIREBASE_PROJECT_ID: GCP project ID
    - FIREBASE_WEB_API_KEY: Firebase Web API key (for REST API)
    - GOOGLE_APPLICATION_CREDENTIALS: Path to service account key (for Admin SDK)

    OAuth Flow:
    1. Frontend: User clicks "Sign in with Google"
    2. Backend: generate authorization URL → redirect user
    3. Google: User authorizes → redirects with code
    4. Backend: exchange code for tokens
    5. Backend: verify ID token → extract user claims
    6. Backend: create/update UserProfile
    """

    def __init__(
        self,
        project_id: str,
        web_api_key: str,
        service_account_path: Optional[str] = None,
        oauth_client_id: Optional[str] = None,
        oauth_client_secret: Optional[str] = None,
    ):
        """
        Initialize Firebase Auth adapter.

        Args:
            project_id: Firebase project ID (e.g., "alek-core-prod")
            web_api_key: Firebase Web API key (from Firebase Console)
            service_account_path: Path to service account JSON (optional if GOOGLE_APPLICATION_CREDENTIALS set)
            oauth_client_id: Google OAuth Client ID (from GCP Console)
            oauth_client_secret: Google OAuth Client Secret (from GCP Console)
        """
        self.project_id = project_id
        self.web_api_key = web_api_key
        self.oauth_client_id = oauth_client_id or f"{project_id}.apps.googleusercontent.com"
        self.oauth_client_secret = oauth_client_secret or ""

        # Initialize Firebase Admin SDK (if not already initialized)
        try:
            if service_account_path:
                cred = credentials.Certificate(service_account_path)
                initialize_app(cred)
            else:
                # Use Application Default Credentials
                initialize_app()
            logger.info(f"🔐 Firebase Admin SDK initialized for project: {project_id}")
        except ValueError as e:
            # Already initialized
            logger.debug(f"Firebase Admin SDK already initialized: {e}")

        # Firebase Auth REST API endpoints
        self.token_endpoint = "https://securetoken.googleapis.com/v1/token"
        self.userinfo_endpoint = "https://www.googleapis.com/oauth2/v3/userinfo"

        # Google OAuth endpoints (used for authorization URL)
        self.google_auth_url = "https://accounts.google.com/o/oauth2/v2/auth"
        self.google_token_url = "https://oauth2.googleapis.com/token"

    def get_provider_name(self) -> str:
        """Return 'firebase' as provider identifier."""
        return "firebase"

    def get_authorization_url(self, state: str, redirect_uri: str) -> str:
        """
        Generate Google OAuth authorization URL.

        Firebase uses Google's OAuth 2.0 for sign-in.
        This URL redirects user to Google sign-in page.

        Args:
            state: CSRF protection token
            redirect_uri: OAuth callback URL (e.g., "https://alek.ai/auth/callback")

        Returns:
            Full authorization URL with required parameters
        """
        params = {
            "client_id": self.oauth_client_id,
            "redirect_uri": redirect_uri,
            "response_type": "code",
            "scope": "openid email profile",
            "state": state,
            "access_type": "offline",  # Request refresh token
            "prompt": "consent",  # Force consent screen for refresh token
        }

        url = f"{self.google_auth_url}?{urlencode(params)}"
        logger.debug(f"Generated OAuth URL: {url[:100]}...")
        return url

    async def exchange_code_for_tokens(self, code: str, redirect_uri: str) -> OAuthTokens:
        """
        Exchange authorization code for OAuth tokens.

        Uses Google OAuth 2.0 token endpoint to exchange code for:
        - access_token (for UserInfo API)
        - id_token (JWT with user claims)
        - refresh_token (for token refresh)

        Args:
            code: Authorization code from OAuth callback
            redirect_uri: Same redirect_uri used in authorization

        Returns:
            OAuthTokens with access_token, id_token, refresh_token

        Raises:
            ValueError: Invalid code or network error
        """
        payload = {
            "code": code,
            "client_id": self.oauth_client_id,
            "client_secret": self.oauth_client_secret,
            "redirect_uri": redirect_uri,
            "grant_type": "authorization_code",
        }

        try:
            async with aiohttp.ClientSession() as session:
                # Step 1: Exchange code for Google OAuth tokens
                async with session.post(self.google_token_url, data=payload) as response:
                    if response.status != 200:
                        error_text = await response.text()
                        raise ValueError(f"Token exchange failed: {error_text}")

                    google_data = await response.json()

                # Use Google ID tokens directly (no Firebase exchange needed)
                # We'll verify using Google's public keys instead of Firebase Admin SDK
                logger.info("✅ Using Google OAuth tokens (direct verification)")
                return OAuthTokens(
                    access_token=google_data["access_token"],
                    id_token=google_data["id_token"],
                    refresh_token=google_data.get("refresh_token"),
                    expires_in=google_data["expires_in"],
                    token_type=google_data.get("token_type", "Bearer"),
                )

        except Exception as e:
            logger.error(f"Failed to exchange code for tokens: {e}")
            raise ValueError(f"Token exchange failed: {e}")

    async def verify_token(self, id_token: str) -> TokenClaims:
        """
        Verify and decode Google OAuth ID token (JWT).

        Verifies using Google's OIDC discovery and public keys.
        Accepts both Firebase and Google OAuth ID tokens.

        Args:
            id_token: Google OAuth or Firebase ID token (JWT)

        Returns:
            TokenClaims with user claims (sub, email, name, etc.)

        Raises:
            ValueError: Invalid token, expired, or signature failed
        """
        import jwt as pyjwt
        from jwt import PyJWKClient

        try:
            # First, try Firebase Admin SDK (for Firebase tokens)
            try:
                decoded_token = firebase_auth.verify_id_token(id_token)
                logger.debug("✅ Verified as Firebase ID token")

                return TokenClaims(
                    sub=decoded_token["sub"],
                    iss=decoded_token["iss"],
                    aud=decoded_token["aud"],
                    exp=datetime.fromtimestamp(decoded_token["exp"]),
                    iat=datetime.fromtimestamp(decoded_token["iat"]),
                    email=decoded_token.get("email"),
                    email_verified=decoded_token.get("email_verified", False),
                    name=decoded_token.get("name"),
                    picture=decoded_token.get("picture"),
                    custom_claims=decoded_token.get("custom_claims", {}),
                )
            except (FirebaseError, ValueError):
                # Not a Firebase token, try Google OAuth verification
                logger.debug("⚙️ Verifying as Google OAuth ID token")

                # Use Google's OIDC public keys
                jwks_client = PyJWKClient("https://www.googleapis.com/oauth2/v3/certs")
                signing_key = jwks_client.get_signing_key_from_jwt(id_token)

                # Verify and decode
                decoded_token = pyjwt.decode(
                    id_token,
                    signing_key.key,
                    algorithms=["RS256"],
                    audience=self.oauth_client_id,  # Verify audience matches our OAuth client
                    options={"verify_exp": True}
                )

                logger.debug("✅ Verified as Google OAuth ID token")

                return TokenClaims(
                    sub=decoded_token["sub"],
                    iss=decoded_token["iss"],
                    aud=decoded_token["aud"],
                    exp=datetime.fromtimestamp(decoded_token["exp"]),
                    iat=datetime.fromtimestamp(decoded_token["iat"]),
                    email=decoded_token.get("email"),
                    email_verified=decoded_token.get("email_verified", False),
                    name=decoded_token.get("name"),
                    picture=decoded_token.get("picture"),
                    custom_claims={},
                )

        except Exception as e:
            logger.error(f"Token verification failed: {e}")
            raise ValueError(f"Invalid ID token: {e}")

    async def get_user_info(self, access_token: str) -> OAuthUserInfo:
        """
        Fetch user info from Google's UserInfo endpoint.

        Retrieves OIDC standard user profile fields:
        - sub, email, name, picture, etc.

        Args:
            access_token: Valid OAuth access token

        Returns:
            OAuthUserInfo with user profile

        Raises:
            ValueError: Invalid token or network error
        """
        headers = {"Authorization": f"Bearer {access_token}"}

        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(self.userinfo_endpoint, headers=headers) as response:
                    if response.status != 200:
                        error_text = await response.text()
                        raise ValueError(f"UserInfo request failed: {error_text}")

                    data = await response.json()

                    return OAuthUserInfo(
                        sub=data["sub"],
                        email=data.get("email"),
                        email_verified=data.get("email_verified", False),
                        name=data.get("name"),
                        given_name=data.get("given_name"),
                        family_name=data.get("family_name"),
                        picture=data.get("picture"),
                        locale=data.get("locale"),
                        provider_metadata=data,  # Store full response
                    )
        except Exception as e:
            logger.error(f"Failed to fetch user info: {e}")
            raise ValueError(f"UserInfo request failed: {e}")

    async def refresh_access_token(self, refresh_token: str) -> OAuthTokens:
        """
        Refresh expired access token using refresh token.

        Uses Firebase Auth REST API to exchange refresh_token for new tokens.

        Args:
            refresh_token: Valid refresh token

        Returns:
            New OAuthTokens with refreshed access_token and id_token

        Raises:
            ValueError: Invalid refresh token or network error
        """
        payload = {
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
        }

        url = f"{self.token_endpoint}?key={self.web_api_key}"

        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(url, json=payload) as response:
                    if response.status != 200:
                        error_text = await response.text()
                        raise ValueError(f"Token refresh failed: {error_text}")

                    data = await response.json()

                    return OAuthTokens(
                        access_token=data["access_token"],
                        id_token=data["id_token"],
                        refresh_token=data.get("refresh_token", refresh_token),  # May return same token
                        expires_in=int(data["expires_in"]),
                        token_type=data.get("token_type", "Bearer"),
                    )
        except Exception as e:
            logger.error(f"Failed to refresh token: {e}")
            raise ValueError(f"Token refresh failed: {e}")
