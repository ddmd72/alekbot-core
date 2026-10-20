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

from typing import Any, Awaitable, Callable, Collection, Optional

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
        # (signature, expiry, issuer). Treat everything as a reject. Log the class only: google-auth
        # messages can embed the token itself (jwt.py "Wrong number of segments in token: {token}").
        logger.warning(f"Worker OIDC: token verification failed: {type(e).__name__}")
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


_CERT_FETCH_TIMEOUT_S = 10


def _short_timeout_request() -> Callable[..., Any]:
    """A google-auth transport whose key fetch times out after ``_CERT_FETCH_TIMEOUT_S`` seconds."""
    base = google_requests.Request()

    def request(
        url: str,
        method: str = "GET",
        body: Any = None,
        headers: Any = None,
        timeout: float = _CERT_FETCH_TIMEOUT_S,
        **kwargs: Any,
    ) -> Any:
        return base(url, method=method, body=body, headers=headers, timeout=timeout, **kwargs)

    return request


def verify_oidc_identity(
    authorization_header: Optional[str],
    audiences: Collection[str],
) -> Optional[str]:
    """Return the verified ``email`` of a valid Google OIDC bearer token, else ``None``.

    Requires signature, issuer and expiry (via Google's public keys), a truthy
    ``email_verified``, a non-empty ``email`` and ``aud`` in ``audiences``. The caller
    decides what the identity may do (the sandbox gate maps it to op groups).

    Deliberately separate from ``verify_worker_oidc``: that function guards every
    Scheduler/Tasks job and stays untouched (sandbox RFC S-Δ9). Never raises; every
    failure is logged (exception type only: google-auth messages can embed the token) and
    returned as ``None``. A bare ``str`` for ``audiences`` is treated as one audience.

    Blocking: fetching Google's signing keys is synchronous network I/O, bounded here by
    ``_CERT_FETCH_TIMEOUT_S`` instead of google-auth's 120 s default; async callers should
    run it in a thread.
    """
    try:
        accepted = {audiences} if isinstance(audiences, str) else set(audiences)
        if not authorization_header or not authorization_header.startswith("Bearer "):
            logger.warning("OIDC identity: missing or malformed Authorization header")
            return None
        token = authorization_header[len("Bearer "):].strip()
        if not token:
            logger.warning("OIDC identity: empty bearer token")
            return None
        try:
            claims = google_id_token.verify_oauth2_token(token, _short_timeout_request())
        except Exception as e:
            logger.warning(f"OIDC identity: token verification failed: {type(e).__name__}")
            return None
        if not claims.get("email_verified"):
            logger.warning("OIDC identity: email_verified is not set on the token")
            return None
        aud = claims.get("aud")
        if not isinstance(aud, str) or aud not in accepted:
            logger.warning("OIDC identity: audience claim is not one this route accepts")
            return None
        email = claims.get("email")
        if not isinstance(email, str) or not email:
            logger.warning("OIDC identity: token carries no email claim")
            return None
        return email
    except Exception as e:  # defensive: the contract is "never raises"
        logger.warning(f"OIDC identity: unexpected failure: {type(e).__name__}")
        return None
