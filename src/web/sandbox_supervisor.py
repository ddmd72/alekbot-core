"""
Sandbox supervisor (UNTRUSTED_CONTENT_SANDBOX_RFC §4.6, §4.12; plan G4, G7).

`SandboxSupervisor` runs the server child and decides when the instance ends. It handles no payload and imports no
engine, parser or web framework: stdlib and the logger only. Spawning, waiting and signalling are injected so the
policy is unit-testable without processes.

Policy (`always` | `never`; anything else, including the RFC's historical `abnormal`, refuses to start, G4):
    always  the server writes one byte to the pipe after its one response is out. The supervisor then sends SIGTERM
            to the server (Hypercorn finishes the in-flight response and stops), waits, kills what is left of the
            server's process group, and returns 0 so the platform replaces the instance.
    never   local development only: the server stays up until the supervisor itself is told to stop.
A server that exits on its own is a failure (non-zero), never a quiet end. `terminate()` (SIGTERM/SIGINT to the
supervisor) is forwarded to the server. If the server ignores SIGTERM for `kill_grace_s` it gets SIGKILL.

Reaping: leftovers of an engine child reparent to the container's PID 1. When the supervisor is PID 1 (`is_init`) it
collects every exited child with `waitpid(-1)` and keeps the server's own status from that same stream; otherwise it
waits for the server pid only.
"""
from __future__ import annotations

import os
import select
import signal
import time
from typing import Callable, Optional, Tuple

from ..utils.logger import logger

POLICIES = ("always", "never")


class SandboxSupervisor:
    def __init__(
        self,
        policy: str,
        spawn: Callable[[int], int],
        *,
        waitpid: Callable[[int, int], Tuple[int, int]] = os.waitpid,
        kill: Callable[[int, int], None] = os.kill,
        killpg: Callable[[int, int], None] = os.killpg,
        is_init: Optional[bool] = None,
        poll_s: float = 0.1,
        kill_grace_s: float = 15.0,
    ) -> None:
        if policy not in POLICIES:
            logger.error("[Sandbox] refusing to start: unusable recycle policy")
            raise ValueError(f"recycle policy must be one of {POLICIES}")
        self._policy = policy
        self._spawn = spawn
        self._waitpid = waitpid
        self._kill = kill
        self._killpg = killpg
        self._is_init = (os.getpid() == 1) if is_init is None else is_init
        self._poll_s = poll_s
        self._kill_grace_s = kill_grace_s
        self._terminate = False
        self._pid = 0
        self._status: Optional[int] = None    # the server's raw wait status once collected

    def terminate(self) -> None:
        """Ask the supervisor to stop its server and exit. Safe from a signal handler or another thread."""
        self._terminate = True

    def run(self) -> int:
        read_fd, write_fd = os.pipe()
        try:
            try:
                self._pid = self._spawn(write_fd)
            finally:
                os.close(write_fd)     # the child holds its own copy; ours would hide the child's death from the pipe
            logger.info("[Sandbox] server started (policy=%s)", self._policy)
            return self._supervise(read_fd)
        finally:
            os.close(read_fd)

    # -- internals ---------------------------------------------------------------------------------------------

    def _supervise(self, read_fd: int) -> int:
        pipe_open = True
        stopping = False
        while True:
            if self._reap():
                break
            served = False
            if pipe_open:
                ready, _, _ = select.select([read_fd], [], [], self._poll_s)
                if ready:
                    data = os.read(read_fd, 64)
                    served = bool(data)
                    pipe_open = bool(data)      # EOF: the server's end is closed, stop selecting on it
            else:
                time.sleep(self._poll_s)
            if self._reap():
                break
            if served and self._policy == "always":
                logger.info("[Sandbox] request served: stopping the instance")
                stopping = True
            elif served:
                logger.warning("[Sandbox] recycle signal ignored under policy never")
            if self._terminate:
                logger.info("[Sandbox] stop requested: forwarding SIGTERM to the server")
                stopping = True
            if stopping:
                self._stop_server()
                break
        self._clear_group()
        return self._exit_code(stopping)

    def _stop_server(self) -> None:
        self._signal_server(signal.SIGTERM)
        deadline = time.monotonic() + self._kill_grace_s
        while not self._reap():
            if time.monotonic() >= deadline:
                logger.error("[Sandbox] server ignored SIGTERM for %.0f s: killing it", self._kill_grace_s)
                self._signal_server(signal.SIGKILL)
                deadline = time.monotonic() + self._kill_grace_s
                while not self._reap():
                    if time.monotonic() >= deadline:
                        logger.error("[Sandbox] server did not die after SIGKILL")
                        return
                    time.sleep(self._poll_s)
                return
            time.sleep(self._poll_s)

    def _signal_server(self, sig: int) -> None:
        try:
            self._kill(self._pid, sig)
        except ProcessLookupError:
            logger.debug("[Sandbox] server already gone when signalled")

    def _clear_group(self) -> None:
        """End what is left of the server's own group. Engine children run in their own session (start_new_session), so this
        does not reach them: on Cloud Run the container exit bounds them and, as PID 1, the supervisor reaps orphans."""
        try:
            self._killpg(self._pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            logger.debug("[Sandbox] no process group left to clear")

    def _reap(self) -> bool:
        """Collect exited children without blocking. True once the server's own status is known."""
        if self._status is not None:
            return True
        target = -1 if self._is_init else self._pid
        while True:
            try:
                pid, status = self._waitpid(target, os.WNOHANG)
            except ChildProcessError:
                if self._status is None:
                    logger.error("[Sandbox] server status lost (no such child)")
                    self._status = 1 << 8
                return True
            if pid == 0:
                return False
            if pid == self._pid:
                self._status = status
                return True
            logger.debug("[Sandbox] reaped an orphaned child")

    def _exit_code(self, stopping: bool) -> int:
        code = os.waitstatus_to_exitcode(self._status) if self._status is not None else 1
        server_code = code if code >= 0 else 128 - code      # killed by signal n: 128 + n
        if stopping:
            clean = code == 0 or code == -signal.SIGTERM
            if clean:
                return 0
            logger.error("[Sandbox] server ended abnormally while stopping (code %s)", server_code)
            return server_code or 1
        logger.error("[Sandbox] server exited on its own (code %s)", server_code)
        return server_code or 1
