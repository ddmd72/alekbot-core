"""Keyed digests of verify-only secrets, and log fingerprints (SECRETS_AT_REST_RFC §2.5).

A verify-only value (an MCP authorization code, a voice ticket, an invite code) is stored as its keyed digest, the doc
id where the raw value used to be; nothing stores the raw value. `purpose` separates the uses, so one value digested
for two purposes gives two unrelated digests.
"""
import hashlib
import hmac

_SEPARATOR = b"\x00"


def keyed_digest(key: bytes, purpose: str, value: str) -> str:
    """Hex HMAC-SHA256 under `key` of `purpose` ‖ 0x00 ‖ `value` (both UTF-8)."""
    message = purpose.encode("utf-8") + _SEPARATOR + value.encode("utf-8")
    return hmac.new(key, message, hashlib.sha256).hexdigest()


def fingerprint(value: str) -> str:
    """First 8 hex characters of sha256(`value`): log correlation only, never a lookup key."""
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:8]
