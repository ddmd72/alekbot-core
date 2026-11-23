"""DeviceSessionService — revocable Cabinet device sessions (CABINET_DEVICE_SESSIONS_RFC §7.1, §7.5, §7.6).

A sign-in `start`s a session: a stored record (`DeviceSessionRepository`), an access JWT carrying its `sid`
(`SessionService.create_device_access_token`, `DeviceSessionPolicy.ACCESS_TTL`), and a random refresh secret whose
SHA-256 alone is stored; a Google sign-in tells the owner through `UserNotificationService.notify_ui_notice` (the platform hint is
untrusted, sent literally). A passkey sign-in sends no notice: the passkey was announced when it was enrolled and
signing in with it needs that device's biometrics (§7.5). A passkey
sign-in also counts as the session's first passkey assertion (`passkey_at`, read by a call start, §7.3). A `refresh` presents the secret: the repository decides and writes in one transaction
(rotate, grace, replay → revoked, dead). The gate asks `is_live(sid)`, answered from a per-instance cache for at most
`GATE_CACHE_TTL` (the revocation latency bound), else from the record.

The cache is filled after an awaited read and cleared by revocations on this instance, so its write-back is guarded by
a version counter read before the await and compared under the lock right before the write (`src/services/CLAUDE.md`;
`ChannelBindingService._versions`): a read that began before a revocation never writes its live record back.
`revoke_all` cannot know the sids of reads in flight, so it advances an epoch that is part of every version.

`revoke_all` (the kill switch, §7.6) also ends every MCP connector grant of the user through the injected
`revoke_connector_grants` (the MCP refresh-token store's user-wide revocation), so this service imports nothing of MCP;
it runs even when the session revocation raised.

Role and tier claims come from the `user` and `account` the caller loaded (at sign-in, or `resume_session` before a
refresh), never from the record or an earlier token.
"""
import asyncio
import secrets
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Awaitable, Callable, Dict, List, Optional, Tuple

from ..domain.billing import BillingAccount
from ..domain.device_session import DeviceSession, SessionAuthKind, SessionRevokeReason
from ..domain.device_session_policy import DeviceSessionPolicy, RefreshOutcome, refresh_secret_hash
from ..domain.device_session_refresh import RefreshResult
from ..domain.ui_messages import UIMessage
from ..domain.user import UserProfile
from ..ports.device_session_repository import DeviceSessionRepository
from ..utils.logger import logger

if TYPE_CHECKING:
    from .session_service import SessionService
    from .user_notification_service import UserNotificationService


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _new_sid() -> str:
    return secrets.token_urlsafe(16)


def _new_secret() -> str:
    return secrets.token_urlsafe(32)    # 32 random bytes


def _notice_time(now: datetime) -> str:
    return now.astimezone(timezone.utc).strftime("%H:%M UTC")


class DeviceSessionService:

    def __init__(self, repo: DeviceSessionRepository, session_service: "SessionService",
                 notifications: "UserNotificationService", *, clock: Callable[[], datetime] = _utcnow,
                 new_sid: Callable[[], str] = _new_sid, new_secret: Callable[[], str] = _new_secret,
                 revoke_connector_grants: Optional[Callable[[str, datetime], Awaitable[int]]] = None) -> None:
        """`revoke_connector_grants(user_id, now)` revokes every MCP refresh token of the user and returns how many;
        None: `revoke_all` revokes no connector grant (a service without the MCP server)."""
        self._repo = repo
        self._revoke_connector_grants = revoke_connector_grants
        self._sessions = session_service
        self._notifications = notifications
        self._clock = clock
        self._new_sid = new_sid
        self._new_secret = new_secret
        # sid → (the record read, or None for an unknown sid; when it was read). Guarded by `_lock`.
        self._cache: Dict[str, Tuple[Optional[DeviceSession], datetime]] = {}
        # Per sid, advanced by every change this instance makes; `_epoch` by `revoke_all`. A cache-miss read writes
        # back only if neither moved while it awaited the repository.
        self._versions: Dict[str, int] = {}
        self._epoch = 0
        self._lock = asyncio.Lock()

    # ------------------------------------------------------------------ sign-in and refresh

    async def start(self, user: UserProfile, account: BillingAccount, *, label: str, platform_hint: str,
                    auth_kind: SessionAuthKind) -> Tuple[DeviceSession, str, str]:
        """Create the session of a new sign-in and announce it when it is a Google sign-in. Returns the record, the access token and the refresh
        secret (the caller sets the cookies; the secret is never stored)."""
        now = self._clock()
        secret = self._new_secret()
        session = DeviceSession(
            sid=self._new_sid(), user_id=user.user_id, account_id=account.account_id, label=label,
            platform_hint=platform_hint, auth_kind=auth_kind, auth_at=now, created_at=now, last_seen_at=now,
            expires_at=DeviceSessionPolicy.absolute_end(now), refresh_hash=refresh_secret_hash(secret),
            purge_after=DeviceSessionPolicy.purge_after(now),
            passkey_at=now if auth_kind is SessionAuthKind.PASSKEY else None)
        version = await self._read_version(session.sid)
        if not await self._repo.create(session):
            logger.error(f"💥 Device session id collision: sid={session.sid[:8]} user={user.user_id[:8]}")
            raise RuntimeError("device session id is taken")
        await self._remember(session.sid, session, now, version)
        access_token = self._sessions.create_device_access_token(user, account, session.sid, now)
        if auth_kind is not SessionAuthKind.PASSKEY:
            await self._announce(session, now)
        return session, access_token, secret

    async def refresh(self, sid: str, refresh_secret: str, user: UserProfile, account: BillingAccount) -> RefreshResult:
        """Present the refresh cookie's secret for `sid`. `user` and `account` are `resume_session`'s (it ran first).
        ROTATE: a new access token and a new secret; GRACE: an access token only; REPLAY (the session is now revoked)
        and DEAD: neither. A session of another user than `user` is DEAD, logged at ERROR."""
        now = self._clock()
        new_secret = self._new_secret()
        version = await self._read_version(sid)
        outcome, session = await self._repo.refresh(sid, refresh_secret_hash(refresh_secret),
                                                    refresh_secret_hash(new_secret), now)
        if session is None:
            return RefreshResult(outcome, None)
        await self._remember(sid, session, now, version)
        if outcome in (RefreshOutcome.REPLAY, RefreshOutcome.DEAD):
            logger.info(f"⛔ Device session refresh refused: sid={sid[:8]} outcome={outcome.value}")
            return RefreshResult(outcome, session)
        if session.user_id != user.user_id:
            logger.error(f"💥 Device session sid={sid[:8]} belongs to another user than {user.user_id[:8]}; refused")
            return RefreshResult(RefreshOutcome.DEAD, session)
        access_token = self._sessions.create_device_access_token(user, account, sid, now)
        if outcome is RefreshOutcome.GRACE:
            return RefreshResult(outcome, session, access_token=access_token)
        return RefreshResult(outcome, session, access_token=access_token, refresh_secret=new_secret)

    def mint_access(self, session: DeviceSession, user: UserProfile, account: BillingAccount) -> str:
        """A fresh access token for `session`, with the claims of `user` and `account` as loaded now."""
        if session.user_id != user.user_id:
            raise ValueError("the session belongs to another user")
        return self._sessions.create_device_access_token(user, account, session.sid, self._clock())

    # ------------------------------------------------------------------ the gate

    async def is_live(self, sid: str) -> bool:
        """Whether `sid` names a live session (`DeviceSessionPolicy.is_live`), from a record at most
        `GATE_CACHE_TTL` old."""
        now = self._clock()
        async with self._lock:
            cached = self._cache.get(sid)
            if cached is not None and now - cached[1] < DeviceSessionPolicy.GATE_CACHE_TTL:
                return cached[0] is not None and DeviceSessionPolicy.is_live(cached[0], now)
            version = self._version(sid)
        session = await self._repo.get(sid)
        async with self._lock:
            if self._version(sid) == version:
                self._cache[sid] = (session, now)
        return session is not None and DeviceSessionPolicy.is_live(session, now)

    # ------------------------------------------------------------------ revocation and the list

    async def revoke(self, user_id: str, sid: str, reason: SessionRevokeReason) -> bool:
        """Revoke one session of `user_id`. False for an unknown sid, a sid of another user (refused, nothing
        written) or a session already revoked."""
        session = await self._repo.get(sid)
        if session is None or session.user_id != user_id:
            logger.warning(f"⛔ Device session revoke refused: sid={sid[:8]} is not a session of {user_id[:8]}")
            return False
        revoked = await self._repo.revoke(sid, reason, self._clock())
        await self._forget(sid)
        return revoked

    async def revoke_all(self, user_id: str, reason: SessionRevokeReason) -> int:
        """Revoke every session and passkey of `user_id` and cancel a pending facts PIN reset (the kill switch, also the
        account lock's step), then every MCP connector grant of the user. Returns how many sessions were revoked. The
        grants are revoked even when the session revocation raised; that error is then raised after it."""
        now = self._clock()
        try:
            count = await self._repo.revoke_all(user_id, reason, now)
        finally:
            async with self._lock:
                self._epoch += 1
                self._cache.clear()
            if self._revoke_connector_grants is not None:
                grants = await self._revoke_connector_grants(user_id, now)
                logger.info(f"🔒 MCP connector grants revoked with the sessions: user={user_id[:8]} count={grants} "
                            f"reason={reason.value}")
        return count

    async def current(self, user_id: str, sid: str) -> Optional[DeviceSession]:
        """The record of `sid` when it is a live session of `user_id` (read now, not from the gate's cache); None
        otherwise. For a route behind the gate that needs the session itself (its label, how it was signed in)."""
        session = await self._repo.get(sid)
        if session is None or session.user_id != user_id or not DeviceSessionPolicy.is_live(session, self._clock()):
            return None
        return session

    async def list_for_user(self, user_id: str) -> List[DeviceSession]:
        """The user's live sessions, oldest first."""
        now = self._clock()
        return [s for s in await self._repo.list_for_user(user_id) if DeviceSessionPolicy.is_live(s, now)]

    # ------------------------------------------------------------------ helpers

    def _version(self, sid: str) -> Tuple[int, int]:
        return self._epoch, self._versions.get(sid, 0)

    async def _read_version(self, sid: str) -> Tuple[int, int]:
        async with self._lock:
            return self._version(sid)

    async def _remember(self, sid: str, session: DeviceSession, now: datetime, version: Tuple[int, int]) -> None:
        """Cache the record this instance just wrote, unless a change ran on this instance since `version` was read
        (then the entry is dropped: the next gate check reads the record). Advances the version either way, so a read
        in flight cannot write an older record back."""
        async with self._lock:
            unchanged = self._version(sid) == version
            self._versions[sid] = self._versions.get(sid, 0) + 1
            if unchanged:
                self._cache[sid] = (session, now)
            else:
                self._cache.pop(sid, None)

    async def _forget(self, sid: str) -> None:
        async with self._lock:
            self._versions[sid] = self._versions.get(sid, 0) + 1
            self._cache.pop(sid, None)

    async def _announce(self, session: DeviceSession, now: datetime) -> None:
        """The new sign-in notice of a Google sign-in; a failure is logged and never fails the sign-in."""
        message = UIMessage.DEVICE_SIGN_IN_NOTICE
        try:
            sent = await self._notifications.notify_ui_notice(
                session.user_id, session.account_id, message,
                platform_hint=session.platform_hint, time=_notice_time(now))
        except Exception as exc:
            logger.error(f"💥 New sign-in notice failed: user={session.user_id[:8]} sid={session.sid[:8]}: {exc}",
                         exc_info=True)
            return
        if not sent:
            logger.warning(f"New sign-in notice not delivered: user={session.user_id[:8]} sid={session.sid[:8]}")
