"""The Cabinet's one session verifier and its cookies (CABINET_DEVICE_SESSIONS_RFC §7.1 "The gate").

Every route that asks who is signed in asks `CabinetSessionVerifier.identify()`: the gate of the Cabinet blueprints
(`make_auth_required`) and each route that decides by the session itself (the landing redirect, the MCP consent page,
a gated file link, the connect routes, `/auth/me`, `/auth/link-oauth`, the docs pages, `/join`). It verifies the access
JWT (a Bearer header for API clients, else the `__Host-access` cookie), refuses a token without a `sid`
(`SessionService.verify_device_access_token`), and then asks `DeviceSessionService.is_live(sid)`, so a revoked device
is shut out everywhere within the gate cache's bound. Nothing else in `src/` reads the access cookie
(`tests/unit/web/test_access_cookie_one_reader.py`): a route that read it itself would let a revoked device through.
A failed record read refuses (fail closed, RFC §10).

Both cookies live until the session's absolute end. The JWT inside the access cookie expires after
`DeviceSessionPolicy.ACCESS_TTL`, but the cookie must outlive it: `/auth/refresh` and `/auth/logout` take the sid from
that expired JWT (`presented_session`; the record, not the token, decides what the sid may still do). Both are
`__Host-` cookies: Secure, HttpOnly, SameSite=Lax, Path=/, no Domain.

This module holds the identity, the verifier, the gate decorator and the cookie writers on purpose: they are one
contract, and splitting them would invite a second reader of the cookie.
"""
from dataclasses import dataclass
from enum import Enum
from functools import wraps
from typing import TYPE_CHECKING, Any, Dict, Optional, Tuple

import jwt
from quart import g, jsonify, request

from ..domain.device_session import DeviceSession
from ..utils.logger import logger

if TYPE_CHECKING:
    from ..services.device_session_service import DeviceSessionService
    from ..services.session_service import SessionService

ACCESS_COOKIE = "__Host-access"
REFRESH_COOKIE = "__Host-refresh"
_COOKIE_FLAGS: Dict[str, Any] = {"secure": True, "httponly": True, "samesite": "Lax", "path": "/"}


@dataclass(frozen=True)
class CabinetIdentity:
    """Who a verified, live access token names. `expires_at` is the token's `exp` (Unix seconds)."""
    user_id: str
    account_id: str
    role: str
    sid: str
    expires_at: int
    email: Optional[str]
    external_user_id: Optional[str]
    tier: Optional[str]


class Refusal(str, Enum):
    """Why `CabinetSessionVerifier.verify()` named no identity. Routes that word their 401 by cause read this; the
    gate and every other caller treat all of them alike."""
    NO_TOKEN = "no_token"
    EXPIRED = "expired"      # a well-signed access token past its `exp`
    INVALID = "invalid"      # forged, malformed, wrong type, no sid, or missing user/account/expiry claims
    ENDED = "ended"          # the session is revoked, expired or unknown, or its record could not be read


@dataclass(frozen=True)
class Verification:
    """The outcome of verifying the request's access token: an identity, or the reason for none."""
    identity: Optional["CabinetIdentity"]
    refusal: Optional[Refusal]


def _identity(claims: Dict[str, Any]) -> Optional[CabinetIdentity]:
    user_id, account_id, exp = claims.get("sub"), claims.get("account_id"), claims.get("exp")
    if not isinstance(user_id, str) or not user_id or not isinstance(account_id, str) or not isinstance(exp, int):
        return None
    return CabinetIdentity(user_id=user_id, account_id=account_id, role=claims.get("role") or "viewer",
                           sid=claims["sid"], expires_at=exp, email=claims.get("email"),
                           external_user_id=claims.get("external_user_id"), tier=claims.get("tier"))


class CabinetSessionVerifier:
    """The one reader of the access token of the current request."""

    def __init__(self, session_service: "SessionService", device_sessions: "DeviceSessionService") -> None:
        self._tokens = session_service
        self._device_sessions = device_sessions

    def presents_token(self) -> bool:
        """Whether the request carries an access token at all (a Bearer header or the access cookie)."""
        return bool(self._presented_token())

    async def identify(self) -> Optional[CabinetIdentity]:
        """The identity of the request's access token when it verifies, is unexpired, carries a `sid` and that
        session is live; None otherwise (each refusal logged)."""
        return (await self.verify()).identity

    async def verify(self) -> Verification:
        """Like `identify()`, but a refusal says why (`Refusal`), for the routes that word their 401 by cause."""
        token = self._presented_token()
        if not token:
            return Verification(None, Refusal.NO_TOKEN)
        try:
            claims = self._tokens.verify_device_access_token(token)
        except jwt.ExpiredSignatureError:
            return Verification(None, Refusal.EXPIRED)    # logged by SessionService
        except (jwt.InvalidTokenError, ValueError):
            return Verification(None, Refusal.INVALID)    # logged by SessionService
        identity = _identity(claims)
        if identity is None:
            logger.warning("❌ Access token refused: no user, account or expiry claim")
            return Verification(None, Refusal.INVALID)
        try:
            live = await self._device_sessions.is_live(identity.sid)
        except Exception as exc:
            logger.error(f"💥 Device session check failed, request refused: sid={identity.sid[:8]}: {exc}",
                         exc_info=True)
            return Verification(None, Refusal.ENDED)
        if not live:
            logger.info(f"⛔ Access token of an ended device session refused: sid={identity.sid[:8]}")
            return Verification(None, Refusal.ENDED)
        return Verification(identity, None)

    def presented_session(self) -> Optional[Tuple[str, str]]:
        """(user_id, sid) named by the access cookie, its signature and type verified but its expiry not: for
        `/auth/refresh` and `/auth/logout` only, which ask the session record what the sid may still do."""
        token = request.cookies.get(ACCESS_COOKIE)
        if not token:
            return None
        try:
            claims = self._tokens.verify_device_access_token(token, allow_expired=True)
        except (jwt.InvalidTokenError, ValueError):
            return None    # logged by SessionService
        user_id = claims.get("sub")
        if not isinstance(user_id, str) or not user_id:
            logger.warning("❌ Access cookie without a user refused")
            return None
        return user_id, claims["sid"]

    @staticmethod
    def access_expiry(access_token: str) -> int:
        """The `exp` (Unix seconds) of an access token `DeviceSessionService` just minted. Never called on a token a
        client sent, so its claims are read without verification (a verification would also refuse an `iat` a hair in
        the future of this clock)."""
        return int(jwt.decode(access_token, options={"verify_signature": False})["exp"])

    @staticmethod
    def _presented_token() -> Optional[str]:
        auth_header = request.headers.get("Authorization", "")
        if auth_header.startswith("Bearer "):
            return auth_header[len("Bearer "):].strip() or None
        return request.cookies.get(ACCESS_COOKIE) or None


def set_session_cookies(response: Any, session: DeviceSession, access_token: str,
                        refresh_secret: Optional[str]) -> None:
    """Write the access cookie, and the refresh cookie when `refresh_secret` is given (a sign-in or a rotation; a
    grace refresh sets none). Both live until `session.expires_at`, counted from the session's `last_seen_at` (the
    instant of the sign-in or the refresh that just wrote the record)."""
    max_age = max(0, int((session.expires_at - session.last_seen_at).total_seconds()))
    response.set_cookie(ACCESS_COOKIE, access_token, max_age=max_age, **_COOKIE_FLAGS)
    if refresh_secret is not None:
        response.set_cookie(REFRESH_COOKIE, refresh_secret, max_age=max_age, **_COOKIE_FLAGS)


def clear_session_cookies(response: Any) -> None:
    """Delete both session cookies (with the flags a `__Host-` cookie needs to be deleted at all)."""
    for name in (ACCESS_COOKIE, REFRESH_COOKIE):
        response.delete_cookie(name, **_COOKIE_FLAGS)


def make_auth_required(verifier: CabinetSessionVerifier):
    """The Cabinet's session gate, shared by every Cabinet blueprint: 401 without a live session, else `g.user_id`,
    `g.account_id`, `g.role` and `g.session_id` are set for the route."""

    def auth_required(func):
        @wraps(func)
        async def wrapper(*args, **kwargs):
            if not verifier.presents_token():
                return jsonify({"error": "Missing authorization"}), 401
            identity = await verifier.identify()
            if identity is None:
                return jsonify({"error": "Invalid or expired token"}), 401
            g.user_id = identity.user_id
            g.account_id = identity.account_id
            g.role = identity.role
            g.session_id = identity.sid
            return await func(*args, **kwargs)
        return wrapper

    return auth_required
