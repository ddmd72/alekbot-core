"""The answer of `drive_access_policy.decide` (MULTI_DRIVE_RFC §11.3)."""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class AccessDecision:
    allowed: bool
    line: str = ""     # the refusal, verbatim; "" when allowed


ALLOWED = AccessDecision(True)
