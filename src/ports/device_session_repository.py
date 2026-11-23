"""DeviceSessionRepository — the store of Cabinet device sessions and passkeys (CABINET_DEVICE_SESSIONS_RFC §7.1, §7.3,
§7.6).

A session is addressed by its random `sid`, a passkey by its `cred_hash`; the only query is a single equality on
`user_id`. Every write that depends on what is stored is one transaction: the refresh decision and its write (so two
refreshes of one sid cannot both rotate), a revoke, a passkey's counter advance (so two assertions with one counter
cannot both pass), and `revoke_all` over every session and passkey of a user (so a sign-in or refresh racing the kill
switch either sees it or retries after it). Sessions and passkeys share one port because `revoke_all` must cover both
in one transaction.
"""
from abc import ABC, abstractmethod
from datetime import datetime
from typing import List, Optional, Tuple

from src.domain.device_session import DeviceSession, SessionRevokeReason
from src.domain.device_session_policy import RefreshOutcome
from src.domain.passkey import Passkey


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
        """Revoke every not yet revoked session and passkey of the user, in one transaction (passkeys at `now`, with no
        reason: a passkey record has none). Returns how many sessions it revoked."""

    # ------------------------------------------------------------------ passkeys (§7.3)

    @abstractmethod
    async def add_passkey(self, passkey: Passkey) -> bool:
        """Store a new passkey. False, and nothing written, when its cred_hash is taken (never overwrites)."""

    @abstractmethod
    async def get_passkey(self, cred_hash: str) -> Optional[Passkey]:
        """The stored passkey, revoked or not; None when absent."""

    @abstractmethod
    async def list_passkeys(self, user_id: str) -> List[Passkey]:
        """Every stored passkey of the user, revoked ones included, oldest `created_at` first (then cred_hash)."""

    @abstractmethod
    async def touch_passkey(self, cred_hash: str, sign_count: int, last_used_at: datetime) -> bool:
        """Record a sign-in with the passkey, in one transaction: `sign_count` and `last_used_at` replace the stored
        ones. False, and nothing written, when it is absent or revoked, or when `sign_count` does not advance the stored
        counter (`sign_count_advances`)."""

    @abstractmethod
    async def revoke_passkey(self, cred_hash: str, now: datetime) -> bool:
        """Revoke the passkey at `now`, in one transaction. False when absent or already revoked (the first revocation's
        time is kept)."""
