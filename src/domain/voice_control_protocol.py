"""Protocol v1 of the call control channel, page <-> relay (VOICE_CALL_CONTROL_CHANNEL_RFC §4.2).

Pure: frame builders, the inbound frame parser, the token digest and the protocol's limits. The relay's hub
(`src/services/call_control_hub.py`) and `/ctl` handler (`src/handlers/call_control_handler.py`) use them; the page
(`src/web/static/call.html`) speaks the same frames.

Frames are JSON text objects, each with a string `type`. Rules every later feature keeps:
- An unknown `type` is ignored by both sides (forward compatibility). A new capability is a new `features` entry in
  `welcome`, never a changed meaning of an existing message.
- The limits below apply to what the PAGE sends (`MAX_INBOUND_FRAME_BYTES`, `MAX_INBOUND_FRAMES_PER_S`). Relay -> page
  frames are not capped here: the live-text `snapshot` frame is up to 20 KB (VOICE_CALL_LIVE_TEXT_RFC §4.7).
- Relay -> page frames may carry more fields than the builders below set; a page ignores fields it does not know.
- Messages carry enums, `seq` numbers, booleans and, for the `text` feature, call content. A frame is never logged:
  `hello` holds the token and `line`/`snapshot` frames hold what the caller and Lelik said.

The `text` feature (VOICE_CALL_LIVE_TEXT_RFC §4.7, mode calls) is relay -> page only: `line` frames
(`set`/`append`/`end`) and one `snapshot` per (re)connect, each with `v` and the call's own `seq`, one counter shared by
both frame types. The page joins `append` texts verbatim, reads `end` as a status only and orders lines by the number
in `l<n>`. Nothing goes page -> relay, so the feature has no `FEATURE_COMMANDS` entry.
"""
import hashlib
import json
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict, FrozenSet, Iterable, Mapping, Optional, Sequence

from .geo_mark import GeoFix, GeoSource

PROTOCOL_VERSION = 1

# Limits on page -> relay traffic (RFC §4.2, §4.3).
MAX_INBOUND_FRAME_BYTES = 1024
MAX_INBOUND_FRAMES_PER_S = 10
HELLO_TIMEOUT_S = 5.0
# The page pings this often; the relay answers `pong` (keeps proxies and Cloud Run from idling the socket).
PING_INTERVAL_S = 20.0

# Feature names offered in `welcome.features`. Only features a call actually serves are offered.
FEATURE_ASK = "ask"  # slice 4: the "Ask Lelik" latch, interpreter mode only (RFC §5)
FEATURE_GEO = "geo"  # the "share my position" button, every web call with a control link (GEO_MARKS_RFC §4.7)
FEATURE_TEXT = "text"  # live call text, mode calls only (VOICE_CALL_LIVE_TEXT_RFC §4.7); relay -> page only

# "Ask Lelik" frames (RFC §4.2): the page presses (`ask_arm {seq}`) or presses again before speaking
# (`ask_disarm {seq}`); the relay answers every transition with `ask_state {state, seq?, reason?}`.
ASK_ARM = "ask_arm"
ASK_DISARM = "ask_disarm"
ASK_STATE = "ask_state"

# Geo frames (GEO_MARKS_RFC §4.7): the page sends one fix (`geo {seq, latitude, longitude, accuracy}`); the relay answers
# with `geo_state {seq?, ok, address?, accuracy?, reason?}`: ok with the nearest address (absent when none was found),
# or not ok with a reason (`bad`, `rate`, `not_ready`).
GEO = "geo"
GEO_STATE = "geo_state"
GEO_REASON_BAD = "bad"
GEO_REASON_RATE = "rate"
GEO_REASON_NOT_READY = "not_ready"

# Live text frames, relay -> page (VOICE_CALL_LIVE_TEXT_RFC §4.7).
LINE = "line"
SNAPSHOT = "snapshot"
LINE_SET = "set"        # a caller line's whole text ("as heard")
LINE_APPEND = "append"  # more of a Lelik line, joined to what it has verbatim
LINE_END = "end"        # a Lelik line is finished: `status` done or interrupted
WHO_CALLER = "caller"
WHO_LELIK = "lelik"
LINE_DONE = "done"
LINE_INTERRUPTED = "interrupted"
KIND_ASK = "ask"  # both lines of an "Ask Lelik" exchange

# Page -> relay command types each feature accepts; the hub queues only these for the session (slice 4 onward).
# Everything else the page sends, apart from the core frames, is ignored.
FEATURE_COMMANDS: Mapping[str, FrozenSet[str]] = {
    FEATURE_ASK: frozenset({ASK_ARM, ASK_DISARM}),
    FEATURE_GEO: frozenset({GEO}),
}

# Core frame types, handled by the `/ctl` handler itself and never queued.
HELLO = "hello"
WELCOME = "welcome"
BYE = "bye"
PING = "ping"
PONG = "pong"
# Page -> relay, no fields, once per call: the page has subscribed to Lelik's audio (its `/renegotiate` is done) and
# the channel is welcomed. A web call's greeting waits for it (capped), so the caller hears it from the first word.
# Accepted on every call whatever its features; a repeat is ignored. A relay that predates it ignores it as unknown.
LISTENING = "listening"


class ByeReason(str, Enum):
    """Why the relay closes a control socket (`bye {reason}`, then close)."""

    VERSION = "version"        # the page's `hello.v` is not one the relay speaks
    AUTH = "auth"              # the token names no live call on this relay process
    CALL_ENDED = "call_ended"  # the call's teardown closed the channel
    REPLACED = "replaced"      # a newer socket presented the same token


def control_token_digest(token: str) -> str:
    """Hex SHA-256 of a control token: the only form stored (ticket record) or held (relay hub).

    A plain hash suffices: the token is 32 random bytes (RFC §4.1), so no keyed digest is needed and the relay needs no
    new secret."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def parse_inbound(text: str) -> Optional[Dict[str, Any]]:
    """A page frame as a dict with a string `type`, or None when it is not one (bad JSON, not an object, no type).

    The caller enforces the size cap before calling this."""
    try:
        frame = json.loads(text)
    except ValueError:
        return None
    if not isinstance(frame, dict) or not isinstance(frame.get("type"), str):
        return None
    return frame


def hello_version(frame: Mapping[str, Any]) -> Optional[int]:
    """The page's protocol version from `hello.v`, or None when it is missing or not a positive integer."""
    version = frame.get("v")
    if isinstance(version, bool) or not isinstance(version, int) or version < 1:
        return None
    return version


def hello_token(frame: Mapping[str, Any]) -> Optional[str]:
    """The control token from `hello.token`, or None when it is missing, not a string or empty."""
    token = frame.get("token")
    return token if isinstance(token, str) and token else None


def welcome_frame(features: Iterable[str], state: Optional[Mapping[str, Any]] = None) -> Dict[str, Any]:
    """`welcome {v, features, ...state}`: the version the relay speaks, the features this call offers, and each
    feature's current state (slice 4 adds `ask: "idle"|"armed"|"active"`), so a reconnecting page re-syncs."""
    return {**(dict(state) if state else {}), "type": WELCOME, "v": PROTOCOL_VERSION, "features": sorted(features)}


def bye_frame(reason: ByeReason) -> Dict[str, Any]:
    return {"type": BYE, "reason": reason.value}


def pong_frame() -> Dict[str, Any]:
    return {"type": PONG}


def ask_state_frame(state: str, seq: Optional[int] = None, reason: Optional[str] = None) -> Dict[str, Any]:
    """`ask_state {state, seq?, reason?}`: `state` is idle|armed|active; `seq` echoes the page command it answers;
    `reason` says why it went idle (answered, cancelled, timeout, call_ended)."""
    frame: Dict[str, Any] = {"type": ASK_STATE, "state": state}
    if seq is not None:
        frame["seq"] = seq
    if reason is not None:
        frame["reason"] = reason
    return frame


def parse_geo_command(frame: Mapping[str, Any], now: Optional[datetime] = None) -> GeoFix:
    """The fix a `geo` frame carries, validated by `GeoFix.parse`; the page's `accuracy` is optional. `now` is the
    instant the fix counts as shared (the relay's clock when None). ValueError for anything else: a missing or
    non-numeric coordinate, a value out of range, a negative accuracy."""
    if frame.get("type") != GEO:
        raise ValueError("not a geo frame")
    shared_at = now if now is not None else datetime.now(timezone.utc)
    return GeoFix.parse(frame.get("latitude"), frame.get("longitude"), frame.get("accuracy"), shared_at,
                        GeoSource.LELIK_BUTTON)


def geo_state_frame(ok: bool, seq: Optional[int] = None, address: Optional[str] = None,
                    accuracy: Optional[float] = None, reason: Optional[str] = None) -> Dict[str, Any]:
    """`geo_state {ok, seq?, address?, accuracy?, reason?}`: the relay's answer to one `geo` frame. `seq` echoes the
    command; an ok answer carries the address when one was found and the fix's accuracy in metres when it had one."""
    frame: Dict[str, Any] = {"type": GEO_STATE, "ok": ok}
    if seq is not None:
        frame["seq"] = seq
    if address:
        frame["address"] = address
    if accuracy is not None:
        frame["accuracy"] = accuracy
    if reason is not None:
        frame["reason"] = reason
    return frame


def line_frame(seq: int, line: str, op: str, *, who: Optional[str] = None, text: Optional[str] = None,
               status: Optional[str] = None, kind: Optional[str] = None) -> Dict[str, Any]:
    """`line {v, seq, line, op, who?, text?, status?, kind?}`: one change to one line of the call text. `op` is
    set|append|end; an `end` frame carries `status` and no `who`. Keys left None are left out."""
    frame: Dict[str, Any] = {"v": PROTOCOL_VERSION, "seq": seq, "type": LINE, "line": line}
    if who is not None:
        frame["who"] = who
    frame["op"] = op
    for key, value in (("text", text), ("status", status), ("kind", kind)):
        if value is not None:
            frame[key] = value
    return frame


def snapshot_frame(seq: int, lines: Sequence[Mapping[str, Any]]) -> Dict[str, Any]:
    """`snapshot {v, seq, lines}`: the visible lines, oldest first, each `{line, who, text, status?, kind?}`. Sent on
    every (re)connect; it replaces the page's view."""
    return {"v": PROTOCOL_VERSION, "seq": seq, "type": SNAPSHOT, "lines": [dict(line) for line in lines]}


def command_seq(frame: Mapping[str, Any]) -> Optional[int]:
    """The page's `seq` of a command frame, or None when it is missing or not a non-negative integer."""
    seq = frame.get("seq")
    if isinstance(seq, bool) or not isinstance(seq, int) or seq < 0:
        return None
    return seq


def accepted_commands(features: Iterable[str]) -> FrozenSet[str]:
    """Page -> relay command types the offered features accept (the union of `FEATURE_COMMANDS`)."""
    accepted: FrozenSet[str] = frozenset()
    for feature in features:
        accepted = accepted | FEATURE_COMMANDS.get(feature, frozenset())
    return accepted


def encode(frame: Mapping[str, Any]) -> str:
    """A frame as the JSON text sent on the socket (compact, UTF-8 kept)."""
    return json.dumps(frame, separators=(",", ":"), ensure_ascii=False)
