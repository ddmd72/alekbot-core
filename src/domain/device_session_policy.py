"""DeviceSessionPolicy — the lifetimes of a device session and the refresh decision (CABINET_DEVICE_SESSIONS_RFC §7.1,
§7.2). Pure: every instant is passed in.

Why these numbers (the ASVS V7.1.1 record, RFC §7.2): one user on their own locked devices, every session revocable
server-side within `GATE_CACHE_TTL` and announced when it starts; NIST's 1 h idle / 24 h overall would cost a Google
sign-in per entry point per day, the defect the RFC fixes. Every end is exclusive: at exactly the idle end, the
absolute end or the grace end the later state already holds.

The refresh decision (RFC §7.1 "Refresh"): the presented secret's hash equals the current one → `ROTATE` (a new secret
is issued); it equals the previous one within `ROTATION_GRACE` of the rotation → `GRACE` (two tabs refreshed at once:
an access token only, no new secret, so both end with the newer cookie); the previous one after the grace, or neither
→ `REPLAY` (an old secret came back: the session is revoked); a revoked, idle-ended or absolute-ended session →
`DEAD`, whatever was presented.

A voice call start asks for a passkey assertion not older than `CALL_PASSKEY_GRACE` when the user has a passkey (RFC
§7.3, §13 decision 1): about a second of Face ID guards an unlocked phone in another person's hands, and the grace lets
a redial after a dropped call through without asking again. `passkey_fresh` is that rule on the session's `passkey_at`.

This module holds `RefreshOutcome` and the secret hash next to the policy on purpose: they are the policy's vocabulary.
"""
import hashlib
import hmac
from datetime import datetime, timedelta
from enum import Enum
from typing import ClassVar, Optional

from .device_session import DeviceSession


class RefreshOutcome(str, Enum):
    ROTATE = "rotate"
    GRACE = "grace"
    REPLAY = "replay"
    DEAD = "dead"


def refresh_secret_hash(secret: str) -> str:
    """The stored form of a refresh secret: SHA-256 hex of its text. The secret is 32 random bytes in URL-safe text,
    so an unsalted hash is enough (nothing to guess); the record never holds the secret itself."""
    return hashlib.sha256(secret.encode("utf-8")).hexdigest()


def _same(stored: Optional[str], presented: str) -> bool:
    return stored is not None and hmac.compare_digest(stored, presented)


class DeviceSessionPolicy:
    ACCESS_TTL: ClassVar[timedelta] = timedelta(minutes=15)       # the access JWT, refreshed silently
    IDLE_TTL: ClassVar[timedelta] = timedelta(days=14)            # no refresh for this long ends the session
    ABSOLUTE_TTL: ClassVar[timedelta] = timedelta(days=30)        # the session ends whatever its activity
    ROTATION_GRACE: ClassVar[timedelta] = timedelta(seconds=60)   # two tabs refreshing at once
    GATE_CACHE_TTL: ClassVar[timedelta] = timedelta(seconds=30)   # the revocation latency bound of the gate
    PURGE_AFTER: ClassVar[timedelta] = timedelta(days=30)         # the record's TTL after its last use
    CALL_PASSKEY_GRACE: ClassVar[timedelta] = timedelta(minutes=5)  # a call start's passkey assertion stays fresh

    @classmethod
    def access_expiry(cls, now: datetime) -> datetime:
        """When an access token minted at `now` expires."""
        return now + cls.ACCESS_TTL

    @classmethod
    def absolute_end(cls, created_at: datetime) -> datetime:
        """`expires_at` of a session created at `created_at`."""
        return created_at + cls.ABSOLUTE_TTL

    @classmethod
    def purge_after(cls, last_seen_at: datetime) -> datetime:
        """`purge_after` of a session last used at `last_seen_at`."""
        return last_seen_at + cls.PURGE_AFTER

    @classmethod
    def is_live(cls, session: DeviceSession, now: datetime) -> bool:
        """Not revoked, and before both its idle end and its absolute end."""
        return (session.revoked_at is None
                and now < session.last_seen_at + cls.IDLE_TTL
                and now < session.expires_at)

    @classmethod
    def passkey_fresh(cls, session: DeviceSession, now: datetime) -> bool:
        """Whether a passkey assertion proved `session` less than `CALL_PASSKEY_GRACE` before `now`."""
        return session.passkey_at is not None and now < session.passkey_at + cls.CALL_PASSKEY_GRACE

    @classmethod
    def decide_refresh(cls, session: DeviceSession, presented_hash: str, now: datetime) -> RefreshOutcome:
        """What a refresh presenting `presented_hash` (the hash of the cookie's secret) at `now` does to `session`."""
        if not cls.is_live(session, now):
            return RefreshOutcome.DEAD
        if _same(session.refresh_hash, presented_hash):
            return RefreshOutcome.ROTATE
        if (_same(session.prev_refresh_hash, presented_hash) and session.rotated_at is not None
                and now < session.rotated_at + cls.ROTATION_GRACE):
            return RefreshOutcome.GRACE
        return RefreshOutcome.REPLAY
