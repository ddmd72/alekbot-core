"""FakeDeviceSessionRepository — the in-memory twin of `DeviceSessionRepository` (CABINET_DEVICE_SESSIONS_RFC §7.1).

A fake with semantics, not a mock: it applies the port's rules (create never overwrites, the refresh decision is
`DeviceSessionPolicy.decide_refresh` on the stored record and its write lands atomically under a lock, the first
revocation is kept, the order the port promises). The store kit (`tests/unit/adapters/test_device_session_store_kit.py`)
runs the port's rules on both this and the Firestore adapter so the two cannot drift. `sessions` is the stored records
by sid, for assertions.
"""
import asyncio
from datetime import datetime
from typing import Dict, List, Optional, Tuple

from src.domain.device_session import DeviceSession, SessionRevokeReason
from src.domain.device_session_policy import DeviceSessionPolicy, RefreshOutcome
from src.ports.device_session_repository import DeviceSessionRepository


class FakeDeviceSessionRepository(DeviceSessionRepository):

    def __init__(self) -> None:
        self.sessions: Dict[str, DeviceSession] = {}
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

    async def revoke_all(self, user_id: str, reason: SessionRevokeReason, now: datetime) -> int:
        async with self._lock:
            live = [s for s in self.sessions.values() if s.user_id == user_id and s.revoked_at is None]
            for session in live:
                self.sessions[session.sid] = session.model_copy(update={"revoked_at": now, "revoke_reason": reason})
            return len(live)
