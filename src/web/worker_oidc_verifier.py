"""
Worker OIDC verifier
====================

In-app verification of the Google-signed OIDC token that Cloud Tasks attaches to
every `/worker` invocation.

Why in-app (not Cloud Run IAM): the service must stay `--allow-unauthenticated`
because the same Cloud Run service hosts public Slack/Telegram webhooks, OAuth
callbacks, the Cabinet UI and the remote MCP server. We cannot lock the whole
service down at the ingress, so the `/worker` route verifies the OIDC token
itself.

The enqueue side (`GcpTaskQueue`) attaches `oidc_token{service_account_email}`
only when `SERVICE_ACCOUNT_EMAIL` is configured. Verification is symmetric: the
caller enforces iff that same value is set (see `main.py`). This module only
performs the cryptographic check — it does not decide whether to enforce.
"""

from typing import Optional

from google.auth.transport import requests as google_requests
from google.oauth2 import id_token as google_id_token

from ..utils.logger import logger


def verify_worker_oidc(
    authorization_header: Optional[str],
    expected_sa_email: str,
    expected_audience: str,
) -> bool:
    """Return True iff the Authorization header carries a valid Google OIDC token.

    Validates, via Google's public keys:
      - signature, issuer (accounts.google.com) and expiry,
      - audience == ``expected_audience`` (Cloud Tasks defaults the OIDC audience
        to the target URL, i.e. ``<service_url>/worker``),
      - ``email`` claim == ``expected_sa_email`` and ``email_verified`` is truthy.

    Never raises: any failure (missing/malformed header, bad signature, expired
    token, audience/email mismatch) is logged and returned as ``False`` so the
    route can answer 401 cleanly.
    """
    if not authorization_header or not authorization_header.startswith("Bearer "):
        logger.warning("Worker OIDC: missing or malformed Authorization header")
        return False

    token = authorization_header[len("Bearer "):].strip()
    if not token:
        logger.warning("Worker OIDC: empty bearer token")
        return False

    try:
        claims = google_id_token.verify_oauth2_token(
            token,
            google_requests.Request(),
            audience=expected_audience,
        )
    except Exception as e:
        # verify_oauth2_token raises ValueError on any validation failure
        # (signature, audience, expiry, issuer). Treat everything as a reject.
        logger.warning(f"Worker OIDC: token verification failed: {e}")
        return False

    if claims.get("email") != expected_sa_email:
        logger.warning(
            "Worker OIDC: email claim does not match expected service account"
        )
        return False

    if not claims.get("email_verified"):
        logger.warning("Worker OIDC: email_verified is not set on the token")
        return False

    return True
