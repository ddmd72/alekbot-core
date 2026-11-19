"""The application graph: every non-web object `main()` serves, built in one place.

`main()` keeps the I/O it starts with (telemetry, settings, the Firestore client and its warm-up) and the web layer
(blueprints, voice, MCP, `/worker`, hypercorn); composition/ must not import `src.web`. Everything between is built here.
"""
import asyncio
import os
from functools import partial
from dataclasses import dataclass, field
from typing import Any, Optional, Tuple

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
from src.adapters.slack.http_adapter import HTTPModeAdapter
from src.adapters.slack.webhook_adapter import SlackWebhookAdapter
from src.adapters.telegram.media_adapter import TelegramMediaAdapter
from src.adapters.telegram.response_channel import TelegramResponseChannel
from src.adapters.telegram.webhook_adapter import TelegramWebhookAdapter
from src.services.rich_content_service import RichContentService
from src.agents.infrastructure.billing_agent import BillingAgent
from src.agents.infrastructure.logger_agent import LoggerAgent
from src.composition.agents_setup import Agents, build_agents
from src.composition.background_jobs_setup import BackgroundJobs, build_background_jobs
from src.composition.boot_setup import BootChecks, build_boot_checks
from src.composition.companion_setup import Companions, build_companions
from src.composition.file_delivery_setup import FileDelivery, build_file_delivery
from src.composition.identity_setup import Identity, build_identity
from src.composition.integrations_setup import Integrations, build_integrations
from src.composition.language_setup import Languages, build_languages
from src.composition.memory_search_setup import build_memory_search
from src.composition.memory_setup import Memory, build_memory
from src.composition.notification_setup import Notifications, build_notifications
from src.composition.operations_setup import Operations, build_operations
from src.composition.session_routing_setup import SessionRouting, build_session_routing
from src.composition.companion_extractor_runner import CompanionExtractorRunner
from src.composition.fact_profile_setup import LazyAtlas
from src.composition.document_service_setup import DocumentBackends, build_document_backends
from src.composition.drive_note_setup import command_drive_note_service
from src.composition.service_container import ServiceContainer
from src.composition.chat_services import ChatServices
from src.composition.platform_kit import PlatformKit, register_late_platform
from src.composition.slack_adapter_factory import SlackAdapterFactory
from src.composition.telegram_adapter_factory import TelegramAdapterFactory
from src.composition.user_agent_factory import UserAgentFactory
from src.composition.geo_mark_setup import build_geo_mark_service
from src.composition.worker_setup import build_worker_tasks
from src.config.auth import AuthConfig
from src.config.environment import EnvironmentConfig, validate_telegram_config
from src.handlers.agent_worker_handler import AgentWorkerHandler
from src.handlers.worker_handler import WorkerHandler
from src.infrastructure.agent_coordinator import AgentCoordinator
from src.infrastructure.agent_manifest import ALL_DESCRIPTORS
from src.infrastructure.agent_registry import AgentRegistry
from src.ports.cipher_port import CipherPort
from src.ports.html_renderer_port import HtmlRendererPort
from src.services.agent_fallback_service import AgentFallbackService
from src.services.authentication_service import AuthenticationService
from src.services.auth_provider_registry import AuthProviderRegistry
from src.services.cabinet_fact_service import CabinetFactService
from src.services.call_text_file_service import CallTextFileService
from src.services.fact_answer_service import FactAnswerService
from src.services.file_access_token_service import FileAccessTokenService
from src.services.invite_code_service import InviteCodeService
from src.services.language_preference_service import LanguagePreferenceService
from src.services.language_resolver import LanguageResolver
from src.services.localization_service import LocalizationService as _LocalizationService
from src.services.provider_registry import ProviderRegistry
from src.services.session_service import SessionService
from src.services.short_link_service import ShortLinkService
from src.services.task_dispatch_service import TaskDispatchService
from src.services.task_setup_service import TaskSetupService
from src.composition.deploy_topology import DeployTopology
from src.ports.fact_search_port import FactSearchPort
from src.services.credential_revocation_service import CredentialRevocationService
from src.services.drive_disconnect_service import DriveDisconnectService
from src.services.geo_mark_service import GeoMarkService
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
    file_token_service: FileAccessTokenService
    media_storage: Optional[GcsMediaAdapter]
    short_link_service: Optional[ShortLinkService]
    companion_extractor_runner: CompanionExtractorRunner
    call_text_files: CallTextFileService    # the web call's "keep as a file" (VOICE_CALL_LIVE_TEXT_RFC §4.11)
    geo_marks: Optional[GeoMarkService]     # the `$geo` page's routes (GEO_MARKS_RFC §4.5); None without a task queue
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
    """Build every non-web object of the main service from the settings dict and the Firestore client; starts nothing.

    One bundle per subsystem, in dependency order: a bundle a builder takes is a bundle built before it."""
    env_config = config["ENVIRONMENT_CONFIG"]
    boot = build_boot_checks(config, env_config)    # refuses the start before anything touches the database
    identity = build_identity(config, db_client, env_config, auth_config=boot.auth_config, digest_key=boot.digest_key)
    ops = build_operations(config, env_config, topology=boot.deploy_topology, account_repo=identity.account_repo)
    routing = build_session_routing(db_client, env_config, user_repo=identity.user_repo, task_queue=ops.task_queue)
    documents = build_document_backends(config, parser=boot.parse_adapter)
    logger.info("🏭 Initializing Service Container...")
    container = ServiceContainer(
        config=config, db_client=db_client, env_config=env_config, deploy_topology=boot.deploy_topology,
        account_repo=identity.account_repo, overflow_callback=routing.overflow_callback,
        threshold_resolver=routing.threshold_resolver, alert_webhook=ops.alert_sink, cipher=boot.cipher,
        user_repo=identity.user_repo,      # the email classifier reads the user's configuration per batch
        quota_service=ops.quota_service,   # and bills the indexing job's account
        document_backends=documents)
    coordinator = _build_coordinator(container, ops)
    job_registry, image_registry, video_registry = _build_generation_registries(
        config, boot.deploy_topology, ops.task_queue)
    delivery = build_file_delivery(config, db_client, env_config, token_secret=boot.auth_config.oauth_session_secret,
                                   service_url=boot.deploy_topology.service_url)
    languages = build_languages(config, user_repo=identity.user_repo, account_repo=identity.account_repo)
    notifications = build_notifications(config, db_client, env_config, coordinator=coordinator,
                                        session_store=container.session_store, media_storage=delivery.media_storage,
                                        link_service=delivery.link_service, languages=languages)
    jobs = build_background_jobs(container=container, coordinator=coordinator,
                                 notification_service=notifications.service, task_queue=ops.task_queue,
                                 delivery=delivery, user_repo=identity.user_repo, languages=languages)
    companions = build_companions(db_client, env_config, container=container, user_repo=identity.user_repo,
                                  quota_service=ops.quota_service, extraction_queue=routing.companion_extraction_queue,
                                  notification_service=notifications.service, languages=languages)
    memory_search = build_memory_search(db_client, env_config, container=container)
    agents = build_agents(config, db_client, env_config, container=container, coordinator=coordinator, boot=boot,
                          identity=identity, ops=ops, job_registry=job_registry, image_registry=image_registry,
                          video_registry=video_registry, companions=companions,
                          notification_service=notifications.service, languages=languages, memory_search=memory_search)
    coordinator.set_agent_factory(agents.factory)  # Enable lazy agent instantiation
    integrations = build_integrations(db_client, env_config, container=container, boot=boot, ops=ops,
                                      user_repo=identity.user_repo, notification_service=notifications.service,
                                      languages=languages, ensure_agents=agents.factory.ensure_agents_for_user)
    memory = build_memory(db_client, env_config, container=container, coordinator=coordinator,
                          agent_factory=agents.factory, identity=identity, ops=ops, routing=routing,
                          memory_search=memory_search, notifications=notifications, languages=languages,
                          cabinet_memory_url=boot.deploy_topology.cabinet_memory_url)
    chat, slack_adapter = _build_slack(config, db_client, container=container, coordinator=coordinator, boot=boot,
                                       identity=identity, ops=ops, routing=routing, documents=documents,
                                       delivery=delivery, languages=languages, notifications=notifications,
                                       agents=agents, memory=memory)
    worker_handler = _build_worker_handler(
        db_client, env_config, container=container, identity=identity, ops=ops, delivery=delivery,
        notifications=notifications, jobs=jobs, companions=companions, agents=agents, integrations=integrations,
        memory=memory, video_registry=video_registry, slack_adapter=slack_adapter)
    telegram_adapter = _build_telegram(db_client, env_config, chat=chat, container=container, identity=identity,
                                       ops=ops, languages=languages, notifications=notifications,
                                       worker_handler=worker_handler)
    return ApplicationGraph(
        container=container, agent_factory=agents.factory, coordinator=coordinator, worker_handler=worker_handler,
        agent_worker_handler=jobs.agent_worker_handler, slack_adapter=slack_adapter, telegram_adapter=telegram_adapter,
        notification_service=notifications.service,
        user_repo=identity.user_repo, whitelist_repo=identity.whitelist_repo, account_repo=identity.account_repo,
        invite_service=identity.invite_service, cabinet_facts=memory.cabinet_facts, fact_answers=memory.fact_answers,
        quota_service=ops.quota_service, alert_sink=ops.alert_sink, session_service=identity.session_service,
        auth_service=identity.auth_service, auth_registry=identity.auth_registry, auth_config=boot.auth_config,
        deploy_topology=boot.deploy_topology,
        connect_service=integrations.connect, credential_revocation=integrations.credential_revocation,
        drive_disconnect_service=integrations.drive_disconnect, task_setup_service=integrations.task_setup,
        reconnect_service=integrations.reconnect, twilio_verify_client=identity.twilio_verify_client,
        task_dispatch=jobs.task_dispatch, file_token_service=delivery.file_token_service,
        media_storage=delivery.media_storage, short_link_service=delivery.short_link_service,
        companion_extractor_runner=companions.extractor_runner, call_text_files=companions.call_text_files,
        geo_marks=chat.geo_mark_service,
        language_service=languages.resolver, language_preferences=agents.language_preferences,
        localization=languages.localization, cipher=boot.cipher, digest_key=boot.digest_key,
        agent_task_queue=ops.task_queue, billing_agent=ops.billing_agent, logger_agent=ops.logger_agent,
        html_renderer=notifications.html_renderer, fact_search=memory_search.fact_search,
        place_atlas=memory_search.place_atlas, voice_mcp_signing_key=boot.voice_mcp_signing_key,
    )


# ----------------------------------------------------------------------------------------------------------------------
# The steps kept in this module: the Registry pattern's and the router-factory cycle's calls are allowlisted here
# (late_wiring_calls_allowlist.txt), and the graph tests patch the chat and platform names in this module.

def _build_coordinator(container: ServiceContainer, ops: Operations) -> AgentCoordinator:
    """The coordinator over every declared agent, with the container's file-ref resolver for specialist delegation and
    the billing and logger agents registered; the agent factory is bound to it once built."""
    logger.info("🎯 Initializing Agent Registry and Coordinator...")
    agent_registry = AgentRegistry()
    for descriptor in ALL_DESCRIPTORS:
        agent_registry.register(descriptor)
    coordinator = AgentCoordinator(
        registry=agent_registry,
        task_queue=ops.task_queue,
        file_ref_resolver=(
            container.file_conversion_service.prefetch if container.file_conversion_service else None
        ),
        alert_sink=ops.alert_sink,
    )
    coordinator.register_agent(ops.billing_agent)
    coordinator.register_agent(ops.logger_agent)
    return coordinator


def _build_generation_registries(config: dict, topology: DeployTopology, task_queue: Optional[GcpTaskQueue]) -> Tuple[
        ProviderRegistry, ProviderRegistry, ProviderRegistry]:
    """The deep research, image and video provider registries.

    Deep research has one provider, "claude" (a Cloud Run Job); its adapter maps tier to model itself, and
    CLAUDE_DEEP_RESEARCH_MODEL pins a model. Image and video reuse the XAI_API_KEY of the
    Grok LLM adapter behind their own ports (IMAGE_GENERATION_RFC §3.6-3.7, VIDEO_GENERATION_RFC §3.5-3.6); the video
    adapter takes the raw task queue (adapters depend on ports only) to enqueue its first poll tick."""
    job_registry = ProviderRegistry()
    if config.get("ANTHROPIC_API_KEY") and config.get("GOOGLE_CLOUD_PROJECT"):
        cloud_jobs_adapter = CloudRunJobsAdapter(project=config["GOOGLE_CLOUD_PROJECT"], region=topology.region)
        job_registry.register("claude", ClaudeDeepResearchAdapter(
            job_runner=cloud_jobs_adapter,
            job_name=f"alek-research-job-{topology.queue_suffix}",
            model_override=os.getenv("CLAUDE_DEEP_RESEARCH_MODEL"),
        ))
        logger.info("🔬 Deep research adapter registered: provider=claude (Cloud Run Job)")

    image_registry = ProviderRegistry()
    video_registry = ProviderRegistry()
    if config.get("XAI_API_KEY"):
        image_registry.register("grok", GrokImageAdapter(api_key=config["XAI_API_KEY"]))
        video_registry.register("grok", GrokVideoAdapter(api_key=config["XAI_API_KEY"], task_queue=task_queue))
        logger.info("🎨 Image and video generation adapters registered: provider=grok")
    else:
        logger.info("ℹ️ Image and video generation not configured (XAI_API_KEY not set)")
    return job_registry, image_registry, video_registry


def _build_slack(config: dict, db_client: Any, *, container: ServiceContainer, coordinator: AgentCoordinator,
                 boot: BootChecks, identity: Identity, ops: Operations, routing: SessionRouting,
                 documents: DocumentBackends, delivery: FileDelivery, languages: Languages,
                 notifications: Notifications, agents: Agents, memory: Memory) -> Tuple[ChatServices, HTTPModeAdapter]:
    """The ConversationHandler dependencies both platforms share, held once, and the Slack adapter over them and the
    notifications' Slack app. The fallback service (Smart failed or timed out → Quick → a synthetic apology) is built
    here for the chat handlers only."""
    logger.info("🔌 Initializing Slack Adapter...")
    _consolidation_service = memory.consolidation_service
    geo_marks = build_geo_mark_service(
        config, db_client, config["ENVIRONMENT_CONFIG"], digest_key=boot.digest_key,
        service_url=boot.deploy_topology.service_url, session_store=container.session_store,
        channel_factory=notifications.channel_factory, task_queue=ops.task_queue, user_repo=identity.user_repo,
        localization_port=languages.localization_port)
    chat = ChatServices(
        coordinator=coordinator,
        agent_factory=agents.factory,
        consolidation_queue=routing.consolidation_queue,
        consolidation_config=config.get("CONSOLIDATION"),
        consolidation_service=_consolidation_service,
        audio_service=documents.transcriber,
        notification_service=notifications.service,
        localization=languages.localization,
        file_conversion_service=container.file_conversion_service,
        fallback_service=AgentFallbackService(coordinator=coordinator, alert_webhook=ops.alert_sink),
        short_link_service=delivery.short_link_service,
        skill_service=container.skill_service,
        memory_fact_service=container.memory_fact_service,
        fact_answers=memory.fact_answers,
        drive_note_service=command_drive_note_service(container.drive_note_service, container.drive_notes_enabled),
        long_turn_service=agents.long_turn_service,
        doc_delivery_service=delivery.doc_delivery_service,
        geo_mark_service=geo_marks,
    )
    slack_adapter = SlackAdapterFactory.create_adapter(
        app=notifications.slack_app,
        config=config,
        deploy_topology=boot.deploy_topology,
        chat=chat,
        iam_service=identity.iam_service,
        session_store=container.session_store,
        db_client=db_client,
        language_service=languages.resolver,
        channel_binding_service=routing.channel_binding_service,
        worker_queue=ops.task_queue,
        rich_content_service=notifications.slack_kit.rich_content,
    )
    slack_adapter.register_handlers()
    return chat, slack_adapter


def _build_worker_handler(db_client: Any, env_config: EnvironmentConfig, *, container: ServiceContainer,
                          identity: Identity, ops: Operations, delivery: FileDelivery, notifications: Notifications,
                          jobs: BackgroundJobs, companions: Companions, agents: Agents, integrations: Integrations,
                          memory: Memory, video_registry: ProviderRegistry,
                          slack_adapter: HTTPModeAdapter) -> WorkerHandler:
    """`/worker`: Cloud Tasks is at-least-once, so claims keyed by task name stop a redelivered task from running twice
    (a claim whose attempt died is taken over by the retry; the 1 h TTL is garbage collection and the done-window, not
    the liveness rule). Built after the Slack adapter, whose queued-event entry it takes."""
    user_repo, ensure_agents = identity.user_repo, agents.factory.ensure_agents_for_user
    return WorkerHandler(
        task_claims=FirestoreTaskClaimStore(
            db_client=db_client,
            collection_name=env_config.worker_task_dedup_collection,
            ttl_seconds=3600,
        ),
        agent_worker_handler=jobs.agent_worker_handler,
        email_indexing_service=container.email_indexing_service,
        consolidation_service=memory.consolidation_service,
        agent_factory=agents.factory,
        user_repo=user_repo,
        task_dispatch=jobs.task_dispatch,
        task_setup=integrations.task_setup,
        task_indexing=container.task_indexing,
        reminders_service=jobs.reminders,
        companion_extraction=companions.extraction_service,
        drive_sync_scheduler=integrations.drive_sync_scheduler,
        fact_lifecycle_sweep=memory.fact_lifecycle_sweep,
        fact_review=memory.fact_review,
        interactive_choice=memory.interactive_choice,
        reconnect=integrations.reconnect,
        gmail_reconnect_flag=integrations.gmail_reconnect_flag,
        slack_worker=slack_adapter.handle_queued_event,
        tasks=build_worker_tasks(
            notes_port=container.notes_adapter, user_repo=user_repo, ensure_agents=ensure_agents,
            notification_service=notifications.service, alert_sink=ops.alert_sink,
            task_dispatch=jobs.task_dispatch, video_registry=video_registry, quota_service=ops.quota_service,
            media_storage=delivery.media_storage, link_service=delivery.link_service,
            short_link_service=delivery.short_link_service, email_review=container.email_review_service,
            # One review per user per local date; 48 h outlives the 3 h window and the date edge.
            daily_review_marks=FirestoreDedupStore(
                db_client=db_client,
                collection_name=env_config.worker_task_dedup_collection,
                ttl_seconds=172800,
            ),
            gmail_reconnect_flag=integrations.gmail_reconnect_flag, account_repo=identity.account_repo,
            geo_mark_turn=slack_adapter.handle_geo_mark_turn),
    )


def _build_telegram(db_client: Any, env_config: EnvironmentConfig, *, chat: ChatServices, container: ServiceContainer,
                    identity: Identity, ops: Operations, languages: Languages, notifications: Notifications,
                    worker_handler: WorkerHandler) -> Optional[TelegramWebhookAdapter]:
    """Telegram, when configured. All or nothing (owner ruling): the adapter and its media adapter are built first; the
    late platform binding (notifications, channels, the worker's queued-update entry) runs only after both exist, so it
    is built last. A failure is logged and the bot runs without Telegram."""
    telegram_config = validate_telegram_config()
    if not telegram_config:
        logger.info("ℹ️ Telegram not configured (TELEGRAM_BOT_TOKEN not set)")
        return None
    logger.info("🤖 Initializing Telegram adapter...")
    try:
        dedup_store = FirestoreDedupStore(db_client=db_client, collection_name=env_config.event_dedup_collection)
        # One Bot, one media adapter, one RichContentService: shared by the chat handler and the notification service
        telegram_media_adapter = TelegramMediaAdapter(bot=Bot(telegram_config["token"]))
        telegram_rich_content = RichContentService(media_port=telegram_media_adapter,
                                                   html_renderer=notifications.html_renderer)
        telegram_adapter = TelegramAdapterFactory.create_adapter(
            token=telegram_config["token"],
            webhook_secret=telegram_config["webhook_secret"],
            dedup_store=dedup_store,
            session_store=container.session_store,
            chat=chat,
            iam_service=identity.iam_service,
            language_service=languages.resolver,
            task_queue=ops.task_queue,
            rich_content_service=telegram_rich_content,
        )
    except Exception as e:
        logger.error(f"❌ Failed to initialize Telegram adapter: {e}", exc_info=True)
        logger.warning("🤖 Bot will continue without Telegram support")
        return None
    register_late_platform(
        PlatformKit(
            platform="telegram",
            media=telegram_media_adapter,
            rich_content=telegram_rich_content,
            channel=partial(_make_telegram_channel, telegram_adapter),
        ),
        notifications=notifications.service,
        channels=notifications.channel_factory,
        worker=worker_handler,
        queued_update=telegram_adapter.handle_queued_update,
    )
    return telegram_adapter


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
