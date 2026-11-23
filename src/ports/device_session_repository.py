"""DeviceSessionRepository — the store of Cabinet device sessions (CABINET_DEVICE_SESSIONS_RFC §7.1, §7.6).

A session is addressed by its random `sid`; the only query is a single equality on `user_id`. Every write that
depends on what is stored is one transaction: the refresh decision and its write (so two refreshes of one sid cannot
both rotate), a revoke, and `revoke_all` over every session of a user (so a sign-in or refresh racing the kill switch
either sees it or retries after it).
"""
from abc import ABC, abstractmethod
from datetime import datetime
from typing import List, Optional, Tuple

from src.domain.device_session import DeviceSession, SessionRevokeReason
from src.domain.device_session_policy import RefreshOutcome


class DeviceSessionRepository(ABC):

    @abstractmethod
    async def create(self, session: DeviceSession) -> bool:
        """Store a new session. False, and nothing written, when its sid is taken (never overwrites)."""

    @abstractmethod
    async def get(self, sid: str) -> Optional[DeviceSession]:
        """The stored session, revoked or not; None when absent."""

    @abstractmethod
    async def list_for_user(self, user_id: str) -> List[DeviceSession]:
        """Every stored session of the user, revoked and ended ones included, oldest `created_at` first (then sid)."""

    @abstractmethod
    async def refresh(self, sid: str, presented_hash: str, new_hash: str,
                      now: datetime) -> Tuple[RefreshOutcome, Optional[DeviceSession]]:
        """Decide the refresh with `DeviceSessionPolicy.decide_refresh` on the stored record and apply its write, in one
        transaction. `ROTATE`: `prev_refresh_hash` ← the current hash, `refresh_hash` ← `new_hash`, `rotated_at`,
        `last_seen_at` ← `now`, `purge_after` from `now`. `GRACE`: `last_seen_at` ← `now` only. `REPLAY`: revoked at
        `now` with reason `replay`. `DEAD`: nothing written. Returns the outcome and the record after the write
        (`(DEAD, None)` for an unknown sid)."""

    @abstractmethod
    async def revoke(self, sid: str, reason: SessionRevokeReason, now: datetime) -> bool:
        """Revoke the session at `now` with `reason`, in one transaction. False when absent or already revoked (the
        first revocation's time and reason are kept)."""

    @abstractmethod
    async def revoke_all(self, user_id: str, reason: SessionRevokeReason, now: datetime) -> int:
        """Revoke every not yet revoked session of the user, in one transaction. Returns how many it revoked."""
