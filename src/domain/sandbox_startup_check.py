"""
Sandbox startup self-check (docs/10_rfcs/UNTRUSTED_CONTENT_SANDBOX_RFC.md §4.3, invariant I1: holds nothing).

Public interface:
    check_sandbox_environment(env, allowed) -> list[str]
        names of environment variables that look like a secret and are not allowlisted, plus any
        GOOGLE_APPLICATION_CREDENTIALS. Values are never returned.
"""
from __future__ import annotations

import re
from typing import List, Mapping

_SECRET_LIKE = re.compile(r"KEY|SECRET|TOKEN|PASSWORD|CREDENTIAL")
_ALWAYS_FORBIDDEN = "GOOGLE_APPLICATION_CREDENTIALS"


def check_sandbox_environment(env: Mapping[str, str], allowed: frozenset[str]) -> List[str]:
    """Return the offending variable names, sorted. Empty means the environment holds nothing secret.

    GOOGLE_APPLICATION_CREDENTIALS is a violation even if allowlisted: the service has no credentials file.
    """
    violations = []
    for name in env:
        if name == _ALWAYS_FORBIDDEN:
            violations.append(name)
        elif name not in allowed and _SECRET_LIKE.search(name.upper()):
            violations.append(name)
    return sorted(violations)
