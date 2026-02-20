"""
User Agent Factory
==================

Creates per-user agent instances and registers them with the AgentCoordinator.

All shared services (LLM adapters, repositories, prompt infrastructure) are
received as ports via constructor injection from ServiceContainer — no adapter
instantiation happens here.
"""

import asyncio
import time
from typing import Callable, Dict, List, Optional

from google.genai import types

from ..config.environment import EnvironmentConfig
from ..domain.agent import AgentConfig
from ..domain.user import UserProfile
from ..ports.user_repository import UserRepository
from ..ports.account_repository import AccountRepository
from ..ports.embedding_service import EmbeddingService
from ..ports.llm_service import LLMService
from ..ports.session_store import SessionStore
from ..services.user_prompt_builder import UserPromptBuilder
from ..services.search_enrichment_service import SearchEnrichmentService
from ..services.biographical_context_service import BiographicalContextService
from ..services.fact_write_service import FactWriteService
from ..services.provider_registry import ProviderRegistry
from ..services.agent_context_builder import AgentContextBuilder
from ..services.configuration_service import ConfigurationService
from ..services.prompt_component_service import PromptComponentService
from ..agents.core.quick_response_agent import create_quick_response_agent
from ..agents.core.smart_response_agent import create_smart_response_agent
from ..services.history_summary_service import HistorySummaryService
from ..agents.core.router_agent import create_router_agent
from ..agents.memory_search_agent import MemorySearchAgent
from ..agents.web_search_agent import WebSearchAgent
from ..agents.consolidation_agent import ConsolidationAgent
from ..infrastructure.agent_coordinator import AgentCoordinator
from ..utils.logger import logger


class UserAgentFactory:
    """
    Factory for per-user agent instances.

    Creates and caches agents per user to ensure isolation while reusing
    instances within a worker process. All shared services are injected
    via constructor — this class owns only per-user lifecycle logic.
    """

    def __init__(
        self,
        *,
        config: dict,
        env_config: EnvironmentConfig,
        coordinator: AgentCoordinator,
        user_repo: UserRepository,
        account_repo: AccountRepository,
        session_store: SessionStore,
        llm_service: LLMService,
        claude_service: LLMService,
        grok_service: Optional[LLMService],
        embedding_service: EmbeddingService,
        repository,
        config_service: ConfigurationService,
        biographical_context_service: BiographicalContextService,
        registry: ProviderRegistry,
        context_builder: AgentContextBuilder,
        component_service: Optional[PromptComponentService],
        assembly_service,
        fact_write_service: FactWriteService,
        fact_management_adapter_factory: Callable,
    ) -> None:
        self.config = config
        self.env_config = env_config
        self.coordinator = coordinator
        self.user_repo = user_repo
        self.account_repo = account_repo
        self.session_store = session_store
        self.llm_service = llm_service
        self.claude_service = claude_service
        self.grok_service = grok_service
        self.embedding_service = embedding_service
        self.repository = repository
        self.config_service = config_service
        self.biographical_context_service = biographical_context_service
        self.registry = registry
        self.context_builder = context_builder
        self.component_service = component_service
        self.assembly_service = assembly_service
        self.fact_write_service = fact_write_service
        self.fact_management_adapter_factory = fact_management_adapter_factory

        self._cache: Dict[str, Dict[str, object]] = {}
        self._cache_ttl = 3600
        self._creation_locks: Dict[str, asyncio.Lock] = {}  # Per-user locks
        self._sweep_task: Optional[asyncio.Task] = None

    async def ensure_agents_for_user(self, user_id: str) -> Dict[str, object]:
        # Fast path: check cache without acquiring a lock
        cached = self._cache.get(user_id)
        if cached and (time.time() - cached["last_used"]) < self._cache_ttl:
            cached["last_used"] = time.time()
            return cached

        # Slow path: per-user lock prevents duplicate agent creation on concurrent requests
        lock = self._creation_locks.setdefault(user_id, asyncio.Lock())
        async with lock:
            # Re-check: another coroutine may have populated the cache while we waited
            cached = self._cache.get(user_id)
            if cached and (time.time() - cached["last_used"]) < self._cache_ttl:
                cached["last_used"] = time.time()
                return cached
            return await self._create_and_cache_agents(user_id)

    async def _create_and_cache_agents(self, user_id: str) -> Dict[str, object]:
        """Create all agents for a user and store them in the cache. Called under per-user lock."""
        user_profile = await self._load_user_profile(user_id)

        # Ensure repository is initialized (pre-computes bio query vector once)
        await self.repository.initialize()

        prompt_builder = UserPromptBuilder(
            repo=self.repository,
            user_id=user_id,
            config=user_profile.config,
            assembly_service=self.assembly_service,
        )
        await prompt_builder.preload_components()

        light_llm_service, light_model = self._resolve_light_llm(user_profile)
        smart_llm_service, smart_model = self._resolve_smart_llm(user_profile)

        router_context = self.context_builder.build("router", user_profile.config)
        quick_context = self.context_builder.build("quick", user_profile.config)
        smart_context = self.context_builder.build("smart", user_profile.config)
        postprocessing_context = self.context_builder.build("postprocessing", user_profile.config)
        history_summary_service = HistorySummaryService(
            llm_service=postprocessing_context.provider,
            model_name=postprocessing_context.model_name,
        )

        # Load BillingAccount for config defaults
        account = await self.account_repo.get_account(user_profile.account_id)

        # Resolve per-user limits via 3-level config inheritance (USER → ACCOUNT → SYSTEM)
        semantic_limit = self.config_service.get_semantic_search_limit(
            user_config=user_profile.config,
            account_defaults=account.account_defaults if account else None,
        )
        facts_limit = self.config_service.get_biographical_cache_limit(
            user_config=user_profile.config,
            account_defaults=account.account_defaults if account else None,
        )
        principles_limit = self.config_service.get_principles_cache_limit(
            user_config=user_profile.config,
            account_defaults=account.account_defaults if account else None,
        )
        history_recent_full_turns = self.config_service.get_history_recent_full_turns(
            user_config=user_profile.config,
            account_defaults=account.account_defaults if account else None,
        )

        logger.info(
            f"🔍 [UserAgentFactory] Creating agents for user {user_id[:8]}... "
            f"semantic_limit={semantic_limit}, facts_limit={facts_limit}, "
            f"principles_limit={principles_limit}, "
            f"history_recent_full_turns={history_recent_full_turns} "
            f"(account_tier={account.tier if account else 'N/A'})"
        )

        from ..config.settings import SearchConfig
        search_config = SearchConfig()

        search_enrichment_service = SearchEnrichmentService(
            repository=self.repository,
            embedding_service=self.embedding_service,
            keyword_limit=search_config.DEFAULT_KEYWORD_LIMIT,
            phrase_one_limit=search_config.DEFAULT_PHRASE_ONE_LIMIT,
            phrase_two_limit=search_config.DEFAULT_PHRASE_TWO_LIMIT,
            total_limit=semantic_limit,
        )

        quick_agent = create_quick_response_agent(
            execution_context=quick_context,
            session_store=self.session_store,
            prompt_builder=prompt_builder,
            repository=self.repository,
            embedding_service=self.embedding_service,
            coordinator=self.coordinator,
            user_id=user_id,
            model_name=light_model,
        )

        smart_agent = create_smart_response_agent(
            execution_context=smart_context,
            session_store=self.session_store,
            prompt_builder=prompt_builder,
            repository=self.repository,
            embedding_service=self.embedding_service,
            coordinator=self.coordinator,
            user_id=user_id,
            model_name=smart_model,
            history_recent_full_turns=history_recent_full_turns,
            history_summary_service=history_summary_service,
        )

        router_agent = create_router_agent(
            execution_context=router_context,
            coordinator=self.coordinator,
            quick_agent_id=quick_agent.agent_id,
            smart_agent_id=smart_agent.agent_id,
            user_id=user_id,
            session_store=self.session_store,
            repository=self.repository,
            embedding_service=self.embedding_service,
            search_enrichment_service=search_enrichment_service,
            prompt_builder=prompt_builder,
        )

        account_id = user_profile.account_id

        memory_agent = MemorySearchAgent(
            config=AgentConfig(
                agent_id=f"memory_search_agent_{user_id}",
                agent_type="memory_search",
                timeout_ms=5000,
                capabilities=["personal_data_retrieval", "fact_search"],
            ),
            repository=self.repository,
            embedding_service=self.embedding_service,
            account_id=account_id,
            search_enrichment=search_enrichment_service,
        )

        grounding_tool = types.Tool(google_search=types.GoogleSearch())
        web_agent = WebSearchAgent(
            config=AgentConfig(
                agent_id=f"web_search_agent_{user_id}",
                agent_type="web_search",
                timeout_ms=60000,
                capabilities=["web_search", "current_events"],
            ),
            execution_context=quick_context,
            grounding_tool=grounding_tool,
            prompt_builder=prompt_builder,
            user_id=user_id,
        )

        fact_management_adapter = self.fact_management_adapter_factory(search_enrichment_service)

        consolidation_agent = ConsolidationAgent(
            config=AgentConfig(
                agent_id=f"consolidation_agent_{user_id}",
                agent_type="consolidation",
                timeout_ms=300000,
                capabilities=["fact_consolidation", "synthesis", "deduplication"],
            ),
            execution_context=smart_context,
            repository=self.repository,
            embedding_service=self.embedding_service,
            fact_write_service=self.fact_write_service,
            fact_management_port=fact_management_adapter,
            prompt_version=self.config["CONSOLIDATION"].prompt_version,
            prompt_builder=prompt_builder,
            facts_limit=facts_limit,
            principles_limit=principles_limit,
        )

        self._register_agents([
            router_agent,
            quick_agent,
            smart_agent,
            memory_agent,
            web_agent,
            consolidation_agent,
        ])

        # Preload prompt assembly cache (warm-up optimization, non-critical)
        if self.assembly_service and hasattr(self.assembly_service, "preload_cache"):
            try:
                await asyncio.gather(
                    self.assembly_service.preload_cache("quick", account_id, user_id),
                    self.assembly_service.preload_cache("smart", account_id, user_id),
                )
            except Exception as e:
                logger.warning(f"Failed to preload prompt cache for user {user_id[:8]}: {e}")

        cached = {
            "last_used": time.time(),
            "search_enrichment": search_enrichment_service,
            "router_agent": router_agent,
            "quick_agent": quick_agent,
            "smart_agent": smart_agent,
            "memory_agent": memory_agent,
            "web_agent": web_agent,
            "consolidation_agent": consolidation_agent,
        }
        self._cache[user_id] = cached
        return cached

    async def _load_user_profile(self, user_id: str) -> UserProfile:
        user_profile = await self.user_repo.get_user(user_id)
        if not user_profile:
            raise ValueError(f"User {user_id} not found")
        if not user_profile.is_active:
            raise PermissionError(f"User {user_id} is not active")
        return user_profile

    def _register_agents(self, agents: List[object]) -> None:
        for agent in agents:
            try:
                self.coordinator.register_agent(agent)
            except ValueError:
                continue

    # ------------------------------------------------------------------
    # Lifecycle: start / shutdown / TTL sweep
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """Start background cache sweep. Call after the event loop is running."""
        self._sweep_task = asyncio.create_task(self._evict_expired_cache())

    async def shutdown(self) -> None:
        """Cancel background sweep."""
        if self._sweep_task and not self._sweep_task.done():
            self._sweep_task.cancel()
            try:
                await self._sweep_task
            except asyncio.CancelledError:
                pass

    async def _evict_expired_cache(self) -> None:
        """Periodically evict expired user-agent sets to bound memory usage."""
        while True:
            await asyncio.sleep(300)  # sweep every 5 minutes
            now = time.time()
            expired = [
                uid for uid, c in list(self._cache.items())
                if now - c["last_used"] > self._cache_ttl
            ]
            for uid in expired:
                entry = self._cache.pop(uid, None)
                if entry is None:
                    continue
                for key in ("router_agent", "quick_agent", "smart_agent",
                            "memory_agent", "web_agent", "consolidation_agent"):
                    agent = entry.get(key)
                    if agent and hasattr(agent, "agent_id"):
                        self.coordinator.unregister_agent(agent.agent_id)
                logger.info("♻️ [AgentFactory] Evicted expired cache for user %s", uid[:8])

    # ------------------------------------------------------------------
    # Provider resolution
    # ------------------------------------------------------------------

    def _resolve_smart_llm(self, user_profile: UserProfile) -> tuple[object, str]:
        """Resolve the Smart agent adapter and model via AgentContextBuilder."""
        context = self.context_builder.build("smart", user_profile.config)
        if context.model_name.startswith("claude") and not self.config.get("ANTHROPIC_API_KEY"):
            raise ValueError(
                f"User {user_profile.user_id} resolved to ANTHROPIC provider "
                "but ANTHROPIC_API_KEY is missing"
            )
        return context.provider, context.model_name

    def _resolve_light_llm(self, user_profile: UserProfile) -> tuple[object, str]:
        """Resolve the Light agent adapter and model via AgentContextBuilder."""
        context = self.context_builder.build("quick", user_profile.config)
        if context.model_name.startswith("claude") and not self.config.get("ANTHROPIC_API_KEY"):
            raise ValueError(
                f"User {user_profile.user_id} resolved to ANTHROPIC provider "
                "but ANTHROPIC_API_KEY is missing"
            )
        return context.provider, context.model_name

    def get_session_store(self) -> SessionStore:
        return self.session_store
