from dataclasses import dataclass


@dataclass(frozen=True)
class AuthDecision:
    """Result of §4.6 identity resolution. No authorization level in v1 —
    a field with one value is a seam without a user (RFC §4.6)."""

    user_id: str
    account_id: str
