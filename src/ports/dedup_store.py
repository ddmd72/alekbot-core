"""
DedupStore port — deduplication store for platform events.

Prevents duplicate processing of platform events (Slack, Telegram, etc.)
by tracking processed event/update IDs with a TTL.
"""
from abc import ABC, abstractmethod
from typing import Optional


class DedupStore(ABC):
    """A mark that an event was taken, for the writer's TTL: daily-review starts (48 h), OpenAI deep-research webhook
    jobs (72 h) and Telegram inline updates (1 h). Liveness follows each mark's own expiry, so writers with different
    TTLs can share one store.

    Holds for every method:
    - Tenancy: none; keys are global and namespaced by callers (`daily_review:{user}:{date}`, `openai_deep_research:{job}`,
      `telegram::{update_id}`).
    - Legacy: a mark without an expiry is live for the reader's TTL from its creation time; a mark with neither is
      expired.
    - Errors: backend errors propagate.
    """

    @abstractmethod
    async def is_duplicate(self, event_id: Optional[str]) -> bool:
        """True when a live mark exists. A plain read, no claim. (Observed contract; no caller in src/.)"""

    @abstractmethod
    async def try_mark_processed(self, event_id: str) -> bool:
        """Atomically claim the event.

        True only when this caller now holds a live mark; False when a live mark exists.
        Atomicity: of concurrent claimers of a missing or an expired mark, exactly one gets True.
        Idempotency: a repeat by the same caller is False (a mark has no holder).
        Errors other than "already marked" raise, and nothing is written.
        Residual: the SDK retries a commit on ServiceUnavailable/ResourceExhausted; if the first write landed but its
        response was lost, the retry sees our own mark and returns False (a false duplicate). Fixing that needs a claim
        token (as TaskClaimStore has).
        """

    @abstractmethod
    async def release(self, event_id: str) -> None:
        """Delete the mark so the event can be taken again (its attempt failed). No holder check: callers release only
        right after their own failed use, far inside the TTL. Idempotency: an absent mark is a no-op."""
