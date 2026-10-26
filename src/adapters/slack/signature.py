"""Slack request signing: the one check of a request's signature, for every route Slack calls (events, interactivity).

Slack signs `v0:<timestamp>:<raw body>` with the app's signing secret (HMAC-SHA256) and sends the result in
`X-Slack-Signature` beside `X-Slack-Request-Timestamp`. The check is made on the raw bytes, before anything is parsed."""
import hashlib
import hmac
from typing import Mapping

from ...utils.logger import logger

TIMESTAMP_HEADER = "X-Slack-Request-Timestamp"
SIGNATURE_HEADER = "X-Slack-Signature"
# A request older (or newer) than this is a replay, whatever its signature.
MAX_AGE_S = 300


def _header(headers: Mapping[str, str], name: str) -> str:
    wanted = name.lower()
    for key, value in headers.items():
        if key.lower() == wanted:
            return value
    return ""


def verify_slack_signature(body: bytes, headers: Mapping[str, str], secret: str, now: float) -> bool:
    """Whether `body` was signed by Slack with `secret` within `MAX_AGE_S` seconds of `now` (epoch seconds).

    False for an empty secret (an unset secret would let anyone sign), a missing header, a timestamp that is not a number or
    is out of the window, and a signature that does not match. Header names are matched without regard to case. Nothing
    that reveals the signature or the secret is logged."""
    if not secret:
        logger.error("[SlackSignature] the signing secret is empty; every request is refused")
        return False
    timestamp = _header(headers, TIMESTAMP_HEADER)
    received = _header(headers, SIGNATURE_HEADER)
    if not timestamp or not received:
        logger.warning("[SlackSignature] a signature header is missing")
        return False
    try:
        age = abs(now - int(timestamp))
    except (ValueError, OverflowError):
        logger.warning("[SlackSignature] the timestamp is not a number")
        return False
    if age > MAX_AGE_S:
        logger.warning(f"[SlackSignature] the timestamp is {age:.0f} s off")
        return False
    expected = "v0=" + hmac.new(secret.encode(), b"v0:" + timestamp.encode() + b":" + body, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected.encode(), received.encode()):
        logger.warning("[SlackSignature] the signature does not match")
        return False
    return True
