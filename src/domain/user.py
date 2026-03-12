from enum import Enum
from typing import Dict, List, Optional, Any
from datetime import datetime, timezone
from uuid import uuid4
from pydantic import BaseModel, Field

class UserTier(str, Enum):
    FREE = "free"
    PRO = "pro"
    ENTERPRISE = "enterprise"
    ADMIN = "admin"

class LLMProvider(str, Enum):
    GEMINI = "gemini"
    OPENAI = "openai"
    ANTHROPIC = "anthropic"
    GROK = "grok"


# ============================================================================
# NEW Provider Refactor Session 2: Performance tier abstraction
# Plan: docs/architecture/provider_refactor/PROVIDER_REFACTOR_EXECUTION_PLAN.md
# Purpose: Decouple agent performance requirements from model strings
# RFC: docs/architecture/rfcs/PROVIDER_SPLIT_RFC.md Section 2
# ==========================================================================
class PerformanceTier(str, Enum):
    ECO = "eco"
    BALANCED = "balanced"
    PERFORMANCE = "performance"


# ============================================================================
# NEW Provider Refactor Session 3: Prompt preferences abstraction
# Plan: docs/architecture/provider_refactor/PROVIDER_REFACTOR_EXECUTION_PLAN.md
# Purpose: Centralize prompt customization settings
# ============================================================================
class PromptPreferences(BaseModel):
    """User preferences for prompt customization."""
    # NOTE: Semantic meaning of custom_kernel_* fields is not fully documented.
    # These fields are actively used by UserPromptBuilder but may need clarification.
    # Keep for now pending usage verification.
    custom_kernel_id: Optional[str] = None
    custom_kernel_light_id: Optional[str] = None
    custom_examples_id: Optional[str] = None
    custom_anchors_id: Optional[str] = None
    custom_instructions: Optional[str] = None
    language: str = "uk"
    vibe: str = "friendly"  # friendly, professional, witty

# Default per-agent tiers used as a fallback in get_tier_for_agent.
# Kept outside the class so it can be referenced by both default_factory and the method
# without a forward reference. Add new agent types here; existing users with stale
# stored agent_tiers dicts will pick up the new defaults automatically.
_DEFAULT_AGENT_TIERS: Dict[str, "PerformanceTier"] = {
    "router": PerformanceTier.ECO,
    "quick": PerformanceTier.BALANCED,
    "smart": PerformanceTier.PERFORMANCE,
    "consolidation": PerformanceTier.PERFORMANCE,
    "web_search": PerformanceTier.BALANCED,
    "web_search_light": PerformanceTier.ECO,
    "memory_search": PerformanceTier.ECO,
    "email_search": PerformanceTier.ECO,
    "postprocessing": PerformanceTier.BALANCED,
    "email_classifier": PerformanceTier.BALANCED,
    "deep_research": PerformanceTier.BALANCED,  # BALANCED → o4-mini; PERFORMANCE → o3
    "doc_planner": PerformanceTier.BALANCED,   # JSON layout spec
    "doc_generator": PerformanceTier.BALANCED, # JS code generation + retries
}


class UserBotConfig(BaseModel):
    """Configuration for the user's personalized bot."""
    # ========================================================================
    # Provider Configuration
    # ========================================================================
    # ========================================================================
    # LEGACY Provider Refactor Session 16: Provider fields deprecated
    # Plan: docs/architecture/provider_refactor/PROVIDER_REFACTOR_EXECUTION_PLAN.md
    # Reason: Domain must not contain infrastructure coupling per Master Spec §2.1
    # Use provider_preference instead (added in Session 9)
    # Removal: After UAT and full migration complete
    # ========================================================================
    # light_llm_provider: LLMProvider = LLMProvider.GEMINI
    # smart_llm_provider: LLMProvider = LLMProvider.ANTHROPIC

    # ========================================================================
    # LEGACY Provider Refactor Session 2: Model-based configuration
    # Plan: docs/architecture/provider_refactor/PROVIDER_REFACTOR_EXECUTION_PLAN.md
    # Reason: Replaced by tier abstraction for provider independence
    # Removal: Session 10 after UserAgentFactory refactor completes
    # ========================================================================
    # light_model: str = "gemini-3-flash-preview"
    # full_model: str = "gemini-3-flash-preview"
    # smart_model: str = "models/gemini-3-pro-preview"

    # ========================================================================
    # NEW Provider Refactor Session 2: Tier-based configuration
    # Plan: docs/architecture/provider_refactor/PROVIDER_REFACTOR_EXECUTION_PLAN.md
    # Purpose: Abstract performance tier from provider-specific model strings
    # ========================================================================
    default_tier: PerformanceTier = PerformanceTier.ECO
    agent_tiers: Optional[Dict[str, PerformanceTier]] = Field(
        default_factory=lambda: dict(_DEFAULT_AGENT_TIERS)
    )

    # ========================================================================
    # NEW Provider Refactor Session 9: Provider preference & model overrides
    # Plan: docs/architecture/provider_refactor/PROVIDER_REFACTOR_EXECUTION_PLAN.md
    # Purpose: Allow user-level provider choice and power-user model overrides
    # ========================================================================
    provider_preference: Optional[str] = None  # "gemini" | "claude" | "openai"
    
    # ========================================================================
    # NEW Session 2026-02-12: Per-agent provider selection
    # Purpose: Allow different providers for different agents (e.g., Gemini for router, Claude for smart)
    # ========================================================================
    agent_providers: Optional[Dict[str, str]] = None  # agent_type -> provider_name
    
    model_overrides: Dict[str, str] = Field(default_factory=dict)  # agent_type -> model name

    temperature: float = 0.7
    
    # Prompt Overrides (Lineage IDs of custom components)
    # ========================================================================
    # LEGACY Provider Refactor Session 3: Scattered prompt overrides
    # Plan: docs/architecture/provider_refactor/PROVIDER_REFACTOR_EXECUTION_PLAN.md
    # Reason: Replaced by centralized PromptPreferences model
    # Removal: Session 13 after PromptBuilder refactor completes
    # ========================================================================
    # custom_kernel_id: Optional[str] = None
    # custom_kernel_light_id: Optional[str] = None
    # custom_examples_id: Optional[str] = None
    
    # Feature Flags
    tools_enabled: List[str] = ["search_memory", "ask_web_search_agent"]
    is_paranoid_mode: bool = False  # Disables vector search, enables encryption

    # Consolidation Overrides
    consolidation_threshold: Optional[int] = None
    consolidation_batch_size: Optional[int] = None

    # ========================================================================
    # NEW Multi-Vector Search Session (2026-02-07): Search context limits
    # Plan: docs/SESSION_2026_02_07_MULTI_VECTOR_SEMANTIC_SEARCH.md
    # Purpose: Account-level control over semantic search context size
    # ========================================================================
    semantic_search_limit: Optional[int] = None  # enriched_context cap (default: 30)
    memory_search_limit: Optional[int] = None    # future: MemorySearchAgent
    
    # ========================================================================
    # NEW Biographical Cache Optimization (2026-02-07): Cache context limits
    # Plan: docs/SESSION_2026_02_07_BIOGRAPHICAL_CACHE_OPTIMIZATION.md
    # RFC: docs/10_rfcs/BIOGRAPHICAL_CACHE_MULTI_VECTOR_RFC.md
    # Purpose: Account-level control over biographical cache size
    # ========================================================================
    biographical_cache_limit: Optional[int] = None  # biographical facts (default: 50)
    principles_cache_limit: Optional[int] = None    # principles/anchors (default: 15)

    # ========================================================================
    # NEW History Optimization (2026-02-18): Tiered history loading
    # Purpose: Last N model turns use full_text, older turns use summary (text)
    # ========================================================================
    history_recent_full_turns: Optional[int] = None  # recent turns with full text (default: 5)
    
    # ========================================================================
    # NEW Biographical Keywords (2026-02-07): Configurable query keywords
    # Plan: docs/SESSION_2026_02_07_BIOGRAPHICAL_CACHE_REFACTORING.md
    # Purpose: 3-level resolution for biographical search query keywords
    # Backward compatible: If only query1 provided → use for all 3 queries
    # ========================================================================
    bio_keywords_query1: Optional[List[str]] = None  # Query 1: tags + metadata (e.g. ["identity", "name", "bio"])
    bio_keywords_query2: Optional[List[str]] = None  # Query 2: vector + tags (e.g. ["medical", "health"])
    bio_keywords_query3: Optional[List[str]] = None  # Query 3: vector + metadata (e.g. ["assets", "vehicles"])
    
    # ========================================================================
    # LEGACY Provider Refactor Session 3: Prompt UI preferences
    # Plan: docs/architecture/provider_refactor/PROVIDER_REFACTOR_EXECUTION_PLAN.md
    # Reason: Replaced by centralized PromptPreferences model
    # Removal: Session 13 after PromptBuilder refactor completes
    # ========================================================================
    # language: str = "en"
    # response_style: str = "standard" # concise, standard, detailed

    # ========================================================================
    # NEW Provider Refactor Session 3: Centralized prompt preferences
    # Plan: docs/architecture/provider_refactor/PROVIDER_REFACTOR_EXECUTION_PLAN.md
    # Purpose: Single source of truth for prompt customization
    # ========================================================================
    prompt_preferences: PromptPreferences = Field(default_factory=PromptPreferences)

    def get_tier_for_agent(self, agent_type: str) -> PerformanceTier:
        """Return the configured performance tier for a given agent type.

        Resolution order:
        1. Per-agent override from self.agent_tiers (user/account stored config)
        2. Class-level default from _DEFAULT_AGENT_TIERS (for known agent types)
        3. self.default_tier (for unknown agent types)

        The class-level fallback ensures that new agent types added to
        _DEFAULT_AGENT_TIERS are picked up even for users whose stored
        agent_tiers dict predates the addition of that agent.
        """
        stored = self.agent_tiers or {}
        return stored.get(
            agent_type,
            _DEFAULT_AGENT_TIERS.get(agent_type, self.default_tier),
        )

    def get_model_override(self, agent_type: str) -> Optional[str]:
        """Return model override for agent type if exists."""
        return self.model_overrides.get(agent_type)
    
    def get_provider_for_agent(self, agent_type: str) -> Optional[str]:
        """Return provider override for specific agent type if exists."""
        if not self.agent_providers:
            return None
        return self.agent_providers.get(agent_type)

class UsageStats(BaseModel):
    """Resource usage tracking."""
    total_requests: int = 0
    total_tokens: int = 0
    last_request_at: Optional[datetime] = None
    
    # Daily counters (reset logic handled by service)
    daily_tokens: int = 0
    daily_requests: int = 0
    daily_reset_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

class UserProfile(BaseModel):
    """Core user identity entity."""
    # Internal Identity
    user_id: str = Field(default_factory=lambda: str(uuid4()))
    email: Optional[str] = None
    display_name: str = "Anonymous"

    # ========================================================================
    # OAuth Multi-Tenant Session 1: OAuth identity integration
    # RFC: docs/10_rfcs/MULTI_TENANT_OAUTH_RFC.md
    # Purpose: Support external OAuth providers (Firebase, AWS Cognito, Okta)
    # ========================================================================
    external_user_id: Optional[str] = None  # OAuth identity: "firebase|abc123", "cognito|xyz789"
    auth_metadata: Optional[Dict[str, Any]] = None  # Provider-specific metadata (name, picture, etc.)

    # Platform Bindings (platform_name -> platform_user_id)
    # e.g., {"slack": "U123456", "telegram": "123456789"}
    platform_identities: Dict[str, str] = {}

    # Billing relationship (account-level quota & billing)
    account_id: Optional[str] = None

    # Configuration & State
    config: UserBotConfig = Field(default_factory=UserBotConfig)

    # ========================================================================
    # REMOVED OAuth Multi-Tenant Session 1: Moved to BillingAccount
    # RFC: docs/10_rfcs/MULTI_TENANT_OAUTH_RFC.md
    # Reason: Single source of truth - tier/usage tracked at account level
    # ========================================================================
    # usage: UsageStats = Field(default_factory=UsageStats)  # → BillingAccount.usage
    # tier: UserTier = UserTier.FREE  # → BillingAccount.tier

    # Metadata
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    is_active: bool = True
    
    def get_platform_id(self, platform: str) -> Optional[str]:
        return self.platform_identities.get(platform)


# ============================================================================
# Pydantic v2: Rebuild models with circular dependencies
# Reason: BillingAccount.account_defaults references UserBotConfig (forward ref)
# Must rebuild after UserBotConfig is fully defined
# ============================================================================
from .billing import BillingAccount
BillingAccount.model_rebuild()
