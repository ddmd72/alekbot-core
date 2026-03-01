"""
Service Container
=================

Composition root for shared (singleton-per-worker) services.

Creates all infrastructure adapters and shared services once and provides
them to UserAgentFactory as typed ports. Owns the session store and the
overflow-callback's deferred factory reference.
"""

from typing import Callable, Optional, Any

from ..config.environment import EnvironmentConfig
from ..ports.account_repository import AccountRepository
from ..ports.llm_service import LLMService
from ..adapters.gemini_adapter import GeminiAdapter
from ..adapters.claude_adapter import ClaudeAdapter
from ..adapters.gemini_embedding_adapter import GeminiEmbeddingAdapter
from ..adapters.firestore_repo import FirestoreFactRepository
from ..adapters.firestore_session_store import FirestoreSessionStore
from ..adapters.firestore_prompt_repository import FirestorePromptComponentRepository
from ..adapters.groovy_prompt_assembler import GroovyPromptAssembler
from ..adapters.xml_prompt_assembler import XmlPromptAssembler
from ..adapters.fact_management_adapter import FactManagementAdapter
from ..domain.deduplication_service import SmartDeduplicationService
from ..services.configuration_service import ConfigurationService
from ..services.biographical_context_service import BiographicalContextService
from ..services.fact_write_service import FactWriteService
from ..services.provider_registry import ProviderRegistry
from ..services.agent_context_builder import AgentContextBuilder
from ..services.prompt_cache_strategy import PromptCacheStrategy
from ..services.prompt_component_service import PromptComponentService
from ..services.search_enrichment_service import SearchEnrichmentService
from ..services.email_search_service import EmailSearchService
from ..services.email_indexing_service import EmailIndexingService
from ..services.prompt_builder import PromptBuilder
from ..adapters.firestore_indexed_email_repo import FirestoreIndexedEmailRepository
from ..adapters.firestore_oauth_credentials_adapter import FirestoreOAuthCredentialsAdapter
from ..adapters.gmail_provider_adapter import GmailProviderAdapter
from ..adapters.firestore_email_job_repo import FirestoreEmailJobRepository
from ..adapters.firestore_email_exclusions_adapter import FirestoreEmailExclusionsAdapter
from ..agents.email_classification_agent import EmailClassificationAgent
from ..domain.agent import AgentConfig
from ..domain.user import UserBotConfig
from ..utils.logger import logger


class ServiceContainer:
    """
    Owns all shared (singleton-per-worker) services and adapters.

    Accepts infrastructure primitives (config, db_client, env_config) plus
    collaborator ports (account_repo, overflow_callback) and produces typed
    service instances for injection into UserAgentFactory via agent_services().
    """

    def __init__(
        self,
        config: dict,
        db_client: Any,
        env_config: EnvironmentConfig,
        account_repo: AccountRepository,
        overflow_callback: Optional[Callable] = None,
    ) -> None:
        # ------------------------------------------------------------------
        # LLM adapters
        # ------------------------------------------------------------------
        self.llm_service: LLMService = GeminiAdapter(api_key=config["GEMINI_API_KEY"])
        self.claude_service: LLMService = ClaudeAdapter(
            api_key=config.get("ANTHROPIC_API_KEY", "")
        )
        self.grok_service: Optional[LLMService] = self._init_grok(config)
        self.embedding_service = GeminiEmbeddingAdapter(api_key=config["GEMINI_API_KEY"])

        # ------------------------------------------------------------------
        # Email search adapters (shared, stateless)
        # ------------------------------------------------------------------
        self.indexed_email_repo = FirestoreIndexedEmailRepository(db_client, env_config)
        self.oauth_credentials = FirestoreOAuthCredentialsAdapter(db_client, env_config)
        self.gmail_provider = GmailProviderAdapter(
            client_id=config.get("GOOGLE_OAUTH_CLIENT_ID", ""),
            client_secret=config.get("GOOGLE_OAUTH_CLIENT_SECRET", ""),
        )
        self.email_search_service = EmailSearchService(
            indexed_email_repo=self.indexed_email_repo,
            oauth_credentials=self.oauth_credentials,
            gmail_provider=self.gmail_provider,
            embedding_service=self.embedding_service,
        )

        # Email indexing adapters (shared, stateless)
        self.email_job_repo = FirestoreEmailJobRepository(db_client, env_config)
        self.email_exclusions_repo = FirestoreEmailExclusionsAdapter(db_client, env_config)

        # ------------------------------------------------------------------
        # Config + biographical context (shared; per-user limits resolved later)
        # ------------------------------------------------------------------
        self.config_service = ConfigurationService()
        self.biographical_context_service = BiographicalContextService(
            repository=None,  # Resolved after repository is created (circular dep below)
            config_service=self.config_service,
            account_repo=account_repo,
        )

        # Fact repository — needs embedding_service and biographical_context_service
        self.repository = FirestoreFactRepository(
            db_client,
            env_config,
            embedding_service=self.embedding_service,
            biographical_context_service=self.biographical_context_service,
            dedup_service=SmartDeduplicationService(),
        )

        # Resolve circular dep: BiographicalContextService → repository
        self.biographical_context_service.set_repository(self.repository)

        # Shared fact write service (no per-user deps)
        self.fact_write_service = FactWriteService(
            repository=self.repository,
            embedding_service=self.embedding_service,
        )

        # ------------------------------------------------------------------
        # Provider registry + context builder
        # ------------------------------------------------------------------
        self.registry = ProviderRegistry()
        self.registry.register("gemini", self.llm_service)
        self.registry.register("claude", self.claude_service)
        if self.grok_service:
            self.registry.register("grok", self.grok_service)

        self.cache_strategy = PromptCacheStrategy()
        self.context_builder = AgentContextBuilder(
            self.registry,
            cache_strategy=self.cache_strategy,
        )

        # ------------------------------------------------------------------
        # Prompt v2 component infrastructure
        # ------------------------------------------------------------------
        self.prompt_component_repo = FirestorePromptComponentRepository(
            db_client=db_client,
            collection_name=f"{env_config.firestore_collection_prefix}prompt_components",
        )
        self.component_service = PromptComponentService(
            repository=self.prompt_component_repo,
            assembler={"groovy": GroovyPromptAssembler(), "xml": XmlPromptAssembler()},
            cache_ttl=3600,
        )

        # ------------------------------------------------------------------
        # Prompt v3 design system (optional — graceful fallback to None)
        # ------------------------------------------------------------------
        self.assembly_service = self._init_assembly_service(config, db_client, env_config)

        # ------------------------------------------------------------------
        # Email classification + indexing pipeline
        # ------------------------------------------------------------------
        _email_classifier_context = self.context_builder.build(
            "email_classifier", UserBotConfig()
        )
        _email_prompt_builder = PromptBuilder(
            repo=None, assembly_service=self.assembly_service
        )
        self.email_classifier = EmailClassificationAgent(
            config=AgentConfig(
                agent_id="email_classifier",
                agent_type="email_classifier",
            ),
            execution_context=_email_classifier_context,
            prompt_builder=_email_prompt_builder,
            gmail=self.gmail_provider,
        )
        self.email_indexing_service = EmailIndexingService(
            gmail=self.gmail_provider,
            email_repo=self.indexed_email_repo,
            job_repo=self.email_job_repo,
            exclusions_repo=self.email_exclusions_repo,
            classifier=self.email_classifier,
            embedding=self.embedding_service,
        )

        # ------------------------------------------------------------------
        # Session store
        # ------------------------------------------------------------------
        consolidation_settings = config["CONSOLIDATION"]
        self.session_store = FirestoreSessionStore(
            db_client,
            env_config.sessions_collection,
            max_history_length=consolidation_settings.threshold,
            batch_size=consolidation_settings.batch_size,
            overflow_callback=overflow_callback,
        )

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def agent_services(self) -> dict:
        """Return ports dict suitable for **-unpacking into UserAgentFactory."""
        return {
            "session_store": self.session_store,
            "llm_service": self.llm_service,
            "claude_service": self.claude_service,
            "grok_service": self.grok_service,
            "embedding_service": self.embedding_service,
            "repository": self.repository,
            "config_service": self.config_service,
            "biographical_context_service": self.biographical_context_service,
            "registry": self.registry,
            "context_builder": self.context_builder,
            "component_service": self.component_service,
            "assembly_service": self.assembly_service,
            "fact_write_service": self.fact_write_service,
            "fact_management_adapter_factory": self.create_fact_management_adapter,
            "email_search_service": self.email_search_service,
            "indexed_email_repo": self.indexed_email_repo,
            "email_job_repo": self.email_job_repo,
        }

    def create_fact_management_adapter(
        self, search_enrichment_service: SearchEnrichmentService
    ) -> FactManagementAdapter:
        """Create a per-user FactManagementAdapter with the given search service."""
        return FactManagementAdapter(
            repository=self.repository,
            embedding_service=self.embedding_service,
            fact_write_service=self.fact_write_service,
            search_enrichment_service=search_enrichment_service,
        )

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _init_grok(config: dict) -> Optional[LLMService]:
        if not config.get("XAI_API_KEY"):
            logger.info("ℹ️ Grok not configured (XAI_API_KEY not set)")
            return None
        try:
            from ..adapters.grok_adapter import GrokAdapter
            service = GrokAdapter(api_key=config["XAI_API_KEY"])
            logger.info("✅ Grok adapter initialized")
            return service
        except Exception as e:
            logger.error(f"❌ Failed to initialize Grok adapter: {e}")
            logger.warning("🤖 Bot will continue without Grok support")
            return None

    @staticmethod
    def _init_assembly_service(
        config: dict, db_client: Any, env_config: EnvironmentConfig
    ) -> Optional[Any]:
        logger.info("🔐 Initializing Prompt Design System v3 (optional)...")
        try:
            from ..adapters.security.regex_adapter import RegexSecurityAdapter
            from ..adapters.security.composite_adapter import CompositeAdapter
            from ..adapters.prompt_v3.firestore_token_repository import FirestoreTokenRepository
            from ..adapters.prompt_v3.firestore_blueprint_repository import FirestoreBlueprintRepository
            from ..adapters.prompt_v3.firestore_agent_profile_repository import FirestoreAgentProfileRepository
            from ..services.prompt_v3.prompt_assembly_service import PromptAssemblyService
            from ..services.prompt_v3.context_formatter import ContextFormatter
            from ..services.prompt_v3.biographical_formatter import BiographicalFactsFormatter

            security_port = CompositeAdapter(
                adapters=[RegexSecurityAdapter()],
                strategy="worst_case",
            )
            token_repo = FirestoreTokenRepository(
                db=db_client,
                system_collection=f"{env_config.domain_prompt_tokens_collection}_system",
                user_collection=f"{env_config.domain_prompt_tokens_collection}_user",
                security_port=security_port,
            )
            blueprint_repo = FirestoreBlueprintRepository(
                db=db_client,
                collection_name=env_config.domain_prompt_blueprints_collection,
            )
            profile_repo = FirestoreAgentProfileRepository(
                db=db_client,
                profiles_collection=env_config.domain_prompt_profiles_collection,
                overrides_collection=env_config.domain_prompt_overrides_collection,
            )
            service = PromptAssemblyService(
                token_repo=token_repo,
                blueprint_repo=blueprint_repo,
                profile_repo=profile_repo,
                security_port=security_port,
                formatter=ContextFormatter(),
                bio_formatter=BiographicalFactsFormatter(),
            )
            logger.info("✅ Prompt Design System v3 initialized successfully")
            return service
        except ImportError as e:
            logger.warning(f"⚠️ Prompt Design System v3 not available: {e}")
            return None
        except Exception as e:
            logger.error(f"❌ Failed to initialize Prompt Design System v3: {e}", exc_info=True)
            return None
