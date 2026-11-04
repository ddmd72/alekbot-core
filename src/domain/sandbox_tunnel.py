"""
Media tunnel contract (docs/10_rfcs/UNTRUSTED_CONTENT_SANDBOX_RFC.md §4.7, §7 S5): the reverse tunnel on one HTTP/2
stream that lets the light sandbox decode a drive recording it can never fetch. Pure data and codecs, stdlib plus the
sandbox call contract.

One module because these types are one wire contract, read together by the indexer's client, the server route and the
media engine (CLAUDE.md's one-class rule is waived here on purpose; precedent `sandbox_contract`).

The stream. Every frame is `u8 type | u32 id | u64 value | u32 length | payload[length]` (17-byte header, big-endian).
- Body (indexer -> sandbox): START (the request JSON, first and once), DATA (id = stream, value = offset of the chunk),
  END (id = stream, value = END_EOF or END_UPSTREAM_ERROR).
- Response (sandbox -> indexer): OPEN (id = stream, value = offset: stream the recording from there until CLOSE, EOF or
  budget), CLOSE (id), BEAT (every BEAT_S), after the run WINDOW (id = window index, payload = one Opus window), then
  RESULT (the verdict JSON). Nothing follows RESULT.
Bounds hold on both sides (`TunnelLedger`); the indexer's are authoritative, a compromised sandbox is the threat.

START and RESULT are closed schemas: a key outside the set, a non-finite number or a duplicate key is a violation.
START carries no url, name, id or path key: the recording's identity never enters the sandbox (invariant I3).

Public interface:
    FrameType, Frame, BODY_FRAMES, RESPONSE_FRAMES          the frame vocabulary
    encode_frame(frame) / encode_frame_header(kind, id, value, length)
    FrameReader(accept).feed(data) -> [Frame]; .end()        incremental decoder, protocol order enforced
    TunnelWindow, TunnelStart                                the request (START)
    TunnelProbe, TunnelWindows, TunnelResult                 the verdict (RESULT)
    encode_start(start) / decode_start(payload, op)
    encode_result(result, start) / decode_result(payload, start)
    result_from_child(child, start)                          the media child's result JSON -> RESULT (F7's checks)
    tunnel_budget(source_bytes)                              bytes one call may move
    TunnelLedger                                             open/close/data/end bounds, both sides
    TunnelEngineResult, TunnelLink, TunnelRun, SandboxTunnelEngine   engine <-> server route
    TunnelViolation                                          any breach of the above
    MEDIA_CEILING, MAX_OPENS, MAX_OPEN_STREAMS, DATA_MAX, BEAT_S, IDLE_S, ...   constants
"""
from __future__ import annotations

import json
import math
import re
import struct
from dataclasses import dataclass
from enum import IntEnum
from typing import (
    AbstractSet, Any, Dict, FrozenSet, List, Mapping, NoReturn, Optional, Protocol, Sequence, Set, Tuple, Union,
    runtime_checkable,
)

from src.domain.sandbox_contract import (
    CEILING_FILE_BYTES, CEILING_MEMORY_BYTES, OUTCOME_DETAIL_MAX_CHARS, SandboxLimits, SandboxOp, SandboxStatus,
)

# -- constants -------------------------------------------------------------------------------------------------------

FRAME_HEADER = struct.Struct(">BIQI")
FRAME_HEADER_BYTES = FRAME_HEADER.size          # 17
U32_MAX = (1 << 32) - 1
U64_MAX = (1 << 64) - 1

START_MAX_BYTES = 4 * 1024
RESULT_MAX_BYTES = 16 * 1024
DATA_MAX = 256 * 1024                           # one DATA chunk
WINDOW_MAX_BYTES = CEILING_FILE_BYTES           # one WINDOW: the child's RLIMIT_FSIZE, 64 MiB
MAX_OPENS = 256                                 # OPENs per call (ffmpeg's seeks and reconnects)
MAX_OPEN_STREAMS = 2                            # ffmpeg's seek opens the new connection before closing the old
BUDGET_MARGIN_BYTES = 64 * 1024 * 1024
END_EOF = 0
END_UPSTREAM_ERROR = 1

BEAT_S = 5.0                                    # the server's heartbeat while the run lasts
IDLE_S = 30.0                                   # read-idle deadline on either side: silence = a lost stream
SEND_STALL_S = 30.0                             # a send whose flow window stays at 0 this long = a lost stream
START_READ_S = 10.0                             # the server reads START within this
COLLECT_MARGIN_S = 15.0                         # START wall_s = the port's wall - this: time to stream the windows back

# Mirrors of `drive_index_params` and `drive_parser.media`, not imports: those would grow the light image's closure.
# A test pins each pair equal.
SOURCE_MAX_BYTES = 8 * 1024 ** 3                # drive_index_params.TRANSCRIBE_MAX_BYTES
MEDIA_MAX_SECONDS = 14_400                      # drive_index_params.TRANSCRIBE_MAX_SECONDS
MEDIA_PASS_MAX_WINDOWS = 3                      # drive_index_params.MEDIA_PASS_MAX_WINDOWS
MEDIA_MAX_WINDOWS = 64                          # drive_parser.media.MAX_WINDOWS_PER_RUN
MEDIA_CEILING = SandboxLimits(
    wall_s=240.0,                               # drive_index_params.TRANSCRIBE_EXTRACT_WALL_S
    memory_bytes=CEILING_MEMORY_BYTES,
    cpu_s=150.0,                                # drive_index_params.TRANSCRIBE_CHILD_CPU_S
    file_bytes=CEILING_FILE_BYTES,
)

WINDOW_INDEX_LIMIT = 1_000                      # a window index is below this (the in-process extractor's bound)
CONTAINER_MAX_CHARS = 80
DETAIL_MAX_CHARS = OUTCOME_DETAIL_MAX_CHARS
CHILD_DETAIL_MAX_CHARS = 100                    # the child's own detail text is cut to this, as the extractor did

TUNNEL_OPS: FrozenSet[SandboxOp] = frozenset({SandboxOp.MEDIA_PROBE, SandboxOp.MEDIA_AUDIO_WINDOWS})
RESULT_STATUSES: FrozenSet[SandboxStatus] = frozenset({
    SandboxStatus.OK, SandboxStatus.TIMEOUT, SandboxStatus.MEMORY, SandboxStatus.ERROR, SandboxStatus.TOO_LARGE,
    SandboxStatus.BAD_REQUEST,
})
WINDOWS_STATUSES: FrozenSet[SandboxStatus] = frozenset({SandboxStatus.OK, SandboxStatus.TIMEOUT})

_CONTAINER_RE = re.compile(r"^[a-z0-9_]+(,[a-z0-9_]+)*$")


class TunnelViolation(ValueError):
    """A frame, START, RESULT or stream event outside the contract. Raised by every codec and the ledger."""


# -- frames ----------------------------------------------------------------------------------------------------------

class FrameType(IntEnum):
    """The u8 frame type (a wire byte, hence IntEnum)."""
    START = 1
    DATA = 2
    END = 3
    OPEN = 4
    CLOSE = 5
    BEAT = 6
    WINDOW = 7
    RESULT = 8


BODY_FRAMES: FrozenSet[FrameType] = frozenset({FrameType.START, FrameType.DATA, FrameType.END})
RESPONSE_FRAMES: FrozenSet[FrameType] = frozenset({
    FrameType.OPEN, FrameType.CLOSE, FrameType.BEAT, FrameType.WINDOW, FrameType.RESULT,
})


@dataclass(frozen=True)
class Frame:
    kind: FrameType
    id: int = 0
    value: int = 0
    payload: bytes = b""


@dataclass(frozen=True)
class _Rule:
    id_rule: str                        # "zero": id 0; "stream": id >= 1; "any": any u32
    values: Optional[FrozenSet[int]]    # allowed values; None = any u64
    min_len: int
    max_len: int


_RULES: Dict[FrameType, _Rule] = {
    FrameType.START: _Rule("zero", frozenset({0}), 1, START_MAX_BYTES),
    FrameType.DATA: _Rule("stream", None, 1, DATA_MAX),
    FrameType.END: _Rule("stream", frozenset({END_EOF, END_UPSTREAM_ERROR}), 0, 0),
    FrameType.OPEN: _Rule("stream", None, 0, 0),
    FrameType.CLOSE: _Rule("stream", frozenset({0}), 0, 0),
    FrameType.BEAT: _Rule("zero", frozenset({0}), 0, 0),
    FrameType.WINDOW: _Rule("any", frozenset({0}), 1, WINDOW_MAX_BYTES),
    FrameType.RESULT: _Rule("zero", frozenset({0}), 1, RESULT_MAX_BYTES),
}


def _check_header(code: int, frame_id: int, value: int, length: int) -> FrameType:
    try:
        kind = FrameType(code)
    except ValueError:
        raise TunnelViolation("unknown frame type") from None
    rule = _RULES[kind]
    if not 0 <= frame_id <= U32_MAX or not 0 <= value <= U64_MAX or not 0 <= length <= U32_MAX:
        raise TunnelViolation("frame field out of range")
    if rule.id_rule == "zero" and frame_id != 0:
        raise TunnelViolation(f"{kind.name} carries no id")
    if rule.id_rule == "stream" and frame_id < 1:
        raise TunnelViolation(f"{kind.name} needs a stream id")
    if rule.values is not None and value not in rule.values:
        raise TunnelViolation(f"{kind.name} value out of range")
    if not rule.min_len <= length <= rule.max_len:
        raise TunnelViolation(f"{kind.name} length out of range")
    return kind


def encode_frame_header(kind: FrameType, frame_id: int, value: int, length: int) -> bytes:
    """The 17-byte header alone, for a sender that streams the payload (a WINDOW read from a file)."""
    _check_header(int(kind), frame_id, value, length)
    return FRAME_HEADER.pack(int(kind), frame_id, value, length)


def encode_frame(frame: Frame) -> bytes:
    return encode_frame_header(frame.kind, frame.id, frame.value, len(frame.payload)) + frame.payload


class FrameReader:
    """Incremental decoder of one direction of the stream.

    `accept` is BODY_FRAMES or RESPONSE_FRAMES; a frame of another type is a violation. A header is checked before its
    payload is buffered, so a peer can never make the reader hold more than one frame's cap. Protocol order: a reader
    that accepts START wants it first and once; nothing may follow RESULT (a second RESULT included). After a violation
    the reader stays failed. `end()` is called when the peer ends the stream: a partial frame then is a violation."""

    def __init__(self, accept: AbstractSet[FrameType]) -> None:
        self._accept = frozenset(accept)
        self._buf = bytearray()
        self._header: Optional[Tuple[FrameType, int, int, int]] = None
        self._needs_start = FrameType.START in self._accept
        self._result_seen = False
        self._failed = False

    @property
    def result_seen(self) -> bool:
        return self._result_seen

    def feed(self, data: bytes) -> List[Frame]:
        if self._failed:
            raise TunnelViolation("the frame reader already failed")
        self._buf += data
        frames: List[Frame] = []
        while True:
            if self._result_seen and self._buf:
                self._fail("bytes after RESULT")
            if self._header is None:
                if len(self._buf) < FRAME_HEADER_BYTES:
                    return frames
                code, frame_id, value, length = FRAME_HEADER.unpack_from(self._buf)
                try:
                    kind = _check_header(code, frame_id, value, length)
                except TunnelViolation:
                    self._failed = True
                    raise
                if kind not in self._accept:
                    self._fail(f"{kind.name} is not a frame of this direction")
                del self._buf[:FRAME_HEADER_BYTES]
                self._header = (kind, frame_id, value, length)
            kind, frame_id, value, length = self._header
            if len(self._buf) < length:
                return frames
            payload = bytes(self._buf[:length])
            del self._buf[:length]
            self._header = None
            frames.append(self._admit(Frame(kind, frame_id, value, payload)))

    def end(self) -> None:
        if self._failed:
            raise TunnelViolation("the frame reader already failed")
        if self._header is not None or self._buf:
            self._fail("the stream ended inside a frame")

    def _admit(self, frame: Frame) -> Frame:
        if self._needs_start:
            if frame.kind is not FrameType.START:
                self._fail("the body does not begin with START")
            self._needs_start = False
        elif frame.kind is FrameType.START:
            self._fail("a second START")
        if frame.kind is FrameType.RESULT:
            self._result_seen = True
        return frame

    def _fail(self, reason: str) -> NoReturn:
        self._failed = True
        raise TunnelViolation(reason)


# -- budget and ledger -----------------------------------------------------------------------------------------------

def tunnel_budget(source_bytes: int) -> int:
    """Bytes one call may move: min(SOURCE_MAX_BYTES, 2 x size + 64 MiB) (ffmpeg's seeks re-read some ranges)."""
    if not _is_int(source_bytes) or not 1 <= source_bytes <= SOURCE_MAX_BYTES:
        raise TunnelViolation("source size out of range")
    return min(SOURCE_MAX_BYTES, 2 * source_bytes + BUDGET_MARGIN_BYTES)


class TunnelLedger:
    """The stream bounds of one call, the same on both sides: the media engine checks what its child asks and what the
    caller sends; the client checks what the sandbox asks and what it is about to send.

    - `open(id, offset)`: a new id (never reused), 0 <= offset < source_bytes, at most MAX_OPENS per call and
      MAX_OPEN_STREAMS open at once.
    - `close(id)`: an open id. The id stays used.
    - `data(id, offset, length)`: counted against `budget_bytes` whatever happens to it; True = deliver, False = drop
      (the id is closed: bytes in flight). Never-opened id, DATA after END, an offset other than the stream's position,
      a chunk past the source's end or over DATA_MAX: violation.
    - `end(id)`: True = deliver, False = drop (closed id). Never-opened id or a second END: violation.
    `newest` is the most recently opened open stream: the indexer feeds only it, an older one starves until CLOSE."""

    def __init__(self, source_bytes: int, budget_bytes: int) -> None:
        if not _is_int(budget_bytes) or not 1 <= budget_bytes <= tunnel_budget(source_bytes):
            raise TunnelViolation("budget out of range")
        self._size = source_bytes
        self._budget = budget_bytes
        self._position: Dict[int, int] = {}     # open stream -> the next offset it expects
        self._ended: Set[int] = set()
        self._closed: Set[int] = set()
        self._order: List[int] = []             # open streams, oldest first
        self._opens = 0
        self._bytes = 0

    @property
    def opens(self) -> int:
        return self._opens

    @property
    def bytes_in(self) -> int:
        return self._bytes

    @property
    def newest(self) -> Optional[int]:
        return self._order[-1] if self._order else None

    def is_open(self, stream_id: int) -> bool:
        return stream_id in self._position

    def open(self, stream_id: int, offset: int) -> None:
        if not 1 <= stream_id <= U32_MAX or stream_id in self._position or stream_id in self._closed:
            raise TunnelViolation("OPEN of a used or invalid id")
        if not 0 <= offset < self._size:
            raise TunnelViolation("OPEN offset outside the source")
        if self._opens >= MAX_OPENS:
            raise TunnelViolation("too many OPENs")
        if len(self._order) >= MAX_OPEN_STREAMS:
            raise TunnelViolation("too many open streams")
        self._opens += 1
        self._position[stream_id] = offset
        self._order.append(stream_id)

    def close(self, stream_id: int) -> None:
        if stream_id not in self._position:
            raise TunnelViolation("CLOSE of a stream that is not open")
        del self._position[stream_id]
        self._ended.discard(stream_id)
        self._order.remove(stream_id)
        self._closed.add(stream_id)

    def data(self, stream_id: int, offset: int, length: int) -> bool:
        if not 1 <= length <= DATA_MAX:
            raise TunnelViolation("DATA length out of range")
        self._bytes += length
        if self._bytes > self._budget:
            raise TunnelViolation("byte budget exceeded")
        if stream_id in self._closed:
            return False
        if stream_id not in self._position:
            raise TunnelViolation("DATA for a stream never opened")
        if stream_id in self._ended:
            raise TunnelViolation("DATA after END")
        if offset != self._position[stream_id] or offset + length > self._size:
            raise TunnelViolation("DATA offset out of order or past the source")
        self._position[stream_id] = offset + length
        return True

    def end(self, stream_id: int) -> bool:
        if stream_id in self._closed:
            return False
        if stream_id not in self._position:
            raise TunnelViolation("END for a stream never opened")
        if stream_id in self._ended:
            raise TunnelViolation("a second END")
        self._ended.add(stream_id)
        return True


# -- START -----------------------------------------------------------------------------------------------------------

@dataclass(frozen=True)
class TunnelWindow:
    """One window: in START the plan, in RESULT what was produced."""
    index: int
    start_s: float
    duration_s: float

    def __post_init__(self) -> None:
        if not _is_int(self.index) or not 0 <= self.index < WINDOW_INDEX_LIMIT:
            raise TunnelViolation("window index out of range")
        if not _finite(self.start_s) or self.start_s < 0 or not _finite(self.duration_s) or self.duration_s <= 0:
            raise TunnelViolation("window times out of range")


@dataclass(frozen=True)
class TunnelStart:
    """The request. `container`, `duration_known` and `windows` belong to `media.audio_windows` only (a probe keeps the
    defaults). A pass (`duration_known` False) takes a contiguous run of at most MEDIA_PASS_MAX_WINDOWS windows."""
    op: SandboxOp
    source_bytes: int
    budget_bytes: int
    wall_s: float
    container: str = ""
    duration_known: bool = True
    windows: Tuple[TunnelWindow, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.op, SandboxOp) or self.op not in TUNNEL_OPS:
            raise TunnelViolation("not a tunnel op")
        if not _is_int(self.budget_bytes) or not 1 <= self.budget_bytes <= tunnel_budget(self.source_bytes):
            raise TunnelViolation("budget out of range")
        if not _finite(self.wall_s) or not 0 < self.wall_s <= MEDIA_CEILING.wall_s:
            raise TunnelViolation("wall out of range")
        if not isinstance(self.duration_known, bool) or not isinstance(self.windows, tuple):
            raise TunnelViolation("bad START field types")
        if self.op is SandboxOp.MEDIA_PROBE:
            if self.container or self.windows or not self.duration_known:
                raise TunnelViolation("a probe takes no window fields")
            return
        _check_container(self.container)
        if not 1 <= len(self.windows) <= MEDIA_MAX_WINDOWS:
            raise TunnelViolation("window count out of range")
        if not all(isinstance(w, TunnelWindow) for w in self.windows):
            raise TunnelViolation("bad window")
        indexes = [w.index for w in self.windows]
        if len(set(indexes)) != len(indexes):
            raise TunnelViolation("duplicate window index")
        if not self.duration_known and (len(self.windows) > MEDIA_PASS_MAX_WINDOWS
                                        or indexes != list(range(indexes[0], indexes[0] + len(indexes)))):
            raise TunnelViolation("a pass takes a contiguous run of at most MEDIA_PASS_MAX_WINDOWS windows")

    @property
    def window_indexes(self) -> FrozenSet[int]:
        return frozenset(w.index for w in self.windows)


_PROBE_START_KEYS = frozenset({"source_bytes", "budget_bytes", "wall_s"})
_START_KEYS: Dict[SandboxOp, FrozenSet[str]] = {
    SandboxOp.MEDIA_PROBE: _PROBE_START_KEYS,
    SandboxOp.MEDIA_AUDIO_WINDOWS: _PROBE_START_KEYS | {"container", "duration_known", "windows"},
}


def start_keys(op: SandboxOp) -> FrozenSet[str]:
    """The closed key set of START for `op` (a test pins it: no url, name, id or path)."""
    if op not in _START_KEYS:
        raise TunnelViolation("not a tunnel op")
    return _START_KEYS[op]


def encode_start(start: TunnelStart) -> bytes:
    obj: Dict[str, Any] = {
        "source_bytes": start.source_bytes, "budget_bytes": start.budget_bytes, "wall_s": start.wall_s,
    }
    if start.op is SandboxOp.MEDIA_AUDIO_WINDOWS:
        obj["container"] = start.container
        obj["duration_known"] = start.duration_known
        obj["windows"] = [[w.index, w.start_s, w.duration_s] for w in start.windows]
    return _dump(obj, START_MAX_BYTES)


def decode_start(payload: bytes, op: SandboxOp) -> TunnelStart:
    obj = _load(payload, START_MAX_BYTES)
    if set(obj) != start_keys(op):
        raise TunnelViolation("START keys outside the closed schema")
    if not _is_int(obj["source_bytes"]) or not _is_int(obj["budget_bytes"]) or not _finite(obj["wall_s"]):
        raise TunnelViolation("bad START field types")
    if op is SandboxOp.MEDIA_PROBE:
        return TunnelStart(op=op, source_bytes=obj["source_bytes"], budget_bytes=obj["budget_bytes"],
                           wall_s=float(obj["wall_s"]))
    raw = obj["windows"]
    if not isinstance(raw, list) or not isinstance(obj["container"], str):
        raise TunnelViolation("bad START field types")
    windows = []
    for item in raw:
        if not isinstance(item, list) or len(item) != 3:
            raise TunnelViolation("a window is [index, start_s, duration_s]")
        windows.append(_window(item[0], item[1], item[2]))
    return TunnelStart(op=op, source_bytes=obj["source_bytes"], budget_bytes=obj["budget_bytes"],
                       wall_s=float(obj["wall_s"]), container=obj["container"],
                       duration_known=obj["duration_known"], windows=tuple(windows))


# -- RESULT ----------------------------------------------------------------------------------------------------------

@dataclass(frozen=True)
class TunnelProbe:
    """What `media.probe` learned. An unknown duration is 0."""
    duration_s: float
    duration_known: bool
    has_audio: bool
    container: str

    def __post_init__(self) -> None:
        if not isinstance(self.duration_known, bool) or not isinstance(self.has_audio, bool):
            raise TunnelViolation("bad probe field types")
        if not _finite(self.duration_s) or self.duration_s < 0:
            raise TunnelViolation("probe duration out of range")
        if self.duration_known != (self.duration_s > 0):
            raise TunnelViolation("a known duration is positive, an unknown one is 0")
        _check_container(self.container)


@dataclass(frozen=True)
class TunnelWindows:
    """What `media.audio_windows` produced: `status` OK, or TIMEOUT with the windows finished before the wall.
    `duration_s` is the recording's end, set only with `eof`; `reached_bound` only in a pass that did not hit the end."""
    status: SandboxStatus
    windows: Tuple[TunnelWindow, ...]
    eof: bool
    duration_s: float
    reached_bound: bool
    detail: str = ""

    def __post_init__(self) -> None:
        if self.status not in WINDOWS_STATUSES:
            raise TunnelViolation("windows status out of range")
        if (not isinstance(self.windows, tuple) or not all(isinstance(w, TunnelWindow) for w in self.windows)
                or not isinstance(self.eof, bool) or not isinstance(self.reached_bound, bool)):
            raise TunnelViolation("bad windows field types")
        indexes = [w.index for w in self.windows]
        if len(set(indexes)) != len(indexes):
            raise TunnelViolation("duplicate window index")
        if not _finite(self.duration_s):
            raise TunnelViolation("duration out of range")
        if self.eof and not 0 < self.duration_s <= MEDIA_MAX_SECONDS + 1:
            raise TunnelViolation("duration out of range")
        if not self.eof and self.duration_s != 0:
            raise TunnelViolation("a duration comes only with eof")
        if self.eof and self.reached_bound:
            raise TunnelViolation("eof and reached_bound together")
        _check_detail(self.detail)


@dataclass(frozen=True)
class TunnelResult:
    """The verdict. `media` is set exactly when `status` is OK: a TunnelProbe for `media.probe`, TunnelWindows for
    `media.audio_windows`. Any other status carries only `detail` (`error/tunnel_bounds`: the server saw a violation)."""
    status: SandboxStatus
    detail: str = ""
    media: Optional[Union[TunnelProbe, TunnelWindows]] = None

    def __post_init__(self) -> None:
        if self.status not in RESULT_STATUSES:
            raise TunnelViolation("result status out of range")
        if (self.media is not None) != (self.status is SandboxStatus.OK):
            raise TunnelViolation("media comes with status ok, and only then")
        _check_detail(self.detail)


_RESULT_KEYS = frozenset({"status", "detail"})
_PROBE_KEYS = frozenset({"duration_s", "duration_known", "has_audio", "container"})
_WINDOWS_KEYS = frozenset({"status", "windows", "eof", "duration_s", "reached_bound", "detail"})
_WINDOW_KEYS = frozenset({"index", "start_s", "duration_s"})


def check_result(result: TunnelResult, start: TunnelStart) -> None:
    """The result answers this request: the media shape of its op, windows from the requested set only,
    `reached_bound` only for a pass."""
    media = result.media
    if media is None:
        return
    if start.op is SandboxOp.MEDIA_PROBE:
        if not isinstance(media, TunnelProbe):
            raise TunnelViolation("a probe result carries probe media")
        return
    if not isinstance(media, TunnelWindows):
        raise TunnelViolation("a windows result carries windows media")
    asked = start.window_indexes
    if any(w.index not in asked for w in media.windows):
        raise TunnelViolation("a window outside the request")
    if media.reached_bound and start.duration_known:
        raise TunnelViolation("reached_bound is a pass's")


def encode_result(result: TunnelResult, start: TunnelStart) -> bytes:
    check_result(result, start)
    obj: Dict[str, Any] = {"status": result.status.value, "detail": result.detail}
    media = result.media
    if isinstance(media, TunnelProbe):
        obj["media"] = {"duration_s": media.duration_s, "duration_known": media.duration_known,
                        "has_audio": media.has_audio, "container": media.container}
    elif isinstance(media, TunnelWindows):
        obj["media"] = {
            "status": media.status.value, "eof": media.eof, "duration_s": media.duration_s,
            "reached_bound": media.reached_bound, "detail": media.detail,
            "windows": [{"index": w.index, "start_s": w.start_s, "duration_s": w.duration_s} for w in media.windows],
        }
    return _dump(obj, RESULT_MAX_BYTES)


def decode_result(payload: bytes, start: TunnelStart) -> TunnelResult:
    obj = _load(payload, RESULT_MAX_BYTES)
    keys = set(obj)
    if keys != _RESULT_KEYS and keys != _RESULT_KEYS | {"media"}:
        raise TunnelViolation("RESULT keys outside the closed schema")
    status = _status(obj["status"])
    detail = obj["detail"]
    if not isinstance(detail, str):
        raise TunnelViolation("bad RESULT field types")
    media: Optional[Union[TunnelProbe, TunnelWindows]] = None
    if "media" in obj:
        raw = obj["media"]
        if not isinstance(raw, dict):
            raise TunnelViolation("media must be an object")
        media = _probe_media(raw) if start.op is SandboxOp.MEDIA_PROBE else _windows_media(raw)
    result = TunnelResult(status=status, detail=detail, media=media)
    check_result(result, start)
    return result


def result_from_child(child: Mapping[str, Any], start: TunnelStart) -> TunnelResult:
    """The media child's result JSON (`drive_parser.media.run_probe` / `run_windows`) as the RESULT, with the checks
    the in-process extractor made: child `error` -> ERROR with its detail; a probe's `timeout` -> TIMEOUT; windows `ok`
    or `timeout` -> OK with TunnelWindows (`eof` only when true, `duration_s` only with `eof`, `reached_bound` only for a
    pass that did not hit the end). Anything else is a violation. The window files themselves are the engine's to
    check."""
    status = child.get("status")
    detail = child.get("detail", "")
    detail = (detail if isinstance(detail, str) else "")[:CHILD_DETAIL_MAX_CHARS]
    if status == "error":
        return TunnelResult(status=SandboxStatus.ERROR, detail=detail)
    if status not in ("ok", "timeout"):
        raise TunnelViolation("child status out of range")
    if start.op is SandboxOp.MEDIA_PROBE:
        if status == "timeout":
            return TunnelResult(status=SandboxStatus.TIMEOUT, detail=detail)
        known: Any = child.get("duration_known")
        duration: Any = child.get("duration_s")
        has_audio: Any = child.get("has_audio")
        container: Any = child.get("container")
        if not isinstance(known, bool) or not _finite(duration) or not isinstance(container, str):
            raise TunnelViolation("child probe result has an invalid shape")
        probe = TunnelProbe(duration_s=float(duration) if known else 0.0, duration_known=known,
                            has_audio=has_audio, container=container[:CONTAINER_MAX_CHARS])
        return TunnelResult(status=SandboxStatus.OK, media=probe)
    items = child.get("windows")
    if not isinstance(items, list):
        raise TunnelViolation("child windows missing")
    windows = []
    for item in items:
        if not isinstance(item, dict):
            raise TunnelViolation("child window entry has an invalid shape")
        windows.append(_window(item.get("index"), item.get("start_s"), item.get("duration_s")))
    eof = child.get("eof") is True
    reached = child.get("reached_bound") is True and not start.duration_known and not eof
    total: Any = child.get("duration_s") if eof else 0.0
    if not _finite(total):
        raise TunnelViolation("child duration out of range")
    media = TunnelWindows(
        status=SandboxStatus.OK if status == "ok" else SandboxStatus.TIMEOUT, windows=tuple(windows), eof=eof,
        duration_s=float(total), reached_bound=reached, detail=detail if status == "timeout" else "")
    result = TunnelResult(status=SandboxStatus.OK, media=media)
    check_result(result, start)
    return result


# -- engine <-> server route -----------------------------------------------------------------------------------------

@dataclass(frozen=True)
class TunnelEngineResult:
    """What the media engine hands the server route after the child ended. `windows` are (index, path) of the copies in
    a server-owned directory, streamed as WINDOW frames in this order before the RESULT; their indexes are the
    RESULT's windows exactly. The route removes `output_dir` once streamed. `opens` and `bytes_in` are the ledger's."""
    result: TunnelResult
    windows: Tuple[Tuple[int, str], ...]
    output_dir: Optional[str]
    peak_rss_bytes: int
    wall_s: float
    opens: int
    bytes_in: int

    def __post_init__(self) -> None:
        media = self.result.media
        reported = [w.index for w in media.windows] if isinstance(media, TunnelWindows) else []
        if [index for index, _ in self.windows] != reported:
            raise TunnelViolation("window files differ from the RESULT's windows")


class TunnelLink(Protocol):
    """The engine's end of the tunnel, as the server route sees it.

    `send`: a DATA or END frame of the body, delivered to the child after the engine's ledger check (a violation raises
    TunnelViolation: the engine kills the child and `wait()` reports `error/tunnel_bounds`).
    `receive`: the child's next OPEN or CLOSE as a frame for the response; None once the child's side has ended."""

    async def send(self, frame: Frame) -> None: ...

    async def receive(self) -> Optional[Frame]: ...


class TunnelRun(Protocol):
    """One running child. `wait()` returns when it has ended and its windows are collected; cancelling `wait()` kills
    the child and removes what it left."""

    @property
    def link(self) -> TunnelLink: ...

    async def wait(self) -> TunnelEngineResult: ...


@runtime_checkable
class SandboxTunnelEngine(Protocol):
    async def start(self, workdir: str, limits: SandboxLimits, start: TunnelStart) -> TunnelRun: ...


# -- helpers ---------------------------------------------------------------------------------------------------------

def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _finite(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _check_container(container: Any) -> None:
    if (not isinstance(container, str) or len(container) > CONTAINER_MAX_CHARS
            or not _CONTAINER_RE.fullmatch(container)):
        raise TunnelViolation("container missing or invalid")


def _check_detail(detail: Any) -> None:
    if not isinstance(detail, str) or len(detail) > DETAIL_MAX_CHARS:
        raise TunnelViolation("detail invalid or too long")


def _window(index: Any, start_s: Any, duration_s: Any) -> TunnelWindow:
    if not _is_int(index) or not _finite(start_s) or not _finite(duration_s):
        raise TunnelViolation("bad window field types")
    return TunnelWindow(index=index, start_s=float(start_s), duration_s=float(duration_s))


def _status(value: Any) -> SandboxStatus:
    try:
        return SandboxStatus(value)
    except ValueError:
        raise TunnelViolation("unknown status") from None


def _probe_media(raw: Dict[str, Any]) -> TunnelProbe:
    if set(raw) != _PROBE_KEYS:
        raise TunnelViolation("probe media keys outside the closed schema")
    if not _finite(raw["duration_s"]):
        raise TunnelViolation("probe duration out of range")
    return TunnelProbe(duration_s=float(raw["duration_s"]), duration_known=raw["duration_known"],
                       has_audio=raw["has_audio"], container=raw["container"])


def _windows_media(raw: Dict[str, Any]) -> TunnelWindows:
    if set(raw) != _WINDOWS_KEYS:
        raise TunnelViolation("windows media keys outside the closed schema")
    items = raw["windows"]
    if not isinstance(items, list) or not _finite(raw["duration_s"]):
        raise TunnelViolation("bad windows field types")
    windows = []
    for item in items:
        if not isinstance(item, dict) or set(item) != _WINDOW_KEYS:
            raise TunnelViolation("window keys outside the closed schema")
        windows.append(_window(item["index"], item["start_s"], item["duration_s"]))
    return TunnelWindows(status=_status(raw["status"]), windows=tuple(windows), eof=raw["eof"],
                         duration_s=float(raw["duration_s"]), reached_bound=raw["reached_bound"],
                         detail=raw["detail"])


def _refuse_constant(name: str) -> NoReturn:
    raise TunnelViolation(f"non-finite number {name}")


def _unique_pairs(pairs: Sequence[Tuple[str, Any]]) -> Dict[str, Any]:
    obj: Dict[str, Any] = {}
    for key, value in pairs:
        if key in obj:
            raise TunnelViolation("duplicate key")
        obj[key] = value
    return obj


def _load(payload: bytes, max_bytes: int) -> Dict[str, Any]:
    if not isinstance(payload, (bytes, bytearray)) or not 0 < len(payload) <= max_bytes:
        raise TunnelViolation("payload empty or too large")
    try:
        obj = json.loads(bytes(payload).decode("utf-8"), parse_constant=_refuse_constant,
                         object_pairs_hook=_unique_pairs)
    except TunnelViolation:
        raise
    except (ValueError, RecursionError):      # UnicodeDecodeError and JSONDecodeError are ValueErrors
        raise TunnelViolation("payload is not JSON") from None
    if not isinstance(obj, dict):
        raise TunnelViolation("payload must be a JSON object")
    return obj


def _dump(obj: Dict[str, Any], max_bytes: int) -> bytes:
    raw = json.dumps(obj, separators=(",", ":"), sort_keys=True, allow_nan=False).encode()   # numbers already checked
    if len(raw) > max_bytes:
        raise TunnelViolation("payload too large")
    return raw
