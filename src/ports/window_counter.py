"""WindowCounter port: a count shared by every instance, per key and fixed time window (EXTERNAL_TASK_QUEUE_RFC §5.11).

Implemented by FirestoreWindowCounter; fakes are the evaluating Firestore fake under the same adapter.
"""
from abc import ABC, abstractmethod
from datetime import datetime


class WindowCounter(ABC):
    """Holds for every method:
    - Windows: fixed and aligned to the epoch (`window_start = now // window_s * window_s`); a new window starts at 0.
    - Tenancy: none; keys are global and namespaced by the caller, and never contain `/`.
    - Atomicity: concurrent hits from any instance each get a distinct count.
    - Retention: a window's record is deleted by the store some time after the window ends; no caller reads it then.
    - Errors: backend errors propagate.
    """

    @abstractmethod
    async def increment(self, key: str, window_s: int, now: datetime) -> int:
        """Count one hit of `key` in the window holding `now` (timezone-aware); the count after this hit (first hit: 1)."""
