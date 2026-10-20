"""
Sandbox recycler (UNTRUSTED_CONTENT_SANDBOX_RFC §4.6; plan G7).

`PipeRecycler` is the object the server app calls (`SandboxRecycler.request_served`) once the one request of an
instance has been answered. It only signals: one byte on the pipe the supervisor gave the server. It runs from an
asyncio done-callback, so it never blocks, awaits or raises: the write end is non-blocking, a full or closed pipe is
logged, and a second call writes nothing. The supervisor does the stopping.

`fd=None` is a logged no-op (a `never` policy server run by hand has no supervisor).
"""
from __future__ import annotations

import os
from typing import Optional

from ..utils.logger import logger


class PipeRecycler:
    def __init__(self, fd: Optional[int]) -> None:
        self._fd = fd
        self._sent = False
        if fd is not None:
            os.set_blocking(fd, False)

    def request_served(self) -> None:
        if self._sent:
            return
        self._sent = True
        if self._fd is None:
            logger.warning("[Sandbox] request served but no supervisor pipe: nothing will recycle this instance")
            return
        try:
            os.write(self._fd, b"1")
        except OSError as exc:
            # BlockingIOError (pipe full: a byte is already pending) and BrokenPipeError (supervisor gone) both land here.
            logger.error("[Sandbox] recycle signal not delivered: %s", type(exc).__name__)
