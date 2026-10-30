"""The application graph: every non-web object `main()` serves, built in one place.

`main()` keeps the I/O it starts with (telemetry, settings, the Firestore client and its warm-up) and the web layer
(blueprints, voice, MCP, `/worker`, hypercorn); composition/ must not import `src.web`. Everything between is built here.
"""
import asyncio
import os
from dataclasses import dataclass
from typing import Any, Callable, Optional

from anthropic import AsyncAnthropic
from slack_bolt.async_app import AsyncApp
from telegram import Bot
from twilio.rest import Client as TwilioClient

from src.adapters.claude_deep_research_adapter import ClaudeDeepResearchAdapter
from src.adapters.cloud_run_jobs_adapter import CloudRunJobsAdapter
from src.adapters.file_localization_adapter import FileLocalizationAdapter
from src.adapters.firebase_auth_adapter import FirebaseAuthAdapter
from src.adapters.firestore_account_repo import FirestoreAccountRepository
from src.adapters.firestore_channel_binding_adapter import FirestoreChannelBindingAdapter
from src.adapters.firestore_companion_cache_repository import FirestoreCompanionCacheRepository
from src.adapters.firestore_companion_extraction_queue import FirestoreCompanionExtractionQueue
from src.adapters.firestore_companion_memory_repository import FirestoreCompanionMemoryRepository
from src.adapters.firestore_consolidation_queue import FirestoreConsolidationQueue
from src.adapters.firestore_dedup_store import FirestoreDedupStore
from src.adapters.firestore_invite_code_repo import FirestoreInviteCodeRepository
from src.adapters.firestore_long_turn_registry import FirestoreLongTurnRegistry
from src.adapters.firestore_memory_write_lease import FirestoreMemoryWriteLease
from src.adapters.firestore_notification_state_adapter import FirestoreNotificationStateAdapter
from src.adapters.firestore_quota_service import FirestoreQuotaService
from src.adapters.firestore_short_link_repository import FirestoreShortLinkRepository
from src.adapters.firestore_task_claim_store import FirestoreTaskClaimStore
from src.adapters.firestore_user_repo import FirestoreUserRepository
from src.adapters.firestore_whitelist_repo import FirestoreWhitelistRepository
from src.adapters.gcp_log_sink import GcpLogSink
from src.adapters.gcp_task_queue import GcpTaskQueue
from src.adapters.gcs_media_adapter import GcsMediaAdapter
from src.adapters.grok_image_adapter import GrokImageAdapter
from src.adapters.grok_video_adapter import GrokVideoAdapter
from src.adapters.notification_channel_factory import NotificationChannelFactory
from src.adapters.openai_deep_research_adapter import OpenAIDeepResearchAdapter
from src.adapters.openai_transcription_adapter import OpenAITranscriptionAdapter
from src.adapters.platform.factory import PlatformAdapterFactory
from src.adapters.playwright_html_renderer import PlaywrightHtmlRenderer
from src.adapters.plain_text_interactive_message_adapter import PlainTextInteractiveMessageAdapter
from src.adapters.slack.http_adapter import HTTPModeAdapter
from src.adapters.slack.interactive_message_adapter import PLATFORM as SLACK_PLATFORM
from src.adapters.slack.interactive_message_adapter import SlackInteractiveMessageAdapter
from src.adapters.slack.media_adapter import SlackMediaAdapter
from src.adapters.slack.response_channel import SlackResponseChannel
from src.adapters.slack.webhook_adapter import SlackWebhookAdapter
from src.adapters.telegram.media_adapter import TelegramMediaAdapter
from src.adapters.telegram.response_channel import TelegramResponseChannel
from src.adapters.telegram.webhook_adapter import TelegramWebhookAdapter
from src.services.rich_content_service import RichContentService
from src.agents.infrastructure.billing_agent import BillingAgent
from src.agents.infrastructure.logger_agent import LoggerAgent
from src.composition import cipher_setup
from src.composition.companion_extractor_runner import CompanionExtractorRunner
from src.composition.drive_indexing_setup import build_drive_sync_scheduler
from src.composition.drive_note_setup import command_drive_note_service
from src.composition.service_container import ServiceContainer
from src.composition.slack_adapter_factory import SlackAdapterFactory
from src.composition.telegram_adapter_factory import TelegramAdapterFactory
from src.composition.user_agent_factory import UserAgentFactory
from src.config.auth import AuthConfig
from src.config.environment import validate_telegram_config
from src.domain.agent import AgentConfig
from src.domain.fact_lifecycle_sweep import sweep_dry_run
from src.domain.fact_review_question import FACT_QUESTION_KIND
from src.domain.language import LanguageCode
from src.domain.notification_kind import NotificationKind as _NotificationKind
from src.domain.settings import SearchConfig
from src.handlers.agent_worker_handler import AgentWorkerHandler
from src.handlers.worker_handler import WorkerHandler
from src.infrastructure.agent_config import CONSOLIDATION as CONSOLIDATION_CFG
from src.infrastructure.agent_coordinator import AgentCoordinator
from src.infrastructure.agent_manifest import ALL_DESCRIPTORS
from src.infrastructure.agent_registry import AgentRegistry
from src.infrastructure.interactive_message_router import InteractiveMessageRouter
from src.infrastructure.notification_sla import NOTIFICATION_SLA, dispatch_deadline_s
from src.ports.cipher_port import CipherPort
from src.services.agent_fallback_service import AgentFallbackService
from src.services.authentication_service import AuthenticationService
from src.services.auth_provider_registry import AuthProviderRegistry
from src.services.cabinet_fact_service import CabinetFactService
from src.services.channel_binding_service import ChannelBindingService
from src.services.companion_context_assembler_service import CompanionContextAssemblerService
from src.services.companion_extraction_service import CompanionExtractionService
from src.services.companion_window_resolver import CompanionWindowResolver
from src.services.consolidation_service import ConsolidationService
from src.services.deep_research_drive_copy_service import DeepResearchDriveCopyService
from src.services.document_delivery_service import DocumentDeliveryService
from src.services.email_embedding_repair_service import EmailEmbeddingRepairService
from src.services.fact_answer_service import FactAnswerService
from src.services.fact_lifecycle_sweep_service import FactLifecycleSweepService
from src.services.fact_question_texts import FactQuestionTexts
from src.services.fact_review_service import FactReviewService
from src.services.file_access_token_service import FileAccessTokenService
from src.services.file_link_service import FileLinkService
from src.services.file_upload_service import FileUploadService
from src.services.iam_service import IAMService
from src.services.interactive_choice_service import InteractiveChoiceService
from src.services.invite_code_service import InviteCodeService
from src.services.language_preference_service import LanguagePreferenceService
from src.services.localization_service import LocalizationService as _LocalizationService
from src.services.long_turn_service import LongTurnService
from src.services.overflow_routing_service import OverflowRoutingService
from src.services.owner_timezone_resolver import OwnerTimezoneResolver
from src.services.prompt_builder import PromptBuilder
from src.services.provider_registry import ProviderRegistry
from src.services.reminders_service import RemindersService
from src.services.search_enrichment_service import SearchEnrichmentService
from src.services.session_service import SessionService
from src.services.short_link_service import ShortLinkService
from src.services.task_dispatch_service import TaskDispatchService
from src.services.task_setup_service import TaskSetupService
from src.composition.connector_registry import GMAIL, MICROSOFT_TODO
from src.composition.drive_connection_setup import build_drive_activation
from src.composition.oauth_connect_setup import build_connect_activations, build_oauth_connect_service
from src.services.credential_revocation_service import CredentialRevocationService
from src.services.oauth_connect_service import OAuthConnectService
from src.services.integration_reconnect_service import IntegrationReconnectService
from src.services.user_notification_service import UserNotificationService
from src.utils.logger import logger


@dataclass
class ApplicationGraph:
    """What `build_application_graph` built: the handlers `main()` serves, what its blueprints take, and what to start and stop."""

    container: ServiceContainer
    agent_factory: UserAgentFactory
    coordinator: AgentCoordinator
    worker_handler: WorkerHandler
    agent_worker_handler: AgentWorkerHandler
    slack_adapter: HTTPModeAdapter
    telegram_adapter: Optional[TelegramWebhookAdapter]
    notification_service: UserNotificationService
    # consumed by main()'s blueprint calls and its voice, MCP, file-access, short-link and landing wiring
    user_repo: FirestoreUserRepository
    account_repo: FirestoreAccountRepository
    invite_service: InviteCodeService
    cabinet_facts: CabinetFactService
    fact_answers: Optional[FactAnswerService]
    quota_service: FirestoreQuotaService
    alert_sink: Optional[SlackWebhookAdapter]
    session_service: SessionService
    auth_service: AuthenticationService
    auth_registry: AuthProviderRegistry
    auth_config: AuthConfig
    # the integration connect flow and the Cabinet's credential removal (MULTI_DRIVE_RFC §4.3, §5.2)
    connect_service: OAuthConnectService
    credential_revocation: CredentialRevocationService
    task_setup_service: Optional[TaskSetupService]
    reconnect_service: IntegrationReconnectService
    twilio_verify_client: Any
    task_dispatch: Optional[TaskDispatchService]
    deep_research_fetch: Optional[Callable[..., Any]]
    deep_research_dedup_store: FirestoreDedupStore
    file_token_service: FileAccessTokenService
    media_storage: Optional[GcsMediaAdapter]
    short_link_service: Optional[ShortLinkService]
    companion_extractor_runner: CompanionExtractorRunner
    language_service: LanguagePreferenceService
    localization: _LocalizationService
    # the stored-secrets cipher and the verify-only digest key (SECRETS_AT_REST_RFC §2.6), consumed by main()'s MCP wiring
    cipher: CipherPort
    digest_key: bytes
    # needed by start() and shutdown()
    agent_task_queue: Optional[GcpTaskQueue]
    billing_agent: BillingAgent
    logger_agent: LoggerAgent
    html_renderer: Optional[PlaywrightHtmlRenderer]

    async def start(self) -> None:
        """Create the agent-task queue if there is one, then start the billing and logger agents and the agent factory."""
        if self.agent_task_queue is not None:
            await self.agent_task_queue.create_queue_if_not_exists()
            logger.info(f"📬 Agent task queue initialized: {self.agent_task_queue.queue_name}")
        await self.billing_agent.start()
        await self.logger_agent.start()
        await self.agent_factory.start()

    async def shutdown(self) -> None:
        """Drain the session store's background tasks, then stop the factory, the agents and the renderer."""
        logger.info("🛑 Draining background tasks...")
        session_store = self.container.session_store
        if session_store._pending_tasks:
            await asyncio.gather(*list(session_store._pending_tasks), return_exceptions=True)
        await self.agent_factory.shutdown()
        await self.billing_agent.shutdown()
        await self.logger_agent.shutdown()
        if self.html_renderer:
            await self.html_renderer.stop()


def build_application_graph(config: dict, db_client: Any) -> ApplicationGraph:
    """Build every non-web object of the main service from the settings dict and the Firestore client; starts nothing."""
    env_config = config["ENVIRONMENT_CONFIG"]

    # Fail closed before anything else is built (SECRETS_AT_REST_RFC I6, plan D-P1): here, never inside the MCP
    # build, whose errors main() swallows.
    cipher = cipher_setup.build_cipher(config.get("KMS_CRYPTO_KEY"))
    secret_digest_key = cipher_setup.digest_key(config.get("SECRET_DIGEST_KEY"))

    logger.info("� Initializing Account Repository...")
    account_repo = FirestoreAccountRepository(
        db_client=db_client,
        collection_name=env_config.account_collection_name
    )

    logger.info("�👤 Initializing User Repository...")
    user_repo = FirestoreUserRepository(db_client, env_config, account_repo)

    logger.info("💌 Initializing Invite Code Service...")

    invite_repo = FirestoreInviteCodeRepository(db_client, env_config, digest_key=secret_digest_key)

    # IAM-Centric Architecture (2026-02-05)
    logger.info("📋 Initializing Whitelist Repository...")
    whitelist_repo = FirestoreWhitelistRepository(db_client, env_config)

    logger.info("🆔 Initializing IAM Service...")
    iam_service = IAMService(
        user_repo=user_repo,
        account_repo=account_repo,
        whitelist_repo=whitelist_repo
    )

    logger.info("💌 Initializing Invite Code Service (with whitelist)...")
    invite_service = InviteCodeService(
        invite_repo,
        user_repo,
        account_repo,
        whitelist_repo
    )

    logger.info("🎯 Initializing Agent Registry...")
    agent_registry = AgentRegistry()
    for descriptor in ALL_DESCRIPTORS:
        agent_registry.register(descriptor)

    agent_task_queue = None
    if config.get("GOOGLE_CLOUD_PROJECT"):
        queue_suffix = "dev" if env_config.is_development else "prod"
        service_url = config.get("CLOUD_RUN_SERVICE_URL") or "http://localhost:8080"
        agent_task_queue = GcpTaskQueue(
            project_id=config["GOOGLE_CLOUD_PROJECT"],
            location="us-central1",
            queue_name=f"agent-tasks-{queue_suffix}",
            service_url=service_url,
            service_account_email=config.get("SERVICE_ACCOUNT_EMAIL"),
        )
    else:
        logger.info("📬 Agent task queue: disabled (no GCP project)")

    # Ops alert sink — shared by the AgentCoordinator (delegation-loop alerts),
    # AlertingLLMProxy (LLM 4xx alerts), the billing daily summary, and the daily
    # budget alert in FirestoreQuotaService. Optional: wired only when
    # BILLING_SLACK_WEBHOOK_URL is set. Built here because the coordinator, just
    # below, is the earliest consumer.
    _alert_webhook = None
    _alert_webhook_url = os.getenv("BILLING_SLACK_WEBHOOK_URL")
    if _alert_webhook_url:
        _alert_webhook = SlackWebhookAdapter(_alert_webhook_url)
        logger.info("✅ Ops Slack webhook configured (billing + LLM client-error alerts)")

    logger.info("💳 Initializing Billing Agent...")
    quota_service = FirestoreQuotaService(account_repo, alert_sink=_alert_webhook)
    billing_agent = BillingAgent(
        config=AgentConfig(
            agent_id="billing_agent",
            agent_type="billing",
            timeout_ms=None,
            capabilities=["usage_tracking", "quota_management"]
        ),
        quota_service=quota_service
    )

    logger.info("🧾 Initializing Logger Agent...")
    log_sink = None
    if not env_config.is_development:
        try:
            log_sink = GcpLogSink()
        except Exception as e:
            logger.error(f"❌ Failed to initialize GcpLogSink: {e}")
            raise

    logger_agent = LoggerAgent(
        config=AgentConfig(
            agent_id="logger_agent",
            agent_type="logger",
            timeout_ms=None,
            capabilities=["centralized_logging", "trace_correlation"]
        ),
        env_config=env_config,
        log_sink=log_sink
    )

    logger.info("📦 Initializing Consolidation Queue...")
    consolidation_queue = FirestoreConsolidationQueue(
        db_client=db_client,
        env_config=env_config
    )

    logger.info("🔗 Initializing Channel Binding Service...")
    channel_binding_adapter = FirestoreChannelBindingAdapter(
        db_client=db_client, env_config=env_config
    )
    channel_binding_service = ChannelBindingService(port=channel_binding_adapter)

    logger.info("🧑‍🏫 Initializing Companion Window Resolver...")
    companion_window_resolver = CompanionWindowResolver(channel_binding_service)

    # Overflow routing — when a session's sliding window overflows, the batch goes
    # either to Alek's consolidation pipeline or to a companion's extraction pipeline,
    # decided by the channel binding (RFC docs/10_rfcs/COMPANION_AGENTS_RFC.md §9 item 3:
    # "Absorb the ad-hoc flags one at a time: ChannelBinding stateless first").
    # Built here, not with the rest of the companion pipeline further below, because
    # ServiceContainer's session store needs overflow_callback at construction time.
    # companion_extraction_queue is constructed here for the same reason and reused
    # by the companion extraction pipeline below.
    logger.info("🌊 Initializing Overflow Routing Service...")

    companion_extraction_queue = FirestoreCompanionExtractionQueue(db_client=db_client, env_config=env_config)
    overflow_routing_service = OverflowRoutingService(
        channel_binding_service=channel_binding_service,
        user_repo=user_repo,
        consolidation_queue=consolidation_queue,
        companion_extraction_queue=companion_extraction_queue,
        task_queue=agent_task_queue,
    )
    overflow_callback = overflow_routing_service.route_overflow

    # 1. Shared service container (LLM adapters, repositories, prompt infra, session store)
    logger.info("🏭 Initializing Service Container...")
    container = ServiceContainer(
        config=config,
        db_client=db_client,
        env_config=env_config,
        account_repo=account_repo,
        overflow_callback=overflow_callback,
        threshold_resolver=companion_window_resolver.resolve,
        alert_webhook=_alert_webhook,
        cipher=cipher,
    )
    file_service = FileUploadService(container.llm_port)
    session_store = container.session_store  # Alias for Slack/Telegram adapters and shutdown

    # Long-running turns (LONG_RUNNING_TURNS_RFC) — registry + service shared by both
    # platform adapters (status/late-answer delivery) and UserAgentFactory (Smart's
    # running-jobs note + cancel_long_turn tool).
    long_turn_registry = FirestoreLongTurnRegistry(db_client, env_config.long_turns_collection)
    long_turn_service = LongTurnService(registry=long_turn_registry, session_store=session_store)

    logger.info("🎯 Initializing Agent Coordinator...")
    # The file ref resolver is wired into the coordinator for specialist delegation
    coordinator = AgentCoordinator(
        registry=agent_registry,
        task_queue=agent_task_queue,
        file_ref_resolver=(
            container.file_conversion_service.resolve_content if container.file_conversion_service else None
        ),
        alert_sink=_alert_webhook,
    )
    coordinator.register_agent(billing_agent)
    coordinator.register_agent(logger_agent)

    # Async job adapters (Deep Research) — pure API clients, no queue logic.
    # Per-user selection via UserBotConfig.agent_providers["deep_research"]
    # ("claude" default, "openai" alternative). Tier → model mapping is adapter-internal.
    # Optional model_override pins a specific model from env var, bypassing tier mapping.
    # Gemini provider removed 2026-05-29 — see decisions/gemini_deep_research_adapter_removal.md.
    job_registry = ProviderRegistry()
    _openai_dr_fetch = None
    if config.get("OPENAI_API_KEY"):
        _openai_dr_adapter = OpenAIDeepResearchAdapter(
            api_key=config["OPENAI_API_KEY"],
            webhook_url=config.get("OPENAI_DEEP_RESEARCH_WEBHOOK_URL"),
            model_override=os.getenv("OPENAI_DEEP_RESEARCH_MODEL"),
        )
        _openai_dr_fetch = _openai_dr_adapter.fetch_response
        job_registry.register("openai", _openai_dr_adapter)
        logger.info("🔬 Deep research adapter registered: provider=openai")
    if config.get("ANTHROPIC_API_KEY") and config.get("GOOGLE_CLOUD_PROJECT"):
        queue_suffix = "dev" if env_config.is_development else "prod"
        cloud_jobs_adapter = CloudRunJobsAdapter(
            project=config["GOOGLE_CLOUD_PROJECT"],
            region="us-central1",
        )
        job_registry.register("claude", ClaudeDeepResearchAdapter(
            job_runner=cloud_jobs_adapter,
            job_name=f"alek-research-job-{queue_suffix}",
            model_override=os.getenv("CLAUDE_DEEP_RESEARCH_MODEL"),
        ))
        logger.info("🔬 Deep research adapter registered: provider=claude (Cloud Run Job)")

    # Image generation adapter — reuses the same XAI_API_KEY secret as GrokAdapter
    # (LLMPort). Separate registry/adapter: ImageGenerationPort is a structurally
    # different port (see docs/10_rfcs/IMAGE_GENERATION_RFC.md §3.6-3.7).
    image_registry = ProviderRegistry()
    if config.get("XAI_API_KEY"):
        image_registry.register("grok", GrokImageAdapter(api_key=config["XAI_API_KEY"]))
        logger.info("🎨 Image generation adapter registered: provider=grok")
    else:
        logger.info("ℹ️ Image generation not configured (XAI_API_KEY not set)")

    # Video generation adapter — reuses the same XAI_API_KEY secret as
    # GrokImageAdapter/GrokAdapter. Separate registry/adapter: VideoGenerationPort
    # is a structurally different port (see
    # docs/10_rfcs/VIDEO_GENERATION_RFC.md §3.5-3.6). Needs task_queue — the
    # adapter enqueues the first poll tick as a side effect of submission.
    # GrokVideoAdapter.__init__ type-hints task_queue: TaskQueue (Task 1) — the raw
    # port, not the TaskDispatchService services/-layer wrapper (which also isn't
    # constructed until much later in this function, after UserAgentFactory). Passing
    # agent_task_queue (already constructed above, the actual TaskQueue implementation)
    # both satisfies the adapter's real dependency and keeps adapters/ depending only
    # on ports/, not services/.
    video_registry = ProviderRegistry()
    if config.get("XAI_API_KEY"):
        video_registry.register(
            "grok", GrokVideoAdapter(api_key=config["XAI_API_KEY"], task_queue=agent_task_queue)
        )
        logger.info("🎬 Video generation adapter registered: provider=grok")
    else:
        logger.info("ℹ️ Video generation not configured (XAI_API_KEY not set)")

    # GCS media adapter for HTML report uploads (optional — requires GCS_MEDIA_BUCKET).
    # Keyless V4 signed-URL minting via IAM signBlob as the runtime's own identity
    # (for the /f/<token> capability route); no signer email is passed in.
    gcs_bucket = config.get("GCS_MEDIA_BUCKET", "")
    gcs_media_adapter = (
        GcsMediaAdapter(bucket_name=gcs_bucket)
        if gcs_bucket else None
    )

    # File access: capability tokens + link builder for private file storage.
    # Reuses the same HS256 secret as the Cabinet session service. Constructed
    # here (before delivery services) so DocumentDeliveryService can mint links.
    _file_secret = config.get("OAUTH_SESSION_SECRET") or "dev-secret-change-in-production-must-be-32-chars-long"
    file_token_service = FileAccessTokenService(secret_key=_file_secret)
    file_link_service = (
        FileLinkService(
            token_service=file_token_service,
            base_url=config.get("CLOUD_RUN_SERVICE_URL") or "http://localhost:8080",
        )
        if gcs_media_adapter else None
    )

    # Short links: /s/<code> alias wrapping the /f/<token> capability link,
    # so what lands in chat is short. Firestore-backed (repo layer only —
    # ShortLinkService owns the code-generation/collision logic).
    short_link_service = (
        ShortLinkService(
            repository=FirestoreShortLinkRepository(db_client=db_client, env_config=env_config),
            base_url=config.get("CLOUD_RUN_SERVICE_URL") or "http://localhost:8080",
        )
        if gcs_media_adapter else None
    )

    # Notification service — channel adapters (Slack, Telegram) registered later via set_* methods.
    notification_state_repo = FirestoreNotificationStateAdapter(
        db_client=db_client, env_config=env_config
    )
    notification_channel_factory = NotificationChannelFactory()  # adapters wired below
    notification_service = UserNotificationService(
        state_repo=notification_state_repo,
        channel_factory=notification_channel_factory,
        coordinator=coordinator,
        notification_sla=NOTIFICATION_SLA,
        session_store=session_store,
    )

    logger.info("🌐 Initializing Language Services...")
    _localization_port = FileLocalizationAdapter()  # also read directly by the memory review's question texts
    _localization = _LocalizationService(_localization_port)
    _system_lang = LanguageCode.from_str(
        config.get("SYSTEM_DEFAULT_LANGUAGE", "en"), default=LanguageCode.EN
    )
    _language_service = LanguagePreferenceService(
        user_repo=user_repo,
        account_repo=account_repo,
        profile_repo=container.profile_repo,
        prompt_builder=container.assembly_service,
        system_default_language=_system_lang,
        notification_service=notification_service,
    )
    # The drive-note receipt posted from a call is rendered in the owner's UI language (DRIVE_FILE_NOTES_RFC §4.9).
    notification_service.attach_localization(_localization, _language_service)

    # DocumentDeliveryService — GCS-backed, used by both async (AgentWorkerHandler)
    # and sync (ConversationHandler) document delivery paths.
    doc_delivery_service = None
    if gcs_media_adapter:
        doc_delivery_service = DocumentDeliveryService(
            storage=gcs_media_adapter, link_service=file_link_service
        )
        logger.info("✅ DocumentDeliveryService initialized")
    else:
        logger.info("ℹ️ DocumentDeliveryService disabled (GCS_MEDIA_BUCKET not set)")

    # Agent worker handler: notification + media + task_queue + doc_delivery are wired at construction.
    logger.info("🔧 Initializing Agent Worker Handler...")
    agent_worker_handler = AgentWorkerHandler(
        coordinator=coordinator,
        notification_service=notification_service,
        media_storage=gcs_media_adapter,
        task_queue=agent_task_queue,
        doc_delivery_service=doc_delivery_service,
        link_service=file_link_service,
        short_link_service=short_link_service,
        # Opt-in copy of deep research reports to the user's drive (decisions/deep_research_drive_copy.md).
        # Always wired: with no drive on this deployment, an opted-in user gets the "not connected" line.
        deep_research_drive_copy=DeepResearchDriveCopyService(
            users=user_repo,
            drive=container.user_drive_service,
            localization=_localization,
            language=_language_service,
        ),
    )
    # Anthropic client — created once, shared by ClaudeDeepResearchRunnerAgent instances.
    # The agent receives the client via constructor; does not import or instantiate the SDK.
    anthropic_client = None
    if config.get("ANTHROPIC_API_KEY"):
        anthropic_client = AsyncAnthropic(api_key=config["ANTHROPIC_API_KEY"])

    logger.info("🧑‍🏫 Initializing Companion Context infrastructure...")

    companion_memory_repo = FirestoreCompanionMemoryRepository(db_client=db_client, env_config=env_config)
    companion_cache_repo = FirestoreCompanionCacheRepository(db_client=db_client, env_config=env_config)

    _companion_search_config = SearchConfig()
    _companion_enrichment = SearchEnrichmentService(
        repository=container.repository,
        embedding_service=container.embedding_service,
        keyword_limit=_companion_search_config.DEFAULT_KEYWORD_LIMIT,
        phrase_one_limit=_companion_search_config.DEFAULT_PHRASE_ONE_LIMIT,
        phrase_two_limit=_companion_search_config.DEFAULT_PHRASE_TWO_LIMIT,
        total_limit=10,
    )
    companion_context_assembler = CompanionContextAssemblerService(
        companion_repo=companion_memory_repo,
        cache_repo=companion_cache_repo,
        embedding_service=container.embedding_service,
        enrichment_port=_companion_enrichment,
        fact_repo=container.repository,
    )

    # 2. Initialize UserAgentFactory — receives ports only, no adapter instantiation
    logger.info("🏭 Initializing User Agent Factory...")
    agent_factory = UserAgentFactory(
        config=config,
        env_config=env_config,
        coordinator=coordinator,
        user_repo=user_repo,
        account_repo=account_repo,
        **container.agent_services(),
        job_registry=job_registry,
        image_registry=image_registry,
        video_registry=video_registry,
        task_queue=agent_task_queue,
        anthropic_client=anthropic_client,
        quota_service=quota_service,
        companion_context_assembler=companion_context_assembler,
        notification_service=notification_service,
        long_turn_registry=long_turn_registry,
        localization=_localization,
        language_service=_language_service,
        alert_sink=_alert_webhook,
    )
    coordinator.set_agent_factory(agent_factory)  # Enable lazy agent instantiation
    _language_service.attach_agent_refresher(agent_factory.ensure_agents_for_user)

    logger.info("🧑‍🏫 Initializing Companion Extraction pipeline...")

    # companion_memory_repo/companion_cache_repo constructed earlier (before
    # UserAgentFactory), reused here — see "Initializing Companion Context infrastructure" above.
    # companion_extraction_queue constructed earlier too (before ServiceContainer) — see
    # "Initializing Overflow Routing Service" above.
    # repo=None is safe: TutorExtractorAgent always calls build_for_agent() with
    # include_biographical=False, and PromptBuilder only touches self.repo when
    # include_biographical=True (see PromptBuilder.build_for_agent). Same pattern
    # as ServiceContainer's _email_prompt_builder for EmailClassificationAgent.
    companion_prompt_builder = PromptBuilder(repo=None, assembly_service=container.assembly_service)
    companion_extractor_runner = CompanionExtractorRunner(
        context_builder=container.context_builder,
        user_repo=user_repo,
        prompt_builder=companion_prompt_builder,
        quota_service=quota_service,
        prompt_content_store=container.prompt_content_store,
    )
    companion_extraction_service = CompanionExtractionService(
        queue=companion_extraction_queue,
        companion_repo=companion_memory_repo,
        cache_repo=companion_cache_repo,
        embedding_service=container.embedding_service,
        extractor=companion_extractor_runner,
    )

    # ====================================================================
    # Initialize OAuth + Cabinet Services (will be registered on Slack app)
    # RFC: docs/10_rfcs/USER_INVITE_DEEP_LINK_RFC.md
    # Note: Blueprints registered after slack_adapter creation
    # ====================================================================
    logger.info("🔐 Initializing OAuth + Cabinet Services...")
    auth_config = AuthConfig(config)  # Pass config with secrets from Secret Manager
    firebase_adapter = FirebaseAuthAdapter(
        project_id=auth_config.firebase_project_id,
        web_api_key=auth_config.firebase_web_api_key,
        service_account_path=auth_config.firebase_service_account,
        oauth_client_id=auth_config.google_oauth_client_id,
        oauth_client_secret=auth_config.google_oauth_client_secret,
    )
    auth_registry = AuthProviderRegistry(providers={"firebase": firebase_adapter})

    # Services
    auth_service = AuthenticationService(
        auth_registry=auth_registry,
        user_repo=user_repo,
        account_repo=account_repo,
        whitelist_repo=whitelist_repo,
    )
    session_service = SessionService(
        secret_key=auth_config.oauth_session_secret,
        access_token_ttl=auth_config.access_token_ttl,
        refresh_token_ttl=auth_config.refresh_token_ttl
    )

    # Email adapters — reuse from ServiceContainer (no duplicate instantiation)
    oauth_credentials_port = container.oauth_credentials
    indexed_email_repo = container.indexed_email_repo
    email_indexing_service = container.email_indexing_service

    # MS To Do services — reuse adapters from ServiceContainer
    task_setup_service = None
    if container.ms_todo_adapter and agent_task_queue:
        service_url = config.get("CLOUD_RUN_SERVICE_URL") or "http://localhost:8080"
        task_setup_service = TaskSetupService(
            lifecycle=container.ms_todo_adapter,
            task_config=container.task_config_repo,
            tasks_provider=container.ms_todo_adapter,
            oauth_credentials=oauth_credentials_port,
            task_search_index=container.task_search_index,
            task_queue=agent_task_queue,
            notification_url_base=service_url,
            connector=container.connectors.get(MICROSOFT_TODO),
        )

    # Twilio Verify client for phone-binding OTP (Voice Companion RFC §4.6,
    # Slice 1 Task 11) — same conditional-construction guard as
    # UserAgentFactory._telephony (user_agent_factory.py ~line 208-212):
    # optional, MVP/dev-only feature, gracefully absent when unconfigured.
    # The Cabinet's phone-binding routes already 501 cleanly on None.
    twilio_account_sid = config.get("TWILIO_ACCOUNT_SID")
    twilio_auth_token = config.get("TWILIO_AUTH_TOKEN")
    twilio_verify_service_sid = config.get("TWILIO_VERIFY_SERVICE_SID")
    twilio_verify_client = (
        TwilioClient(twilio_account_sid, twilio_auth_token).verify.v2.services(
            twilio_verify_service_sid
        )
        if twilio_account_sid and twilio_auth_token and twilio_verify_service_sid
        else None
    )

    # The one owner-zone resolver: the consolidation run, the memory review and the owner's answers (Facts S4).
    _owner_zones = OwnerTimezoneResolver(account_repo=account_repo, user_repo=user_repo)
    # The one memory-write lease: a consolidation batch and a memory review of the same account never write at once.
    _memory_write_lease = FirestoreMemoryWriteLease(db_client, env_config)

    # Services for WorkerHandler — wrap ports so the handler never imports ports directly.
    # The one ConsolidationService also serves `$consolidate` on both chat adapters (Facts S2).
    _consolidation_service = ConsolidationService(
        queue=consolidation_queue,
        coordinator=coordinator,
        agent_factory=agent_factory,
        lease=_memory_write_lease,
        run_timeout_s=CONSOLIDATION_CFG.timeout_ms / 1000,
        task_queue=agent_task_queue,
        indexed_email_repo=indexed_email_repo,
        user_repo=user_repo,
        owner_zones=_owner_zones,
    ) if consolidation_queue else None
    _task_dispatch_service = TaskDispatchService(agent_task_queue) if agent_task_queue else None
    _reminders_service = RemindersService(
        notes_port=container.notes_adapter,
        user_repo=user_repo,
        task_dispatch=_task_dispatch_service,
        recurrence=container.recurrence_adapter,
        # Derived from NOTIFICATION_SLA here, in the composition root, because
        # services cannot import the infrastructure layer (REQ-ARCH-22).
        dispatch_deadline_s=dispatch_deadline_s(_NotificationKind.REMINDER),
    ) if (container.notes_adapter and _task_dispatch_service) else None
    _email_embedding_repair_service = EmailEmbeddingRepairService(
        email_repo=indexed_email_repo,
        embedding=container.embedding_service,
    ) if (indexed_email_repo and container.embedding_service) else None

    # AgentFallbackService: Smart (FAILED/TIMEOUT) → Quick → synthetic apology.
    # Built once here (composition root) and injected into ConversationHandler —
    # it no longer assembles its own fallback service from raw parts (that was a
    # handlers/ layer doing composition/'s job). The two-phase timeout retry
    # (SmartRetryService) was retired 2026-10-05 — see LONG_RUNNING_TURNS_RFC §5.9.
    _fallback_service = AgentFallbackService(
        coordinator=coordinator,
        alert_webhook=_alert_webhook,
    )

    # Drive index fan-out (USER_DRIVE_INDEXING_RFC §7.1, §12.1; RFC Δ5, Δ6). Built only when the
    # drive queue exists (DRIVE_INDEXER_URL set): its ticks run on the indexer service.
    _drive_sync_scheduler = None
    if container.drive_task_queue and container.drive_index_repo and container.user_drive:
        _drive_sync_scheduler = build_drive_sync_scheduler(
            repo=container.drive_index_repo,
            queue=container.drive_task_queue,
            drive=container.user_drive,
            users=user_repo,
            oauth_credentials=oauth_credentials_port,
            notifier=notification_service,
            alert_sink=_alert_webhook,
            ensure_agents=agent_factory.ensure_agents_for_user,
            localization=_localization,
            language=_language_service,
        )

    # R1 (SECRETS_AT_REST_RFC §6.2, plan D-P4): Reconnect and one notice for a past connection with no credential.
    # Lists its users from the feature stores; the hourly `reconnect_notice_sweep` covers Gmail and To Do (Drive's
    # notice is the drive fan-out's; Drive's Reconnect reads its connections, MULTI_DRIVE_RFC §4.4).
    _reconnect_service = IntegrationReconnectService(
        credentials=oauth_credentials_port,
        users=user_repo,
        notifier=notification_service,
        email_states=indexed_email_repo,
        task_config=container.task_config_repo,
        drive_connections=container.drive_connection_service,
        alert_sink=_alert_webhook,
        ensure_agents=agent_factory.ensure_agents_for_user,
        mail_connector=container.connectors.get(GMAIL),
        todo_connector=container.connectors.get(MICROSOFT_TODO),
    )

    # The integration connect flow (MULTI_DRIVE_RFC §4.3, §5.2): one revocation service (the provider-side revoke
    # rule, also the Cabinet's disconnects) and one connect service over the registry's specs and realm edges, with
    # one activation per integration. The drive one creates or reconnects the drive connection (§4.4, §5.2).
    _credential_revocation = CredentialRevocationService(oauth_credentials_port, container.connectors.edges())
    _drive_activation = (
        build_drive_activation(connections=container.drive_connection_service, drive=container.user_drive,
                               task_queue=container.drive_task_queue)
        if container.drive_connection_service is not None and container.user_drive is not None else None)
    _connect_service = build_oauth_connect_service(
        db_client, env_config,
        connectors=container.connectors,
        credentials=oauth_credentials_port,
        cipher=cipher,
        digest_key=secret_digest_key,
        revocation=_credential_revocation,
        activations=build_connect_activations(
            reconnect=_reconnect_service,
            todo_connector=container.connectors.get(MICROSOFT_TODO),
            task_queue=agent_task_queue,
            drive_activation=_drive_activation,
        ),
        redirect_uri=auth_config.oauth_connect_redirect_uri,
    )

    # The fact janitor (FACT_LIFECYCLE_RFC D4). Log-only unless FACT_LIFECYCLE_SWEEP_DRY_RUN is an explicit off value:
    # an optional rollout knob, read here with os.getenv (not load_settings).
    _fact_sweep_dry_run = sweep_dry_run(os.getenv("FACT_LIFECYCLE_SWEEP_DRY_RUN"))
    logger.info("fact lifecycle sweep: log-only" if _fact_sweep_dry_run else "fact lifecycle sweep: writes")
    _fact_lifecycle_sweep = FactLifecycleSweepService(
        repo=container.repository,
        counters=container.fact_lifecycle_counters,
        alert_sink=_alert_webhook,
        dry_run=_fact_sweep_dry_run,
        review_store=container.fact_review_store,
        task_queue=agent_task_queue,
        owner_zones=_owner_zones,
        account_repo=account_repo,
        user_repo=user_repo,
    )

    # The Cabinet's fact writes (the owner's delete), over the same fact repository the agents use.
    cabinet_facts = CabinetFactService(container.repository)

    # The Slack app is built here, before the memory review, whose questions are posted through its client; the
    # Slack adapter below is built over the same app.
    bot_token = config["SLACK_BOT_TOKEN"]
    logger.debug(f"Bot token starts with: {bot_token[:10]}...")
    logger.debug("Creating AsyncApp...")
    app = AsyncApp(token=bot_token)
    logger.debug("AsyncApp created successfully")

    # Interactive messages (Facts S4 plan §3.20): buttons on Slack, numbered text on Telegram, picked by platform.
    # The Telegram channel itself is registered on the channel factory below, only when Telegram is configured.
    _interactive = InteractiveMessageRouter({
        SLACK_PLATFORM: SlackInteractiveMessageAdapter(app.client),
        "telegram": PlainTextInteractiveMessageAdapter(notification_channel_factory),
    })
    _fact_question_texts = FactQuestionTexts(
        language=_language_service,
        localization=_localization_port,
        review_store=container.fact_review_store,
        counters=container.fact_lifecycle_counters,
        system_default=_system_lang,
    )
    # The owner's answers (digit, button, Cabinet) and every question close. Its fact writes go through the typed
    # update path only, which never searches; the search service is the adapter's constructor argument, at the
    # system defaults.
    _answer_search_config = SearchConfig()
    # One adapter instance for both services: the answer service's owner writes, the review's history fallbacks and folds.
    _review_fact_updates = container.create_fact_management_adapter(SearchEnrichmentService(
        repository=container.repository,
        embedding_service=container.embedding_service,
        keyword_limit=_answer_search_config.DEFAULT_KEYWORD_LIMIT,
        phrase_one_limit=_answer_search_config.DEFAULT_PHRASE_ONE_LIMIT,
        phrase_two_limit=_answer_search_config.DEFAULT_PHRASE_TWO_LIMIT,
        total_limit=_answer_search_config.DEFAULT_SEMANTIC_SEARCH_LIMIT,
    )) if agent_task_queue else None
    _fact_answer_service = FactAnswerService(
        fact_updates=_review_fact_updates,
        repo=container.repository,
        review_store=container.fact_review_store,
        counters=container.fact_lifecycle_counters,
        task_queue=agent_task_queue,
        session_store=session_store,
        texts=_fact_question_texts,
        interactive=_interactive,
        owner_zones=_owner_zones,
    ) if agent_task_queue else None
    # An owner's click on an interactive message (Facts S4 plan §3.20): the Slack interactivity route queues it, the
    # worker's `interactive_choice` branch runs it. The handlers are keyed by the binding kind they answer.
    _interactive_choice = InteractiveChoiceService(
        iam=iam_service,
        handlers={FACT_QUESTION_KIND: _fact_answer_service},
        interactive=_interactive,
    ) if _fact_answer_service is not None else None
    # The memory review (Facts S4): built when its answer path (so its task queue, for the deferral and the next
    # question) and its ops sink exist; without it `/worker` answers fact_review with 501.
    _fact_review_service = FactReviewService(
        repo=container.repository,
        review_store=container.fact_review_store,
        counters=container.fact_lifecycle_counters,
        lease=_memory_write_lease,
        coordinator=coordinator,
        agent_factory=agent_factory,
        user_repo=user_repo,
        account_repo=account_repo,
        texts=_fact_question_texts,
        owner_zones=_owner_zones,
        notification_state=notification_state_repo,
        channel_bindings=channel_binding_adapter,
        notifications=notification_service,
        session_store=session_store,
        task_queue=agent_task_queue,
        answer_service=_fact_answer_service,
        interactive=_interactive,
        alert_sink=_alert_webhook,
        run_timeout_s=CONSOLIDATION_CFG.timeout_ms / 1000,
        fact_updates=_review_fact_updates,
        cabinet_url=f"{config.get('CLOUD_RUN_SERVICE_URL') or 'http://localhost:8080'}/cabinet?tab=memory",
    ) if (_fact_answer_service is not None and _review_fact_updates is not None and agent_task_queue is not None
          and _alert_webhook is not None) else None

    # Worker handler — dispatches Cloud Tasks to appropriate handlers
    # Cloud Tasks is at-least-once: claims keyed by task name stop a redelivered task
    # from running twice; a claim whose attempt died is taken over by the retry.
    # TTL 3600s is garbage collection and the done-window, not the liveness rule.
    worker_handler = WorkerHandler(
        task_claims=FirestoreTaskClaimStore(
            db_client=db_client,
            collection_name=env_config.worker_task_dedup_collection,
            ttl_seconds=3600,
        ),
        # One review per user per local date; 48 h outlives the 3 h window and the date edge.
        daily_review_marks=FirestoreDedupStore(
            db_client=db_client,
            collection_name=env_config.worker_task_dedup_collection,
            ttl_seconds=172800,
        ),
        agent_worker_handler=agent_worker_handler,
        email_indexing_service=email_indexing_service,
        notification_service=notification_service,
        consolidation_service=_consolidation_service,
        coordinator=coordinator,
        agent_factory=agent_factory,
        indexed_email_repo=indexed_email_repo,
        user_repo=user_repo,
        task_dispatch=_task_dispatch_service,
        video_registry=video_registry,
        quota_service=quota_service,
        media_storage=gcs_media_adapter,
        task_setup=task_setup_service,
        task_indexing=container.task_indexing,
        reminders_service=_reminders_service,
        notes_port=container.notes_adapter,
        email_review=container.email_review_service,
        account_repo=account_repo,
        billing_webhook=_alert_webhook,
        email_embedding_repair=_email_embedding_repair_service,
        link_service=file_link_service,
        short_link_service=short_link_service,
        companion_extraction=companion_extraction_service,
        oauth_credentials=oauth_credentials_port,
        drive_sync_scheduler=_drive_sync_scheduler,
        deep_research_fetch=_openai_dr_fetch,
        prompt_content_store=container.prompt_content_store,
        fact_lifecycle_sweep=_fact_lifecycle_sweep,
        fact_review=_fact_review_service,
        interactive_choice=_interactive_choice,
        reconnect=_reconnect_service,
        mail_connector=container.connectors.get(GMAIL),
    )

    # 72 h = OpenAI's webhook retry window; liveness follows each doc's own expires_at.
    deep_research_dedup_store = FirestoreDedupStore(
        db_client=db_client,
        collection_name=env_config.worker_task_dedup_collection,
        ttl_seconds=259200,
    )

    # HTML renderer (optional — lazy-starts Chromium on first widget request)
    html_renderer = None
    if config.get("ENABLE_HTML_RENDERER"):
        html_renderer = PlaywrightHtmlRenderer()
        logger.info("✅ HTML renderer (Playwright) enabled")
    else:
        logger.info("ℹ️ HTML renderer disabled (ENABLE_HTML_RENDERER not set)")

    # Audio transcription (optional — voice messages stay an honest alert without it)
    audio_service = None
    if config.get("OPENAI_API_KEY"):
        audio_service = OpenAITranscriptionAdapter(
            api_key=config["OPENAI_API_KEY"],
            model=os.getenv("OPENAI_TRANSCRIPTION_MODEL"),
        )
    else:
        logger.info("ℹ️ Audio transcription disabled (OPENAI_API_KEY not set)")

    logger.info("🔌 Initializing Slack Adapter...")

    # One RichContentService per platform: the chat handler and the notification service deliver files through the same one.
    slack_media_adapter = SlackMediaAdapter(
        app_client=app.client,
        bot_token=config.get("SLACK_BOT_TOKEN", ""),
    )
    slack_rich_content = RichContentService(
        media_port=slack_media_adapter,
        storage_port=gcs_media_adapter,
        html_renderer=html_renderer,
    )

    slack_adapter = SlackAdapterFactory.create_adapter(
        app=app,
        config=config,
        env_config=env_config,
        coordinator=coordinator,
        agent_factory=agent_factory,
        iam_service=iam_service,
        file_service=file_service,
        session_store=session_store,
        db_client=db_client,
        consolidation_queue=consolidation_queue,
        consolidation_config=config.get("CONSOLIDATION"),
        consolidation_service=_consolidation_service,
        audio_service=audio_service,
        html_renderer=html_renderer,
        notification_service=notification_service,
        language_service=_language_service,
        localization=_localization,
        file_conversion_service=container.file_conversion_service,
        channel_binding_service=channel_binding_service,
        fallback_service=_fallback_service,
        short_link_service=short_link_service,
        skill_service=container.skill_service,
        memory_fact_service=container.memory_fact_service,
        fact_answers=_fact_answer_service,
        drive_note_service=command_drive_note_service(container.drive_note_service, container.drive_notes_enabled),
        long_turn_service=long_turn_service,
        doc_delivery_service=doc_delivery_service,
        worker_queue=agent_task_queue,
        rich_content_service=slack_rich_content,
    )
    worker_handler.attach_slack_worker(slack_adapter.handle_queued_event)
    notification_channel_factory.register_factory(
        "slack",
        lambda channel_id: SlackResponseChannel(
            app_client=slack_adapter.app.client,
            channel_id=channel_id,
            bot_token=slack_adapter.slack_bot_token,
        ),
    )

    # Wire Slack media adapter into notification service for async file delivery (DOCX).
    notification_service.register_platform_media("slack", slack_media_adapter)
    notification_service.register_platform_rich_content("slack", slack_rich_content)

    slack_adapter.register_handlers()

    # ====================================================================
    # PHASE 3: Telegram Integration (Optional)
    # Initialize Telegram adapter if configured. All or nothing: the adapter and its media
    # adapter are built first; the worker, channel and media registrations run only after both exist.
    # ====================================================================
    telegram_config = validate_telegram_config()
    telegram_adapter = None

    if telegram_config:
        logger.info("🤖 Initializing Telegram adapter...")
        try:
            # Initialize dedup store for Telegram
            dedup_store = FirestoreDedupStore(
                db_client=db_client,
                collection_name=env_config.event_dedup_collection
            )

            # One Bot, one media adapter, one RichContentService: shared by the chat handler and the notification service
            telegram_media_adapter = TelegramMediaAdapter(bot=Bot(telegram_config["token"]))
            telegram_rich_content = RichContentService(media_port=telegram_media_adapter, html_renderer=html_renderer)

            # Create adapter via factory (wires RichContentService + html_renderer)
            built_telegram_adapter = TelegramAdapterFactory.create_adapter(
                token=telegram_config["token"],
                webhook_secret=telegram_config["webhook_secret"],
                dedup_store=dedup_store,
                session_store=session_store,
                coordinator=coordinator,
                agent_factory=agent_factory,
                iam_service=iam_service,
                file_service=file_service,
                consolidation_queue=consolidation_queue,
                consolidation_config=config.get("CONSOLIDATION"),
                consolidation_service=_consolidation_service,
                audio_service=audio_service,
                html_renderer=html_renderer,
                notification_service=notification_service,
                language_service=_language_service,
                localization=_localization,
                file_conversion_service=container.file_conversion_service,
                fallback_service=_fallback_service,
                short_link_service=short_link_service,
                skill_service=container.skill_service,
                memory_fact_service=container.memory_fact_service,
                fact_answers=_fact_answer_service,
                drive_note_service=command_drive_note_service(container.drive_note_service,
                                                              container.drive_notes_enabled),
                task_queue=agent_task_queue,
                long_turn_service=long_turn_service,
                doc_delivery_service=doc_delivery_service,
                rich_content_service=telegram_rich_content,
            )
        except Exception as e:
            logger.error(f"❌ Failed to initialize Telegram adapter: {e}", exc_info=True)
            logger.warning("🤖 Bot will continue without Telegram support")
        else:
            telegram_adapter = built_telegram_adapter
            worker_handler.attach_telegram_worker(telegram_adapter.handle_queued_update)
            notification_channel_factory.register_factory(
                "telegram",
                lambda channel_id: _make_telegram_channel(
                    telegram_adapter, channel_id
                ),
            )
            # File delivery to a Telegram chat (notify_file_bytes picks the port by platform)
            notification_service.register_platform_media("telegram", telegram_media_adapter)
            notification_service.register_platform_rich_content("telegram", telegram_rich_content)

            # Register in factory
            PlatformAdapterFactory.register("telegram", telegram_adapter)
    else:
        logger.info("ℹ️ Telegram not configured (TELEGRAM_BOT_TOKEN not set)")

    return ApplicationGraph(
        container=container,
        agent_factory=agent_factory,
        coordinator=coordinator,
        worker_handler=worker_handler,
        agent_worker_handler=agent_worker_handler,
        slack_adapter=slack_adapter,
        telegram_adapter=telegram_adapter,
        notification_service=notification_service,
        user_repo=user_repo,
        account_repo=account_repo,
        invite_service=invite_service,
        cabinet_facts=cabinet_facts,
        fact_answers=_fact_answer_service,
        quota_service=quota_service,
        alert_sink=_alert_webhook,
        session_service=session_service,
        auth_service=auth_service,
        auth_registry=auth_registry,
        auth_config=auth_config,
        connect_service=_connect_service,
        credential_revocation=_credential_revocation,
        task_setup_service=task_setup_service,
        reconnect_service=_reconnect_service,
        twilio_verify_client=twilio_verify_client,
        task_dispatch=_task_dispatch_service,
        deep_research_fetch=_openai_dr_fetch,
        deep_research_dedup_store=deep_research_dedup_store,
        file_token_service=file_token_service,
        media_storage=gcs_media_adapter,
        short_link_service=short_link_service,
        companion_extractor_runner=companion_extractor_runner,
        language_service=_language_service,
        localization=_localization,
        cipher=cipher,
        digest_key=secret_digest_key,
        agent_task_queue=agent_task_queue,
        billing_agent=billing_agent,
        logger_agent=logger_agent,
        html_renderer=html_renderer,
    )


def _make_telegram_channel(adapter, channel_id):
    try:
        return TelegramResponseChannel(
            bot=adapter.bot,
            chat_id=int(channel_id),
        )
    except (ValueError, TypeError) as e:
        logger.error(
            f"[NotificationFactory] Invalid Telegram chat_id "
            f"'{channel_id}': {e}"
        )
        return None
