"""
Session Service (OAuth Multi-Tenant Session 4).

Handles JWT token generation, validation, and session management.
Provides secure stateless authentication for web UI.

RFC: docs/10_rfcs/MULTI_TENANT_OAUTH_RFC.md
"""
import jwt
from typing import Optional, Dict, Any
from datetime import datetime, timedelta, timezone

from ..domain.user import UserProfile
from ..domain.billing import BillingAccount
from ..utils.logger import logger


class SessionService:
    """
    JWT-based session management service.

    Provides stateless authentication via JWT tokens:
    - Access tokens (short-lived, 1 hour)
    - Refresh tokens (long-lived, 30 days)

    Tokens contain:
    - user_id: Internal user UUID
    - account_id: Billing account UUID
    - external_user_id: OAuth identity ("firebase|abc123")
    - role: User role in account (owner, member, viewer)
    - tier: Account tier (free, family, pro, enterprise)

    Security:
    - HS256 algorithm (symmetric key)
    - Secret key from environment (OAUTH_SESSION_SECRET)
    - Token expiration enforcement
    - Signature verification
    """

    def __init__(
        self,
        secret_key: str,
        access_token_ttl: int = 3600,  # 1 hour
        refresh_token_ttl: int = 2592000,  # 30 days
    ):
        """
        Initialize session service.

        Args:
            secret_key: JWT signing secret (min 32 characters)
            access_token_ttl: Access token TTL in seconds (default: 1 hour)
            refresh_token_ttl: Refresh token TTL in seconds (default: 30 days)

        Raises:
            ValueError: If secret_key is too short
        """
        if len(secret_key) < 32:
            raise ValueError("JWT secret must be at least 32 characters")

        self.secret_key = secret_key
        self.access_token_ttl = access_token_ttl
        self.refresh_token_ttl = refresh_token_ttl
        self.algorithm = "HS256"

    def create_access_token(
        self,
        user: UserProfile,
        account: BillingAccount,
    ) -> str:
        """
        Create JWT access token for authenticated user.

        Token payload:
        - sub: user_id (subject)
        - account_id: Billing account UUID
        - external_user_id: OAuth identity
        - role: User role in account
        - tier: Account tier
        - email: User email
        - iat: Issued at timestamp
        - exp: Expiration timestamp
        - type: "access"

        Args:
            user: Authenticated user profile
            account: User's billing account

        Returns:
            JWT access token (encoded string)
        """
        # Get user role from account IAM policy
        role = account.iam_policy.get(user.user_id, "viewer")

        now = datetime.now(timezone.utc)
        exp = now + timedelta(seconds=self.access_token_ttl)

        payload = {
            # Standard JWT claims
            "sub": user.user_id,  # Subject (user identifier)
            "iat": int(now.timestamp()),  # Issued at
            "exp": int(exp.timestamp()),  # Expiration
            # Custom claims
            "account_id": account.account_id,
            "external_user_id": user.external_user_id,
            "role": role,
            "tier": account.tier.value,
            "email": user.email,
            "type": "access",
        }

        token = jwt.encode(payload, self.secret_key, algorithm=self.algorithm)
        logger.debug(f"🎫 Created access token for user: {user.user_id}, expires: {exp}")

        return token

    def create_refresh_token(
        self,
        user: UserProfile,
        account: BillingAccount,
    ) -> str:
        """
        Create JWT refresh token for token renewal.

        Refresh tokens have longer TTL and contain minimal data.

        Token payload:
        - sub: user_id
        - account_id: Billing account UUID
        - iat: Issued at timestamp
        - exp: Expiration timestamp
        - type: "refresh"

        Args:
            user: Authenticated user profile
            account: User's billing account

        Returns:
            JWT refresh token (encoded string)
        """
        now = datetime.now(timezone.utc)
        exp = now + timedelta(seconds=self.refresh_token_ttl)

        payload = {
            "sub": user.user_id,
            "iat": int(now.timestamp()),
            "exp": int(exp.timestamp()),
            "account_id": account.account_id,
            "type": "refresh",
        }

        token = jwt.encode(payload, self.secret_key, algorithm=self.algorithm)
        logger.debug(f"🎫 Created refresh token for user: {user.user_id}, expires: {exp}")

        return token

    def verify_access_token(self, token: str) -> Dict[str, Any]:
        """
        Verify and decode JWT access token.

        Verifies:
        - Signature (HS256)
        - Expiration (exp claim)
        - Token type (type: "access")

        Args:
            token: JWT access token

        Returns:
            Decoded token payload (dict)

        Raises:
            jwt.ExpiredSignatureError: Token expired
            jwt.InvalidTokenError: Invalid token or signature
            ValueError: Wrong token type
        """
        try:
            payload = jwt.decode(
                token,
                self.secret_key,
                algorithms=[self.algorithm],
            )

            # Verify token type
            if payload.get("type") != "access":
                raise ValueError(f"Expected access token, got: {payload.get('type')}")

            logger.debug(f"✅ Access token verified - user: {payload.get('sub')}")
            return payload

        except jwt.ExpiredSignatureError:
            logger.warning("⏰ Access token expired")
            raise
        except jwt.InvalidTokenError as e:
            logger.warning(f"❌ Invalid access token: {e}")
            raise

    def verify_refresh_token(self, token: str) -> Dict[str, Any]:
        """
        Verify and decode JWT refresh token.

        Verifies:
        - Signature (HS256)
        - Expiration (exp claim)
        - Token type (type: "refresh")

        Args:
            token: JWT refresh token

        Returns:
            Decoded token payload (dict)

        Raises:
            jwt.ExpiredSignatureError: Token expired
            jwt.InvalidTokenError: Invalid token or signature
            ValueError: Wrong token type
        """
        try:
            payload = jwt.decode(
                token,
                self.secret_key,
                algorithms=[self.algorithm],
            )

            # Verify token type
            if payload.get("type") != "refresh":
                raise ValueError(f"Expected refresh token, got: {payload.get('type')}")

            logger.debug(f"✅ Refresh token verified - user: {payload.get('sub')}")
            return payload

        except jwt.ExpiredSignatureError:
            logger.warning("⏰ Refresh token expired")
            raise
        except jwt.InvalidTokenError as e:
            logger.warning(f"❌ Invalid refresh token: {e}")
            raise

    def decode_token_unsafe(self, token: str) -> Optional[Dict[str, Any]]:
        """
        Decode JWT token without verification (for debugging).

        WARNING: Does not verify signature or expiration.
        Use only for logging/debugging purposes.

        Args:
            token: JWT token

        Returns:
            Decoded payload or None if invalid
        """
        try:
            payload = jwt.decode(
                token,
                options={"verify_signature": False, "verify_exp": False},
            )
            return payload
        except Exception as e:
            logger.error(f"Failed to decode token: {e}")
            return None
