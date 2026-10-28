"""
Service Container
=================

Composition root for shared (singleton-per-worker) services.

Creates all infrastructure adapters and shared services once and provides
them to UserAgentFactory as typed ports. Owns the session store and the
overflow-callback's deferred factory reference.
"""

import os
from typing import Callable, Optional, Any

from ..config.environment import EnvironmentConfig
from ..ports.account_repository import AccountRepository
from ..ports.llm_port import LLMPort
from ..adapters.gemini_adapter import GeminiAdapter
from ..adapters.claude_adapter import ClaudeAdapter
from ..adapters.gemini_embedding_adapter import GeminiEmbeddingAdapter
from ..adapters.firestore_repo import FirestoreFactRepository
from ..adapters.firestore_session_store import FirestoreSessionStore
from ..adapters.firestore_prompt_repository import FirestorePromptComponentRepository
from ..adapters.groovy_prompt_assembler import GroovyPromptAssembler
from ..adapters.xml_prompt_assembler import XmlPromptAssembler
from ..adapters.fact_management_adapter import FactManagementAdapter
from ..adapters.in_memory_provider_resilience import InMemoryProviderResilience
from ..domain.deduplication_service import SmartDeduplication
from ..domain.drive_transcript_plan import effective_monthly_cap
from ..services.configuration_service import ConfigurationService
from ..services.biographical_context_service import BiographicalContextService
from ..services.fact_write_service import FactWriteService
from ..services.provider_registry import ProviderRegistry
from ..services.agent_context_builder import AgentContextBuilder
from ..services.alerting_llm_proxy import AlertingLLMProxy
from ..services.provider_breaker_alerter import ProviderBreakerAlerter
from ..services.caching_llm_proxy import CachingLLMProxy
from ..services.prompt_cache_strategy import PromptCacheStrategy
from ..services.prompt_component_service import PromptComponentService
from ..services.search_enrichment_service import SearchEnrichmentService
from ..services.email_search_service import EmailSearchService
from ..services.email_indexing_service import EmailIndexingService
from ..services.email_review_service import EmailReviewService
from ..services.prompt_builder import PromptBuilder
from ..adapters.firestore_indexed_email_repo import FirestoreIndexedEmailRepository
from ..adapters.firestore_oauth_credentials_adapter import FirestoreOAuthCredentialsAdapter
from ..adapters.gmail_provider_adapter import GmailProviderAdapter
from ..adapters.firestore_email_job_repo import FirestoreEmailJobRepository
from ..adapters.firestore_email_exclusions_adapter import FirestoreEmailExclusionsAdapter
# GoogleTasksAdapter frozen — deactivated in favour of MicrosoftToDoAdapter
# from ..adapters.google_tasks_adapter import GoogleTasksAdapter
from ..adapters.dateutil_recurrence_adapter import DateutilRecurrenceAdapter
from ..adapters.firestore_agent_note_adapter import FirestoreAgentNoteAdapter
from ..adapters.firestore_task_config_repository import FirestoreTaskConfigRepository
from ..adapters.firestore_task_search_index import FirestoreTaskSearchIndex
from ..adapters.microsoft import OneDriveAdapter
from ..adapters.microsoft.todo_adapter import MicrosoftToDoAdapter
from ..ports.drive_index_repository import DriveIndexRepository
from ..ports.drive_mirror_repository import DriveMirrorRepository
from ..ports.drive_note_repository import DriveNoteRepository
from ..ports.task_queue import TaskQueue
from ..ports.user_drive_port import UserDrivePort
from ..services.user_drive_service import UserDriveService
from ..adapters.firestore_user_repo import FirestoreUserRepository
from ..services.drive_search_service import DriveSearchService
from .drive_indexing_setup import build_drive_index_repository, build_drive_mirror_repository, build_drive_task_queue
from .drive_note_setup import (
    DRIVE_NOTES_ENABLED_ENV, add_drive_note_deleted_source, build_drive_note_repository, build_drive_note_service,
    drive_notes_switch_on,
)
from ..services.drive_note_service import DriveNoteService
from ..services.task_indexing_service import TaskIndexingService
from ..adapters.gcs_file_storage_adapter import GcsFileStorageAdapter
from ..adapters.gcs_workspace_adapter import GcsWorkspaceAdapter
from ..adapters.gcs_media_adapter import GcsMediaAdapter
from ..adapters.bigquery_prompt_content_adapter import BigQueryPromptContentAdapter
from ..adapters.firestore_skill_repository import FirestoreSkillRepository
from ..adapters.filesystem_skill_loader import load_system_skill_bundle, SYSTEM_SKILLS_ROOT
from ..adapters.security.composite_adapter import CompositeAdapter
from ..adapters.security.regex_adapter import RegexSecurityAdapter
from ..services.skill_service import SkillService
from ..services.memory_fact_service import MemoryFactService
from ..services.workspace_service import WorkspaceService
from ..adapters.firestore_memory_fact_draft_repository import FirestoreMemoryFactDraftRepository
from ..adapters.firestore_fact_lifecycle_counters import FirestoreFactLifecycleCounters
from ..adapters.firestore_fact_review_store import FirestoreFactReviewStore
from ..ports.fact_lifecycle_counters_port import FactLifecycleCountersPort
from ..ports.fact_review_store import FactReviewStore
from ..services.skill_file_resolver import SkillFileResolver
from ..services.file_conversion_service import FileConversionService
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
        threshold_resolver: Optional[Callable] = None,
        alert_webhook: Optional[Any] = None,
    ) -> None:
        # ------------------------------------------------------------------
        # LLM adapters
        # ------------------------------------------------------------------
        self.llm_port: LLMPort = GeminiAdapter(api_key=config["GEMINI_API_KEY"])
        self.claude_service: LLMPort = ClaudeAdapter(
            api_key=config.get("ANTHROPIC_API_KEY", "")
        )
        self.grok_service: Optional[LLMPort] = self._init_grok(config)
        self.openai_service: Optional[LLMPort] = self._init_openai(config)
        self.embedding_service = GeminiEmbeddingAdapter(api_key=config["GEMINI_API_KEY"])

        # Provider-level circuit breaker — process-local singleton.
        # Shared by all agents via AgentExecutionContext.resilience_port.
        # Replace with Redis/Firestore adapter when multi-instance deployment lands.
        # on_open escalates a tripped breaker to Slack: failover keeps users served
        # on transient faults, but a chronically-down provider would otherwise be
        # masked silently — the breaker trip is the "transient became chronic" signal.
        breaker_on_open = None
        if alert_webhook is not None:
            self.breaker_alerter = ProviderBreakerAlerter(alert_fn=alert_webhook.post)
            breaker_on_open = self.breaker_alerter.on_open
        self.resilience_port = InMemoryProviderResilience(on_open=breaker_on_open)

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

        # GoogleTasksAdapter deactivated — MicrosoftToDoAdapter is the active provider
        self.google_tasks_provider = None

        # MS To Do adapters (always instantiated; functional only when OAuth credentials present)
        self.task_config_repo = FirestoreTaskConfigRepository(db_client, env_config)
        self.task_search_index = FirestoreTaskSearchIndex(db_client, env_config)
        self.ms_todo_adapter: Optional[MicrosoftToDoAdapter] = None
        if config.get("MICROSOFT_TODO_CLIENT_ID") and config.get("MICROSOFT_TODO_CLIENT_SECRET"):
            self.ms_todo_adapter = MicrosoftToDoAdapter(
                oauth_credentials=self.oauth_credentials,
                task_config=self.task_config_repo,
                client_id=config["MICROSOFT_TODO_CLIENT_ID"],
                client_secret=config["MICROSOFT_TODO_CLIENT_SECRET"],
                webhook_secret=config.get("MICROSOFT_TASKS_WEBHOOK_SECRET"),
            )

        self.task_indexing: Optional[TaskIndexingService] = None
        if self.ms_todo_adapter:
            self.task_indexing = TaskIndexingService(
                embedding_service=self.embedding_service,
                search_index=self.task_search_index,
                tasks_provider=self.ms_todo_adapter,
            )

        # User drive — OneDrive App Folder (USER_DRIVE_RFC §4.2). Same app registration as To Do;
        # functional only once the user has connected it in the Cabinet. The adapter and the
        # service exist together or not at all: conversion reads and the agent's mutations hang
        # off this one pair.
        # The drive index (USER_DRIVE_INDEXING_RFC §4, §13) hangs off the same pair: its repository
        # (search, Cabinet counters, the fan-out) and the drive queue to the indexer service, which
        # carries the fan-out's ticks and UserDriveService's nudges. The queue is None until
        # DRIVE_INDEXER_URL is set (drive_indexing_setup.build_drive_task_queue).
        self.user_drive: Optional[UserDrivePort] = None
        self.user_drive_service: Optional[UserDriveService] = None
        self.drive_index_repo: Optional[DriveIndexRepository] = None
        self.drive_task_queue: Optional[TaskQueue] = None
        # The transcript mirror's roots and ledger (§27.9): the Step 1 guard, the deleted-files search and the open of
        # an archived transcript read them on the main service.
        self.drive_mirror_repo: Optional[DriveMirrorRepository] = None
        if config.get("MICROSOFT_TODO_CLIENT_ID") and config.get("MICROSOFT_TODO_CLIENT_SECRET"):
            self.user_drive = OneDriveAdapter(
                oauth_credentials=self.oauth_credentials,
                client_id=config["MICROSOFT_TODO_CLIENT_ID"],
                client_secret=config["MICROSOFT_TODO_CLIENT_SECRET"],
            )
            self.drive_index_repo = build_drive_index_repository(db_client, env_config)
            self.drive_task_queue = build_drive_task_queue(config, env_config)
            self.drive_mirror_repo = build_drive_mirror_repository(db_client, env_config)
            self.user_drive_service = UserDriveService(self.user_drive, nudge_queue=self.drive_task_queue,
                                                       mirror_roots=self.drive_mirror_repo)

        # Drive search (USER_DRIVE_INDEXING_RFC §10.1): over the index, on the main service. The opt-in
        # flag is read from Firestore on every search, never from the agent cache, so the service gets
        # its own (uncached) user repository.
        self.drive_search_service: Optional[DriveSearchService] = None
        # The transcription kill switch as the indexer sees it (os.getenv: an optional knob, not a secret): the Cabinet
        # shows the transcription disclosure only while it is on (same values as indexer_main.transcribe_enabled).
        self.drive_transcribe_switch_on: bool = (
            str(os.getenv("DRIVE_TRANSCRIBE_ENABLED") or "off").strip().lower() in ("1", "on", "true", "yes"))
        if self.user_drive is not None and self.drive_index_repo is not None:
            self.drive_search_service = DriveSearchService(
                repo=self.drive_index_repo, embeddings=self.embedding_service,
                users=FirestoreUserRepository(db_client, env_config, account_repo), drive=self.user_drive,
                embedding_space=GeminiEmbeddingAdapter.EMBEDDING_SPACE,
                # the Cabinet shows the transcription cap the indexer applies: the optional knob only lowers it (T-R3)
                transcribe_cap_usd=effective_monthly_cap(os.getenv("DRIVE_TRANSCRIBE_USD_CAP")),
                mirror=self.drive_mirror_repo,
            )

        # Owner notes on drive files (DRIVE_FILE_NOTES_RFC §4.4): on the same drive pair, with its own uncached user
        # repository (the indexing switch for the receipt's status). Built whatever the kill switch says: off stops
        # writes (no tool), reads keep answering (§3.6). Never handed to the memory side (§1.4 anti-goal). One note
        # store for the process: the service and the deleted-files source share `drive_note_repo`.
        self.drive_note_repo: Optional[DriveNoteRepository] = None
        self.drive_note_service: Optional[DriveNoteService] = None
        self.drive_notes_enabled: bool = drive_notes_switch_on(os.getenv(DRIVE_NOTES_ENABLED_ENV))
        if self.user_drive is not None and self.drive_index_repo is not None:
            self.drive_note_repo = build_drive_note_repository(db_client, env_config)
            self.drive_note_service = build_drive_note_service(
                db_client, env_config, notes=self.drive_note_repo, drive=self.user_drive, index=self.drive_index_repo,
                embeddings=self.embedding_service, users=FirestoreUserRepository(db_client, env_config, account_repo),
                nudge_queue=self.drive_task_queue,
            )
            # Owner notes on deleted files join the same deleted-files section (§6.4, §0.3); a read, so added whatever
            # DRIVE_NOTES_ENABLED says (§3.6). The search service is built under the same condition above.
            if self.drive_search_service is not None:
                add_drive_note_deleted_source(self.drive_search_service, self.drive_note_repo, self.embedding_service,
                                              embedding_space=GeminiEmbeddingAdapter.EMBEDDING_SPACE)

        self.notes_adapter = FirestoreAgentNoteAdapter(db_client, env_config)
        self.recurrence_adapter = DateutilRecurrenceAdapter()

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
            dedup_service=SmartDeduplication(),
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
        self.registry.register("gemini", self.llm_port)
        self.registry.register("claude", self.claude_service)
        if self.grok_service:
            self.registry.register("grok", self.grok_service)
        if self.openai_service:
            self.registry.register("openai", self.openai_service)

        self.cache_strategy = PromptCacheStrategy()
        # Alerting proxy factory — only when an alert sink (Slack webhook) is wired.
        # Wraps every agent's provider so any LLMClientError (4xx) pushes an alert.
        self.alert_webhook = alert_webhook
        alerting_proxy_factory = None
        if alert_webhook is not None:
            def alerting_proxy_factory(inner):
                return AlertingLLMProxy(inner, alert_fn=alert_webhook.post)
        self.context_builder = AgentContextBuilder(
            self.registry,
            resilience_port=self.resilience_port,
            cache_strategy=self.cache_strategy,
            caching_proxy_factory=CachingLLMProxy,
            alerting_proxy_factory=alerting_proxy_factory,
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
        self.assembly_service, self.profile_repo = self._init_assembly_service(config, db_client, env_config)

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
            oauth=self.oauth_credentials,
        )
        self.email_review_service = EmailReviewService(
            email_provider=self.gmail_provider,
            oauth_credentials=self.oauth_credentials,
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
            threshold_resolver=threshold_resolver,
        )

        # ------------------------------------------------------------------
        # File storage
        # ------------------------------------------------------------------
        gcs_bucket = config.get("GCS_MEDIA_BUCKET", "")
        self.file_storage = GcsFileStorageAdapter(gcs_bucket) if gcs_bucket else None
        # MediaStoragePort for re-reading system-delivered documents (docs/,
        # email_review/, deep_research/) via open_file — see FileConversionService.
        self.media_storage = (
            GcsMediaAdapter(bucket_name=gcs_bucket)
            if gcs_bucket else None
        )
        # ------------------------------------------------------------------
        # Agent Skills (RFC §15) — built after file storage: skill files are
        # read through open_file (FileConversionService → SkillFileResolver),
        # and draft_skill's from_file copies an upload through the same service.
        # ------------------------------------------------------------------
        # System skills (and their text files) are read from git (src/skills/smart/)
        # once at startup. Deliberately NOT wrapped in try/except: a malformed skill
        # is a packaging bug that must fail the deploy, not silently vanish from
        # is_system() checks.
        skill_repository = FirestoreSkillRepository(db_client, env_config)
        # Agent workspace (AGENT_WORKSPACE_RFC §5): Smart's own working files in a dedicated versioned bucket.
        # Empty key → no adapter: no tool, workspace: refs refused, NotesAgent refuses workspace_file (§5.5).
        workspace_bucket = config.get("GCS_WORKSPACE_BUCKET", "")
        self.workspace_store = GcsWorkspaceAdapter(workspace_bucket) if workspace_bucket else None
        system_skills, system_skill_contents = load_system_skill_bundle(SYSTEM_SKILLS_ROOT)
        skill_file_resolver = SkillFileResolver(
            skill_repository, system_skills, system_skill_contents
        )
        self.file_conversion_service = (
            FileConversionService(
                storage=self.file_storage,
                media_storage=self.media_storage,
                skill_files=skill_file_resolver,
                drive=self.user_drive,
                workspace=self.workspace_store,
            )
            if self.file_storage else None
        )
        # A second stateless CompositeAdapter instance is deliberate here: the assembly
        # service's own CompositeAdapter is a local inside build_prompt_assembly_service and
        # _init_assembly_service may return None — widening its return for skills would couple two
        # unrelated features.
        self.skill_service = SkillService(
            repository=skill_repository,
            security_port=CompositeAdapter(adapters=[RegexSecurityAdapter()], strategy="worst_case"),
            system_skills=system_skills,
            file_conversion=self.file_conversion_service,
        )
        # Owner-confirmed memory facts (MEMORY_FACT_CONFIRMATION_RFC): drafts are
        # stored here, saved only by the owner's `$memory save <code>`.
        self.memory_fact_service = MemoryFactService(
            repository=FirestoreMemoryFactDraftRepository(db_client, env_config),
            security_port=CompositeAdapter(adapters=[RegexSecurityAdapter()], strategy="worst_case"),
            session_store=self.session_store,
        )

        # The fact lifecycle's per-day numbers (D10): the consolidator adds its expiry tool errors, the janitor its run.
        self.fact_lifecycle_counters: FactLifecycleCountersPort = FirestoreFactLifecycleCounters(db_client, env_config)
        # The memory review's questions and per-account state (Facts S4): the review, the owner's answers and the
        # question texts share this one store.
        self.fact_review_store: FactReviewStore = FirestoreFactReviewStore(db_client, env_config)

        # Built after FileConversionService: copy sources (uploads, delivered documents) go through it (§12).
        self.workspace_service = (
            WorkspaceService(store=self.workspace_store, file_conversion=self.file_conversion_service)
            if self.workspace_store else None
        )

        # ------------------------------------------------------------------
        # LLM prompt/response content store (BigQuery, 30-day TTL).
        # DEBUG_PROMPTS is the GLOBAL capture switch (write / don't write) — it
        # gates whether the store is wired at all, so None propagates through the
        # factory and BaseAgent skips the write. The wired adapter is BigQuery;
        # the flag decides on/off, not which adapter. BIGQUERY_PROMPT_DATASET is
        # required config for the adapter (no dataset → nothing to write to).
        # ------------------------------------------------------------------
        capture_enabled = config.get("DEBUG_PROMPTS", "false").lower() == "true"
        bq_dataset = config.get("BIGQUERY_PROMPT_DATASET", "")
        self.prompt_content_store = (
            BigQueryPromptContentAdapter(
                dataset=bq_dataset,
                table=config.get("BIGQUERY_PROMPT_TABLE", "prompt_content"),
                project=config.get("GOOGLE_CLOUD_PROJECT", "") or "",
            )
            if capture_enabled and bq_dataset else None
        )

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def agent_services(self) -> dict:
        """Return ports dict suitable for **-unpacking into UserAgentFactory."""
        return {
            "session_store": self.session_store,
            "llm_port": self.llm_port,
            "claude_service": self.claude_service,
            "grok_service": self.grok_service,
            "openai_service": self.openai_service,
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
            "tasks_provider": self.ms_todo_adapter,
            "task_indexing": self.task_indexing,
            "notes_provider": self.notes_adapter,
            "recurrence": self.recurrence_adapter,
            "file_conversion_service": self.file_conversion_service,
            "file_storage": self.file_storage,
            "user_drive_service": self.user_drive_service,
            "drive_search_service": self.drive_search_service,
            "prompt_content_store": self.prompt_content_store,
            "skill_service": self.skill_service,
            "memory_fact_service": self.memory_fact_service,
            "lifecycle_counters": self.fact_lifecycle_counters,
            "workspace_service": self.workspace_service,
            "drive_note_service": self.drive_note_service,
            "drive_notes_enabled": self.drive_notes_enabled,
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
    def _init_grok(config: dict) -> Optional[LLMPort]:
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
    def _init_openai(config: dict) -> Optional[LLMPort]:
        if not config.get("OPENAI_API_KEY"):
            logger.info("ℹ️ OpenAI not configured (OPENAI_API_KEY not set)")
            return None
        try:
            from ..adapters.openai_adapter import OpenAIAdapter
            service = OpenAIAdapter(api_key=config["OPENAI_API_KEY"])
            logger.info("✅ OpenAI adapter initialized")
            return service
        except Exception as e:
            logger.error(f"❌ Failed to initialize OpenAI adapter: {e}")
            logger.warning("🤖 Bot will continue without OpenAI support")
            return None

    @staticmethod
    def _init_assembly_service(
        config: dict, db_client: Any, env_config: EnvironmentConfig
    ) -> tuple:
        """Returns (assembly_service, profile_repo). Both None on failure."""
        logger.info("🔐 Initializing Prompt Design System v3 (optional)...")
        try:
            from .prompt_assembly_setup import build_prompt_assembly_service

            service, profile_repo = build_prompt_assembly_service(db_client, env_config)
            logger.info("✅ Prompt Design System v3 initialized successfully")
            return service, profile_repo
        except ImportError as e:
            logger.warning(f"⚠️ Prompt Design System v3 not available: {e}")
            return None, None
        except Exception as e:
            logger.error(f"❌ Failed to initialize Prompt Design System v3: {e}", exc_info=True)
            return None, None
