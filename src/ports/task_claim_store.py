"""
TaskClaimStore port — a lease on one Cloud Tasks task, held by one delivery attempt.

Unlike a plain DedupStore mark, the claim knows its holder (process) and attempt, so a retry
after a crashed attempt takes it over instead of being skipped as a duplicate.
See decisions/worker_task_dedup.md.
"""
from abc import ABC, abstractmethod


class TaskClaimStore(ABC):
    @abstractmethod
    async def try_claim(self, key: str, attempt: str) -> bool:
        """True when this attempt now holds the claim, False when it is a true duplicate.

        The caller guarantees no other attempt of this key is live in its own process.
        Duplicate: the task is done. Taken over: an expired claim, or any running claim
        (its holder is another process or a dead attempt). The same ``attempt`` finding its own claim
        (SDK retry of a landed write) is True. Store errors raise.
        """

    @abstractmethod
    async def mark_done(self, key: str, attempt: str) -> None:
        """Mark the task finished, only if ``attempt`` still holds the claim."""

    @abstractmethod
    async def release(self, key: str, attempt: str) -> None:
        """Delete the claim so a retry runs, only if ``attempt`` still holds it."""
