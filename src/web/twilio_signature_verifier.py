"""
Twilio signature verifier
=========================

In-app verification of the `X-Twilio-Signature` header Twilio attaches to every
webhook it sends (`/voice/auth`, `/voice/answer`, `/voice/status`).

Why in-app (not Cloud Run IAM): identical reasoning to
`src/web/worker_oidc_verifier.py` — the service must stay
`--allow-unauthenticated` because the same Cloud Run service hosts public
Slack/Telegram webhooks, OAuth callbacks, the Cabinet UI and the remote MCP
server. Twilio cannot present a Google-signed OIDC token either, so the voice
routes verify Twilio's own HMAC-SHA1 signature themselves. Before this existed,
`/voice/auth` and `/voice/answer` accepted any POST from anyone — the only
unauthenticated external entry points in the codebase (every other one
validates: Slack signature, Telegram webhook secret, `/worker` OIDC).

Signature scheme (Twilio's own, implemented by `twilio.request_validator.
RequestValidator`): HMAC-SHA1 over the **full request URL including its query
string**, concatenated with every POST form parameter sorted by name. The URL
part is load-bearing here: the call ticket rides in the query string
(`/voice/answer?ticket=...`, `/voice/status?ticket=...`), so a caller cannot
swap the ticket without invalidating the signature.

This module performs the cryptographic check only. The token source and the
local-dev bypass policy (`TWILIO_AUTH_TOKEN` unset -> no verification, matching
`/worker`'s `SERVICE_ACCOUNT_EMAIL` bypass) live in main.py's wiring, and the
verifier reaches the routes as an injected async callable — same shape and same
testability reason as `worker_oidc_verifier`.
"""

from typing import Mapping, Optional

from twilio.request_validator import RequestValidator

from ..utils.logger import logger


def verify_twilio_signature(
    auth_token: str,
    url: str,
    form_params: Mapping[str, str],
    signature: Optional[str],
) -> bool:
    """Return True iff ``signature`` is a valid Twilio signature for this request.

    Args:
        auth_token: the Twilio account's auth token (the HMAC key).
        url: the absolute URL Twilio POSTed to, **including the query string**.
            This must be the externally visible URL, not the one Cloud Run's
            load balancer forwards internally — see
            `voice_webhook_app._external_url`.
        form_params: the POST body parameters as a flat mapping.
        signature: the `X-Twilio-Signature` header value.

    Never raises: any failure (missing signature, bad token, malformed input) is
    logged and returned as ``False`` so the route can answer 403 cleanly.
    """
    if not signature:
        logger.warning("Twilio signature: missing X-Twilio-Signature header")
        return False
    if not auth_token:
        logger.warning("Twilio signature: no auth token configured, cannot verify")
        return False

    try:
        validator = RequestValidator(auth_token)
        valid = validator.validate(url, dict(form_params), signature)
    except Exception as exc:
        logger.warning(f"Twilio signature: validation raised {exc}")
        return False

    if not valid:
        logger.warning(f"Twilio signature: invalid signature for {url}")
    return bool(valid)
