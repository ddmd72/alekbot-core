"""
Worker OIDC verifier
====================

In-app verification of the Google-signed OIDC token attached to `/worker`
invocations by Cloud Tasks and Cloud Scheduler.

Why in-app (not Cloud Run IAM): the service must stay `--allow-unauthenticated`
because the same Cloud Run service hosts public Slack/Telegram webhooks, OAuth
callbacks, the Cabinet UI and the remote MCP server. We cannot lock the whole
service down at the ingress, so the `/worker` route verifies the OIDC token
itself.

Identity-only check (no audience pinning): `/worker` is driven by several Google
callers whose token audiences are inconsistent — Cloud Tasks defaults the
audience to `<service_url>/worker`, while Cloud Scheduler jobs use the bare
Cloud Run URL (and `CLOUD_RUN_SERVICE_URL` is not reliably set). Pinning a single
audience would reject legitimate callers. They all share one service-account
identity, so the gate is: valid Google signature + `email == expected SA` +
`email_verified`. The real threat — anonymous internet POSTs (denial-of-wallet)
— carries no Google-signed token at all and is fully stopped by the signature +
SA-email check. Audience pinning would only additionally block a token minted by
the *same* SA for a different audience, which buys little here.

Enforcement is symmetric with the enqueue side (`GcpTaskQueue` attaches the OIDC
token only when `SERVICE_ACCOUNT_EMAIL` is set); the route enforces iff that same
value is set (see `main.py`). This module only performs the cryptographic check.
"""

from typing import Optional

from google.auth.transport import requests as google_requests
from google.oauth2 import id_token as google_id_token

from ..utils.logger import logger


def verify_worker_oidc(
    authorization_header: Optional[str],
    expected_sa_email: str,
) -> bool:
    """Return True iff the Authorization header carries a valid Google OIDC token
    issued for ``expected_sa_email``.

    Validates, via Google's public keys: signature, issuer and expiry; then that
    the ``email`` claim equals ``expected_sa_email`` and ``email_verified`` is
    truthy. Audience is intentionally NOT pinned (see module docstring).

    Never raises: any failure (missing/malformed header, bad signature, expired
    token, email mismatch) is logged and returned as ``False`` so the route can
    answer 401 cleanly.
    """
    if not authorization_header or not authorization_header.startswith("Bearer "):
        logger.warning("Worker OIDC: missing or malformed Authorization header")
        return False

    token = authorization_header[len("Bearer "):].strip()
    if not token:
        logger.warning("Worker OIDC: empty bearer token")
        return False

    try:
        # audience=None → skip audience verification; signature/issuer/expiry are
        # still enforced. Identity is checked via the email claim below.
        claims = google_id_token.verify_oauth2_token(
            token,
            google_requests.Request(),
        )
    except Exception as e:
        # verify_oauth2_token raises ValueError on any validation failure
        # (signature, expiry, issuer). Treat everything as a reject.
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
