"""
Session Service (OAuth Multi-Tenant Session 4): the Cabinet's access JWTs, each bound to a device session.

RFC: docs/10_rfcs/MULTI_TENANT_OAUTH_RFC.md, docs/10_rfcs/CABINET_DEVICE_SESSIONS_RFC.md §7.1
"""
import jwt
from typing import Optional, Dict, Any
from datetime import datetime

from ..domain.user import UserProfile
from ..domain.billing import BillingAccount
from ..domain.device_session_policy import DeviceSessionPolicy
from ..utils.logger import logger


class SessionService:
    """
    Signs and verifies the Cabinet's access JWTs (HS256, secret `OAUTH_SESSION_SECRET`).

    Every access token belongs to a device session (CABINET_DEVICE_SESSIONS_RFC §7.1): it carries the session's `sid`
    and lives `DeviceSessionPolicy.ACCESS_TTL`. Renewal is not a token: `DeviceSessionService` rotates a stored refresh
    secret, and the gate (`src/web/cabinet_auth.py`) asks whether the sid is still live.

    Claims: sub (user_id), account_id, external_user_id, role (from the account's IAM policy), tier, email, iat, exp,
    type ("access"), sid.
    """

    def __init__(self, secret_key: str):
        """
        Args:
            secret_key: JWT signing secret (min 32 characters)

        Raises:
            ValueError: If secret_key is too short
        """
        if len(secret_key) < 32:
            raise ValueError("JWT secret must be at least 32 characters")

        self.secret_key = secret_key
        self.algorithm = "HS256"

    def create_device_access_token(
        self,
        user: UserProfile,
        account: BillingAccount,
        sid: str,
        now: datetime,
    ) -> str:
        """
        Create the access JWT of a device session (CABINET_DEVICE_SESSIONS_RFC §7.1): the claims of
        `create_access_token` plus `sid`, expiring `DeviceSessionPolicy.ACCESS_TTL` after `now`. Role and tier come
        from `user` and `account` as stored now, never from an earlier token.
        """
        exp = DeviceSessionPolicy.access_expiry(now)
        payload = {
            "sub": user.user_id,
            "iat": int(now.timestamp()),
            "exp": int(exp.timestamp()),
            "account_id": account.account_id,
            "external_user_id": user.external_user_id,
            "role": account.iam_policy.get(user.user_id, "viewer"),
            "tier": account.tier.value,
            "email": user.email,
            "type": "access",
            "sid": sid,
        }
        token = jwt.encode(payload, self.secret_key, algorithm=self.algorithm)
        logger.debug(f"🎫 Created device access token for user: {user.user_id}, sid: {sid[:8]}, expires: {exp}")
        return token

    def verify_device_access_token(self, token: str, *, allow_expired: bool = False) -> Dict[str, Any]:
        """
        Verify a device-session access JWT and return its payload, which carries `sid`.

        The signature and the type are always checked, and a token without a `sid` is refused. Expiration is checked
        unless `allow_expired`: the refresh path asks for it, because it needs the sid of an expired access token
        (the session record, not the token, decides whether that sid may still refresh).

        Raises:
            jwt.ExpiredSignatureError: Token expired (only without `allow_expired`)
            jwt.InvalidTokenError: Invalid token or signature
            ValueError: Wrong token type, or no session id
        """
        try:
            payload: Dict[str, Any] = jwt.decode(
                token,
                self.secret_key,
                algorithms=[self.algorithm],
                options={"verify_exp": not allow_expired},
            )
        except jwt.InvalidTokenError as e:
            logger.warning(f"❌ Device access token refused: {type(e).__name__}")
            raise
        if payload.get("type") != "access":
            raise ValueError(f"Expected access token, got: {payload.get('type')}")
        sid = payload.get("sid")
        if not isinstance(sid, str) or not sid:
            logger.warning(f"❌ Access token without a session id refused - user: {payload.get('sub')}")
            raise ValueError("Access token carries no session id")
        return payload

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
