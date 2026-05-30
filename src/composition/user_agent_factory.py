"""
User Agent Factory
==================

Creates per-user agent instances and registers them with the AgentCoordinator.

All shared services (LLM adapters, repositories, prompt infrastructure) are
received as ports via constructor injection from ServiceContainer — no adapter
instantiation happens here.
"""

from __future__ import annotations

import asyncio
import os
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Callable, Dict, List, Optional

from ..adapters.mcp.mcp_client import MCPClient
from ..adapters.mcp.mcp_maps_adapter import MCPMapsAdapter

from ..config.environment import EnvironmentConfig
from ..domain.agent import AgentConfig
from ..domain.user import UserProfile
from ..ports.user_repository import UserRepository
from ..ports.account_repository import AccountRepository
from ..ports.embedding_service import EmbeddingService
from ..ports.llm_port import LLMPort
from ..ports.session_store import SessionStore
from ..services.prompt_builder import UserPromptBuilder
from ..services.search_enrichment_service import SearchEnrichmentService
from ..services.biographical_context_service import BiographicalContextService
from ..ports.fact_write_port import FactWritePort
from ..services.provider_registry import ProviderRegistry
from ..services.agent_context_builder import AgentContextBuilder
from ..ports.llm_port import AgentExecutionContext
from ..services.configuration_service import ConfigurationService
from ..services.prompt_component_service import PromptComponentService
from ..infrastructure.agent_config import (
    MEMORY_SEARCH as MEMORY_SEARCH_CFG,
    WEB_SEARCH as WEB_SEARCH_CFG,
    EMAIL_SEARCH as EMAIL_SEARCH_CFG,
    CONSOLIDATION as CONSOLIDATION_CFG,
    MAPS_SEARCH as MAPS_SEARCH_CFG,
    COMPUTE as COMPUTE_CFG,
    DEEP_RESEARCH as DEEP_RESEARCH_CFG,
    CLAUDE_DEEP_RESEARCH_RUNNER as CLAUDE_DEEP_RESEARCH_RUNNER_CFG,
    NOTES as NOTES_CFG,
    TASKS as TASKS_CFG,
    DOC_PLANNER as DOC_PLANNER_CFG,
    DOC_GENERATOR as DOC_GENERATOR_CFG,
    PDF_GENERATOR as PDF_GENERATOR_CFG,
    HTML_PAGE_GENERATOR as HTML_PAGE_GENERATOR_CFG,
    DOMAIN_RESEARCHER as DOMAIN_RESEARCHER_CFG,
)
from ..agents.core.quick_response_agent import create_quick_response_agent
from ..agents.core.smart_response_agent import create_smart_response_agent
from ..infrastructure.task_execution_resolver import TaskExecutionResolver
from ..services.history_summary_service import HistorySummaryService
from ..agents.core.router_agent import create_router_agent
from ..agents.memory_search_agent import FactsMemoryAgent
from ..agents.web_search_agent import WebSearchAgent
from ..agents.email_search_agent import EmailSearchAgent
from ..agents.consolidation_agent import ConsolidationAgent
from ..agents.maps_search_agent import MapsSearchAgent
from ..agents.compute_agent import ComputeAgent
from ..agents.deep_research_agent import DeepResearchAgent
from ..agents.claude_deep_research_runner_agent import ClaudeDeepResearchRunnerAgent
from ..agents.tasks_agent import TasksAgent
from ..agents.notes_agent import NotesAgent
from ..agents.doc_planner_agent import DocPlannerAgent
from ..agents.doc_generator_agent import DocGeneratorAgent
from ..agents.pdf_generator_agent import PdfGeneratorAgent
from ..agents.html_page_generator_agent import HtmlPageGeneratorAgent
from ..agents.help_agent import HelpAgent
from ..agents.file_management_agent import FileManagementAgent
from ..agents.domain_researcher_agent import DomainResearcherAgent
from ..adapters.node_docx_runner import NodeDocxRunner
from ..adapters.node_puppeteer_runner import NodePuppeteerRunner
from ..adapters.unsplash_adapter import UnsplashAdapter
from ..ports.task_queue import TaskQueue
from ..ports.tasks_provider_port import TasksProviderPort
from ..ports.agent_note_port import AgentNotePort
from ..services.email_search_service import EmailSearchService
from ..services.task_indexing_service import TaskIndexingService
from ..ports.indexed_email_repository import IndexedEmailRepository
from ..ports.agent_factory_port import AgentFactoryPort
from ..utils.logger import logger

if TYPE_CHECKING:
    from ..infrastructure.agent_coordinator import AgentCoordinator


@dataclass
class _UserContext:
    """Per-user shared context cached for lazy agent creation."""
    user_profile: UserProfile
    prompt_builder: UserPromptBuilder


class UserAgentFactory(AgentFactoryPort):
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
        llm_port: LLMPort,
        claude_service: LLMPort,
        grok_service: Optional[LLMPort],
        openai_service: Optional[LLMPort] = None,
        embedding_service: EmbeddingService,
        repository,
        config_service: ConfigurationService,
        biographical_context_service: BiographicalContextService,
        registry: ProviderRegistry,
        context_builder: AgentContextBuilder,
        component_service: Optional[PromptComponentService],
        assembly_service,
        fact_write_service: FactWritePort,
        fact_management_adapter_factory: Callable,
        email_search_service: EmailSearchService,
        indexed_email_repo: Optional[IndexedEmailRepository] = None,
        tasks_provider: Optional[TasksProviderPort] = None,
        task_indexing: Optional[TaskIndexingService] = None,
        notes_provider: Optional[AgentNotePort] = None,
        notification_service: Optional[object] = None,
        job_registry: Optional[ProviderRegistry] = None,
        task_queue: Optional[TaskQueue] = None,
        anthropic_client: Optional[object] = None,
        file_conversion_service: Optional[object] = None,
        file_storage: Optional[object] = None,
        prompt_content_store: Optional[object] = None,
    ) -> None:
        self.config = config
        self.env_config = env_config
        self.coordinator = coordinator
        self.user_repo = user_repo
        self.account_repo = account_repo
        self.session_store = session_store
        self.llm_port = llm_port
        self.claude_service = claude_service
        self.grok_service = grok_service
        self.openai_service = openai_service
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
        self.email_search_service = email_search_service
        self.indexed_email_repo = indexed_email_repo
        self.tasks_provider = tasks_provider
        self.task_indexing = task_indexing
        self.notes_provider = notes_provider
        self.notification_service = notification_service
        self.job_registry: Optional[ProviderRegistry] = job_registry
        self.task_queue = task_queue
        self.anthropic_client = anthropic_client
        self.file_conversion_service = file_conversion_service
        self.file_storage = file_storage
        self.prompt_content_store = prompt_content_store

        unsplash_key = os.getenv("UNSPLASH_ACCESS_KEY")
        self._image_search = UnsplashAdapter(unsplash_key) if unsplash_key else None

        self._cache: Dict[str, Dict[str, object]] = {}
        self._cache_ttl = 3600
        self._creation_locks: Dict[str, asyncio.Lock] = {}  # Per-user locks
        self._sweep_task: Optional[asyncio.Task] = None

    # ------------------------------------------------------------------
    # ARCHITECTURE FIX: Law of Demeter facade
    # Previously ConversationHandler accessed self.agent_factory.assembly_service.invalidate_cache()
    # — two levels deep + hasattr() guard. Now callers use this single method.
    # ------------------------------------------------------------------
    def invalidate_prompt_cache(self) -> None:
        """Invalidate prompt assembly caches. Facade hiding assembly_service internals."""
        if self.assembly_service and hasattr(self.assembly_service, "invalidate_cache"):
            self.assembly_service.invalidate_cache()

    def invalidate_user_cache(self, user_id: str) -> None:
        """Drop a user's cached profile + agents, forcing a fresh Firestore read next request."""
        self._cache.pop(user_id, None)

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

        router_context = self.context_builder.build("router", user_profile.config)
        quick_context = self.context_builder.build("quick", user_profile.config)
        smart_context = self.context_builder.build("smart", user_profile.config)
        consolidation_context = self.context_builder.build("consolidation", user_profile.config)
        self._validate_anthropic_key(quick_context, user_profile.user_id)
        self._validate_anthropic_key(smart_context, user_profile.user_id)
        postprocessing_context = self.context_builder.build("postprocessing", user_profile.config)
        history_summary_service = HistorySummaryService(
            llm_port=postprocessing_context.provider,
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

        from ..domain.settings import SearchConfig
        search_config = SearchConfig()

        search_enrichment_service = SearchEnrichmentService(
            repository=self.repository,
            embedding_service=self.embedding_service,
            keyword_limit=search_config.DEFAULT_KEYWORD_LIMIT,
            phrase_one_limit=search_config.DEFAULT_PHRASE_ONE_LIMIT,
            phrase_two_limit=search_config.DEFAULT_PHRASE_TWO_LIMIT,
            total_limit=semantic_limit,
        )

        user_timezone = user_profile.config.timezone

        quick_agent = create_quick_response_agent(
            execution_context=quick_context,
            session_store=self.session_store,
            prompt_builder=prompt_builder,
            repository=self.repository,
            embedding_service=self.embedding_service,
            coordinator=self.coordinator,
            user_id=user_id,
            model_name=quick_context.model_name,
            history_recent_full_turns=history_recent_full_turns,
            history_summary_service=history_summary_service,
            user_timezone=user_timezone,
        )

        smart_agent = create_smart_response_agent(
            execution_context=smart_context,
            session_store=self.session_store,
            prompt_builder=prompt_builder,
            resolver=TaskExecutionResolver(self.context_builder),
            user_config=user_profile.config,
            repository=self.repository,
            embedding_service=self.embedding_service,
            coordinator=self.coordinator,
            user_id=user_id,
            model_name=smart_context.model_name,
            history_recent_full_turns=history_recent_full_turns,
            history_summary_service=history_summary_service,
            user_timezone=user_timezone,
            thinking_effort=user_profile.config.get_thinking_for_agent("smart"),
        )

        notes_agent = None
        if self.notes_provider:
            notes_context = self.context_builder.build("notes", user_profile.config)
            notes_agent = NotesAgent(
                config=AgentConfig(
                    agent_id=f"notes_agent_{user_id}",
                    agent_type="notes",
                    timeout_ms=NOTES_CFG.timeout_ms,
                    capabilities=["note_management"],
                ),
                execution_context=notes_context,
                notes_port=self.notes_provider,
                prompt_builder=prompt_builder,
                user_timezone=user_profile.config.timezone,
                notification_service=self.notification_service,
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
            notes_port=self.notes_provider,
        )

        account_id = user_profile.account_id

        memory_search_context = self.context_builder.build("facts_memory", user_profile.config)
        memory_agent = FactsMemoryAgent(
            config=AgentConfig(
                agent_id=f"facts_memory_agent_{user_id}",
                agent_type="facts_memory",
                timeout_ms=MEMORY_SEARCH_CFG.timeout_ms,
                capabilities=["personal_data_retrieval", "fact_search"],
            ),
            repository=self.repository,
            embedding_service=self.embedding_service,
            account_id=account_id,
            search_enrichment=search_enrichment_service,
            execution_context=memory_search_context,
            prompt_builder=prompt_builder,
            user_id=user_id,
        )

        web_search_context = self.context_builder.build("web_search", user_profile.config)
        web_agent = WebSearchAgent(
            config=AgentConfig(
                agent_id=f"web_search_agent_{user_id}",
                agent_type="web_search",
                timeout_ms=WEB_SEARCH_CFG.timeout_ms,
                capabilities=["web_search", "current_events"],
            ),
            execution_context=web_search_context,
            prompt_builder=prompt_builder,
            user_id=user_id,
        )

        email_search_context = self.context_builder.build("email_search", user_profile.config)
        email_search_agent = EmailSearchAgent(
            config=AgentConfig(
                agent_id=f"email_search_agent_{user_id}",
                agent_type="email_search",
                timeout_ms=EMAIL_SEARCH_CFG.timeout_ms,
                capabilities=["email_search", "email_retrieval"],
            ),
            execution_context=email_search_context,
            prompt_builder=prompt_builder,
            email_search_service=self.email_search_service,
            user_id=user_id,
        )

        maps_search_context = self.context_builder.build("maps_search", user_profile.config)
        mcp_client = MCPClient(
            base_url="https://mapstools.googleapis.com/mcp",
            api_key=self.config.get("GOOGLE_SEARCH_API_KEY", ""),
        )
        maps_agent = MapsSearchAgent(
            config=AgentConfig(
                agent_id=f"maps_search_agent_{user_id}",
                agent_type="maps_search",
                timeout_ms=MAPS_SEARCH_CFG.timeout_ms,
                capabilities=["location_search", "place_search", "routing", "weather"],
            ),
            execution_context=maps_search_context,
            maps_port=MCPMapsAdapter(mcp_client),
            prompt_builder=prompt_builder,
            account_id=account_id,
            user_id=user_id,
        )

        compute_context = self.context_builder.build("compute", user_profile.config)
        compute_agent = ComputeAgent(
            config=AgentConfig(
                agent_id=f"compute_agent_{user_id}",
                agent_type="compute",
                timeout_ms=COMPUTE_CFG.timeout_ms,
                capabilities=["computation", "math", "finance"],
            ),
            execution_context=compute_context,
            prompt_builder=prompt_builder,
            user_id=user_id,
        )

        tasks_agent = None
        if self.tasks_provider and self.task_indexing:
            tasks_context = self.context_builder.build("tasks", user_profile.config)
            tasks_agent = TasksAgent(
                config=AgentConfig(
                    agent_id=f"tasks_agent_{user_id}",
                    agent_type="tasks",
                    timeout_ms=TASKS_CFG.timeout_ms,
                    capabilities=["task_management"],
                ),
                execution_context=tasks_context,
                prompt_builder=prompt_builder,
                tasks_provider=self.tasks_provider,
                task_indexing=self.task_indexing,
                user_id=user_id,
            )

        fact_management_adapter = self.fact_management_adapter_factory(search_enrichment_service)

        consolidation_agent = ConsolidationAgent(
            config=AgentConfig(
                agent_id=f"consolidation_agent_{user_id}",
                agent_type="consolidation",
                timeout_ms=CONSOLIDATION_CFG.timeout_ms,
                capabilities=["fact_consolidation", "synthesis", "deduplication"],
            ),
            execution_context=consolidation_context,
            repository=self.repository,
            embedding_service=self.embedding_service,
            fact_write_service=self.fact_write_service,
            fact_management_port=fact_management_adapter,
            prompt_version=self.config["CONSOLIDATION"].prompt_version,
            prompt_builder=prompt_builder,
            facts_limit=facts_limit,
            principles_limit=principles_limit,
            indexed_email_repo=self.indexed_email_repo,
        )

        help_agent = HelpAgent(
            config=AgentConfig(
                agent_id=f"help_agent_{user_id}",
                agent_type="help",
                timeout_ms=5_000,
                capabilities=["system_help"],
            ),
        )

        agents_to_register = [
            router_agent,
            quick_agent,
            smart_agent,
            memory_agent,
            web_agent,
            email_search_agent,
            maps_agent,
            compute_agent,
            consolidation_agent,
            help_agent,
        ]
        if notes_agent:
            agents_to_register.append(notes_agent)
        if tasks_agent:
            agents_to_register.append(tasks_agent)
        self._register_agents(agents_to_register)

        # Preload prompt assembly cache (warm-up optimization, non-critical)
        if self.assembly_service and hasattr(self.assembly_service, "preload_cache"):
            try:
                preload_coros = [
                    self.assembly_service.preload_cache("quick", account_id, user_id),
                    self.assembly_service.preload_cache("smart", account_id, user_id),
                ]
                if self.notes_provider:
                    preload_coros.append(
                        self.assembly_service.preload_cache("notes", account_id, user_id)
                    )
                await asyncio.gather(*preload_coros)
            except Exception as e:
                logger.warning(f"Failed to preload prompt cache for user {user_id[:8]}: {e}")

        # Inject coordinator into all agents so billing works universally.
        # Quick/Smart/Router already set self.coordinator in their constructors;
        # overwriting with the same value is harmless.
        for agent in agents_to_register:
            if agent is not None:
                agent.coordinator = self.coordinator
                agent._prompt_content_store = self.prompt_content_store

        cached = {
            "last_used": time.time(),
            "_user_context": _UserContext(user_profile=user_profile, prompt_builder=prompt_builder),
            "_lazy_agent_ids": [],  # tracks lazy agents for eviction
            "search_enrichment": search_enrichment_service,
            "router_agent": router_agent,
            "quick_agent": quick_agent,
            "smart_agent": smart_agent,
            "memory_agent": memory_agent,
            "web_agent": web_agent,
            "email_search_agent": email_search_agent,
            "maps_agent": maps_agent,
            "compute_agent": compute_agent,
            "notes_agent": notes_agent,
            "tasks_agent": tasks_agent,
            "consolidation_agent": consolidation_agent,
            "help_agent": help_agent,
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
    # Lazy agent instantiation (AgentFactoryPort implementation)
    # ------------------------------------------------------------------

    async def create_agent_on_demand(self, agent_type: str, user_id: str) -> bool:
        """
        Create a single lazy agent on first delegation.

        Looks up user's cached context, constructs the agent, registers it
        with the coordinator, and tracks it for later eviction.
        Returns True if the agent was created or already existed.
        """
        cached = self._cache.get(user_id)
        if cached is None:
            # Eager agents not yet created — caller should have called
            # ensure_agents_for_user() first. Do it now as safety net.
            await self.ensure_agents_for_user(user_id)
            cached = self._cache[user_id]

        builder = self._LAZY_BUILDERS.get(agent_type)
        if builder is None:
            logger.warning(
                "[UserAgentFactory] No lazy builder for agent_type='%s'", agent_type,
            )
            return False

        # Per-user lock prevents duplicate instantiation on concurrent delegations
        lock = self._creation_locks.setdefault(user_id, asyncio.Lock())
        async with lock:
            ctx: _UserContext = cached["_user_context"]

            # Check if already registered in coordinator (idempotent)
            expected_id = f"{self._LAZY_AGENT_IDS[agent_type]}_{user_id}"
            if self.coordinator.get_agent(expected_id) is not None:
                return True

            agent = builder(self, user_id, ctx)
            if agent is None:
                return False

            agent.coordinator = self.coordinator
            agent._prompt_content_store = self.prompt_content_store
            try:
                self.coordinator.register_agent(agent)
            except ValueError:
                return True  # already registered by concurrent call

            cached["_lazy_agent_ids"].append(agent.agent_id)
            logger.info(
                "🦥 [AgentFactory] Lazy-created %s for user %s",
                agent_type, user_id[:8],
            )
            return True

    # -- Builder methods: one per lazy agent type -----------------------

    def _build_doc_generator(self, user_id: str, ctx: _UserContext) -> DocGeneratorAgent:
        execution_context = self.context_builder.build("doc_generator", ctx.user_profile.config)
        return DocGeneratorAgent(
            config=AgentConfig(
                agent_id=f"doc_generator_agent_{user_id}",
                agent_type="doc_generator",
                timeout_ms=DOC_GENERATOR_CFG.timeout_ms,
                capabilities=["docx_code_generation"],
            ),
            execution_context=execution_context,
            docx_runner=NodeDocxRunner(),
            prompt_builder=ctx.prompt_builder,
            user_id=user_id,
        )

    def _build_doc_planner(self, user_id: str, ctx: _UserContext) -> DocPlannerAgent:
        execution_context = self.context_builder.build("doc_planner", ctx.user_profile.config)
        return DocPlannerAgent(
            config=AgentConfig(
                agent_id=f"doc_planner_agent_{user_id}",
                agent_type="doc_planner",
                timeout_ms=DOC_PLANNER_CFG.timeout_ms,
                capabilities=["document_creation"],
            ),
            execution_context=execution_context,
            coordinator=self.coordinator,
            prompt_builder=ctx.prompt_builder,
            user_id=user_id,
        )

    def _build_pdf_generator(self, user_id: str, ctx: _UserContext) -> PdfGeneratorAgent:
        execution_context = self.context_builder.build("pdf_generator", ctx.user_profile.config)
        return PdfGeneratorAgent(
            config=AgentConfig(
                agent_id=f"pdf_generator_agent_{user_id}",
                agent_type="pdf_generator",
                timeout_ms=PDF_GENERATOR_CFG.timeout_ms,
                capabilities=["pdf_generation"],
            ),
            execution_context=execution_context,
            pdf_runner=NodePuppeteerRunner(),
            prompt_builder=ctx.prompt_builder,
            user_id=user_id,
        )

    def _build_html_page(self, user_id: str, ctx: _UserContext) -> HtmlPageGeneratorAgent:
        execution_context = self.context_builder.build("html_page", ctx.user_profile.config)
        return HtmlPageGeneratorAgent(
            config=AgentConfig(
                agent_id=f"html_page_generator_agent_{user_id}",
                agent_type="html_page",
                timeout_ms=HTML_PAGE_GENERATOR_CFG.timeout_ms,
                capabilities=["html_page_generation"],
            ),
            execution_context=execution_context,
            prompt_builder=ctx.prompt_builder,
            user_id=user_id,
            image_search=self._image_search,
        )

    def _build_deep_research(
        self, user_id: str, ctx: _UserContext,
    ) -> Optional[DeepResearchAgent]:
        if not self.job_registry:
            return None
        try:
            job_port, tier, _ = self.context_builder.resolve_async_context(
                "deep_research", self.job_registry, ctx.user_profile.config
            )
        except ValueError:
            logger.warning(
                "[UserAgentFactory] Deep research provider not registered, skipping"
            )
            return None
        return DeepResearchAgent(
            config=AgentConfig(
                agent_id=f"deep_research_agent_{user_id}",
                agent_type="deep_research",
                timeout_ms=DEEP_RESEARCH_CFG.timeout_ms,
                capabilities=["deep_research"],
            ),
            job_port=job_port,
            tier=tier,
            prompt_builder=ctx.prompt_builder,
            user_id=user_id,
            second_pass=ctx.user_profile.config.deep_research_second_pass,
        )

    def _build_claude_runner(
        self, user_id: str, ctx: _UserContext,
    ) -> Optional[ClaudeDeepResearchRunnerAgent]:
        if not self.anthropic_client:
            return None
        return ClaudeDeepResearchRunnerAgent(
            config=AgentConfig(
                agent_id=f"claude_deep_research_runner_{user_id}",
                agent_type="claude_deep_research_runner",
                timeout_ms=CLAUDE_DEEP_RESEARCH_RUNNER_CFG.timeout_ms,
                capabilities=["execute_deep_research_claude"],
            ),
            anthropic_client=self.anthropic_client,
        )

    def _build_file_management(
        self, user_id: str, ctx: _UserContext,
    ) -> Optional[FileManagementAgent]:
        if not (self.file_conversion_service and self.file_storage):
            return None
        return FileManagementAgent(
            config=AgentConfig(
                agent_id=f"file_management_agent_{user_id}",
                agent_type="file_management",
                timeout_ms=30_000,
                capabilities=["file_storage"],
            ),
            conversion_service=self.file_conversion_service,
            storage=self.file_storage,
        )

    def _build_domain_researcher(self, user_id: str, ctx: _UserContext) -> DomainResearcherAgent:
        execution_context = self.context_builder.build("domain_researcher", ctx.user_profile.config)
        return DomainResearcherAgent(
            config=AgentConfig(
                agent_id=f"domain_researcher_agent_{user_id}",
                agent_type="domain_researcher",
                timeout_ms=DOMAIN_RESEARCHER_CFG.timeout_ms,
                capabilities=["domain_research"],
            ),
            execution_context=execution_context,
            prompt_builder=ctx.prompt_builder,
            user_id=user_id,
            user_timezone=ctx.user_profile.config.timezone,
        )

    # -- Dispatch table: agent_type → builder method + base agent_id ----

    _LAZY_BUILDERS: Dict[str, Callable] = {
        "doc_generator": _build_doc_generator,
        "doc_planner": _build_doc_planner,
        "pdf_generator": _build_pdf_generator,
        "html_page": _build_html_page,
        "deep_research": _build_deep_research,
        "claude_deep_research_runner": _build_claude_runner,
        "file_management": _build_file_management,
        "domain_researcher": _build_domain_researcher,
    }

    _LAZY_AGENT_IDS: Dict[str, str] = {
        "doc_generator": "doc_generator_agent",
        "doc_planner": "doc_planner_agent",
        "pdf_generator": "pdf_generator_agent",
        "html_page": "html_page_generator_agent",
        "deep_research": "deep_research_agent",
        "claude_deep_research_runner": "claude_deep_research_runner",
        "file_management": "file_management_agent",
        "domain_researcher": "domain_researcher_agent",
    }

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
                logger.debug("Cache sweep task cancelled during shutdown")

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
                            "memory_agent", "web_agent",
                            "email_search_agent", "maps_agent", "compute_agent",
                            "tasks_agent", "notes_agent",
                            "consolidation_agent", "help_agent"):
                    agent = entry.get(key)
                    if agent and hasattr(agent, "agent_id"):
                        self.coordinator.unregister_agent(agent.agent_id)
                # Evict any lazy agents that were created on demand
                for agent_id in entry.get("_lazy_agent_ids", []):
                    self.coordinator.unregister_agent(agent_id)
                logger.info("♻️ [AgentFactory] Evicted expired cache for user %s", uid[:8])

    # ------------------------------------------------------------------
    # Provider validation
    # ------------------------------------------------------------------

    def _validate_anthropic_key(self, context: AgentExecutionContext, user_id: str) -> None:
        """Raise if context resolved to a Claude model but ANTHROPIC_API_KEY is absent."""
        if context.model_name.startswith("claude") and not self.config.get("ANTHROPIC_API_KEY"):
            raise ValueError(
                f"User {user_id} resolved to ANTHROPIC provider "
                "but ANTHROPIC_API_KEY is missing"
            )

    def get_session_store(self) -> SessionStore:
        return self.session_store

    def get_user_config(self, user_id: str) -> Optional["UserBotConfig"]:
        """Return cached UserBotConfig for a user, or None if not cached."""
        cached = self._cache.get(user_id)
        if not cached:
            return None
        ctx: Optional[_UserContext] = cached.get("_user_context")
        return ctx.user_profile.config if ctx else None
