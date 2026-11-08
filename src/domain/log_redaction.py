"""Text from outside (a provider's error event, an exception message, a traceback) made safe for a log line or an alert.

A realtime provider may echo the session payload it was sent, and a library may put a URL or a header into an
exception message. The relay holds a call's voice MCP bearer tokens (LELIK_VOICE_MCP_RFC §5.4: the token and the
`Authorization` header are never logged, on any side), so such text is redacted before it is logged:
1. by value first: each of the call's exact tokens given in `secrets`, and each token's payload and signature segment
   on its own (a JWT header is public, so `payload.signature` alone rebuilds the token);
2. then by shape: JWT-shaped strings, any two long base64url runs joined by a dot (the backstop for a headerless
   token), `Bearer` values, any `authorization` field's value and every URL.
The result is cut short. Pure: stdlib only.
"""
import re
import traceback
from typing import Iterable, List

REDACTED = "<redacted>"
URL_REDACTED = "<url>"
_MAX_CHARS = 300
_TRACEBACK_MAX_CHARS = 4000
# A segment shorter than this is not scrubbed on its own: it would hit ordinary words.
_MIN_SEGMENT_CHARS = 8

# Three base64url segments, the first a JSON header (`{"` is `eyJ`): every JWT, signed or not.
_JWT = re.compile(r"eyJ[A-Za-z0-9_-]*\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]*")
# Backstop: a JWT without its header (`payload.signature`) or any similar dotted pair of long base64url runs.
_DOTTED_PAIR = re.compile(r"[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{20,}")
_BEARER = re.compile(r"(?i)\bbearer\s+[^\s\"',}\]]+")
# `"authorization": "<anything>"` / `'authorization': '<anything>'` / `authorization=<anything>`.
_AUTH_FIELD = re.compile(r"(?i)(authorization[\"']?\s*[:=]\s*)(\"[^\"]*\"|'[^']*'|[^\s,}\]]+)")
_URL = re.compile(r"(?i)\b[a-z][a-z0-9+.-]*://[^\s\"'<>]+")


def _secret_values(secrets: Iterable[str]) -> List[str]:
    """Each secret and, for a dotted token, its payload and signature segments; longest first, so a whole token is
    replaced before its parts."""
    values = set()
    for secret in secrets:
        if not isinstance(secret, str) or not secret:
            continue
        values.add(secret)
        segments = secret.split(".")
        for segment in segments[1:3] if len(segments) >= 3 else segments[1:]:
            if len(segment) >= _MIN_SEGMENT_CHARS:
                values.add(segment)
    return sorted(values, key=len, reverse=True)


def redact_for_log(text: object, max_chars: int = _MAX_CHARS, secrets: Iterable[str] = ()) -> str:
    """`str(text)` with the exact `secrets` (and their payload and signature segments), then tokens, `Bearer` values,
    `authorization` values and URLs replaced; cut at `max_chars`."""
    value = str(text)
    for secret in _secret_values(secrets):
        value = value.replace(secret, REDACTED)
    value = _JWT.sub(REDACTED, value)
    value = _DOTTED_PAIR.sub(REDACTED, value)
    value = _BEARER.sub(f"Bearer {REDACTED}", value)
    value = _AUTH_FIELD.sub(lambda m: f"{m.group(1)}{REDACTED}", value)
    value = _URL.sub(URL_REDACTED, value)
    if len(value) > max_chars:
        value = value[:max_chars] + "…"
    return value


def redact_exception(exc: BaseException, secrets: Iterable[str] = ()) -> str:
    """`Type: message`, the message redacted: an exception for a one-line log entry."""
    return f"{type(exc).__name__}: {redact_for_log(exc, secrets=secrets)}"


def redact_traceback(exc: BaseException, secrets: Iterable[str] = ()) -> str:
    """The exception's whole traceback (chained causes included), redacted like a message, cut at 4 000 characters:
    what `exc_info=True` would print, minus anything a message or a source line could carry."""
    text = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
    return redact_for_log(text, max_chars=_TRACEBACK_MAX_CHARS, secrets=secrets)
