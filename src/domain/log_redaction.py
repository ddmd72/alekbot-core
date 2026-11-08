"""Text from outside (a provider's error event, an exception message) made safe for a log line or an alert.

A realtime provider may echo the session payload it was sent, and a library may put a URL or a header into an
exception message. The relay holds a call's voice MCP bearer tokens (LELIK_VOICE_MCP_RFC §5.4: the token and the
`Authorization` header are never logged, on any side), so such text is redacted before it is logged: JWT-shaped
strings, `Bearer` values, any `authorization` field's value and every URL are replaced, and the result is cut short.
Pure: stdlib only.
"""
import re

REDACTED = "<redacted>"
URL_REDACTED = "<url>"
_MAX_CHARS = 300

# Three base64url segments, the first a JSON header (`{"` is `eyJ`): every JWT, signed or not.
_JWT = re.compile(r"eyJ[A-Za-z0-9_-]*\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]*")
_BEARER = re.compile(r"(?i)\bbearer\s+[^\s\"',}\]]+")
# `"authorization": "<anything>"` / `'authorization': '<anything>'` / `authorization=<anything>`.
_AUTH_FIELD = re.compile(r"(?i)(authorization[\"']?\s*[:=]\s*)(\"[^\"]*\"|'[^']*'|[^\s,}\]]+)")
_URL = re.compile(r"(?i)\b[a-z][a-z0-9+.-]*://[^\s\"'<>]+")


def redact_for_log(text: object, max_chars: int = _MAX_CHARS) -> str:
    """`str(text)` with tokens, `Bearer` values, `authorization` values and URLs replaced, cut at `max_chars`."""
    value = str(text)
    value = _JWT.sub(REDACTED, value)
    value = _BEARER.sub(f"Bearer {REDACTED}", value)
    value = _AUTH_FIELD.sub(lambda m: f"{m.group(1)}{REDACTED}", value)
    value = _URL.sub(URL_REDACTED, value)
    if len(value) > max_chars:
        value = value[:max_chars] + "…"
    return value
