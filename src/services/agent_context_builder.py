from typing import Dict, Optional, List, Any

from ..domain.user import UserBotConfig, PerformanceTier
from ..ports.llm_service import LLMService, ProviderCapabilities, AgentExecutionContext
from ..ports.prompt_cache_strategy_port import PromptCacheStrategyPort
from .provider_registry import ProviderRegistry
from .caching_llm_proxy import CachingLLMProxy

# Backward-compat re-export: importers of agent_context_builder still work unchanged.
__all__ = ["AgentExecutionContext", "AgentContextBuilder"]


# ============================================================================
# NEW Provider Refactor Session 9: Agent provider selection strategy
# Plan: docs/architecture/provider_refactor/PROVIDER_REFACTOR_EXECUTION_PLAN.md
# Purpose: Centralize logic for choosing providers based on agent requirements
# ============================================================================
class AgentProviderStrategy:
    """
    Defines default providers and allowed overrides for different agent types.
    
    Required capabilities are used for future validation/routing logic.
    """
    STRATEGIES = {
        # ========================================================================
        # NEW Provider Refactor Session 20: Router agent strategy
        # Plan: docs/architecture/provider_refactor/POST_AUDIT_EXECUTION_PLAN.md
        # Purpose: Enable tier-based model selection for RouterAgent triage
        # ========================================================================
        # SESSION 2026-02-12: Grok integration - set as default for Router + Quick
        "router": {
            "default_provider": "gemini",
            "allowed_providers": ["grok", "gemini"],
            "required_capabilities": ["fast_inference"],
            "fallback": "gemini"
        },
        "quick": {
            "default_provider": "gemini",
            "allowed_providers": ["grok", "gemini", "claude"],
            "required_capabilities": ["native_tools"],
            "fallback": "gemini"
        },
        "smart": {
            "default_provider": "gemini",
            "allowed_providers": ["claude", "openai", "gemini", "grok"],
            "required_capabilities": ["tool_orchestration"],
            "fallback": "gemini"
        },
        "web_search": {
            "default_provider": "gemini",
            "allowed_providers": ["gemini"],
            "required_capabilities": ["native_tools"],
            "fallback": None
        },
        "consolidation": {
            "default_provider": "claude",
            "allowed_providers": ["claude", "gemini"],
            "required_capabilities": ["context_caching"],
            "fallback": "gemini"
        },
        "postprocessing": {
            "default_provider": "gemini",
            "allowed_providers": ["gemini"],   # locked: response_schema is Gemini-only
            "required_capabilities": ["fast_inference"],
            "fallback": "gemini"
        }
    }

    @classmethod
    def get_strategy(cls, agent_type: str) -> Dict[str, Any]:
        """Get strategy for agent type. Defaults to 'quick' if unknown."""
        return cls.STRATEGIES.get(agent_type, cls.STRATEGIES["quick"])


# ============================================================================
# NEW Provider Refactor Session 9: Agent context builder service
# Plan: docs/architecture/provider_refactor/PROVIDER_REFACTOR_EXECUTION_PLAN.md
# Purpose: Orchestrate provider selection and context assembly
# ============================================================================
class AgentContextBuilder:
    """
    Service that builds AgentExecutionContext based on user config and agent type.
    
    Follows the strategy defined in AgentProviderStrategy and uses ProviderRegistry
    to resolve concrete LLMService instances.
    """

    def __init__(
        self,
        registry: ProviderRegistry,
        cache_strategy: Optional[PromptCacheStrategyPort] = None,
    ):
        self.registry = registry
        self._cache_strategy = cache_strategy

    def build(self, agent_type: str, config: UserBotConfig) -> AgentExecutionContext:
        """
        Build execution context for a specific agent and user.
        
        Resolution order:
        1. Get strategy for agent_type.
        2. Determine provider (3-level resolution):
           a. Per-agent provider (config.agent_providers[agent_type])
           b. Global provider preference (config.provider_preference)
           c. Strategy default
        3. Determine tier: user per-agent tier OR user default tier.
        4. Determine model: user model override OR provider-specific mapping for tier.
        """
        strategy = AgentProviderStrategy.get_strategy(agent_type)

        # 1. Resolve Provider (3-level resolution)
        provider_name = strategy["default_provider"]
        
        # Level 1: Check per-agent provider override
        agent_provider = config.get_provider_for_agent(agent_type)
        if agent_provider and agent_provider in strategy["allowed_providers"]:
            provider_name = agent_provider
        # Level 2: Check global provider preference
        elif config.provider_preference and config.provider_preference in strategy["allowed_providers"]:
            provider_name = config.provider_preference
        # Level 3: Use strategy default (already set above)
        
        provider = self.registry.get(provider_name)
        capabilities = provider.get_capabilities()

        # 2. Resolve Tier
        tier = config.get_tier_for_agent(agent_type)

        # 3. Resolve Model Name
        model_override = config.get_model_override(agent_type)
        model_name = model_override or provider.get_model_for_tier(tier)

        # 4. Apply caching strategy (transparent to agents)
        if self._cache_strategy:
            cache_config = self._cache_strategy.resolve(agent_type, capabilities)
            if cache_config:
                provider = CachingLLMProxy(provider, cache_config)

        return AgentExecutionContext(
            agent_type=agent_type,
            provider=provider,
            model_name=model_name,
            tier=tier,
            capabilities=capabilities
        )
