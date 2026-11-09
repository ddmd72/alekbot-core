"""CallControlHub and CallControlLink — the relay side of the page <-> relay control channel
(VOICE_CALL_CONTROL_CHANNEL_RFC §4.3).

**The hub.** One per relay process, injected into the web `VoiceSessionService` only (the phone service gets none). It
maps `control_token_digest(token)` -> `CallControlLink` for each live web call that has a control token:
- the session `open`s the link when the call starts (the digest comes in the session config, from the ticket record)
  and `close`s it in the call's teardown (`bye{call_ended}` to the page);
- the `/ctl` handler (`src/handlers/call_control_handler.py`) `attach`es a socket whose `hello` token hashes to a
  known digest, and `detach`es it when the socket ends.
The hub holds digests only, never a token, and only in memory: a token dies with its call.

**Pairing is by process.** The page dials `/ctl` on the same relay base URL the SFU dials (`voice_web_call_app.py`
builds the three URLs from one value), and the relay runs `--max-instances=1`
(`decisions/voice_web_transport_cloudflare_sfu.md`), so the hub that receives the socket is the one the call
registered in. Relay shards, if ever built, keep this as long as the main service picks one base URL per call for all
three paths.

**The link** is one call's end of the channel; the page's socket comes and goes under it (reconnect, replace). What a
feature (slice 4 "Ask Lelik", the live-text RFC) uses, and nothing else:
- `send(frame)` — push a relay -> page frame to the current socket; never raises, False when nobody is listening.
- `commands` — the page's command frames for the features this call offers (`voice_control_protocol.FEATURE_COMMANDS`),
  in arrival order, for the session's control task.
- `set_state(**fields)` — fields the next `welcome` carries (e.g. `ask="armed"`), so a reconnecting page re-syncs.
- `listened` — set once the page said `listening` (it hears Lelik's audio); the web greeting waits on it, capped.

Three public classes in one module, on purpose: the link is created and torn down only by the hub, and `ControlSocket`
exists only as their parameter type; a split would also be a cross-service import (REQ-ARCH-22).
"""
import asyncio
from typing import Any, Dict, FrozenSet, Iterable, Optional, Protocol, Tuple

from src.domain.voice_control_protocol import ByeReason, accepted_commands, bye_frame, encode, welcome_frame
from src.utils.logger import logger

# Bounds what a page can queue for a session that reads slowly or not at all; the rate cap bounds it per second.
_COMMAND_QUEUE_SIZE = 32


class ControlSocket(Protocol):
    """The page's socket as the link sees it: a `websockets` server connection in production, a fake in tests."""

    async def send(self, message: str) -> None: ...

    async def close(self, code: int = 1000, reason: str = "") -> None: ...


class CallControlHub:
    """digest -> link for every live call of this relay process."""

    def __init__(self, *, send_timeout_s: float = 2.0) -> None:
        self._links: Dict[str, "CallControlLink"] = {}
        self._send_timeout_s = send_timeout_s
        # Set (and replaced) on every `open`, so a socket that said `hello` a moment before its call registered can
        # wait for it (the page can dial `/ctl` before the relay has fetched the call's session config).
        self._opened = asyncio.Event()

    def open(self, digest: str, call_fp: str, features: Iterable[str] = ()) -> "CallControlLink":
        """Register the call's link at call start. `features` are the ones this call offers in `welcome`;
        `call_fp` is `fingerprint(ticket)`, the only call id the logs carry."""
        if digest in self._links:
            # Not reachable with a random 256-bit token; the newer call wins rather than sharing a link.
            logger.warning(f"control {call_fp}: a link with this digest was already open, replaced")
        link = CallControlLink(digest, call_fp, features, send_timeout_s=self._send_timeout_s)
        self._links[digest] = link
        opened, self._opened = self._opened, asyncio.Event()
        opened.set()
        return link

    def get(self, digest: str) -> Optional["CallControlLink"]:
        return self._links.get(digest)

    def __len__(self) -> int:
        return len(self._links)

    async def attach(self, digest: str, socket: ControlSocket, wait_s: float = 0.0) -> Optional["CallControlLink"]:
        """Attach an authenticated socket to the link of `digest` and send it `welcome`; a socket already attached is
        told `bye{replaced}` and closed. None when no live call has this digest within `wait_s` (the caller answers
        `bye{auth}`)."""
        link = await self._wait_for(digest, wait_s)
        if link is None:
            return None
        attached, previous = await link.attach(socket)
        if not attached:
            return None
        if previous is not None and previous is not socket:
            logger.info(f"control {link.call_fp}: replaced")
            await link.say_bye(previous, ByeReason.REPLACED)
        logger.info(f"control {link.call_fp}: connected")
        return link

    async def detach(self, link: "CallControlLink", socket: ControlSocket) -> None:
        """The socket ended (page gone, network, a cap). The link stays open for a reconnect until the call ends."""
        if link.detach(socket):
            logger.info(f"control {link.call_fp}: disconnected")

    async def close(self, link: "CallControlLink", reason: ByeReason = ByeReason.CALL_ENDED) -> None:
        """Teardown: forget the link (by identity) and tell its socket `bye{reason}`. Never raises: it runs inside a
        call's teardown, which must reach `submit_transcript`."""
        try:
            if self._links.get(link.digest) is link:
                del self._links[link.digest]
            await link.shut(reason)
            logger.info(f"control {link.call_fp}: closed ({reason.value})")
        except Exception:
            logger.error(f"control {link.call_fp}: closing the link failed", exc_info=True)

    async def _wait_for(self, digest: str, wait_s: float) -> Optional["CallControlLink"]:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + wait_s
        while True:
            link = self._links.get(digest)
            if link is not None and not link.closed:
                return link
            remaining = deadline - loop.time()
            if remaining <= 0:
                return None
            try:
                await asyncio.wait_for(self._opened.wait(), timeout=remaining)
            except asyncio.TimeoutError:
                return None


class CallControlLink:
    """One call's control channel, relay side. At most one page socket at a time; a newer one replaces it."""

    def __init__(self, digest: str, call_fp: str, features: Iterable[str] = (), *, send_timeout_s: float = 2.0) -> None:
        self.digest = digest
        self.call_fp = call_fp
        self.features: FrozenSet[str] = frozenset(features)
        self.commands: "asyncio.Queue[Dict[str, Any]]" = asyncio.Queue(maxsize=_COMMAND_QUEUE_SIZE)
        # The page's `listening` (protocol core frame, every call): set once, never cleared for the call's life.
        self.listened = asyncio.Event()
        self._accepted = accepted_commands(self.features)
        self._state: Dict[str, Any] = {}
        self._socket: Optional[ControlSocket] = None
        self._closed = False
        self._send_timeout_s = send_timeout_s
        # Serialises every write and the socket swap: `welcome` is always the first frame a new socket gets, and two
        # writers (a control task, a text pacer) never interleave on one socket.
        self._lock = asyncio.Lock()

    # ------------------------------------------------------------------ used by the session and its features

    @property
    def connected(self) -> bool:
        return self._socket is not None

    @property
    def closed(self) -> bool:
        return self._closed

    def set_state(self, **fields: Any) -> None:
        """Fields the next `welcome` carries next to `v` and `features` (each feature's current state)."""
        self._state.update(fields)

    async def send(self, frame: Dict[str, Any]) -> bool:
        """Send a relay -> page frame to the current socket. False when no socket is attached or the write failed or
        stalled; never raises (a lost frame never breaks the call)."""
        async with self._lock:
            if self._socket is None:
                return False
            return await self._write(self._socket, frame)

    # ------------------------------------------------------------------ used by the hub and the `/ctl` handler

    def accepts(self, frame_type: str) -> bool:
        """True when `frame_type` is a command of a feature this call offers."""
        return frame_type in self._accepted

    def mark_listening(self) -> bool:
        """The page said `listening`. True the first time; a repeat (a re-sent frame) changes nothing."""
        if self.listened.is_set():
            return False
        self.listened.set()
        return True

    def offer_command(self, frame: Dict[str, Any]) -> bool:
        """Queue a page command for the session. False (dropped) when the queue is full."""
        try:
            self.commands.put_nowait(frame)
            return True
        except asyncio.QueueFull:
            logger.warning(f"control {self.call_fp}: command queue full, {frame['type']} dropped")
            return False

    async def attach(self, socket: ControlSocket) -> Tuple[bool, Optional[ControlSocket]]:
        """Make `socket` the page's socket and send it `welcome`. Returns (attached, the socket it replaced if any);
        not attached when the link was shut meanwhile (the call ended)."""
        async with self._lock:
            if self._closed:
                return False, None
            previous, self._socket = self._socket, socket
            await self._write(socket, welcome_frame(self.features, self._state))
        return True, previous

    def detach(self, socket: ControlSocket) -> bool:
        """Forget `socket` if it is still the current one (a replaced socket detaching later changes nothing)."""
        if self._socket is socket:
            self._socket = None
            return True
        return False

    async def shut(self, reason: ByeReason) -> None:
        """Mark the link closed, tell the current socket `bye{reason}` and close it."""
        async with self._lock:
            self._closed = True
            socket, self._socket = self._socket, None
            if socket is not None:
                await self._write(socket, bye_frame(reason))
        if socket is not None:
            await self.close_socket(socket)

    async def say_bye(self, socket: ControlSocket, reason: ByeReason) -> None:
        """`bye{reason}` to a socket that is not (or no longer) the current one, then close it."""
        await self._write(socket, bye_frame(reason))
        await self.close_socket(socket)

    async def close_socket(self, socket: ControlSocket, code: int = 1000) -> None:
        try:
            await asyncio.wait_for(socket.close(code=code), timeout=self._send_timeout_s)
        except Exception as exc:
            logger.info(f"control {self.call_fp}: socket close did not complete ({type(exc).__name__})")

    async def _write(self, socket: ControlSocket, frame: Dict[str, Any]) -> bool:
        try:
            await asyncio.wait_for(socket.send(encode(frame)), timeout=self._send_timeout_s)
            return True
        except Exception as exc:
            # The frame itself is never logged (it can carry call content).
            logger.info(f"control {self.call_fp}: {frame['type']} not delivered ({type(exc).__name__})")
            return False
