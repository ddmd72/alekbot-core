"""FakeDeviceSessionRepository — the in-memory twin of `DeviceSessionRepository` (CABINET_DEVICE_SESSIONS_RFC §7.1,
§7.3).

A fake with semantics, not a mock: it applies the port's rules (create and add never overwrite, the refresh decision is
`DeviceSessionPolicy.decide_refresh` on the stored record and its write lands atomically under a lock, a passkey's
counter must advance (`sign_count_advances`), the first revocation is kept, `revoke_all` covers sessions and passkeys,
the order the port promises; with `facts_pins` linked, `revoke_all` also cancels that user's pending facts PIN reset;
`set_facts_unlock` writes only on a live session). The store kit (`tests/unit/adapters/test_device_session_store_kit.py`) runs the port's
rules on both this and the Firestore adapter so the two cannot drift. `sessions` is the stored records by sid and
`passkeys` by cred_hash, for assertions.
"""
import asyncio
from datetime import datetime
from typing import TYPE_CHECKING, Dict, List, Optional, Tuple

from src.domain.device_session import DeviceSession, SessionRevokeReason
from src.domain.device_session_policy import DeviceSessionPolicy, RefreshOutcome
from src.domain.passkey import Passkey, sign_count_advances
from src.ports.device_session_repository import DeviceSessionRepository

if TYPE_CHECKING:
    from tests.support.facts_pin_repo import FakeFactsPinRepository


class FakeDeviceSessionRepository(DeviceSessionRepository):

    def __init__(self, facts_pins: Optional["FakeFactsPinRepository"] = None) -> None:
        self.sessions: Dict[str, DeviceSession] = {}
        self.passkeys: Dict[str, Passkey] = {}
        self.facts_pins = facts_pins
        self._lock = asyncio.Lock()

    async def create(self, session: DeviceSession) -> bool:
        async with self._lock:
            if session.sid in self.sessions:
                return False
            self.sessions[session.sid] = session
            return True

    async def get(self, sid: str) -> Optional[DeviceSession]:
        return self.sessions.get(sid)

    async def list_for_user(self, user_id: str) -> List[DeviceSession]:
        return sorted((s for s in self.sessions.values() if s.user_id == user_id), key=lambda s: (s.created_at, s.sid))

    async def refresh(self, sid: str, presented_hash: str, new_hash: str,
                      now: datetime) -> Tuple[RefreshOutcome, Optional[DeviceSession]]:
        async with self._lock:
            session = self.sessions.get(sid)
            if session is None:
                return RefreshOutcome.DEAD, None
            outcome = DeviceSessionPolicy.decide_refresh(session, presented_hash, now)
            if outcome is RefreshOutcome.ROTATE:
                session = session.model_copy(update={
                    "prev_refresh_hash": session.refresh_hash, "refresh_hash": new_hash, "rotated_at": now,
                    "last_seen_at": now, "purge_after": DeviceSessionPolicy.purge_after(now)})
            elif outcome is RefreshOutcome.GRACE:
                session = session.model_copy(update={"last_seen_at": now})
            elif outcome is RefreshOutcome.REPLAY:
                session = session.model_copy(update={"revoked_at": now, "revoke_reason": SessionRevokeReason.REPLAY})
            self.sessions[sid] = session
            return outcome, session

    async def revoke(self, sid: str, reason: SessionRevokeReason, now: datetime) -> bool:
        async with self._lock:
            session = self.sessions.get(sid)
            if session is None or session.revoked_at is not None:
                return False
            self.sessions[sid] = session.model_copy(update={"revoked_at": now, "revoke_reason": reason})
            return True

    async def record_passkey_assertion(self, sid: str, user_id: str, at: datetime) -> bool:
        async with self._lock:
            session = self.sessions.get(sid)
            if session is None or session.user_id != user_id or not DeviceSessionPolicy.is_live(session, at):
                return False
            self.sessions[sid] = session.model_copy(update={"passkey_at": at})
            return True

    async def revoke_all(self, user_id: str, reason: SessionRevokeReason, now: datetime) -> int:
        async with self._lock:
            live = [s for s in self.sessions.values() if s.user_id == user_id and s.revoked_at is None]
            for session in live:
                self.sessions[session.sid] = session.model_copy(update={"revoked_at": now, "revoke_reason": reason})
            for key in [p for p in self.passkeys.values() if p.user_id == user_id and p.revoked_at is None]:
                self.passkeys[key.cred_hash] = key.model_copy(update={"revoked_at": now})
            if self.facts_pins is not None:
                self.facts_pins.cancel_pending_reset(user_id, now)
            return len(live)

    async def set_facts_unlock(self, sid: str, until: Optional[datetime], now: datetime) -> bool:
        async with self._lock:
            session = self.sessions.get(sid)
            if session is None or not DeviceSessionPolicy.is_live(session, now):
                return False
            self.sessions[sid] = session.model_copy(update={"facts_unlocked_until": until})
            return True

    async def add_passkey(self, passkey: Passkey) -> bool:
        async with self._lock:
            if passkey.cred_hash in self.passkeys:
                return False
            self.passkeys[passkey.cred_hash] = passkey
            return True

    async def get_passkey(self, cred_hash: str) -> Optional[Passkey]:
        return self.passkeys.get(cred_hash)

    async def list_passkeys(self, user_id: str) -> List[Passkey]:
        return sorted((p for p in self.passkeys.values() if p.user_id == user_id),
                      key=lambda p: (p.created_at, p.cred_hash))

    async def touch_passkey(self, cred_hash: str, sign_count: int, last_used_at: datetime) -> bool:
        async with self._lock:
            key = self.passkeys.get(cred_hash)
            if key is None or key.revoked_at is not None or not sign_count_advances(key.sign_count, sign_count):
                return False
            self.passkeys[cred_hash] = key.model_copy(update={"sign_count": sign_count, "last_used_at": last_used_at})
            return True

    async def revoke_passkey(self, cred_hash: str, now: datetime) -> bool:
        async with self._lock:
            key = self.passkeys.get(cred_hash)
            if key is None or key.revoked_at is not None:
                return False
            self.passkeys[cred_hash] = key.model_copy(update={"revoked_at": now})
            return True
