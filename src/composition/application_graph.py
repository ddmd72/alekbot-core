"""The application graph: every non-web object `main()` serves, built in one place.

`main()` keeps the I/O it starts with (telemetry, settings, the Firestore client and its warm-up) and the web layer
(blueprints, voice, MCP, `/worker`, hypercorn); composition/ must not import `src.web`. Everything between is built here.
"""
import asyncio
import os
from functools import partial
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from telegram import Bot

from src.adapters.claude_deep_research_adapter import ClaudeDeepResearchAdapter
from src.adapters.cloud_run_jobs_adapter import CloudRunJobsAdapter
from src.adapters.firestore_account_repo import FirestoreAccountRepository
from src.adapters.firestore_dedup_store import FirestoreDedupStore
from src.adapters.firestore_quota_service import FirestoreQuotaService
from src.adapters.firestore_task_claim_store import FirestoreTaskClaimStore
from src.adapters.firestore_user_repo import FirestoreUserRepository
from src.adapters.firestore_whitelist_repo import FirestoreWhitelistRepository
from src.adapters.gcp_task_queue import GcpTaskQueue
from src.adapters.gcs_media_adapter import GcsMediaAdapter
from src.adapters.grok_image_adapter import GrokImageAdapter
from src.adapters.grok_video_adapter import GrokVideoAdapter
from src.adapters.openai_deep_research_adapter import OpenAIDeepResearchAdapter
from src.adapters.slack.http_adapter import HTTPModeAdapter
from src.adapters.slack.webhook_adapter import SlackWebhookAdapter
from src.adapters.telegram.media_adapter import TelegramMediaAdapter
from src.adapters.telegram.response_channel import TelegramResponseChannel
from src.adapters.telegram.webhook_adapter import TelegramWebhookAdapter
from src.services.rich_content_service import RichContentService
from src.agents.infrastructure.billing_agent import BillingAgent
from src.agents.infrastructure.logger_agent import LoggerAgent
from src.composition.boot_setup import build_boot_checks
from src.composition.identity_setup import build_identity
from src.composition.operations_setup import build_operations
from src.composition.session_routing_setup import build_session_routing
from src.composition.file_delivery_setup import build_file_delivery
from src.composition.language_setup import build_languages
from src.composition.notification_setup import build_notifications
from src.composition.companion_setup import build_companions
from src.composition.memory_search_setup import build_memory_search
from src.composition.agents_setup import build_agents
from src.composition.integrations_setup import build_integrations
from src.composition.memory_setup import build_memory
from src.composition.companion_extractor_runner import CompanionExtractorRunner
from src.composition.fact_profile_setup import LazyAtlas
from src.composition.document_service_setup import build_document_backends
from src.composition.drive_note_setup import command_drive_note_service
from src.composition.service_container import ServiceContainer
from src.composition.chat_services import ChatServices
from src.composition.platform_kit import PlatformKit, register_late_platform
from src.composition.slack_adapter_factory import SlackAdapterFactory
from src.composition.telegram_adapter_factory import TelegramAdapterFactory
from src.composition.user_agent_factory import UserAgentFactory
from src.composition.worker_setup import build_worker_tasks
from src.config.auth import AuthConfig
from src.config.environment import validate_telegram_config
from src.domain.notification_kind import NotificationKind as _NotificationKind
from src.handlers.agent_worker_handler import AgentWorkerHandler
from src.handlers.worker_handler import WorkerHandler
from src.infrastructure.agent_coordinator import AgentCoordinator
from src.infrastructure.agent_manifest import ALL_DESCRIPTORS
from src.infrastructure.agent_registry import AgentRegistry
from src.infrastructure.notification_sla import dispatch_deadline_s
from src.ports.cipher_port import CipherPort
from src.ports.html_renderer_port import HtmlRendererPort
from src.services.agent_fallback_service import AgentFallbackService
from src.services.authentication_service import AuthenticationService
from src.services.auth_provider_registry import AuthProviderRegistry
from src.services.cabinet_fact_service import CabinetFactService
from src.services.call_text_file_service import CallTextFileService
from src.services.deep_research_drive_copy_service import DeepResearchDriveCopyService
from src.services.fact_answer_service import FactAnswerService
from src.services.file_access_token_service import FileAccessTokenService
from src.services.invite_code_service import InviteCodeService
from src.services.language_preference_service import LanguagePreferenceService
from src.services.language_resolver import LanguageResolver
from src.services.localization_service import LocalizationService as _LocalizationService
from src.services.provider_registry import ProviderRegistry
from src.services.reminders_service import RemindersService
from src.services.session_service import SessionService
from src.services.short_link_service import ShortLinkService
from src.services.task_dispatch_service import TaskDispatchService
from src.services.task_setup_service import TaskSetupService
from src.composition.deploy_topology import DeployTopology
from src.ports.fact_search_port import FactSearchPort
from src.services.credential_revocation_service import CredentialRevocationService
from src.services.drive_disconnect_service import DriveDisconnectService
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
    whitelist_repo: FirestoreWhitelistRepository
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
    deploy_topology: DeployTopology
    # the integration connect flow and the Cabinet's credential removal (MULTI_DRIVE_RFC §4.3, §5.2)
    connect_service: OAuthConnectService
    credential_revocation: CredentialRevocationService
    drive_disconnect_service: Optional[DriveDisconnectService]    # the Cabinet's drive disconnect (REQ-ARCH-34)
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
    call_text_files: CallTextFileService    # the web call's "keep as a file" (VOICE_CALL_LIVE_TEXT_RFC §4.11)
    language_service: LanguageResolver
    language_preferences: LanguagePreferenceService
    localization: _LocalizationService
    # the stored-secrets cipher and the verify-only digest key (SECRETS_AT_REST_RFC §2.6), consumed by main()'s MCP wiring
    cipher: CipherPort
    digest_key: bytes
    # needed by start() and shutdown()
    agent_task_queue: Optional[GcpTaskQueue]
    billing_agent: BillingAgent
    logger_agent: LoggerAgent
    html_renderer: Optional[HtmlRendererPort]
    # the fact search (FACT_PROFILE_RFC §5); no caller yet
    fact_search: FactSearchPort
    # The voice MCP token kind's key (LELIK_VOICE_MCP_RFC §5.2), validated before anything else is built; consumed by
    # main()'s voice and MCP wiring. None: no key, every voice token refused. Never in a repr.
    voice_mcp_signing_key: Optional[str] = field(default=None, repr=False)
    # The one place atlas, loaded by start() so the first memory search does not pay for it.
    place_atlas: Optional[LazyAtlas] = None

    async def start(self) -> None:
        """Create the agent-task queue if there is one, then start the billing and logger agents and the agent factory.
        The place table is loaded first (about 6 s), so the first search of a new instance does not time out."""
        if self.place_atlas is not None:
            await self.place_atlas.warm()
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

    # Fail closed before anything else is built.
    boot = build_boot_checks(config, env_config)
    cipher, secret_digest_key, voice_mcp_key = boot.cipher, boot.digest_key, boot.voice_mcp_signing_key
    auth_config, topology, parse_adapter = boot.auth_config, boot.deploy_topology, boot.parse_adapter

    identity = build_identity(config, db_client, env_config, auth_config=auth_config, digest_key=secret_digest_key)
    account_repo, user_repo, whitelist_repo = identity.account_repo, identity.user_repo, identity.whitelist_repo
    iam_service, invite_service = identity.iam_service, identity.invite_service

    logger.info("🎯 Initializing Agent Registry...")
    agent_registry = AgentRegistry()
    for descriptor in ALL_DESCRIPTORS:
        agent_registry.register(descriptor)

    ops = build_operations(config, env_config, topology=topology, account_repo=account_repo)
    agent_task_queue, _alert_webhook, quota_service = ops.task_queue, ops.alert_sink, ops.quota_service
    billing_agent, logger_agent = ops.billing_agent, ops.logger_agent

    # Audio transcription (optional — voice messages stay an honest alert without it), with the parse adapter: the
    # container's document service takes both (EXTERNAL_FILES_RFC P21).
    document_backends = build_document_backends(config, parser=parse_adapter)
    audio_service = document_backends.transcriber

    routing = build_session_routing(db_client, env_config, user_repo=user_repo, task_queue=agent_task_queue)
    consolidation_queue, companion_extraction_queue = routing.consolidation_queue, routing.companion_extraction_queue
    channel_binding_service = routing.channel_binding_service

    # 1. Shared service container (LLM adapters, repositories, prompt infra, session store)
    logger.info("🏭 Initializing Service Container...")
    container = ServiceContainer(
        config=config,
        db_client=db_client,
        env_config=env_config,
        deploy_topology=topology,
        account_repo=account_repo,
        overflow_callback=routing.overflow_callback,
        threshold_resolver=routing.threshold_resolver,
        alert_webhook=_alert_webhook,
        cipher=cipher,
        user_repo=user_repo,          # the email classifier reads the user's configuration per batch
        quota_service=quota_service,  # and bills the indexing job's account
        document_backends=document_backends,
    )
    session_store = container.session_store  # Alias for Slack/Telegram adapters and shutdown

    logger.info("🎯 Initializing Agent Coordinator...")
    # The file ref resolver is wired into the coordinator for specialist delegation
    coordinator = AgentCoordinator(
        registry=agent_registry,
        task_queue=agent_task_queue,
        file_ref_resolver=(
            container.file_conversion_service.prefetch if container.file_conversion_service else None
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
        cloud_jobs_adapter = CloudRunJobsAdapter(
            project=config["GOOGLE_CLOUD_PROJECT"],
            region=topology.region,
        )
        job_registry.register("claude", ClaudeDeepResearchAdapter(
            job_runner=cloud_jobs_adapter,
            job_name=f"alek-research-job-{topology.queue_suffix}",
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

    delivery = build_file_delivery(config, db_client, env_config, token_secret=auth_config.oauth_session_secret,
                                   service_url=topology.service_url)
    gcs_media_adapter, file_token_service, file_link_service = (
        delivery.media_storage, delivery.file_token_service, delivery.link_service)
    short_link_service, doc_delivery_service = delivery.short_link_service, delivery.doc_delivery_service

    languages = build_languages(config, user_repo=user_repo, account_repo=account_repo)
    _localization_port, _localization = languages.localization_port, languages.localization
    _system_lang, _language_service = languages.system_default, languages.resolver

    notifications = build_notifications(config, db_client, env_config, coordinator=coordinator,
                                        session_store=session_store, media_storage=gcs_media_adapter,
                                        languages=languages)
    app, html_renderer, slack_kit = notifications.slack_app, notifications.html_renderer, notifications.slack_kit
    notification_channel_factory, notification_service = notifications.channel_factory, notifications.service

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
    companions = build_companions(db_client, env_config, container=container, user_repo=user_repo,
                                  quota_service=quota_service, extraction_queue=companion_extraction_queue,
                                  notification_service=notification_service, languages=languages)
    companion_extractor_runner = companions.extractor_runner
    call_text_files, companion_extraction_service = companions.call_text_files, companions.extraction_service

    memory_search = build_memory_search(db_client, env_config, container=container)
    _person_cards, _place_atlas, fact_search = (
        memory_search.person_cards, memory_search.place_atlas, memory_search.fact_search)

    agents = build_agents(config, db_client, env_config, container=container, coordinator=coordinator, boot=boot,
                          identity=identity, ops=ops, job_registry=job_registry, image_registry=image_registry,
                          video_registry=video_registry, companions=companions,
                          notification_service=notification_service, languages=languages, memory_search=memory_search)
    agent_factory, long_turn_service, language_preferences = (
        agents.factory, agents.long_turn_service, agents.language_preferences)
    coordinator.set_agent_factory(agent_factory)  # Enable lazy agent instantiation

    email_indexing_service = container.email_indexing_service

    integrations = build_integrations(db_client, env_config, container=container, boot=boot, ops=ops,
                                      user_repo=user_repo, notification_service=notification_service,
                                      languages=languages, ensure_agents=agent_factory.ensure_agents_for_user)
    task_setup_service, _drive_sync_scheduler = integrations.task_setup, integrations.drive_sync_scheduler
    _reconnect_service, _credential_revocation = integrations.reconnect, integrations.credential_revocation
    _drive_disconnect, _connect_service = integrations.drive_disconnect, integrations.connect
    _gmail_reconnect_flag = integrations.gmail_reconnect_flag

    memory = build_memory(db_client, env_config, container=container, coordinator=coordinator,
                          agent_factory=agent_factory, identity=identity, ops=ops, routing=routing,
                          memory_search=memory_search, notifications=notifications, languages=languages,
                          cabinet_memory_url=topology.cabinet_memory_url)
    _consolidation_service, _fact_lifecycle_sweep = memory.consolidation_service, memory.fact_lifecycle_sweep
    _fact_answer_service, _interactive_choice = memory.fact_answers, memory.interactive_choice
    _fact_review_service, cabinet_facts = memory.fact_review, memory.cabinet_facts
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

    # AgentFallbackService: Smart (FAILED/TIMEOUT) → Quick → synthetic apology.
    # Built once here (composition root) and injected into ConversationHandler —
    # it no longer assembles its own fallback service from raw parts (that was a
    # handlers/ layer doing composition/'s job). The two-phase timeout retry
    # (SmartRetryService) was retired 2026-10-05 — see LONG_RUNNING_TURNS_RFC §5.9.
    _fallback_service = AgentFallbackService(
        coordinator=coordinator,
        alert_webhook=_alert_webhook,
    )

    # 72 h = OpenAI's webhook retry window; liveness follows each doc's own expires_at.
    deep_research_dedup_store = FirestoreDedupStore(
        db_client=db_client,
        collection_name=env_config.worker_task_dedup_collection,
        ttl_seconds=259200,
    )

    logger.info("🔌 Initializing Slack Adapter...")

    # The ConversationHandler dependencies both platforms share, held once.
    chat = ChatServices(
        coordinator=coordinator,
        agent_factory=agent_factory,
        consolidation_queue=consolidation_queue,
        consolidation_config=config.get("CONSOLIDATION"),
        consolidation_service=_consolidation_service,
        audio_service=audio_service,
        notification_service=notification_service,
        localization=_localization,
        file_conversion_service=container.file_conversion_service,
        fallback_service=_fallback_service,
        short_link_service=short_link_service,
        skill_service=container.skill_service,
        memory_fact_service=container.memory_fact_service,
        fact_answers=_fact_answer_service,
        drive_note_service=command_drive_note_service(container.drive_note_service, container.drive_notes_enabled),
        long_turn_service=long_turn_service,
        doc_delivery_service=doc_delivery_service,
    )

    slack_adapter = SlackAdapterFactory.create_adapter(
        app=app,
        config=config,
        deploy_topology=topology,
        chat=chat,
        iam_service=iam_service,
        session_store=session_store,
        db_client=db_client,
        language_service=_language_service,
        channel_binding_service=channel_binding_service,
        worker_queue=agent_task_queue,
        rich_content_service=slack_kit.rich_content,
    )
    slack_adapter.register_handlers()

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
        agent_worker_handler=agent_worker_handler,
        email_indexing_service=email_indexing_service,
        consolidation_service=_consolidation_service,
        agent_factory=agent_factory,
        user_repo=user_repo,
        task_dispatch=_task_dispatch_service,
        task_setup=task_setup_service,
        task_indexing=container.task_indexing,
        reminders_service=_reminders_service,
        companion_extraction=companion_extraction_service,
        drive_sync_scheduler=_drive_sync_scheduler,
        fact_lifecycle_sweep=_fact_lifecycle_sweep,
        fact_review=_fact_review_service,
        interactive_choice=_interactive_choice,
        reconnect=_reconnect_service,
        gmail_reconnect_flag=_gmail_reconnect_flag,
        slack_worker=slack_adapter.handle_queued_event,
        tasks=build_worker_tasks(
            notes_port=container.notes_adapter, user_repo=user_repo, ensure_agents=agent_factory.ensure_agents_for_user,
            notification_service=notification_service, alert_sink=_alert_webhook,
            task_dispatch=_task_dispatch_service, video_registry=video_registry, quota_service=quota_service,
            media_storage=gcs_media_adapter, link_service=file_link_service, short_link_service=short_link_service,
            deep_research_fetch=_openai_dr_fetch, prompt_content_store=container.prompt_content_store,
            email_review=container.email_review_service,
            # One review per user per local date; 48 h outlives the 3 h window and the date edge.
            daily_review_marks=FirestoreDedupStore(
                db_client=db_client,
                collection_name=env_config.worker_task_dedup_collection,
                ttl_seconds=172800,
            ),
            gmail_reconnect_flag=_gmail_reconnect_flag, account_repo=account_repo),
    )

    # ====================================================================
    # PHASE 3: Telegram Integration (Optional)
    # Initialize Telegram adapter if configured. All or nothing (owner ruling): the adapter and its media
    # adapter are built first; the worker and the late platform binding run only after both exist.
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

            # Create adapter via factory (wires RichContentService)
            built_telegram_adapter = TelegramAdapterFactory.create_adapter(
                token=telegram_config["token"],
                webhook_secret=telegram_config["webhook_secret"],
                dedup_store=dedup_store,
                session_store=session_store,
                chat=chat,
                iam_service=iam_service,
                language_service=_language_service,
                task_queue=agent_task_queue,
                rich_content_service=telegram_rich_content,
            )
        except Exception as e:
            logger.error(f"❌ Failed to initialize Telegram adapter: {e}", exc_info=True)
            logger.warning("🤖 Bot will continue without Telegram support")
        else:
            telegram_adapter = built_telegram_adapter
            register_late_platform(
                PlatformKit(
                    platform="telegram",
                    media=telegram_media_adapter,
                    rich_content=telegram_rich_content,
                    channel=partial(_make_telegram_channel, telegram_adapter),
                ),
                notifications=notification_service,
                channels=notification_channel_factory,
                worker=worker_handler,
                queued_update=telegram_adapter.handle_queued_update,
            )

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
        whitelist_repo=whitelist_repo,
        account_repo=account_repo,
        invite_service=invite_service,
        cabinet_facts=cabinet_facts,
        fact_answers=_fact_answer_service,
        quota_service=quota_service,
        alert_sink=_alert_webhook,
        session_service=identity.session_service,
        auth_service=identity.auth_service,
        auth_registry=identity.auth_registry,
        auth_config=auth_config,
        deploy_topology=topology,
        connect_service=_connect_service,
        credential_revocation=_credential_revocation,
        drive_disconnect_service=_drive_disconnect,
        task_setup_service=task_setup_service,
        reconnect_service=_reconnect_service,
        twilio_verify_client=identity.twilio_verify_client,
        task_dispatch=_task_dispatch_service,
        deep_research_fetch=_openai_dr_fetch,
        deep_research_dedup_store=deep_research_dedup_store,
        file_token_service=file_token_service,
        media_storage=gcs_media_adapter,
        short_link_service=short_link_service,
        companion_extractor_runner=companion_extractor_runner,
        call_text_files=call_text_files,
        language_service=_language_service,
        language_preferences=language_preferences,
        localization=_localization,
        cipher=cipher,
        digest_key=secret_digest_key,
        agent_task_queue=agent_task_queue,
        billing_agent=billing_agent,
        logger_agent=logger_agent,
        html_renderer=html_renderer,
        fact_search=fact_search,
        place_atlas=_place_atlas,
        voice_mcp_signing_key=voice_mcp_key,
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
