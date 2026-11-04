"""
The one request slot of a sandbox instance (UNTRUSTED_CONTENT_SANDBOX_RFC §4.6: one request per instance).

`SandboxSlot` is shared by every route of one server process: the Quart blueprint (`sandbox_app`) and the raw ASGI
tunnel route (`sandbox_tunnel_app`) take the same object, so a parse request that reaches an instance busy with a media
tunnel answers 503 `recycling`, and the other way round.

- `try_acquire()`: False when the instance is busy or draining; else marks it busy.
- `release()`: frees it for the next request (a request that does not recycle the instance).
- `drain()`: the instance ends after this request; it never takes another one.

Every method is synchronous and awaits nothing, so a check and its set cannot interleave with another request on the
one event loop: the asyncio.Lock the shared-state rule asks for would guard nothing here.
"""
from __future__ import annotations


class SandboxSlot:
    def __init__(self) -> None:
        self._busy = False
        self._draining = False

    @property
    def busy(self) -> bool:
        return self._busy

    @property
    def draining(self) -> bool:
        return self._draining

    def try_acquire(self) -> bool:
        if self._busy or self._draining:
            return False
        self._busy = True
        return True

    def release(self) -> None:
        self._busy = False

    def drain(self) -> None:
        self._draining = True
