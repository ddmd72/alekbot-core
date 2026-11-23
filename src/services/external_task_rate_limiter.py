"""ExternalTaskRateLimiter: the worker REST endpoint's fixed-window limits (EXTERNAL_TASK_QUEUE_RFC §5.6, §5.11).

Public interface:
    ExternalTaskRateLimiter(counter, *, clock)
        .unauthorized_retry_after(address) -> Optional[int]     memory only: seconds to wait, None = open
        .count_unauthorized(address) -> Optional[int]            one `401` answer: counted per address and globally,
                                                                 in memory only (an anonymous call writes nothing)
        .key_retry_after(key_digest, scopes) -> Optional[int]    memory only: a key whose limit tripped this window
        .hit(scope, worker_key, limit, key_digest) -> Optional[int]  one authorised call of a worker; seconds to wait or None

Per-worker counts live in a store every instance sees (`WindowCounter`). `401` counts never touch it: they are fixed
windows in this instance's memory, bounded in size, so a flood of keyless calls writes nothing. Each instance also remembers the windows it saw
exhausted, behind an `asyncio.Lock`: once a key is over its limit the rest of the window is answered from the memo, so a
flood costs one counter write per instance per window and, for `401`s, never reads the key store. A window's count
only grows, so a memoised refusal is never wrong within its window; what the memo cannot see is another instance's
refusal, which that instance's own first over-limit hit finds in the shared count.

Over the limit means the (limit + 1)-th hit of a window. A counter failure on an authorised call propagates.
"""
import asyncio
from datetime import datetime
from typing import Callable, Dict, Iterable, Optional, Tuple

from ..domain.external_task import (
    RATE_WINDOW_S,
    UNAUTHORIZED_GLOBAL_PER_WINDOW,
    UNAUTHORIZED_PER_ADDRESS_PER_WINDOW,
)
from ..ports.window_counter import WindowCounter
from ..utils.logger import logger

GLOBAL_KEY = "401:global"
MAX_TRACKED_UNAUTHORIZED = 10_000    # addresses remembered at once; the oldest are evicted above this


def address_key(address: str) -> str:
    return f"401:addr:{address}"


class ExternalTaskRateLimiter:
    def __init__(self, counter: WindowCounter, *, clock: Callable[[], datetime]) -> None:
        self._counter = counter
        self._clock = clock
        self._exhausted: Dict[str, int] = {}    # key -> epoch second its exhausted window ends
        self._unauthorized: Dict[str, Tuple[int, int]] = {}    # `401` key -> (epoch second its window ends, count)
        self._lock = asyncio.Lock()

    def _window_end(self, now: datetime) -> int:
        return (int(now.timestamp()) // RATE_WINDOW_S + 1) * RATE_WINDOW_S

    async def _remembered(self, key: str, now: datetime) -> Optional[int]:
        async with self._lock:
            end = self._exhausted.get(key)
            if end is None:
                return None
            if end <= now.timestamp():
                del self._exhausted[key]
                return None
            return max(1, int(end - now.timestamp()))

    async def _remember(self, key: str, now: datetime) -> int:
        end = self._window_end(now)
        async with self._lock:
            self._exhausted = {k: e for k, e in self._exhausted.items() if e > now.timestamp()}
            self._exhausted.pop(key, None)
            self._exhausted[key] = end
            while len(self._exhausted) > MAX_TRACKED_UNAUTHORIZED:     # oldest first, as the `401` memo is bounded
                del self._exhausted[next(iter(self._exhausted))]
        return max(1, int(end - now.timestamp()))

    async def _count(self, key: str, limit: int) -> Optional[int]:
        """None while the window's count is within `limit`; else the seconds until the window ends."""
        now = self._clock()
        remembered = await self._remembered(key, now)
        if remembered is not None:
            return remembered
        if await self._counter.increment(key, RATE_WINDOW_S, now) > limit:
            return await self._remember(key, now)
        return None

    async def unauthorized_retry_after(self, address: str) -> Optional[int]:
        """Whether this address, or every address together, is already over its `401` limit; memory only."""
        now = self._clock().timestamp()
        async with self._lock:
            waits = [self._over(key, limit, now) for key, limit in self._unauthorized_keys(address)]
        live = [w for w in waits if w is not None]
        return max(live) if live else None

    def _over(self, key: str, limit: int, now: float) -> Optional[int]:
        entry = self._unauthorized.get(key)
        if entry is None or entry[0] <= now or entry[1] <= limit:
            return None
        return max(1, int(entry[0] - now))

    @staticmethod
    def _unauthorized_keys(address: str):
        return ((address_key(address), UNAUTHORIZED_PER_ADDRESS_PER_WINDOW),
                (GLOBAL_KEY, UNAUTHORIZED_GLOBAL_PER_WINDOW))

    async def count_unauthorized(self, address: str) -> Optional[int]:
        """Count one `401` answer under the address and globally, in this instance's memory only: an anonymous request
        must write nothing, or a flood of keyless calls would be a flood of store writes. Seconds to wait when either
        is now over its limit. The limits are per instance, which still bounds each instance's key-store reads."""
        now_dt = self._clock()
        now = now_dt.timestamp()
        end = self._window_end(now_dt)
        waits = []
        async with self._lock:
            for key, limit in self._unauthorized_keys(address):
                entry = self._unauthorized.get(key)
                count = entry[1] + 1 if entry is not None and entry[0] > now else 1
                self._unauthorized.pop(key, None)
                self._unauthorized[key] = (end, count)      # re-inserted last: insertion order is age order
                if count > limit:
                    waits.append(max(1, int(end - now)))
            self._trim_unauthorized(now)
        return max(waits) if waits else None

    def _trim_unauthorized(self, now: float) -> None:
        """Bounded: expired windows go first, then the oldest, so an address flood cannot grow memory without limit."""
        if len(self._unauthorized) <= MAX_TRACKED_UNAUTHORIZED:
            return
        self._unauthorized = {k: v for k, v in self._unauthorized.items() if v[0] > now}
        while len(self._unauthorized) > MAX_TRACKED_UNAUTHORIZED:
            del self._unauthorized[next(iter(self._unauthorized))]

    async def key_retry_after(self, key_digest: str, scopes: Iterable[str]) -> Optional[int]:
        """Seconds to wait when a limit this key tripped in the current window covers one of `scopes`; memory only, so
        the request is refused before any key, worker or user read. It only refuses: a key that is not remembered, or
        whose window ended, is looked up as usual, so a revocation is never delayed by it."""
        now = self._clock()
        waits = [w for w in [await self._remembered(f"key:{scope}:{key_digest}", now) for scope in scopes]
                 if w is not None]
        return max(waits) if waits else None

    async def hit(self, scope: str, worker_key: str, limit: int, key_digest: Optional[str] = None) -> Optional[int]:
        """Count one authorised call of one worker under `scope` ('calls', 'claims', 'submits'); seconds to wait when
        over `limit`. `worker_key` names the worker (user and worker id); `key_digest` (the stored digest of the key
        the call came with, never the key) is remembered until the window ends when the limit trips."""
        try:
            wait = await self._count(f"{scope}:{worker_key}", limit)
        except Exception as exc:
            logger.error(f"[TaskAPI] {scope} counter failed: {type(exc).__name__}")
            raise
        if wait is not None and key_digest is not None:
            await self._remember(f"key:{scope}:{key_digest}", self._clock())
        return wait
