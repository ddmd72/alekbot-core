"""
EphemeralStore port — shared, short-TTL key/value store.

Backs both the voice call ticket and the RFC §3 one-call-per-user marker
(RFC §4.5, §3): same store, same TTL semantics, different keys/collections.
"""
from abc import ABC, abstractmethod
from typing import Any, Dict, Optional


class EphemeralStore(ABC):
    """A shared short-TTL key/value store for one voice call (VOICE_COMPANION_RFC §3, §4.5, §12): the ticket, the
    one-call-per-user marker, the parked callback, the call kind, the AMD verdict, the web-call record and the
    late-delegation result and abandon marks.

    Holds for every method:
    - Tenancy: none. A key is global; callers namespace keys (`voice_one_call:{user_id}`) and check a stored `user_id`
      themselves where it matters (`voice_web_call_app._owned`).
    - Expiry: a value expires ttl_s seconds after its set; it is live up to and including that instant and expired once
      now is past it. An expired value reads as absent and is removed lazily.
    - Consistency: a completed write is seen by every later read, from any instance. The late-answer handshake
      (result first, abandon check second, and the reverse) relies on this.
    - Legacy: an entry stored without an expiry raises KeyError on every read and is kept; one without a value that has
      expired reads as absent (no writer produces either).
    - Errors: backend errors propagate; nothing is swallowed.
    """

    @abstractmethod
    async def set(self, key: str, value: Dict[str, Any], ttl_s: int) -> None:
        """Store `value` under `key` for `ttl_s` seconds, replacing any value (live or expired) and its expiry.

        Atomicity: one unconditional write; the last writer wins. It is not a claim: a caller's get-then-set is not
        atomic, so two callers can both find a key absent and both write it.
        Idempotency: a repeat rewrites the value and restarts the TTL.
        """

    @abstractmethod
    async def get(self, key: str) -> Optional[Dict[str, Any]]:
        """The live value of `key`, or None when it is absent or expired.

        Expired: the entry is also removed, unconditionally (a value set by another caller in between can be removed
        with it).
        """

    @abstractmethod
    async def delete(self, key: str) -> None:
        """Remove `key`. Idempotency: deleting an absent key is a no-op, never an error."""

    @abstractmethod
    async def get_and_delete(self, key: str) -> Optional[Dict[str, Any]]:
        """Read and remove `key` atomically. Of concurrent callers on one key, exactly one gets the live value
        and every other gets None. This backs the single-use ticket, the parked callback (Twilio retries) and the late
        delegation answer (posted exactly once); a plain get()-then-delete() would be a TOCTOU race between callers.

        Expired: the entry is removed and None is returned. Absent: None, and nothing is written.
        Errors: backend errors propagate; contention that never resolves raises ValueError (the SDK's).
        """
