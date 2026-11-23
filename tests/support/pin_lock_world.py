"""The world of the facts PIN and the account lock (CABINET_DEVICE_SESSIONS_RFC §7.6–§7.8), as `build_device_auth`
wires them: `FactsPinService` over the PIN twin (`tests/support/facts_pin_repo.py`) and the device store twin linked to
it (so `revoke_all` cancels a pending reset), the real `DeviceSessionService` whose `revoke_all` revokes MCP grants
through the real `FirestoreMCPClientRepository` on an `EvalDb`, the real `AccountLockService`, and the real
`UserNotificationService` posting to recording channels in English. One controlled clock.

The cast: `alex@example.com` (`user-alex`), with a primary Slack channel and a last active Telegram one, and
`user-other`, who shares nothing. Codes are `0001` (the PIN) and `0002` (the duress code) only.
"""
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Optional

from src.adapters.file_localization_adapter import FileLocalizationAdapter
from src.adapters.firestore_mcp_client_repository import FirestoreMCPClientRepository
from src.domain.device_session import DeviceSession, SessionAuthKind
from src.domain.device_session_policy import DeviceSessionPolicy
from src.domain.language import LanguageCode
from src.domain.mcp import MCPRefreshToken
from src.domain.passkey import Passkey, PasskeyKind
from src.domain.ui_messages import UIMessage
from src.services.account_lock_service import AccountLockService
from src.services.device_session_service import DeviceSessionService
from src.services.facts_pin_service import FactsPinService
from src.services.localization_service import LocalizationService
from src.services.session_service import SessionService
from src.services.user_notification_service import UserNotificationService
from tests.support.aes_gcm_cipher import AesGcmTestCipher
from tests.support.device_session_repo import FakeDeviceSessionRepository
from tests.support.fact_review_world import FakeNotificationState, RecordingChannelFactory
from tests.support.facts_pin_repo import FakeFactsPinRepository
from tests.support.firestore_eval import EvalDb

T0 = datetime(2026, 11, 1, 9, 0, tzinfo=timezone.utc)
KEY = bytes(range(32))
SECRET = "test-secret-key-must-be-32-characters-minimum"
PIN, DURESS, WRONG = "0001", "0002", "0000"
PHONE, MAC = "iPhone — Home Screen (call)", "Mac — Chrome"
REFRESH = "pin_world_mcp_refresh_tokens"
_LOCALIZATION = LocalizationService(FileLocalizationAdapter())


def en(message: UIMessage, **fields: str) -> str:
    """The English notice the owner gets."""
    return _LOCALIZATION.get_ui_string(LanguageCode.EN, message).format(**fields)


class Clock:
    def __init__(self) -> None:
        self.now = T0

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **delta: float) -> None:
        self.now += timedelta(**delta)


def _counter(prefix: str):
    n = iter(range(1, 1000))
    return lambda: f"{prefix}-{next(n)}"


class PinLockWorld:

    def __init__(self, notifications: Optional[object] = None) -> None:
        self.clock = Clock()
        self.pins = FakeFactsPinRepository()
        self.pins.add_user("user-alex")
        self.pins.add_user("user-other")
        self.devices = FakeDeviceSessionRepository(facts_pins=self.pins)
        self.db = EvalDb()
        self.mcp = FirestoreMCPClientRepository(self.db, SimpleNamespace(
            mcp_oauth_clients_collection="pin_world_mcp_clients", mcp_auth_codes_collection="pin_world_mcp_codes",
            mcp_refresh_tokens_collection=REFRESH), cipher=AesGcmTestCipher(), digest_key=KEY)
        self.state = FakeNotificationState(T0)
        self.channels = RecordingChannelFactory()
        self.notifier = notifications or UserNotificationService(
            self.state, self.channels, coordinator=None, notification_sla={}, localization=_LOCALIZATION)
        self.device_sessions = DeviceSessionService(
            self.devices, SessionService(secret_key=SECRET), self.notifier, clock=self.clock,
            new_sid=_counter("sid"), new_secret=_counter("secret"),
            revoke_connector_grants=self.mcp.revoke_user_refresh_tokens)
        self.lock = AccountLockService(self.device_sessions, self.notifier)
        self.service = FactsPinService(self.pins, self.devices, self.lock, self.notifier, KEY, clock=self.clock)

    async def seed(self) -> None:
        """alex's two channels, one live session per user, alex's passkey and MCP grant."""
        await self.state.save_primary("user-alex", "slack", "C-alex")
        await self.state.save("user-alex", "telegram", "chat-alex")
        await self.add_session("sid-alex")
        await self.add_session("sid-other", user_id="user-other", hint=MAC)
        await self.devices.add_passkey(Passkey(
            cred_hash="key-alex", credential_id=b"cred-alex", user_id="user-alex", account_id="acct-alex",
            kind=PasskeyKind.WEBAUTHN, public_key=b"cose", algorithm=-7, sign_count=0, label=PHONE,
            platform_hint=PHONE, backup_eligible=True, created_at=T0))
        for user_id in ("user-alex", "user-other"):
            await self.mcp.save_refresh_token(MCPRefreshToken(
                token_hash=f"rt-{user_id}", client_id="cli-a", user_id=user_id, account_id=f"acct-{user_id}",
                scopes=["user_context"], resource=None, expires_at=T0 + timedelta(days=30), revoked_at=None))

    async def add_session(self, sid: str, *, user_id: str = "user-alex", hint: str = PHONE,
                          auth_kind: SessionAuthKind = SessionAuthKind.GOOGLE,
                          auth_at: Optional[datetime] = None) -> DeviceSession:
        at = auth_at or self.clock.now
        session = DeviceSession(
            sid=sid, user_id=user_id, account_id=f"acct-{user_id.removeprefix('user-')}", label=hint,
            platform_hint=hint, auth_kind=auth_kind, auth_at=at, created_at=at, last_seen_at=at,
            expires_at=DeviceSessionPolicy.absolute_end(at), refresh_hash="h",
            purge_after=DeviceSessionPolicy.purge_after(at))
        await self.devices.create(session)
        return session

    async def set_pin(self, user_id: str = "user-alex") -> None:
        result = await self.service.set(user_id, PIN, DURESS)
        assert result.status.value == "done"

    def flag(self, user_id: str = "user-alex"):
        user = self.pins.users[user_id]
        return user["is_active"], user["inactive_reason"]

    def token_revoked(self, user_id: str) -> bool:
        return self.db.collection(REFRESH).docs[f"rt-{user_id}"]["revoked_at"] is not None

    @property
    def sent(self):
        """(platform, channel_id, text) of every notice posted, in order per channel."""
        return self.channels.sent_literal
