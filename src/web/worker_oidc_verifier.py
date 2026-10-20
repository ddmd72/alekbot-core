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
audience would reject legitimate callers. They share a small set of
service-account identities (`parse_sa_emails`), so the gate is: valid Google signature + `email in accepted set` +
`email_verified`. The real threat — anonymous internet POSTs (denial-of-wallet)
— carries no Google-signed token at all and is fully stopped by the signature +
SA-email check. Audience pinning would only additionally block a token minted by
the *same* SA for a different audience, which buys little here.

Enforcement: a non-empty accepted set is verified; an empty set fails closed on Cloud
Run and opens only locally (`build_oidc_gate`; see `main.py`). This module only performs the cryptographic check.
"""

from typing import Awaitable, Callable, Collection, Optional

from google.auth.transport import requests as google_requests
from google.oauth2 import id_token as google_id_token

from ..utils.logger import logger


# Sentinel for "no identities" in a comma list kept in Secret Manager (which rejects an empty payload).
_EMPTY_SENTINEL = "-"

OidcGate = Callable[[Optional[str]], Awaitable[bool]]


def parse_sa_emails(*values: Optional[str]) -> frozenset[str]:
    """Union of comma-separated service-account email lists.

    Every item is stripped (a mounted secret may carry a trailing newline); empty items and the
    sentinel ``-`` are dropped; ``None`` contributes nothing.
    """
    emails: set[str] = set()
    for value in values:
        if not value:
            continue
        for item in value.split(","):
            item = item.strip()
            if item and item != _EMPTY_SENTINEL:
                emails.add(item)
    return frozenset(emails)


def build_oidc_gate(
    accepted_emails: Collection[str],
    audiences: Optional[Collection[str]] = None,
    *,
    on_cloud_run: bool = False,
    label: str = "Worker OIDC",
) -> OidcGate:
    """Async gate over ``verify_worker_oidc`` with the shared fail-closed rule.

    Non-empty set: verify against it. Empty set on Cloud Run: refuse every call (ERROR logged once,
    here). Empty set locally: open, so manual curl triggers keep working (CLAUDE.md "Manual Triggers").
    """
    accepted = frozenset(accepted_emails)
    if accepted:
        async def verify(authorization: Optional[str]) -> bool:
            return verify_worker_oidc(authorization, accepted, audiences)
        return verify

    if on_cloud_run:
        logger.error(f"[{label}] no accepted service-account identity is configured on Cloud Run: "
                     "every call is refused")

        async def closed(authorization: Optional[str]) -> bool:
            return False
        return closed

    async def open_for_local_dev(authorization: Optional[str]) -> bool:
        return True
    return open_for_local_dev


def verify_worker_oidc(
    authorization_header: Optional[str],
    accepted_emails: Collection[str],
    audiences: Optional[Collection[str]] = None,
) -> bool:
    """Return True iff the Authorization header carries a valid Google OIDC token
    issued for one of ``accepted_emails``.

    Validates, via Google's public keys: signature, issuer and expiry; then that
    the ``email`` claim is in ``accepted_emails`` and ``email_verified`` is
    truthy. An empty set accepts nothing; a bare ``str`` is a programming error
    (it would be compared character by character) and raises ``TypeError``. Audience is NOT pinned by default (see module docstring). A caller with
    a single, known token source passes ``audiences``: the ``aud`` claim must then
    be one of them. The drive indexer does (USER_DRIVE_INDEXING_RFC §4, RFC Δ21):
    its only caller is Cloud Tasks, whose default audience is the task's target URL.

    Never raises on a bad token: any failure (missing/malformed header, bad signature, expired
    token, email mismatch) is logged and returned as ``False`` so the route can
    answer 401 cleanly.
    """
    if isinstance(accepted_emails, str):
        raise TypeError("accepted_emails must be a collection of emails, not a str")
    if not accepted_emails:
        logger.warning("Worker OIDC: no accepted identities configured")
        return False

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

    email = claims.get("email")
    if email not in accepted_emails:
        # An SA email is not a secret; logging it makes a wrong identity diagnosable.
        logger.warning(f"Worker OIDC: email claim {email!r} is not an accepted service account")
        return False

    if not claims.get("email_verified"):
        logger.warning("Worker OIDC: email_verified is not set on the token")
        return False

    if audiences is not None and claims.get("aud") not in audiences:
        logger.warning("Worker OIDC: audience claim is not one this route accepts")
        return False

    return True
