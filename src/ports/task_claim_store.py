"""
TaskClaimStore port — a lease on one Cloud Tasks task, held by one delivery attempt.

Unlike a plain DedupStore mark, the claim knows its holder (process) and attempt, so a retry
after a crashed attempt takes it over instead of being skipped as a duplicate.
See decisions/worker_task_dedup.md.
"""
from abc import ABC, abstractmethod


class TaskClaimStore(ABC):
    """A lease on one Cloud Tasks task (key = the task name, `[A-Za-z0-9_-]`, never containing `:`), held by one
    delivery attempt. Its key space is disjoint from the DedupStore marks'.

    - Expiry: a claim is expired when now >= its expiry (a missing or non-datetime expiry counts as expired).
    - Tenancy: none (task names are global).
    - Legacy: a claim without `state`, or with an unknown one, is treated as running (taken over).
    """

    @abstractmethod
    async def try_claim(self, key: str, attempt: str) -> bool:
        """True when this attempt now holds the claim, False when it is a true duplicate.

        The caller guarantees no other attempt of this key is live in its own process.
        Duplicate: the task is done. Taken over: an expired claim, or any running claim
        (its holder is another process or a dead attempt). The same ``attempt`` finding its own claim
        (SDK retry of a landed write) is True. Store errors raise.
        Atomicity: of concurrent takers exactly one gets True.
        """

    @abstractmethod
    async def mark_done(self, key: str, attempt: str) -> None:
        """Mark the task finished, only if ``attempt`` still holds the claim.

        Restarts the expiry: a done claim is a duplicate for ttl seconds from the finish. Lost to a takeover:
        changes nothing, no error.
        """

    @abstractmethod
    async def release(self, key: str, attempt: str) -> None:
        """Delete the claim so a retry runs, only if ``attempt`` still holds it.

        Lost to a takeover: changes nothing, no error. Other errors raise.
        """
