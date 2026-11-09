"""The relay's `/ctl` socket: the page's end of the call control channel (VOICE_CALL_CONTROL_CHANNEL_RFC §4).

`/ctl` is public, like `/sfu/*`: a socket proves itself with its first frame, `hello {v, token}`, within
`HELLO_TIMEOUT_S`; its token's SHA-256 must name a live call in `CallControlHub`. The token is never in the URL (Cloud
Run's request log records upgrade URLs) and never logged: no frame is ever logged, only the call's
`fingerprint(ticket)` once authenticated.

Caps (RFC §4.2, §9): a page frame over `MAX_INBOUND_FRAME_BYTES`, or more than `MAX_INBOUND_FRAMES_PER_S` frames in
any second, closes the socket (codes 1009 / 1008; the page's backoff redials and the call is unaffected). Sockets that
have not finished `hello` are bounded like `_MAX_UNPAIRED_CALLS` in `sfu_stream_handler.py`.

After `welcome` the handler answers `ping` with `pong`, marks the link on `listening` (every call, whatever its
features; a repeat is ignored), queues the commands of the call's features on the link, and ignores everything else
(forward-compatible).
"""
import asyncio
import time
from collections import deque
from typing import Callable, Deque, Optional

from websockets.exceptions import ConnectionClosed

from src.domain.voice_control_protocol import (
    HELLO,
    HELLO_TIMEOUT_S,
    LISTENING,
    MAX_INBOUND_FRAME_BYTES,
    MAX_INBOUND_FRAMES_PER_S,
    PING,
    ByeReason,
    bye_frame,
    control_token_digest,
    encode,
    hello_token,
    hello_version,
    parse_inbound,
    pong_frame,
)
from src.services.call_control_hub import CallControlHub, CallControlLink
from src.utils.logger import logger

_MAX_UNAUTHENTICATED = 32
# How long a valid-looking `hello` waits for its call to register: the page can dial as soon as `/connect` answers,
# while the relay is still pairing the SFU sockets (up to 15 s) and fetching the session config.
_LINK_WAIT_S = 15.0
# A dead peer must not hold a handler (and an unauthenticated slot) on a send or close.
_SOCKET_OP_TIMEOUT_S = 2.0
_CLOSE_POLICY = 1008
_CLOSE_TOO_BIG = 1009
_CLOSE_TRY_LATER = 1013


def _too_big(message, limit: int) -> bool:
    size = len(message) if isinstance(message, (bytes, bytearray)) else len(message.encode("utf-8"))
    return size > limit


class CallControlHandler:
    """Every `/ctl` socket of this relay process."""

    def __init__(self, hub: CallControlHub, *, hello_timeout_s: float = HELLO_TIMEOUT_S,
                 link_wait_s: float = _LINK_WAIT_S, max_frame_bytes: int = MAX_INBOUND_FRAME_BYTES,
                 max_frames_per_s: int = MAX_INBOUND_FRAMES_PER_S, max_unauthenticated: int = _MAX_UNAUTHENTICATED,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self._hub = hub
        self._hello_timeout_s = hello_timeout_s
        self._link_wait_s = link_wait_s
        self._max_frame_bytes = max_frame_bytes
        self._max_frames_per_s = max_frames_per_s
        self._max_unauthenticated = max_unauthenticated
        self._clock = clock
        self._unauthenticated = 0

    async def handle_connection(self, ws) -> None:
        if self._unauthenticated >= self._max_unauthenticated:
            logger.warning(f"control: rejected a socket, {self._unauthenticated} already waiting for hello")
            await self._close(ws, _CLOSE_TRY_LATER)
            return
        self._unauthenticated += 1
        try:
            link = await self._authenticate(ws)
        finally:
            self._unauthenticated -= 1
        if link is None:
            return
        try:
            await self._serve(link, ws)
        finally:
            await self._hub.detach(link, ws)

    async def _authenticate(self, ws) -> Optional[CallControlLink]:
        try:
            message = await asyncio.wait_for(ws.recv(), timeout=self._hello_timeout_s)
        except asyncio.TimeoutError:
            logger.info(f"control: no hello within {self._hello_timeout_s:.0f}s, closed")
            await self._close(ws, _CLOSE_POLICY)
            return None
        except ConnectionClosed:
            return None
        if _too_big(message, self._max_frame_bytes):
            logger.warning("control: oversized first frame, closed")
            await self._close(ws, _CLOSE_TOO_BIG)
            return None
        frame = parse_inbound(message) if isinstance(message, str) else None
        if frame is None or frame["type"] != HELLO:
            logger.warning("control: first frame is not a hello, closed")
            await self._close(ws, _CLOSE_POLICY)
            return None
        if hello_version(frame) is None:
            logger.info("control: hello with an unsupported version, closed")
            await self._bye(ws, ByeReason.VERSION)
            return None
        token = hello_token(frame)
        link = await self._hub.attach(control_token_digest(token), ws, wait_s=self._link_wait_s) if token else None
        if link is None:
            logger.info("control: hello names no live call, closed")
            await self._bye(ws, ByeReason.AUTH)
        return link

    async def _serve(self, link: CallControlLink, ws) -> None:
        recent: Deque[float] = deque()
        try:
            async for message in ws:
                now = self._clock()
                recent.append(now)
                while recent and recent[0] <= now - 1.0:
                    recent.popleft()
                if len(recent) > self._max_frames_per_s:
                    logger.warning(f"control {link.call_fp}: over {self._max_frames_per_s} frames/s, closed")
                    await self._close(ws, _CLOSE_POLICY)
                    return
                if _too_big(message, self._max_frame_bytes):
                    logger.warning(f"control {link.call_fp}: frame over {self._max_frame_bytes} bytes, closed")
                    await self._close(ws, _CLOSE_TOO_BIG)
                    return
                frame = parse_inbound(message) if isinstance(message, str) else None
                if frame is None:
                    continue
                if frame["type"] == PING:
                    await link.send(pong_frame())
                elif frame["type"] == LISTENING:
                    link.mark_listening()
                elif link.accepts(frame["type"]):
                    link.offer_command(frame)
        except ConnectionClosed:
            return

    @staticmethod
    async def _bye(ws, reason: ByeReason) -> None:
        try:
            await asyncio.wait_for(ws.send(encode(bye_frame(reason))), timeout=_SOCKET_OP_TIMEOUT_S)
        except Exception as exc:
            logger.info(f"control: bye not delivered ({type(exc).__name__})")
        await CallControlHandler._close(ws, 1000)

    @staticmethod
    async def _close(ws, code: int) -> None:
        try:
            await asyncio.wait_for(ws.close(code=code), timeout=_SOCKET_OP_TIMEOUT_S)
        except Exception as exc:
            logger.info(f"control: socket close did not complete ({type(exc).__name__})")
