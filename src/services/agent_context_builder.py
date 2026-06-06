from __future__ import annotations

from typing import Dict, Optional, Any, Callable, TYPE_CHECKING

from ..domain.user import UserBotConfig, PerformanceTier
from ..ports.llm_port import LLMPort, AgentExecutionContext
from ..ports.deep_research_port import DeepResearchPort
from ..ports.prompt_cache_strategy_port import PromptCacheStrategyPort
from ..ports.provider_resilience_port import ProviderResiliencePort
from ..utils.logger import logger

if TYPE_CHECKING:
    from .provider_registry import ProviderRegistry
    from ..domain.llm import PromptCacheConfig
    from ..domain.complexity_settings import ComplexitySettings

# Backward-compat re-export: importers of agent_context_builder still work unchanged.
__all__ = ["AgentExecutionContext", "AgentContextBuilder"]


class AgentProviderStrategy:
    """
    Defines default providers and allowed overrides for different agent types.
    """
    STRATEGIES = {
        "router": {
            "default_provider": "gemini",
            "allowed_providers": ["grok", "gemini", "openai"],
            "required_capabilities": ["fast_inference"],
            "fallback": "gemini"
        },
        "quick": {
            "default_provider": "claude",
            "allowed_providers": ["grok", "gemini", "claude", "openai"],
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
            "default_provider": "openai",
            "allowed_providers": ["openai", "gemini", "claude"],
            "required_capabilities": ["native_tools"],
            "fallback": "gemini"
        },
        "consolidation": {
            "default_provider": "claude",
            "allowed_providers": ["claude", "gemini", "openai"],
            "required_capabilities": ["context_caching"],
            "fallback": "gemini"
        },
        "postprocessing": {
            "default_provider": "gemini",
            "allowed_providers": ["gemini"],   # locked: response_schema is Gemini-only
            "required_capabilities": ["fast_inference"],
            "fallback": "gemini"
        },
        # Memory search key formulation: response_mime_type="application/json" is Gemini-only.
        # Claude/OpenAI ignore it and wrap JSON in markdown → parse failure.
        "facts_memory": {
            "default_provider": "gemini",
            "allowed_providers": ["gemini"],
            "required_capabilities": ["fast_inference"],
            "fallback": None
        },
        "email_classifier": {
            "default_provider": "gemini",
            "allowed_providers": ["gemini"],
            "required_capabilities": ["native_tools"],
            "fallback": "gemini"
        },
        "email_search": {
            "default_provider": "gemini",
            "allowed_providers": ["gemini"],
            "required_capabilities": ["native_tools"],
            "fallback": "gemini"
        },
        # code_execution is Gemini-only (sandbox Python execution).
        "compute": {
            "default_provider": "gemini",
            "allowed_providers": ["gemini"],
            "required_capabilities": ["native_tools"],
            "fallback": None
        },
        # MCP-backed: provider-agnostic. MCP tools passed as FunctionDeclaration
        # dicts — adapters convert natively, so any native_tools provider works.
        # Default OpenAI (BALANCED → gpt-5.4-mini): nano was too weak for multi-turn
        # tool synthesis; mini adds reasoning quality while thinking="low" keeps
        # latency bounded. Tier pinned to BALANCED via _DEFAULT_AGENT_TIERS.
        "maps_search": {
            "default_provider": "openai",
            "allowed_providers": ["openai", "gemini", "claude"],
            "required_capabilities": ["native_tools"],
            "fallback": None
        },
        "tasks": {
            "default_provider": "gemini",
            "allowed_providers": ["gemini", "claude"],
            "required_capabilities": ["native_tools"],
            "fallback": "gemini"
        },
        "notes": {
            "default_provider": "openai",
            "allowed_providers": ["openai", "gemini", "claude"],
            "required_capabilities": ["native_tools"],
            "fallback": "gemini"
        },
        # Deep analytical work: domain decomposition, competency scoring. Reasoning model.
        "domain_researcher": {
            "default_provider": "openai",
            "allowed_providers": ["openai", "claude", "gemini"],
            "required_capabilities": [],
            "fallback": "gemini"
        },
        # Deep research uses DeepResearchPort (not LLMPort) — AgentContextBuilder.build() is
        # NOT called for this agent type. This entry exists solely for unified default/allowed
        # provider declaration. UserAgentFactory reads default_provider from here.
        "deep_research": {
            "default_provider": "claude",
            "allowed_providers": ["openai", "claude"],
            "required_capabilities": [],
            "fallback": None
        },
        # Claude is best for structured JSON layout with complex document content.
        # No tool calling needed — single LLM call, pure JSON output.
        "doc_planner": {
            "default_provider": "claude",
            "allowed_providers": ["claude", "gemini", "openai"],
            "required_capabilities": [],
            "fallback": "gemini"
        },
        # Claude is best for code generation (docx npm JS script).
        "doc_generator": {
            "default_provider": "claude",
            "allowed_providers": ["claude", "gemini", "openai"],
            "required_capabilities": [],
            "fallback": "gemini"
        },
        # PDF generator: Gemini BALANCED for HTML+CSS code generation.
        "pdf_generator": {
            "default_provider": "gemini",
            "allowed_providers": ["claude", "gemini", "openai"],
            "required_capabilities": [],
            "fallback": "claude"
        },
        # HTML page generator: Gemini PERFORMANCE for max-quality single-pass page generation.
        "html_page": {
            "default_provider": "gemini",
            "allowed_providers": ["claude", "gemini", "openai"],
            "required_capabilities": [],
            "fallback": "claude"
        },
    }

    @classmethod
    def get_strategy(cls, agent_type: str) -> Dict[str, Any]:
        """Get strategy for agent type. Defaults to 'quick' if unknown."""
        return cls.STRATEGIES.get(agent_type, cls.STRATEGIES["quick"])


class AgentContextBuilder:
    """
    Service that builds AgentExecutionContext based on user config and agent type.
    
    Follows the strategy defined in AgentProviderStrategy and uses ProviderRegistry
    to resolve concrete LLMPort instances.
    """

    def __init__(
        self,
        registry: ProviderRegistry,
        resilience_port: ProviderResiliencePort,
        cache_strategy: Optional[PromptCacheStrategyPort] = None,
        caching_proxy_factory: Optional[Callable[[LLMPort, PromptCacheConfig], LLMPort]] = None,
        alerting_proxy_factory: Optional[Callable[[LLMPort], LLMPort]] = None,
    ):
        self.registry = registry
        self._resilience_port = resilience_port
        self._cache_strategy = cache_strategy
        self._caching_proxy_factory = caching_proxy_factory
        self._alerting_proxy_factory = alerting_proxy_factory

    def resolve_provider_name(self, agent_type: str, config: UserBotConfig) -> str:
        """
        Resolve the provider name for agent_type using 3-level priority:
          1. Per-agent override  (config.agent_providers[agent_type])
          2. Global preference   (config.provider_preference)
          3. Strategy default    (AgentProviderStrategy.STRATEGIES[agent_type]["default_provider"])

        Shared by build() (LLM context) and external callers that need only the name
        (e.g. UserAgentFactory resolving DeepResearchPort providers).
        """
        strategy = AgentProviderStrategy.get_strategy(agent_type)
        provider_name = strategy["default_provider"]

        agent_provider = config.get_provider_for_agent(agent_type)
        if agent_provider and agent_provider in strategy["allowed_providers"]:
            provider_name = agent_provider
        elif config.provider_preference and config.provider_preference in strategy["allowed_providers"]:
            provider_name = config.provider_preference

        return provider_name

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
        provider_name = self.resolve_provider_name(agent_type, config)
        tier = config.get_tier_for_agent(agent_type)
        return self._build(agent_type, config, provider_name, tier)

    def resolve_for_task(
        self,
        agent_type: str,
        config: UserBotConfig,
        settings: "ComplexitySettings"
    ) -> AgentExecutionContext:
        """
        Build execution context using dynamically resolved complexity settings.
        Overrides tier and (optionally) provider; all other logic identical to build().
        """
        provider_name = settings.provider_override or self.resolve_provider_name(agent_type, config)
        return self._build(agent_type, config, provider_name, settings.tier)

    def _build(
        self,
        agent_type: str,
        config: UserBotConfig,
        provider_name: str,
        tier: PerformanceTier,
    ) -> AgentExecutionContext:
        """Shared context assembly for build() and resolve_for_task()."""
        strategy = AgentProviderStrategy.get_strategy(agent_type)
        provider = self.registry.get(provider_name)
        capabilities = provider.get_capabilities()

        model_override = config.get_model_override(agent_type)
        model_name = model_override or provider.get_model_for_tier(tier)

        # Apply caching strategy (transparent to agents)
        if self._cache_strategy and self._caching_proxy_factory:
            cache_config = self._cache_strategy.resolve(agent_type, capabilities)
            if cache_config:
                provider = self._caching_proxy_factory(provider, cache_config)

        # Wrap with alerting (outermost — sees errors from the real provider call).
        if self._alerting_proxy_factory:
            provider = self._alerting_proxy_factory(provider)

        # Resolve fallback provider from strategy (used by BaseAgent on 429/503).
        # Fallback gets raw provider without caching — cache is useless when switching providers.
        fallback_name = strategy.get("fallback")
        fallback_llm = None
        fallback_model_name = None
        resolved_fallback_name = None
        if fallback_name and fallback_name != provider_name:
            try:
                fb_raw = self.registry.get(fallback_name)
                fallback_llm = self._alerting_proxy_factory(fb_raw) if self._alerting_proxy_factory else fb_raw
                fallback_model_name = fb_raw.get_model_for_tier(tier)
                resolved_fallback_name = fallback_name
            except Exception:
                logger.warning(
                    "llm_fallback_provider_unavailable",
                    extra={
                        "event": "llm_fallback_provider_unavailable",
                        "agent_type": agent_type,
                        "fallback_provider": fallback_name,
                    },
                )

        return AgentExecutionContext(
            agent_type=agent_type,
            provider=provider,
            model_name=model_name,
            tier=tier,
            capabilities=capabilities,
            provider_name=provider_name,
            fallback_provider=fallback_llm,
            fallback_model_name=fallback_model_name,
            fallback_provider_name=resolved_fallback_name,
            resilience_port=self._resilience_port,
        )

    def resolve_async_context(
        self,
        agent_type: str,
        job_registry: ProviderRegistry,
        config: UserBotConfig,
    ) -> tuple[DeepResearchPort, PerformanceTier, str]:
        """
        Build execution context for an async job agent (DeepResearchPort-backed).

        Uses the same 3-level provider resolution chain as build() for LLMPort.
        Model selection is intentionally NOT done here — it is an adapter-internal
        concern. The adapter maps PerformanceTier → model name via its own MODEL_TIERS
        dict (or a construction-time override from environment config).

        Returns:
            (job_port, tier, provider_name)
        """
        provider_name = self.resolve_provider_name(agent_type, config)
        job_port: DeepResearchPort = job_registry.get(provider_name)
        tier = config.get_tier_for_agent(agent_type)
        return job_port, tier, provider_name
