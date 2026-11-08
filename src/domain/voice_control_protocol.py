"""Protocol v1 of the call control channel, page <-> relay (VOICE_CALL_CONTROL_CHANNEL_RFC §4.2).

Pure: frame builders, the inbound frame parser, the token digest and the protocol's limits. The relay's hub
(`src/services/call_control_hub.py`) and `/ctl` handler (`src/handlers/call_control_handler.py`) use them; the page
(`src/web/static/call.html`) speaks the same frames.

Frames are JSON text objects, each with a string `type`. Rules every later feature keeps:
- An unknown `type` is ignored by both sides (forward compatibility). A new capability is a new `features` entry in
  `welcome`, never a changed meaning of an existing message.
- The limits below apply to what the PAGE sends (`MAX_INBOUND_FRAME_BYTES`, `MAX_INBOUND_FRAMES_PER_S`). Relay -> page
  frames are not capped here: the live-text RFC's `snapshot` frame is up to 20 KB (VOICE_CALL_LIVE_TEXT_RFC §4.7).
- Relay -> page frames may carry more fields than the builders below set (live text adds `v` and a per-call `seq`
  to its `line`/`status`/`snapshot` frames); a page ignores fields it does not know.
- Messages carry enums, `seq` numbers and booleans. A frame is never logged: `hello` holds the token and future
  `line` frames hold call content.
"""
import hashlib
import json
from enum import Enum
from typing import Any, Dict, FrozenSet, Iterable, Mapping, Optional

PROTOCOL_VERSION = 1

# Limits on page -> relay traffic (RFC §4.2, §4.3).
MAX_INBOUND_FRAME_BYTES = 1024
MAX_INBOUND_FRAMES_PER_S = 10
HELLO_TIMEOUT_S = 5.0
# The page pings this often; the relay answers `pong` (keeps proxies and Cloud Run from idling the socket).
PING_INTERVAL_S = 20.0

# Feature names offered in `welcome.features`. Only features a call actually serves are offered.
FEATURE_ASK = "ask"  # slice 4: the "Ask Lelik" latch, interpreter mode only (RFC §5)

# "Ask Lelik" frames (RFC §4.2): the page presses (`ask_arm {seq}`) or presses again before speaking
# (`ask_disarm {seq}`); the relay answers every transition with `ask_state {state, seq?, reason?}`.
ASK_ARM = "ask_arm"
ASK_DISARM = "ask_disarm"
ASK_STATE = "ask_state"

# Page -> relay command types each feature accepts; the hub queues only these for the session (slice 4 onward).
# Everything else the page sends, apart from the core frames, is ignored.
FEATURE_COMMANDS: Mapping[str, FrozenSet[str]] = {
    FEATURE_ASK: frozenset({ASK_ARM, ASK_DISARM}),
}

# Core frame types, handled by the `/ctl` handler itself and never queued.
HELLO = "hello"
WELCOME = "welcome"
BYE = "bye"
PING = "ping"
PONG = "pong"


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
