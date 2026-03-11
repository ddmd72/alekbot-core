import logging
from enum import Enum
from typing import List, Dict, TYPE_CHECKING, Optional

_log = logging.getLogger(__name__)
from datetime import datetime, timezone
from uuid import uuid4
from pydantic import BaseModel, Field

# Avoid circular import: user.py imports billing.py
if TYPE_CHECKING:
    from .user import UserBotConfig


class AccountTier(str, Enum):
    FREE = "free"
    FAMILY = "family"
    PRO = "pro"
    ENTERPRISE = "enterprise"
    ADMIN = "admin"


class AccountUsageStats(BaseModel):
    """Account-level usage tracking for billing and quota enforcement."""
    total_requests: int = 0
    total_tokens: int = 0
    total_cost: float = 0.0

    daily_tokens: int = 0
    daily_cost: float = 0.0
    daily_reset_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    monthly_tokens: int = 0
    monthly_cost: float = 0.0
    monthly_reset_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class BillingAccount(BaseModel):
    """
    Billing account entity (tenant in multi-tenant architecture).
    Can contain multiple users with IAM-based role assignments.
    """
    account_id: str = Field(default_factory=lambda: f"account-{uuid4()}")
    tier: AccountTier = AccountTier.FREE
    usage: AccountUsageStats = Field(default_factory=AccountUsageStats)

    daily_token_limit: int = 100_000
    monthly_cost_limit: float = 50.0

    # ========================================================================
    # OAuth Multi-Tenant Session 1: IAM Policy & Configuration Inheritance
    # RFC: docs/10_rfcs/MULTI_TENANT_OAUTH_RFC.md
    # Purpose: Role-based access control and shared account configuration
    # ========================================================================
    iam_policy: Dict[str, str] = Field(default_factory=dict)  # user_id → role (owner, member, viewer)

    # ========================================================================
    # OAuth Multi-Tenant Session 2: Account defaults (shared config)
    # RFC: docs/10_rfcs/MULTI_TENANT_OAUTH_RFC.md
    # Circular import resolved via TYPE_CHECKING + Optional
    # ========================================================================
    account_defaults: Optional["UserBotConfig"] = None
    # Note: None means "use default UserBotConfig()" - populated during registration
    # Services should check: config = account.account_defaults or UserBotConfig()
    # Critical for family accounts (99% users don't override, use account defaults)

    # ========================================================================
    # REMOVED OAuth Multi-Tenant Session 1: Replaced by IAM policy
    # RFC: docs/10_rfcs/MULTI_TENANT_OAUTH_RFC.md
    # Reason: Single source of truth via iam_policy, denormalization removed
    # ========================================================================
    # owner_user_id: str = ""  # → Use iam_policy lookup (checked rarely, query OK)
    # member_user_ids: List[str] = []  # → Query UserProfile WHERE account_id = X

    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    is_active: bool = True


# ---------------------------------------------------------------------------
# Cost calculation (pure function, no I/O)
# ---------------------------------------------------------------------------

_PRICING_PER_MILLION_TOKENS: Dict[str, Dict[str, float]] = {
    # --- Gemini ("latest" aliases resolve to current stable generation) ---
    "gemini-flash-lite-latest":          {"input": 0.10,  "output": 0.40},   # 2.5 Flash-Lite
    "gemini-flash-latest":               {"input": 0.30,  "output": 2.50},   # 2.5 Flash
    "gemini-pro-latest":                 {"input": 1.25,  "output": 10.00},  # 2.5 Pro
    "gemini-3-flash-preview":            {"input": 0.50,  "output": 3.00},   # Gemini 3 Flash Preview (router fallback)
    "deep-research-pro-preview-12-2025": {"input": 1.25,  "output": 10.00},  # approx. Gemini Pro tier
    "models/gemini-3-pro-preview":       {"input": 2.50,  "output": 10.00},  # legacy entry
    # --- Claude ---
    "claude-haiku-4-5-20251001":         {"input": 1.00,  "output": 5.00},
    "claude-sonnet-4-6":                 {"input": 3.00,  "output": 15.00},
    "claude-opus-4-6":                   {"input": 5.00,  "output": 25.00},
    # --- OpenAI ---
    "gpt-5-nano":                        {"input": 0.05,  "output": 0.40},
    "gpt-5-mini":                        {"input": 0.25,  "output": 2.00},
    "gpt-5":                             {"input": 1.25,  "output": 10.00},
    "o4-mini-deep-research-2025-06-26":  {"input": 2.00,  "output": 8.00},
    "o3-deep-research-2025-06-26":       {"input": 10.00, "output": 40.00},
    # --- Grok ---
    "grok-4-1-fast-non-reasoning":       {"input": 0.20,  "output": 0.50},
    "grok-4-1-fast-reasoning":           {"input": 0.20,  "output": 0.50},
}


def calculate_cost(model: str, prompt_tokens: int, completion_tokens: int) -> float:
    """Calculate request cost in USD based on token counts."""
    pricing = _PRICING_PER_MILLION_TOKENS.get(model)
    if not pricing:
        _log.warning("Unknown model pricing for %s; cost set to 0.0", model)
        return 0.0
    cost = (
        (prompt_tokens / 1_000_000) * pricing["input"]
        + (completion_tokens / 1_000_000) * pricing["output"]
    )
    return round(cost, 6)
