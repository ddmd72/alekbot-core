"""
Sandbox caller gate (UNTRUSTED_CONTENT_SANDBOX_RFC §4.4; plan G18, G21, G24).

`SandboxCallerGate` satisfies `sandbox_app.SandboxGate`. It verifies the Google ID token in the `Authorization` header
itself (never `X-Serverless-Authorization`), pins the audience to the service's own URL, and maps the verified email to
the op prefixes its group may call. It knows no op by name: it is built from `{group: emails}` and
`{group: op prefixes}`; adding a group later is configuration. The op arrives raw from the path (the app authorizes
before it validates the op), so its group key is the text before the first "." and anything odd is `forbidden_op`.

Fail closed on Cloud Run (`K_SERVICE` set) when the caller map or the URL is empty. With `K_SERVICE` unset and
nothing configured the gate is open (local development; `worker_oidc_gate` precedent), announced in one log line.
Email values are never logged.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Mapping, Optional

from ..utils.logger import logger
from .worker_oidc_verifier import verify_oidc_identity

UNAUTHENTICATED = "unauthenticated"
FORBIDDEN_OP = "forbidden_op"


@dataclass(frozen=True)
class GateDecision:
    allowed: bool
    reason: Optional[str] = None


_ALLOWED = GateDecision(True, None)
_UNAUTHENTICATED = GateDecision(False, UNAUTHENTICATED)
_FORBIDDEN = GateDecision(False, FORBIDDEN_OP)


class SandboxCallerGate:
    def __init__(
        self,
        caller_sets: Mapping[str, frozenset[str]],
        group_prefixes: Mapping[str, frozenset[str]],
        service_url: str,
    ) -> None:
        self._on_cloud_run = bool(os.getenv("K_SERVICE"))
        self._url = (service_url or "").strip().rstrip("/")
        self._by_email: dict[str, frozenset[str]] = {}
        for group, emails in caller_sets.items():
            prefixes = frozenset(group_prefixes.get(group, frozenset()))
            for email in emails:
                self._by_email[email.strip().lower()] = self._by_email.get(email.strip().lower(), frozenset()) | prefixes
        configured = bool(self._by_email) and bool(self._url)
        self._open = not self._on_cloud_run and not configured
        self._refuse_all = self._on_cloud_run and not configured
        if self._open:
            logger.warning("Sandbox gate: no caller map or URL and not on Cloud Run; gate is OPEN (local development)")
        elif self._refuse_all:
            logger.error("Sandbox gate: caller map or service URL is empty on Cloud Run; refusing every call")
        else:
            logger.info(f"Sandbox gate: enforcing for {len(caller_sets)} group(s), {len(self._by_email)} caller(s)")

    def authorize(self, authorization: Optional[str], op: str) -> GateDecision:
        if self._open:
            return _ALLOWED
        if self._refuse_all:
            return _UNAUTHENTICATED
        email = verify_oidc_identity(authorization, [self._url])
        if email is None:
            return _UNAUTHENTICATED
        prefixes = self._by_email.get(email.strip().lower())
        prefix = op.split(".", 1)[0] if isinstance(op, str) and "." in op else None
        if prefixes is None or prefix is None or prefix not in prefixes:
            logger.warning("Sandbox gate: verified caller is not allowed for this op")
            return _FORBIDDEN
        return _ALLOWED
