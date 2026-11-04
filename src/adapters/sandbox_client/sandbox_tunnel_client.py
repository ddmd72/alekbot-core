"""
SandboxTunnelClient: the indexer's end of the media tunnel (docs/10_rfcs/UNTRUSTED_CONTENT_SANDBOX_RFC.md §7 S5;
S5 design §2.3, §2.6), one HTTP/2 request carrying a full-duplex conversation.

httpx sends the whole body before it reads a reply, so this client speaks raw HTTP/2 (`h2`) over an asyncio connection:
TLS with ALPN `h2` for `https://`, h2c with prior knowledge for `http://` (local runs and tests). The proven shape is the
SPIKE-H2 client, hardened. One `run` = one connection, one stream:

- headers `POST /v1/<op>` with `Authorization: Bearer <ID token>` and the closed `X-Sandbox-Meta` (`request_id` and the
  limits only), then the START frame on the body;
- the response: OPEN asks for the recording from an offset (one `open_range(offset)` upstream read per OPEN), CLOSE ends
  that stream, BEAT keeps the call alive, WINDOW carries one Opus window, RESULT is the verdict;
- the body: DATA frames of the newest open stream only (an older one starves until CLOSE), END at the source's end
  (`END_EOF`) or when the upstream failed (`END_UPSTREAM_ERROR`).

The bounds are enforced here authoritatively (`TunnelLedger` over START's size and budget; a WINDOW only of a requested
index, once, within the response cap; one RESULT whose windows are exactly the WINDOW frames received; nothing after
RESULT). A breach resets the stream (RST_STREAM CANCEL), logs a WARNING and raises `TunnelViolation`. No URL, header,
name or id of the recording is ever written to the connection: the sandbox sees offsets and bytes.

Deadlines, all inside the caller's `deadline` (an event-loop instant): connect `CONNECT_S`; read-idle `IDLE_S` (BEATs
make silence a loss, which covers a server that dropped the stream without a word); send-stall `SEND_STALL_S` (the
flow window stuck at 0); the whole call ends at `deadline`.

Classification (the adapter decides retries, never this client):
    401 / 403 / 404 / 405, any `bad_request` outcome   SandboxMisconfigured, ERROR line
    429; 503 with the sandbox's outcome other than     SandboxUnavailable("busy"); `unconfigured` -> reason
      recycling / unconfigured                          "unavailable"
    connect error / refused / connect timeout          SandboxUnavailable("unavailable")
    503 `recycling`                                    TunnelLost("recycling")
    503 without the sandbox's outcome header           TunnelLost("no_answer") (Cloud Run's own answer)
    any other status; reset, GOAWAY, closed            TunnelLost("lost")
      connection, idle / send-stall / total deadline,
      the stream ended without RESULT
    200 without the `ok`/"tunnel" outcome, any frame,  TunnelViolation
      START-bound or RESULT breach

Three public names in one module, by decision: the client, its reply and its loss signal are one contract (CLAUDE.md's
one-class rule is waived here on purpose; precedent `sandbox_http_client`).

Public interface:
    SandboxTunnelClient(base_url, token_provider, *, ssl_context=None, idle_s=IDLE_S, send_stall_s=SEND_STALL_S)
        .run(op, start, open_range, deadline) -> TunnelReply
    TunnelReply(result, windows, request_id, opens, bytes_in)
    TunnelLost(kind, detail)
"""
from __future__ import annotations

import asyncio
import contextlib
import ssl
from dataclasses import dataclass, replace
from typing import AsyncIterator, Awaitable, Callable, Dict, List, Optional, Tuple
from urllib.parse import urlsplit

import h2.config
import h2.connection
import h2.errors
import h2.events
import h2.exceptions
import h2.settings
from hpack import NeverIndexedHeaderTuple

from ...domain.sandbox_contract import (
    META_HEADER,
    OUTCOME_HEADER,
    RESPONSE_CAP_BYTES,
    SandboxContractError,
    SandboxMeta,
    SandboxOp,
    SandboxOutcome,
    SandboxStatus,
    decode_outcome,
    encode_meta,
    new_request_id,
)
from ...domain.sandbox_errors import SandboxMisconfigured, SandboxUnavailable
from ...domain.sandbox_tunnel import (
    DATA_MAX,
    END_EOF,
    END_UPSTREAM_ERROR,
    IDLE_S,
    MEDIA_CEILING,
    RESPONSE_FRAMES,
    SEND_STALL_S,
    Frame,
    FrameReader,
    FrameType,
    TunnelLedger,
    TunnelResult,
    TunnelStart,
    TunnelViolation,
    TunnelWindows,
    decode_result,
    encode_frame,
    encode_frame_header,
    encode_start,
)
from ...utils.logger import logger
from .metadata_id_token_provider import normalise_audience

CONNECT_S = 10.0
RECV_WINDOW = 16 << 20                 # the response carries Opus windows of up to 64 MiB
TUNNEL_OUTCOME_DETAIL = "tunnel"       # the 200's outcome header: "stream opened", never the verdict (Δ6)
_CLOSE_S = 2.0                         # bound on the polite shutdown of a finished connection
_READ_BYTES = 1 << 16

TokenProvider = Callable[[], Awaitable[str]]
RangeOpener = Callable[[int], AsyncIterator[bytes]]


@dataclass(frozen=True)
class TunnelReply:
    """The verdict and the windows that came with it: `windows` holds (index, Opus bytes) in the RESULT's order, exactly
    the RESULT's windows. `opens` and `bytes_in` are the client ledger's."""
    result: TunnelResult
    windows: Tuple[Tuple[int, bytes], ...]
    request_id: str
    opens: int
    bytes_in: int


class TunnelLost(Exception):
    """The call ended without a verdict. `kind`: `recycling` (503 recycling before the stream), `no_answer` (a 503 the
    sandbox never sent) or `lost` (the stream or the instance went away, or a deadline passed)."""

    def __init__(self, kind: str, detail: str = "") -> None:
        super().__init__(f"{kind}: {detail}" if detail else kind)
        self.kind = kind
        self.detail = detail


class _Stop(Exception):
    """Internal: the call already ended (a failure was recorded); the sender stops quietly."""


@dataclass
class _Upstream:
    position: int
    iterator: Optional[AsyncIterator[bytes]] = None
    pending: bytes = b""
    finished: bool = False


class SandboxTunnelClient:
    def __init__(self, base_url: str, token_provider: TokenProvider, *, ssl_context: Optional[ssl.SSLContext] = None,
                 idle_s: float = IDLE_S, send_stall_s: float = SEND_STALL_S, connect_s: float = CONNECT_S) -> None:
        self._base_url = normalise_audience(base_url)
        parts = urlsplit(self._base_url)
        if parts.scheme not in ("https", "http") or not parts.hostname:
            raise ValueError("the sandbox URL must be http(s)://host")
        self._tls = parts.scheme == "https"
        self._scheme = parts.scheme
        self._host = parts.hostname
        self._port = parts.port or (443 if self._tls else 80)
        self._authority = parts.netloc
        self._path_prefix = parts.path.rstrip("/")
        self._token_provider = token_provider
        self._ssl_context = ssl_context
        self._idle_s = idle_s
        self._send_stall_s = send_stall_s
        self._connect_s = connect_s

    @property
    def base_url(self) -> str:
        return self._base_url

    async def run(self, op: SandboxOp, start: TunnelStart, open_range: RangeOpener, deadline: float) -> TunnelReply:
        """One tunnel call, ended by `deadline` (an instant of the running loop's clock) at the latest. Raises
        TunnelViolation, TunnelLost, SandboxUnavailable or SandboxMisconfigured."""
        if start.op is not op:
            raise ValueError("START is for another op")
        call = _Call(self, op, start, open_range)
        timer = asyncio.timeout_at(deadline)
        try:
            async with timer:
                return await call.run()
        except TimeoutError:
            if not timer.expired():
                raise
            logger.warning("[SandboxTunnel] request_id=%s op=%s lost: the call's deadline passed",
                           call.request_id, op.value)
            raise TunnelLost("lost", "deadline") from None
        finally:
            await call.close()

    # -- used by _Call ---------------------------------------------------------------------------------------------

    async def _token(self, op: SandboxOp) -> str:
        try:
            return await self._token_provider()
        except (SandboxUnavailable, SandboxMisconfigured):
            raise   # the provider classified and logged it
        except Exception as exc:
            logger.error("[SandboxTunnel] request_id=- op=%s no ID token: %s", op.value, type(exc).__name__)
            raise SandboxMisconfigured("could not obtain an ID token") from None

    async def _connect(self) -> Tuple[asyncio.StreamReader, asyncio.StreamWriter]:
        context: Optional[ssl.SSLContext] = None
        if self._tls:
            context = self._ssl_context or ssl.create_default_context()
            context.set_alpn_protocols(["h2"])
        try:
            reader, writer = await asyncio.wait_for(
                asyncio.open_connection(self._host, self._port, ssl=context,
                                        server_hostname=self._host if self._tls else None),
                timeout=self._connect_s)
        except ssl.SSLCertVerificationError as exc:
            logger.error("[SandboxTunnel] the sandbox's certificate failed verification: %s", type(exc).__name__)
            raise SandboxMisconfigured("sandbox certificate rejected") from None
        except (OSError, asyncio.TimeoutError) as exc:
            logger.warning("[SandboxTunnel] the sandbox is unreachable: %s", type(exc).__name__)
            raise SandboxUnavailable("sandbox is unreachable") from None
        if self._tls:
            ssl_object = writer.get_extra_info("ssl_object")
            alpn = ssl_object.selected_alpn_protocol() if ssl_object is not None else None
            if alpn != "h2":
                writer.close()
                logger.error("[SandboxTunnel] the sandbox did not negotiate HTTP/2 (ALPN %s)", alpn)
                raise SandboxMisconfigured("sandbox does not speak HTTP/2")
        return reader, writer


class _Call:
    """One run: the connection, the stream's state and its three tasks (read loop, sender, idle watch)."""

    def __init__(self, client: SandboxTunnelClient, op: SandboxOp, start: TunnelStart, open_range: RangeOpener) -> None:
        self.client = client
        self.op = op
        self.start = start
        self.open_range = open_range
        self.request_id = new_request_id()
        self.loop = asyncio.get_running_loop()
        self.ledger = TunnelLedger(start.source_bytes, start.budget_bytes)
        self.frames = FrameReader(RESPONSE_FRAMES)
        self.windows: Dict[int, bytes] = {}
        self.window_bytes = 0
        self.result: Optional[TunnelResult] = None
        self.streams: Dict[int, _Upstream] = {}
        self.retired: List[_Upstream] = []  # closed by CLOSE; their upstream reads are ended by the sender
        self.streaming = False
        self.succeeded = False
        self.peer_reset = False
        self.failure: Optional[BaseException] = None
        self.done = asyncio.Event()
        self.changed = asyncio.Event()      # an OPEN or CLOSE: the sender looks again
        self.window_open = asyncio.Event()  # a WINDOW_UPDATE or new settings: the sender may send again
        self.last_rx = self.loop.time()
        self.reader: Optional[asyncio.StreamReader] = None
        self.writer: Optional[asyncio.StreamWriter] = None
        self.conn: Optional[h2.connection.H2Connection] = None
        self.sid = 0
        self.tasks: List["asyncio.Task[None]"] = []

    # -- the run ---------------------------------------------------------------------------------------------------

    async def run(self) -> TunnelReply:
        token = await self.client._token(self.op)
        self.reader, self.writer = await self.client._connect()
        conn = h2.connection.H2Connection(config=h2.config.H2Configuration(client_side=True, header_encoding="utf-8"))
        conn.local_settings = h2.settings.Settings(client=True, initial_values={
            h2.settings.SettingCodes.INITIAL_WINDOW_SIZE: RECV_WINDOW,
            h2.settings.SettingCodes.ENABLE_PUSH: 0})
        conn.initiate_connection()
        conn.increment_flow_control_window(RECV_WINDOW)
        self.conn = conn
        self.sid = conn.get_next_available_stream_id()
        meta = encode_meta(SandboxMeta(request_id=self.request_id, limits=replace(MEDIA_CEILING,
                                                                                  wall_s=self.start.wall_s)))
        conn.send_headers(self.sid, [
            (":method", "POST"), (":scheme", self.client._scheme), (":authority", self.client._authority),
            (":path", f"{self.client._path_prefix}/v1/{self.op.value}"),
            NeverIndexedHeaderTuple("authorization", f"Bearer {token}"),     # never in a shared HPACK table
            (META_HEADER.lower(), meta),
            ("content-type", "application/octet-stream")])
        self._write()
        self.last_rx = self.loop.time()
        self.tasks.append(asyncio.create_task(self._read_loop()))
        self.tasks.append(asyncio.create_task(self._watch_idle()))
        try:
            await self._send_body(encode_frame(Frame(FrameType.START, payload=encode_start(self.start))))
            self.tasks.append(asyncio.create_task(self._feed()))
        except _Stop:
            pass
        except (OSError, h2.exceptions.H2Error) as exc:
            self._lost(f"sending START failed ({type(exc).__name__})")
        await self.done.wait()
        if self.failure is not None:
            raise self.failure
        assert self.result is not None
        self.succeeded = True
        self._end_body()
        return TunnelReply(result=self.result, windows=self._ordered_windows(self.result),
                           request_id=self.request_id, opens=self.ledger.opens, bytes_in=self.ledger.bytes_in)

    async def close(self) -> None:
        """Ends everything this run started; safe after any outcome, cancellation included."""
        if not self.succeeded and not self.peer_reset:
            self._reset()
        self.done.set()
        for task in self.tasks:
            task.cancel()
        for task in self.tasks:
            with contextlib.suppress(BaseException):
                await task
        for upstream in list(self.streams.values()) + self.retired:
            await self._close_upstream(upstream)
        if self.writer is not None:
            if self.conn is not None:
                with contextlib.suppress(h2.exceptions.H2Error):
                    self.conn.close_connection()
                self._write()
            with contextlib.suppress(Exception):
                self.writer.close()
                await asyncio.wait_for(self.writer.wait_closed(), timeout=_CLOSE_S)

    # -- receiving -------------------------------------------------------------------------------------------------

    async def _read_loop(self) -> None:
        assert self.reader is not None and self.conn is not None
        try:
            while not self.done.is_set():
                data = await self.reader.read(_READ_BYTES)
                if not data:
                    self._lost("the connection closed")
                    return
                self.last_rx = self.loop.time()
                try:
                    events = self.conn.receive_data(data)
                except h2.exceptions.ProtocolError as exc:
                    self._lost(f"HTTP/2 protocol error {type(exc).__name__}")
                    return
                for event in events:
                    self._on_event(event)
                    if self.done.is_set():
                        break
                self._write()
        except (OSError, EOFError) as exc:
            self._lost(f"connection error {type(exc).__name__}")

    def _on_event(self, event: h2.events.Event) -> None:
        if isinstance(event, h2.events.ResponseReceived) and event.stream_id == self.sid:
            self._on_response(dict((str(k).lower(), str(v)) for k, v in event.headers))
        elif isinstance(event, h2.events.DataReceived):
            assert self.conn is not None
            self.conn.acknowledge_received_data(event.flow_controlled_length, event.stream_id)
            if event.stream_id == self.sid and self.streaming:
                try:
                    frames = self.frames.feed(event.data)
                except TunnelViolation as exc:
                    self._violation(str(exc))
                    return
                for frame in frames:
                    self._on_frame(frame)
                    if self.done.is_set():
                        return
        elif isinstance(event, h2.events.StreamEnded) and event.stream_id == self.sid:
            if not self.streaming:
                return
            try:
                self.frames.end()
            except TunnelViolation as exc:
                self._violation(str(exc))
                return
            if self.result is None:
                self._lost("the stream ended without RESULT")
                return
            self.done.set()
        elif isinstance(event, h2.events.StreamReset) and event.stream_id == self.sid:
            self.peer_reset = True
            self._lost(f"stream reset ({event.error_code!r})")
        elif isinstance(event, h2.events.ConnectionTerminated):
            self._lost(f"GOAWAY ({event.error_code!r})")
        elif isinstance(event, (h2.events.WindowUpdated, h2.events.RemoteSettingsChanged)):
            self.window_open.set()

    def _on_response(self, headers: Dict[str, str]) -> None:
        try:
            status = int(headers.get(":status", ""))
        except ValueError:
            self._lost("no status")
            return
        outcome = _read_outcome(headers.get(OUTCOME_HEADER.lower()))
        if status == 200:
            if outcome is None or outcome.status is not SandboxStatus.OK or outcome.detail != TUNNEL_OUTCOME_DETAIL:
                self._violation("the stream opened without the tunnel outcome")
                return
            self.streaming = True
            return
        if status in (401, 403, 404, 405) or (outcome is not None and outcome.status is SandboxStatus.BAD_REQUEST):
            detail = outcome.detail if outcome is not None else ""
            logger.error("[SandboxTunnel] request_id=%s op=%s refused with HTTP %d %s: deployment mistake",
                         self.request_id, self.op.value, status, detail)
            self._fail(SandboxMisconfigured(f"sandbox answered HTTP {status}"))
        elif status == 429:
            self._fail(SandboxUnavailable("sandbox is busy (429)", reason="busy"))
        elif status == 503 and outcome is None:
            self._lost("503 without the sandbox's answer", kind="no_answer")
        elif status == 503 and outcome is not None and outcome.status is SandboxStatus.RECYCLING:
            self._lost("503 recycling", kind="recycling")
        elif status == 503 and outcome is not None and outcome.status is SandboxStatus.UNCONFIGURED:
            self._fail(SandboxUnavailable("sandbox is not configured"))
        elif status == 503:
            self._fail(SandboxUnavailable("sandbox answered 503", reason="busy"))
        else:
            self._lost(f"HTTP {status} before the stream")

    def _on_frame(self, frame: Frame) -> None:
        try:
            if frame.kind is FrameType.OPEN:
                self.ledger.open(frame.id, frame.value)
                self.streams[frame.id] = _Upstream(position=frame.value)
                self.changed.set()
            elif frame.kind is FrameType.CLOSE:
                self.ledger.close(frame.id)
                upstream = self.streams.pop(frame.id, None)
                if upstream is not None:
                    self.retired.append(upstream)
                self.changed.set()
            elif frame.kind is FrameType.WINDOW:
                if frame.id not in self.start.window_indexes or frame.id in self.windows:
                    raise TunnelViolation("a WINDOW outside the request or repeated")
                self.window_bytes += len(frame.payload)
                if self.window_bytes > RESPONSE_CAP_BYTES:
                    raise TunnelViolation("windows over the response cap")
                self.windows[frame.id] = frame.payload
            elif frame.kind is FrameType.RESULT:
                result = decode_result(frame.payload, self.start)
                media = result.media
                reported = [w.index for w in media.windows] if isinstance(media, TunnelWindows) else []
                if sorted(reported) != sorted(self.windows):
                    raise TunnelViolation("RESULT's windows differ from the WINDOW frames")
                self.result = result
        except TunnelViolation as exc:
            self._violation(str(exc))

    async def _watch_idle(self) -> None:
        while not self.done.is_set():
            left = self.last_rx + self.client._idle_s - self.loop.time()
            if left <= 0:
                self._lost("read idle")
                return
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(self.done.wait(), timeout=left)

    # -- sending ---------------------------------------------------------------------------------------------------

    async def _feed(self) -> None:
        """Streams the newest open stream's bytes as DATA frames, re-checking which stream is newest between frames."""
        size = self.start.source_bytes
        try:
            while not self.done.is_set():
                while self.retired:
                    await self._close_upstream(self.retired.pop())
                newest = self.ledger.newest
                upstream = self.streams.get(newest) if newest is not None else None
                if newest is None or upstream is None or upstream.finished:
                    self.changed.clear()
                    await self.changed.wait()
                    continue
                if not upstream.pending:
                    if upstream.iterator is None:
                        upstream.iterator = self.open_range(upstream.position).__aiter__()
                    try:
                        upstream.pending = await upstream.iterator.__anext__()
                    except StopAsyncIteration:
                        await self._end_stream(newest, upstream, END_EOF if upstream.position == size
                                               else END_UPSTREAM_ERROR)
                        continue
                    except asyncio.CancelledError:
                        raise
                    except Exception as exc:
                        logger.warning("[SandboxTunnel] request_id=%s upstream range failed: %s", self.request_id,
                                       type(exc).__name__)
                        await self._end_stream(newest, upstream, END_UPSTREAM_ERROR)
                        continue
                    if not upstream.pending:
                        continue
                piece, upstream.pending = upstream.pending[:DATA_MAX], upstream.pending[DATA_MAX:]
                if upstream.position + len(piece) > size:
                    logger.warning("[SandboxTunnel] request_id=%s upstream ran past the source's size", self.request_id)
                    await self._end_stream(newest, upstream, END_UPSTREAM_ERROR)
                    continue
                if not self.ledger.data(newest, upstream.position, len(piece)):
                    continue    # closed while in flight: counted, dropped
                offset = upstream.position
                upstream.position += len(piece)
                await self._send_body(encode_frame_header(FrameType.DATA, newest, offset, len(piece)) + piece)
        except TunnelViolation as exc:
            self._violation(str(exc))
        except _Stop:
            return
        except h2.exceptions.H2Error as exc:
            self._lost(f"stream closed while sending ({type(exc).__name__})")
        except (OSError, EOFError) as exc:
            self._lost(f"connection error while sending ({type(exc).__name__})")

    async def _end_stream(self, stream_id: int, upstream: _Upstream, value: int) -> None:
        upstream.finished = True
        upstream.pending = b""
        await self._close_upstream(upstream)
        if self.ledger.end(stream_id):
            await self._send_body(encode_frame_header(FrameType.END, stream_id, value, 0))

    async def _send_body(self, data: bytes) -> None:
        """Writes `data` on the request body within the flow window; a window stuck at 0 for the send-stall deadline is
        a loss. Raises _Stop once the call has ended."""
        assert self.conn is not None and self.writer is not None
        view = memoryview(data)
        while view:
            if self.done.is_set():
                raise _Stop()
            room = min(self.conn.local_flow_control_window(self.sid), self.conn.max_outbound_frame_size, len(view))
            if room <= 0:
                self.window_open.clear()
                self._write()
                try:
                    await asyncio.wait_for(self.window_open.wait(), timeout=self.client._send_stall_s)
                except asyncio.TimeoutError:
                    self._lost("send stalled: the flow window stayed at 0")
                    raise _Stop() from None
                continue
            self.conn.send_data(self.sid, bytes(view[:room]))
            view = view[room:]
            self._write()
        try:
            await asyncio.wait_for(self.writer.drain(), timeout=self.client._send_stall_s)
        except asyncio.TimeoutError:
            self._lost("send stalled: the socket did not drain")
            raise _Stop() from None

    def _write(self) -> None:
        if self.conn is None or self.writer is None:
            return
        data = self.conn.data_to_send()
        if data and not self.writer.is_closing():
            self.writer.write(data)

    def _end_body(self) -> None:
        if self.conn is not None:
            with contextlib.suppress(h2.exceptions.H2Error):
                self.conn.end_stream(self.sid)
            self._write()

    def _reset(self) -> None:
        if self.conn is None or self.sid == 0:
            return
        with contextlib.suppress(h2.exceptions.H2Error):
            self.conn.reset_stream(self.sid, error_code=h2.errors.ErrorCodes.CANCEL)
        self._write()

    @staticmethod
    async def _close_upstream(upstream: _Upstream) -> None:
        """Ends an upstream read; only the sender (or `close`, once the sender is gone) calls it, never while a read of
        that iterator is in progress."""
        iterator, upstream.iterator = upstream.iterator, None
        closer = getattr(iterator, "aclose", None)
        if closer is not None:
            with contextlib.suppress(Exception):
                await asyncio.wait_for(closer(), timeout=_CLOSE_S)

    # -- endings ---------------------------------------------------------------------------------------------------

    def _fail(self, failure: BaseException) -> None:
        if self.done.is_set():
            return
        self.failure = failure
        self.done.set()

    def _lost(self, detail: str, *, kind: str = "lost") -> None:
        if self.done.is_set():
            return
        logger.warning("[SandboxTunnel] request_id=%s op=%s lost (%s): %s", self.request_id, self.op.value, kind,
                       detail)
        self._fail(TunnelLost(kind, detail))

    def _violation(self, reason: str) -> None:
        if self.done.is_set():
            return
        logger.warning("[SandboxTunnel] request_id=%s op=%s tunnel violation: %s", self.request_id, self.op.value,
                       reason)
        self._fail(TunnelViolation(reason))
        self._reset()

    def _ordered_windows(self, result: TunnelResult) -> Tuple[Tuple[int, bytes], ...]:
        media = result.media
        if not isinstance(media, TunnelWindows):
            return ()
        return tuple((w.index, self.windows[w.index]) for w in media.windows)


def _read_outcome(header: Optional[str]) -> Optional[SandboxOutcome]:
    if header is None:
        return None
    try:
        return decode_outcome(header)
    except SandboxContractError:
        return None
