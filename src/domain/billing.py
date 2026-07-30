from dataclasses import dataclass, field as dataclass_field
from enum import Enum
from typing import Dict, TYPE_CHECKING, Optional, Tuple
from .language import LanguageCode
from datetime import date, datetime, timezone
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
    # Calendar day (UTC, "YYYY-MM-DD") the prev_daily_* snapshot belongs to.
    # None for accounts that never rotated. Without it a clock-driven report
    # cannot tell "yesterday" from "the last day that had activity".
    prev_daily_date: Optional[str] = None

    monthly_tokens: int = 0
    monthly_cost: float = 0.0
    monthly_reset_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    def usage_for_date(self, target: date) -> Tuple[int, float]:
        """Tokens/cost for a specific UTC calendar day, or (0, 0.0) if it was idle.

        The daily counters are rotated lazily — only on the first request of a new
        day — so a report that runs on the wall clock (the daily summary) must resolve
        them against an explicit date instead of trusting the raw snapshot:

          - prev_daily belongs to `prev_daily_date` (the day that ended at the last
            rotation). If that equals the target, it IS the target's total.
          - otherwise, if the live counter (`daily_reset_at`) still sits on the target,
            the target was the last active day and no rotation has happened since —
            its total lives in daily_*.
          - otherwise the target had no activity → (0, 0.0).

        This is correct even when the target day was idle but an earlier day was active
        (the bug: that earlier day's value used to be reported as the target's).
        """
        target_str = target.isoformat()
        if self.prev_daily_date == target_str:
            return self.prev_daily_tokens, self.prev_daily_cost
        if self.daily_reset_at and self.daily_reset_at.date() == target:
            return self.daily_tokens, self.daily_cost
        return 0, 0.0


# Daily spend that trips the budget alert. Advisory, NOT a gate: crossing it posts to
# the ops channel and execution continues (the hard monthly cap was dropped 2026-07-26).
# Sized against measured usage — a normal day is ~$3.75, so $5 flags an anomaly, not a
# busy morning.
DEFAULT_DAILY_COST_LIMIT = 5.0


class UsageIncrement(BaseModel):
    """Outcome of one usage increment — enough to detect a budget-limit crossing.

    Returned by the increment so the caller can alert without a second read: the
    transaction already held the account document.
    """
    daily_cost_before: float
    daily_cost_after: float
    daily_cost_limit: float

    @property
    def crossed_daily_limit(self) -> bool:
        """True only on the increment that took the day from under the limit to over it.

        Crossing, not "is over": this fires exactly once per day, so the alert cannot
        spam every subsequent request. It also means a counter that is already inflated
        above the limit stays quiet until the next daily rotation resets it.
        """
        return self.daily_cost_before < self.daily_cost_limit <= self.daily_cost_after


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
    daily_cost_limit: float = DEFAULT_DAILY_COST_LIMIT

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
# Claude: 0.1 (90% discount), OpenAI: 0.1 (90% discount), Gemini: 0.25 (75% discount).
# cache_write: multiplier for cache creation tokens. Claude: 1.25 (25% surcharge), others: 0.
_PRICING_PER_MILLION_TOKENS: Dict[str, Dict[str, float]] = {
    # --- Gemini ("latest" aliases resolve to current stable generation) ---
    # These two price a MOVING alias, so they go stale whenever Google promotes a new
    # generation behind it — silently UNDER-reporting spend. Corrected 2026-07-29 after
    # `make check-pricing` resolved the aliases live and both catalogs agreed:
    #   gemini-flash-lite-latest → gemini-3.5-flash-lite  ($0.10/$0.40 → $0.30/$2.50)
    #   gemini-flash-latest      → gemini-3.6-flash       ($1.50/$9.00 → $1.50/$7.50)
    # Re-run the audit after any Gemini generation bump; it compares the RESOLVED id.
    "gemini-flash-lite-latest":          {"input": 0.30,  "output": 2.50,  "cache_read": 0.25},
    "gemini-flash-latest":               {"input": 1.50,  "output": 7.50,  "cache_read": 0.25},
    "gemini-pro-latest":                 {"input": 2.00,  "output": 12.00, "cache_read": 0.25},
    "gemini-3-flash-preview":            {"input": 0.50,  "output": 3.00,  "cache_read": 0.25},
    "deep-research-pro-preview-12-2025": {"input": 1.25,  "output": 10.00, "cache_read": 0.25},
    "models/gemini-3-pro-preview":       {"input": 2.00,  "output": 12.00, "cache_read": 0.25},
    # --- Claude (Opus 4.8 for ULTRA tier from 2026-05-30; same pricing as 4.7) ---
    "claude-haiku-4-5-20251001":         {"input": 1.00,  "output": 5.00,  "cache_read": 0.10, "cache_write": 1.25},
    "claude-sonnet-4-6":                 {"input": 3.00,  "output": 15.00, "cache_read": 0.10, "cache_write": 1.25},
    # Sonnet 5 (PERFORMANCE tier default from 2026-07): standard $3/$15 (same as 4.6). Intro
    # pricing is $2/$10 through 2026-08-31 — we track standard list price (conservative; correct
    # after the promo ends) so cost is never under-reported.
    # Consequence, measured 2026-07-29: Sonnet spend reads 1.5x high until 2026-09-01 (July
    # volume = $16.19 actual vs $24.28 reported). Deliberate; `make check-pricing` knows this
    # policy via price_consensus.HOLD_FINAL_PRICE and will not report it as drift.
    "claude-sonnet-5":                   {"input": 3.00,  "output": 15.00, "cache_read": 0.10, "cache_write": 1.25},
    "claude-opus-4-6":                   {"input": 5.00,  "output": 25.00, "cache_read": 0.10, "cache_write": 1.25},
    "claude-opus-4-8":                   {"input": 5.00,  "output": 25.00, "cache_read": 0.10, "cache_write": 1.25},
    # --- OpenAI GPT-5.6 family (Luna/Terra/Sol, GA 2026-07-09) — active tier defaults ---
    # cache_write 1.25: GPT-5.6 bills cache writes at 1.25x uncached input (new vs 5.4/5.5 = free).
    # NOTE: only charged if usage surfaces cache-write tokens — verify extraction (RFC §3.4).
    "gpt-5.6-luna":                      {"input": 1.00,  "output": 6.00,  "cache_read": 0.10, "cache_write": 1.25},
    "gpt-5.6-terra":                     {"input": 2.50,  "output": 15.00, "cache_read": 0.10, "cache_write": 1.25},
    "gpt-5.6-sol":                       {"input": 5.00,  "output": 30.00, "cache_read": 0.10, "cache_write": 1.25},
    # --- OpenAI (gpt-5.4 family, Mar 2026; gpt-5.5-pro retained for rollback/history) ---
    "gpt-5.4-nano":                      {"input": 0.20,  "output": 1.25,  "cache_read": 0.10},
    "gpt-5.4-mini":                      {"input": 0.75,  "output": 4.50,  "cache_read": 0.10},
    "gpt-5.4":                           {"input": 2.50,  "output": 15.00, "cache_read": 0.10},
    "gpt-5.5-pro":                       {"input": 30.00, "output": 180.00, "cache_read": 0.10},
    # legacy model IDs (gpt-5 family, Aug–Dec 2025)
    "gpt-5.2":                           {"input": 1.75,  "output": 14.00, "cache_read": 0.10},
    "gpt-5-nano":                        {"input": 0.05,  "output": 0.40,  "cache_read": 0.10},
    "gpt-5-mini":                        {"input": 0.25,  "output": 2.00,  "cache_read": 0.10},
    "gpt-5":                             {"input": 1.25,  "output": 10.00, "cache_read": 0.10},
    "o4-mini-deep-research-2025-06-26":  {"input": 2.00,  "output": 8.00,  "cache_read": 0.10},
    "o3-deep-research-2025-06-26":       {"input": 10.00, "output": 40.00, "cache_read": 0.10},
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


@dataclass
class ModelUsage:
    """Usage accumulated on ONE model within an execution."""

    prompt_tokens: int = 0
    completion_tokens: int = 0
    cache_read_tokens: int = 0
    cache_creation_tokens: int = 0

    @property
    def total_tokens(self) -> int:
        """Billable token count: uncached input + output + both cache legs.

        Mirrors what the account counters track — cache reads and writes are billed
        (at a multiplier), so they belong in the total even though providers report
        them outside ``prompt_tokens``.
        """
        return (
            self.prompt_tokens
            + self.completion_tokens
            + self.cache_read_tokens
            + self.cache_creation_tokens
        )

    def cost(self, model: str) -> float:
        """Cost in USD for this usage priced on ``model``."""
        return calculate_cost(
            model=model,
            prompt_tokens=self.prompt_tokens,
            completion_tokens=self.completion_tokens,
            cache_read_tokens=self.cache_read_tokens,
            cache_creation_tokens=self.cache_creation_tokens,
        )


@dataclass
class TokenLedger:
    """Token accumulator for exactly ONE agent execution, keyed by model.

    Scoped per execution on purpose. An agent instance is a per-user singleton in the
    AgentCoordinator registry, and DelegationEngine dispatches a tool batch through
    ``asyncio.gather`` — so several executions of the same instance run concurrently.
    Accumulating on the instance therefore pooled unrelated executions and billed each
    one the running total (a 22-way fetch_url batch inflated the daily counter ~3.6x;
    found 2026-07-28). The ledger is held in a ContextVar by ``BaseAgent.process()``,
    which gives each execution — and each ``asyncio.gather`` child — its own instance.

    Per-model legs, not one bucket: an execution legitimately spans several models
    (Smart resolves its tier per request, WebSearch downgrades ``fetch_url`` to ECO,
    a provider failover re-serves the turn on the fallback model). Pricing the whole
    execution with the agent's default model under-reported Smart and over-reported
    ``fetch_url`` (TD-7, found 2026-07-30) — the price belongs to the model that ran.

    Mutable by design: ``_call_llm`` adds every turn's usage to the live ledger.
    """

    account_id: Optional[str] = None
    by_model: Dict[str, ModelUsage] = dataclass_field(default_factory=dict)

    def add(
        self,
        model: str,
        prompt_tokens: int = 0,
        completion_tokens: int = 0,
        cache_read_tokens: int = 0,
        cache_creation_tokens: int = 0,
    ) -> None:
        """Add one LLM turn's usage under the model that actually served it."""
        leg = self.by_model.setdefault(model, ModelUsage())
        leg.prompt_tokens += prompt_tokens
        leg.completion_tokens += completion_tokens
        leg.cache_read_tokens += cache_read_tokens
        leg.cache_creation_tokens += cache_creation_tokens

    @property
    def total_tokens(self) -> int:
        """Billable token count across every model this execution touched."""
        return sum(leg.total_tokens for leg in self.by_model.values())

    @property
    def is_empty(self) -> bool:
        """True when no usage accrued — nothing to bill."""
        return self.total_tokens == 0

    def cost(self) -> float:
        """Cost in USD: each model's usage priced at that model's rates, summed."""
        return round(
            sum(leg.cost(model) for model, leg in self.by_model.items()), 6
        )

    @property
    def dominant_model(self) -> Optional[str]:
        """The costliest model of the execution — a label, not the price basis.

        ``cost()`` already sums every leg; this only names the execution for the
        ``model`` argument of ``QuotaService.record_usage`` (which no implementation
        reads). Ties resolve to the first model added.
        """
        if not self.by_model:
            return None
        return max(self.by_model, key=lambda m: self.by_model[m].cost(m))
