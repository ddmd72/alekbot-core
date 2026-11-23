"""CabinetSessionKit — a signed-in Cabinet device session for web tests (CABINET_DEVICE_SESSIONS_RFC §7.1).

Everything on the session path is real: `SessionService` signs the access JWT, `DeviceSessionService` starts,
refreshes and revokes over the in-memory repository twin (`FakeDeviceSessionRepository`, which applies the port's
rules), and `CabinetSessionVerifier` is the one verifier the blueprints take (`cabinet_sessions=kit.verifier`). Only
the new-sign-in notice is recorded instead of sent.

The clock starts at the real now, because the gate verifies a JWT's `exp` against the real time; `clock.set(...)`
and `clock.advance(...)` move the session's own instants (minting, idle and absolute ends, the rotation grace).
"""
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import List, Tuple

from src.domain.billing import AccountTier, BillingAccount
from src.domain.device_session_policy import DeviceSessionPolicy, refresh_secret_hash
from src.domain.device_session import DeviceSession, SessionAuthKind, SessionRevokeReason
from src.domain.user import UserProfile
from src.services.device_session_service import DeviceSessionService
from src.services.session_service import SessionService
from src.web.cabinet_auth import ACCESS_COOKIE, REFRESH_COOKIE, CabinetSessionVerifier
from tests.support.device_session_repo import FakeDeviceSessionRepository

KIT_SECRET = "kit-session-secret-0123456789abcdef-xyz"
ALEX_ID, ALEX_ACCOUNT = "user-alex", "acct-alex"


class KitClock:
    def __init__(self) -> None:
        self.now = datetime.now(timezone.utc)

    def __call__(self) -> datetime:
        return self.now

    def set(self, now: datetime) -> None:
        self.now = now

    def advance(self, **delta: float) -> None:
        self.now += timedelta(**delta)


class RecordingNotices:
    """Stands in for `UserNotificationService.notify_ui_notice`: records (user_id, message, params)."""

    def __init__(self) -> None:
        self.sent: List[Tuple[str, object, dict]] = []

    async def notify_ui_notice(self, user_id: str, account_id: str, message: object, **params: object) -> bool:
        self.sent.append((user_id, message, dict(params)))
        return True


@dataclass(frozen=True)
class SignedIn:
    session: DeviceSession
    access_token: str
    refresh_secret: str

    @property
    def sid(self) -> str:
        return self.session.sid

    @property
    def bearer(self) -> dict:
        return {"Authorization": f"Bearer {self.access_token}"}


class CabinetSessionKit:

    def __init__(self) -> None:
        self.clock = KitClock()
        self.repo = FakeDeviceSessionRepository()
        self.tokens = SessionService(secret_key=KIT_SECRET)
        self.notices = RecordingNotices()
        self.device_sessions = DeviceSessionService(self.repo, self.tokens, self.notices, clock=self.clock)
        self.verifier = CabinetSessionVerifier(self.tokens, self.device_sessions)

    async def sign_in(self, user_id: str = ALEX_ID, account_id: str = ALEX_ACCOUNT, *, role: str = "owner",
                      email: str = "alex@example.com", label: str = "Mac — Chrome") -> SignedIn:
        user = UserProfile(user_id=user_id, account_id=account_id, email=email,
                           external_user_id=f"firebase|{user_id}")
        account = BillingAccount(account_id=account_id, tier=AccountTier.FAMILY, iam_policy={user_id: role})
        session, token, secret = await self.device_sessions.start(
            user, account, label=label, platform_hint=label, auth_kind=SessionAuthKind.GOOGLE)
        return SignedIn(session, token, secret)

    def sign_in_now(self, user_id: str = ALEX_ID, account_id: str = ALEX_ACCOUNT, *, role: str = "owner",
                    email: str = "alex@example.com", label: str = "Mac — Chrome") -> SignedIn:
        """`sign_in` for a synchronous caller (a world's `token()` helper): the same record and the same access JWT,
        stored straight in the repository twin; no new-sign-in notice is recorded."""
        user = UserProfile(user_id=user_id, account_id=account_id, email=email,
                           external_user_id=f"firebase|{user_id}")
        account = BillingAccount(account_id=account_id, tier=AccountTier.FAMILY, iam_policy={user_id: role})
        now = self.clock()
        secret = secrets.token_urlsafe(32)
        session = DeviceSession(
            sid=secrets.token_urlsafe(16), user_id=user_id, account_id=account_id, label=label, platform_hint=label,
            auth_kind=SessionAuthKind.GOOGLE, auth_at=now, created_at=now, last_seen_at=now,
            expires_at=DeviceSessionPolicy.absolute_end(now), refresh_hash=refresh_secret_hash(secret),
            purge_after=DeviceSessionPolicy.purge_after(now))
        self.repo.sessions[session.sid] = session
        return SignedIn(session, self.tokens.create_device_access_token(user, account, session.sid, now), secret)

    async def revoke(self, signed_in: SignedIn) -> None:
        await self.device_sessions.revoke(signed_in.session.user_id, signed_in.sid, SessionRevokeReason.LOGOUT)

    @staticmethod
    def set_cookies(client, signed_in: SignedIn, *, refresh: bool = True) -> None:
        """Put the session's cookies into a Quart test client's jar."""
        client.set_cookie("localhost", ACCESS_COOKIE, signed_in.access_token)
        if refresh:
            client.set_cookie("localhost", REFRESH_COOKIE, signed_in.refresh_secret)


def cabinet_session_for(user_id: str, account_id: str, *, role: str = "owner",
                        email: str = "alex@example.com") -> Tuple[CabinetSessionVerifier, dict]:
    """A verifier and the Bearer header of one live session of `user_id` in `account_id` with `role`: for a test that
    builds a blueprint for one fixed caller (`cabinet_sessions=verifier`, `headers=bearer`)."""
    kit = CabinetSessionKit()
    return kit.verifier, kit.sign_in_now(user_id, account_id, role=role, email=email).bearer
