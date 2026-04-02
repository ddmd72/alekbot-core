from enum import Enum
from typing import List, Dict, TYPE_CHECKING, Optional
from .language import LanguageCode
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

    prev_daily_tokens: int = 0
    prev_daily_cost: float = 0.0

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

    # Account-level default language (RFC: MULTILINGUAL_SUPPORT_RFC.md §5.2)
    # None = use system config default. Set directly in Firestore per account when needed.
    default_language: Optional[LanguageCode] = None

    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    is_active: bool = True


# ---------------------------------------------------------------------------
# Cost calculation (pure function, no I/O)
# ---------------------------------------------------------------------------

# cache_read: multiplier for cached input tokens vs full input price.
# Claude: 0.1 (90% discount), OpenAI: 0.5 (50% discount), Gemini: 0.25 (75% discount).
# cache_write: multiplier for cache creation tokens. Claude: 1.25 (25% surcharge), others: 0.
_PRICING_PER_MILLION_TOKENS: Dict[str, Dict[str, float]] = {
    # --- Gemini ("latest" aliases resolve to current stable generation) ---
    "gemini-flash-lite-latest":          {"input": 0.10,  "output": 0.40,  "cache_read": 0.25},
    "gemini-flash-latest":               {"input": 0.50,  "output": 3.00,  "cache_read": 0.25},
    "gemini-pro-latest":                 {"input": 2.00,  "output": 12.00, "cache_read": 0.25},
    "gemini-3-flash-preview":            {"input": 0.50,  "output": 3.00,  "cache_read": 0.25},
    "deep-research-pro-preview-12-2025": {"input": 1.25,  "output": 10.00, "cache_read": 0.25},
    "models/gemini-3-pro-preview":       {"input": 2.00,  "output": 12.00, "cache_read": 0.25},
    # --- Claude ---
    "claude-haiku-4-5-20251001":         {"input": 1.00,  "output": 5.00,  "cache_read": 0.10, "cache_write": 1.25},
    "claude-sonnet-4-6":                 {"input": 3.00,  "output": 15.00, "cache_read": 0.10, "cache_write": 1.25},
    "claude-opus-4-6":                   {"input": 5.00,  "output": 25.00, "cache_read": 0.10, "cache_write": 1.25},
    # --- OpenAI (gpt-5.4 family, Mar 2026) ---
    "gpt-5.4-nano":                      {"input": 0.20,  "output": 1.25,  "cache_read": 0.50},
    "gpt-5.4-mini":                      {"input": 0.75,  "output": 4.50,  "cache_read": 0.50},
    "gpt-5.4":                           {"input": 2.50,  "output": 15.00, "cache_read": 0.50},
    # legacy model IDs (gpt-5 family, Aug–Dec 2025)
    "gpt-5.2":                           {"input": 1.75,  "output": 14.00, "cache_read": 0.50},
    "gpt-5-nano":                        {"input": 0.05,  "output": 0.40,  "cache_read": 0.50},
    "gpt-5-mini":                        {"input": 0.25,  "output": 2.00,  "cache_read": 0.50},
    "gpt-5":                             {"input": 1.25,  "output": 10.00, "cache_read": 0.50},
    "o4-mini-deep-research-2025-06-26":  {"input": 2.00,  "output": 8.00,  "cache_read": 0.50},
    "o3-deep-research-2025-06-26":       {"input": 10.00, "output": 40.00, "cache_read": 0.50},
    # --- Grok ---
    "grok-4-1-fast-non-reasoning":       {"input": 0.20,  "output": 0.50},
    "grok-4-1-fast-reasoning":           {"input": 0.20,  "output": 0.50},
}


def calculate_cost(
    model: str,
    prompt_tokens: int,
    completion_tokens: int,
    cache_read_tokens: int = 0,
    cache_creation_tokens: int = 0,
) -> float:
    """Calculate request cost in USD based on token counts.

    cache_read_tokens:     tokens served from cache — multiplier per provider in pricing dict.
    cache_creation_tokens: tokens written to cache — Claude only (1.25× input).
    """
    pricing = _PRICING_PER_MILLION_TOKENS.get(model)
    if not pricing:
        return 0.0
    input_price = pricing["input"]
    cache_read_mult = pricing.get("cache_read", 0)
    cache_write_mult = pricing.get("cache_write", 0)
    cost = (
        (prompt_tokens / 1_000_000) * input_price
        + (completion_tokens / 1_000_000) * pricing["output"]
        + (cache_read_tokens / 1_000_000) * input_price * cache_read_mult
        + (cache_creation_tokens / 1_000_000) * input_price * cache_write_mult
    )
    return round(cost, 6)
