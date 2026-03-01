import sys
import os
import signal
import logging
import asyncio
import traceback
from slack_bolt.async_app import AsyncApp

from src.config.settings import load_settings
from src.adapters.firestore_user_repo import FirestoreUserRepository
from src.adapters.firestore_account_repo import FirestoreAccountRepository
from src.adapters.firestore_quota_service import FirestoreQuotaService
from src.adapters.firestore_consolidation_queue import FirestoreConsolidationQueue
from src.services.iam_service import IAMService
from src.services.user_agent_factory import UserAgentFactory
from src.composition.slack_adapter_factory import SlackAdapterFactory
from src.utils.server import run_dummy_server
from src.utils.logger import logger
from src.utils.telemetry import init_telemetry
from src.adapters.firestore_repo import FirestoreFactRepository
from src.services.file_upload_service import FileUploadService
from src.composition.service_container import ServiceContainer
from src.infrastructure.agent_coordinator import AgentCoordinator
from src.infrastructure.agent_registry import AgentRegistry, AgentManifest, ExecutionMode
from src.adapters.gcp_task_queue import GcpTaskQueue
from src.handlers.agent_worker_handler import AgentWorkerHandler
from src.domain.agent import AgentConfig
from src.agents.infrastructure.billing_agent import BillingAgent
from src.agents.infrastructure.logger_agent import LoggerAgent
from quart import Quart
from src.web.oauth_app import create_oauth_blueprint
from src.web.user_cabinet_app import create_user_cabinet_blueprint
from src.services.authentication_service import AuthenticationService
from src.services.session_service import SessionService
from src.services.invite_code_service import InviteCodeService
from src.services.auth_provider_registry import AuthProviderRegistry
from src.config.auth import AuthConfig
from src.adapters.firebase_auth_adapter import FirebaseAuthAdapter
from src.adapters.firestore_invite_code_repo import FirestoreInviteCodeRepository
from src.services.gmail_oauth_service import GmailOAuthService
from src.adapters.firestore_oauth_credentials_adapter import FirestoreOAuthCredentialsAdapter
from src.adapters.firestore_indexed_email_repo import FirestoreIndexedEmailRepository
from src.adapters.firestore_email_job_repo import FirestoreEmailJobRepository
from src.adapters.firestore_email_exclusions_adapter import FirestoreEmailExclusionsAdapter
from src.adapters.gmail_provider_adapter import GmailProviderAdapter
from src.agents.email_classification_agent import EmailClassificationAgent
from src.services.email_indexing_service import EmailIndexingService
from src.services.prompt_builder import PromptBuilder
from src.domain.user import UserBotConfig
from src.adapters.firestore_notification_state_adapter import FirestoreNotificationStateAdapter
from src.adapters.notification_channel_factory import NotificationChannelFactory
from src.services.user_notification_service import UserNotificationService


async def main():
    """Main function to start the bot with clean dependency injection."""

    print("=" * 80)
    print("🚀 ALEK BOT STARTING...")
    print("=" * 80)

    init_telemetry("alek-core")

    # Set external libraries to WARNING to reduce noise, but keep our logs at DEBUG
    logging.getLogger("slack_bolt").setLevel(logging.WARNING)
    logging.getLogger("slack_sdk").setLevel(logging.WARNING)
    logging.getLogger("websocket").setLevel(logging.WARNING)

    logger.info("=" * 80)
    logger.info("🚀 ALEK BOT MAIN() CALLED")
    logger.info("=" * 80)

    try:
        logger.info("🔐 Loading configuration...")
        logger.debug(f"Current working directory: {os.getcwd()}")
        logger.debug(f"Python version: {sys.version}")
        config = load_settings()
        logger.info("✅ Configuration loaded successfully")
    except Exception as e:
        logger.error(f"❌ Configuration Error: {e}", exc_info=True)
        sys.exit(1)

    try:
        env_config = config["ENVIRONMENT_CONFIG"]

        # Database name (supports Named Database for multi-region migration)
        database_id = os.getenv("FIRESTORE_DATABASE", "(default)")
        
        if env_config.use_emulator:
            logger.info(f"🏠 Using Firestore EMULATOR at {env_config.get_emulator_host()}")
            logger.warning("⚠️ Emulator data is EPHEMERAL - will be lost on restart!")
            from google.cloud import firestore
            db_client = firestore.AsyncClient(project="emulator-project", database=database_id)
        else:
            logger.info(f"☁️ Using Firestore CLOUD in {env_config.env.value} mode (database: {database_id})")
            from google.cloud import firestore
            db_client = firestore.AsyncClient(project=config["GOOGLE_CLOUD_PROJECT"], database=database_id)
        
        # Warm up Firestore connection (OAuth) during startup, not on first user request
        logger.info("🔥 Warming up Firestore connection and pre-computing embeddings...")
        try:
            # 1. Trigger OAuth and connection initialization
            test_col = db_client.collection("_warmup")
            await test_col.limit(1).get()
            logger.info("✅ Firestore connection initialized")
        except Exception as e:
            logger.warning(f"⚠️ Firestore Warmup failed (non-critical): {e}")

        # 2. Warm up Firestore vector search backend (separate from gRPC connection warmup).
        # The regular read above warms the gRPC channel, but each FLAT vector index is loaded
        # into memory lazily on the first find_nearest call for that field. Without this,
        # the first consolidation search triggers a 40-60s per-index load → retry cascade.
        # All 3 indexes (vector, tags_vector, metadata_vector) must be warmed in parallel —
        # each is an independent index with its own cold start.
        try:
            from google.cloud.firestore_v1.base_vector_query import DistanceMeasure
            from google.cloud.firestore_v1.vector import Vector as FSVector
            from google.cloud.firestore import FieldFilter as FSFieldFilter
            facts_col = db_client.collection(env_config.domain_facts_collection)
            warmup_vector = FSVector([0.1] * 768)
            warmup_query = facts_col.where(
                filter=FSFieldFilter("account_id", "==", "_warmup")
            ).where(
                filter=FSFieldFilter("state", "==", "current")
            )
            await asyncio.gather(
                warmup_query.find_nearest(
                    vector_field="vector",
                    query_vector=warmup_vector,
                    distance_measure=DistanceMeasure.COSINE,
                    limit=1
                ).get(),
                warmup_query.find_nearest(
                    vector_field="tags_vector",
                    query_vector=warmup_vector,
                    distance_measure=DistanceMeasure.COSINE,
                    limit=1
                ).get(),
                warmup_query.find_nearest(
                    vector_field="metadata_vector",
                    query_vector=warmup_vector,
                    distance_measure=DistanceMeasure.COSINE,
                    limit=1
                ).get(),
                return_exceptions=True  # one index fails → others continue
            )
            logger.info("✅ Firestore vector search warmed up (all 3 fields)")
        except Exception as e:
            logger.warning(f"⚠️ Firestore vector warmup failed (non-critical): {e}")

        # NOTE: Embedding initialization moved to UserAgentFactory.ensure_agents_for_user()
        # where repository is created with proper BiographicalContextService injection

        logger.info("� Initializing Account Repository...")
        account_repo = FirestoreAccountRepository(
            db_client=db_client,
            collection_name=env_config.account_collection_name
        )

        logger.info("�👤 Initializing User Repository...")
        user_repo = FirestoreUserRepository(db_client, env_config, account_repo)

        logger.info("💌 Initializing Invite Code Service...")
        from src.adapters.firestore_invite_code_repo import FirestoreInviteCodeRepository
        from src.services.invite_code_service import InviteCodeService
        
        invite_repo = FirestoreInviteCodeRepository(db_client, env_config)
        
        # IAM-Centric Architecture (2026-02-05)
        logger.info("📋 Initializing Whitelist Repository...")
        from src.adapters.firestore_whitelist_repo import FirestoreWhitelistRepository
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
        agent_registry.register(AgentManifest(
            agent_id="memory_search_agent",
            intents={"search_memory": ExecutionMode.SYNC},
            description="Semantic search through biographical facts and personal knowledge",
        ))
        agent_registry.register(AgentManifest(
            agent_id="web_search_agent",
            intents={"search_web": ExecutionMode.SYNC},
            description="Real-time web search for current information",
        ))
        agent_registry.register(AgentManifest(
            agent_id="web_search_light_agent",
            intents={"search_web_light": ExecutionMode.SYNC},
            description="Lightweight single-pass web search for quick answers",
        ))
        agent_registry.register(AgentManifest(
            agent_id="email_search_agent",
            intents={
                "search_emails": ExecutionMode.SYNC,
                "get_email_details": ExecutionMode.SYNC,
                "get_email_attachment": ExecutionMode.SYNC,
            },
            description=(
                "Email archive specialist. Three intents:\n"
                "• search_emails — semantic search across indexed emails. "
                "Pass query = user's question as-is.\n"
                "• get_email_details — fetch full body of a known email. "
                "Pass context={\"email_id\": \"<id>\"}.\n"
                "• get_email_attachment — read attachment as text. "
                "Pass context={\"email_id\": \"<id>\", \"filename\": \"file.pdf\"}."
            ),
        ))

        # Task queue: only in HTTP mode where Cloud Tasks is available
        agent_task_queue = None
        if env_config.is_http_mode and config.get("GOOGLE_CLOUD_PROJECT"):
            queue_suffix = "dev" if env_config.is_development else "prod"
            service_url = config.get("CLOUD_RUN_SERVICE_URL") or "http://localhost:8080"
            agent_task_queue = GcpTaskQueue(
                project_id=config["GOOGLE_CLOUD_PROJECT"],
                location="us-central1",
                queue_name=f"agent-tasks-{queue_suffix}",
                service_url=service_url,
                service_account_email=config.get("SERVICE_ACCOUNT_EMAIL"),
            )
            await agent_task_queue.create_queue_if_not_exists()
            logger.info(f"📬 Agent task queue initialized: agent-tasks-{queue_suffix}")
        else:
            logger.info("📬 Agent task queue: disabled (socket mode or no GCP project)")

        logger.info("🎯 Initializing Agent Coordinator...")
        coordinator = AgentCoordinator(registry=agent_registry, task_queue=agent_task_queue)

        logger.info("🔧 Initializing Agent Worker Handler...")
        agent_worker_handler = AgentWorkerHandler(coordinator=coordinator)

        logger.info("💳 Initializing Billing Agent...")
        quota_service = FirestoreQuotaService(user_repo)
        billing_agent = BillingAgent(
            config=AgentConfig(
                agent_id="billing_agent",
                agent_type="billing",
                timeout_ms=None,
                capabilities=["usage_tracking", "quota_management"]
            ),
            quota_service=quota_service
        )
        coordinator.register_agent(billing_agent)
        await billing_agent.start()

        logger.info("🧾 Initializing Logger Agent...")
        log_sink = None
        if not env_config.is_development:
            try:
                from src.adapters.gcp_log_sink import GcpLogSink
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
        coordinator.register_agent(logger_agent)
        await logger_agent.start()

        logger.info("📦 Initializing Consolidation Queue...")
        consolidation_queue = FirestoreConsolidationQueue(
            db_client=db_client,
            env_config=env_config
        )

        # Create overflow callback for session store.
        # agent_factory is created AFTER ServiceContainer (it depends on session_store from it),
        # so we use a mutable holder to safely reference it from the closure.
        from src.domain.consolidation import ConsolidationBatch
        from src.ports.llm_service import Message

        _agent_factory_ref: list = [None]  # [0] set after agent_factory is created

        async def overflow_callback(user_id: str, session_id: str, messages: list[Message]):
            """
            Triggered when hot storage exceeds threshold.
            Creates a batch and immediately triggers processing.
            """
            factory = _agent_factory_ref[0]
            if factory is None:
                logger.error("❌ [Overflow] overflow_callback fired before agent_factory initialized — batch lost!")
                return

            try:
                # 1. Serialize messages for storage
                serialized = []
                for msg in messages:
                    serialized.append({
                        "role": msg.role,
                        "parts": [{"text": p.text} for p in msg.parts if p.text],
                        "created_at": msg.created_at
                    })

                # 2. Create a lightweight batch
                batch = ConsolidationBatch(
                    user_id=user_id,
                    session_id=session_id,
                    messages=serialized
                )

                # 3. Enqueue and trigger processing
                if consolidation_queue:
                    batch_id = await consolidation_queue.enqueue_batch(batch)
                    logger.info(f"📦 [Overflow] Created batch {batch_id} for user {user_id[:8]}")

                    # Trigger processing — via Cloud Tasks in HTTP mode (own request = full CPU),
                    # fire-and-forget create_task in socket mode (no CPU throttling there)
                    if agent_task_queue:
                        await agent_task_queue.enqueue_consolidation_task(user_id=user_id)
                        logger.info(f"📬 [Overflow] Consolidation task enqueued for user {user_id[:8]}")
                    else:
                        from src.handlers.consolidation_handler import process_user_batches_on_overflow
                        asyncio.create_task(process_user_batches_on_overflow(
                            user_id=user_id,
                            coordinator=coordinator,
                            agent_factory=factory,
                            queue=consolidation_queue
                        ))
                else:
                    logger.warning("⚠️ Consolidation queue not initialized, overflow batch lost!")
            except Exception as e:
                logger.error(f"❌ Error in overflow_callback: {e}", exc_info=True)

        # 1. Shared service container (LLM adapters, repositories, prompt infra, session store)
        logger.info("🏭 Initializing Service Container...")
        container = ServiceContainer(
            config=config,
            db_client=db_client,
            env_config=env_config,
            account_repo=account_repo,
            overflow_callback=overflow_callback,
        )
        file_service = FileUploadService(container.llm_service)
        session_store = container.session_store  # Alias for Slack/Telegram adapters and shutdown

        # 2. Initialize UserAgentFactory — receives ports only, no adapter instantiation
        logger.info("🏭 Initializing User Agent Factory...")
        agent_factory = UserAgentFactory(
            config=config,
            env_config=env_config,
            coordinator=coordinator,
            user_repo=user_repo,
            account_repo=account_repo,
            **container.agent_services(),
        )
        _agent_factory_ref[0] = agent_factory  # Wire deferred reference for overflow_callback
        await agent_factory.start()

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
            account_repo=account_repo
        )
        session_service = SessionService(
            secret_key=auth_config.oauth_session_secret,
            access_token_ttl=auth_config.access_token_ttl,
            refresh_token_ttl=auth_config.refresh_token_ttl
        )

        # Gmail OAuth + email adapters
        gmail_oauth_service = GmailOAuthService(
            client_id=auth_config.google_oauth_client_id,
            client_secret=auth_config.google_oauth_client_secret,
        )
        oauth_credentials_port = FirestoreOAuthCredentialsAdapter(db_client=db_client, env_config=env_config)
        indexed_email_repo = FirestoreIndexedEmailRepository(db_client=db_client, env_config=env_config)

        # Email indexing pipeline (shared, stateless; user_id passed per-request)
        gmail_provider = GmailProviderAdapter(
            client_id=auth_config.google_oauth_client_id,
            client_secret=auth_config.google_oauth_client_secret,
        )
        email_job_repo = FirestoreEmailJobRepository(db_client=db_client, env_config=env_config)
        email_exclusions_repo = FirestoreEmailExclusionsAdapter(db_client=db_client, env_config=env_config)
        email_prompt_builder = PromptBuilder(repo=None, assembly_service=container.assembly_service)
        email_classifier = EmailClassificationAgent(
            config=AgentConfig(agent_id="email_classifier", agent_type="email_classifier"),
            execution_context=container.context_builder.build("email_classifier", UserBotConfig()),
            prompt_builder=email_prompt_builder,
            gmail=gmail_provider,
        )
        email_indexing_service = EmailIndexingService(
            gmail=gmail_provider,
            email_repo=indexed_email_repo,
            job_repo=email_job_repo,
            exclusions_repo=email_exclusions_repo,
            classifier=email_classifier,
            embedding=container.embedding_service,
        )

        # Notification service: stores last active channel, sends background alerts via QuickAgent
        notification_state_repo = FirestoreNotificationStateAdapter(db_client=db_client, env_config=env_config)
        notification_channel_factory = NotificationChannelFactory()  # adapters wired after creation below
        notification_service = UserNotificationService(
            state_repo=notification_state_repo,
            channel_factory=notification_channel_factory,
            coordinator=coordinator,
        )

        # Create blueprints (will be registered on slack_adapter.quart_app)
        oauth_bp = create_oauth_blueprint(
            auth_service=auth_service,
            session_service=session_service,
            auth_registry=auth_registry,
            auth_config=auth_config,
            invite_service=invite_service,
            gmail_oauth_service=gmail_oauth_service,
            oauth_credentials_port=oauth_credentials_port,
        )

        cabinet_bp = create_user_cabinet_blueprint(
            invite_service=invite_service,
            session_service=session_service,
            user_repo=user_repo,
            fact_repo=FirestoreFactRepository(db_client=db_client, env_config=env_config),
            embedding_service=container.embedding_service,
            oauth_credentials_port=oauth_credentials_port,
            gmail_oauth_service=gmail_oauth_service,
            indexed_email_repo=indexed_email_repo,
            email_indexing_service=email_indexing_service,
            email_job_repo=email_job_repo,
            task_queue=agent_task_queue,
        )

        logger.info("✅ OAuth + Cabinet services initialized")

    except Exception as e:
        logger.error(f"❌ Infrastructure Initialization Error: {e}")
        sys.exit(1)

    try:
        if env_config.is_socket_mode:
            try:
                run_dummy_server()
            except Exception as e:
                logger.error(f"❌ Failed to start dummy server: {e}")

        # HTML renderer (optional — lazy-starts Chromium on first html_card request)
        html_renderer = None
        if config.get("ENABLE_HTML_RENDERER"):
            from src.adapters.playwright_html_renderer import PlaywrightHtmlRenderer
            html_renderer = PlaywrightHtmlRenderer()
            logger.info("✅ HTML renderer (Playwright) enabled")
        else:
            logger.info("ℹ️ HTML renderer disabled (ENABLE_HTML_RENDERER not set)")

        logger.info("🔌 Initializing Slack Adapter...")
        logger.debug(f"Socket mode: {env_config.is_socket_mode}")
        logger.debug(f"HTTP mode: {env_config.is_http_mode}")

        bot_token = config["SLACK_BOT_TOKEN"]
        if env_config.is_socket_mode and config.get("DEV_SLACK_BOT_TOKEN"):
            logger.info("🛠 Using DEVELOPMENT Slack Bot Token")
            bot_token = config["DEV_SLACK_BOT_TOKEN"]

        logger.debug(f"Bot token starts with: {bot_token[:10]}...")

        logger.debug("Creating AsyncApp...")
        app = AsyncApp(token=bot_token)
        logger.debug("AsyncApp created successfully")

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
            audio_service=None,
            html_renderer=html_renderer,
            notification_service=notification_service,
        )
        notification_channel_factory.set_slack_adapter(slack_adapter)

        slack_adapter.register_handlers()

        # ====================================================================
        # PHASE 0.5.1: Shared Quart App with Blueprint Pattern
        # Register Slack, OAuth, and Cabinet blueprints on shared app
        # Only for HTTP Mode
        # ====================================================================
        if hasattr(slack_adapter, 'get_blueprint'):
            logger.info("🌐 Creating shared Quart app for multi-platform support...")
            try:
                from quart import Quart
                from hypercorn.asyncio import serve
                from hypercorn.config import Config as HypercornConfig
                
                # Create shared Quart app
                main_app = Quart(__name__)
                
                # Register Slack blueprint
                slack_bp = slack_adapter.get_blueprint()
                main_app.register_blueprint(slack_bp, url_prefix="/slack")
                logger.info("✅ Slack blueprint registered at /slack/events")
                
                # Register OAuth + Cabinet blueprints
                main_app.register_blueprint(oauth_bp)
                main_app.register_blueprint(cabinet_bp)
                
                # ====================================================================
                # PHASE 3: Telegram Integration (Optional)
                # Initialize Telegram adapter if configured
                # ====================================================================
                from src.config.environment import validate_telegram_config
                telegram_config = validate_telegram_config()
                
                if telegram_config:
                    logger.info("🤖 Initializing Telegram adapter...")
                    try:
                        from src.adapters.firestore_dedup_store import FirestoreDedupStore
                        from src.adapters.platform.factory import PlatformAdapterFactory
                        from src.composition.telegram_adapter_factory import TelegramAdapterFactory

                        # Initialize dedup store for Telegram
                        dedup_store = FirestoreDedupStore(
                            db_client=db_client,
                            collection_name=env_config.event_dedup_collection
                        )

                        # Create adapter via factory (wires RichContentService + html_renderer)
                        telegram_adapter = TelegramAdapterFactory.create_adapter(
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
                            html_renderer=html_renderer,
                            notification_service=notification_service,
                        )
                        notification_channel_factory.set_telegram_adapter(telegram_adapter)

                        # Register Telegram blueprint
                        telegram_bp = telegram_adapter.get_blueprint()
                        main_app.register_blueprint(telegram_bp, url_prefix="/telegram")

                        # Register in factory
                        PlatformAdapterFactory.register("telegram", telegram_adapter)

                        logger.info("✅ Telegram adapter registered at /telegram/webhook")
                    except Exception as e:
                        logger.error(f"❌ Failed to initialize Telegram adapter: {e}", exc_info=True)
                        logger.warning("🤖 Bot will continue without Telegram support")
                else:
                    logger.info("ℹ️ Telegram not configured (TELEGRAM_BOT_TOKEN not set)")
                
                # Add CORS headers for web UI
                _ALLOWED_ORIGINS = {
                    "https://app.alekbot.app",
                    "https://dev.alekbot.app",
                    "http://localhost:3000",
                }

                @main_app.after_request
                async def add_cors_headers(response):
                    """Add CORS headers for OAuth + Cabinet."""
                    from quart import request as _req
                    origin = _req.headers.get("Origin", "")
                    if origin in _ALLOWED_ORIGINS:
                        response.headers["Access-Control-Allow-Origin"] = origin
                        response.headers["Access-Control-Allow-Credentials"] = "true"
                    response.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
                    response.headers["Access-Control-Allow-Headers"] = "Content-Type, Authorization"
                    return response
                
                # Add root endpoint with OAuth redirect
                @main_app.route("/", methods=["GET"])
                async def root():
                    """
                    Root endpoint: redirect to OAuth or Cabinet depending on auth status.
                    
                    UX Flow:
                    - If authenticated → /cabinet
                    - If not authenticated → /auth/login (OAuth flow)
                    """
                    from quart import redirect, session as quart_session
                    
                    # Check if user is authenticated via session
                    access_token = quart_session.get("access_token")
                    
                    if access_token:
                        # User is authenticated → go to cabinet
                        return redirect("/cabinet")
                    else:
                        # User is not authenticated → start OAuth flow
                        return redirect("/auth/login")
                
                # Add health endpoint
                @main_app.route("/health", methods=["GET"])
                async def health():
                    from quart import jsonify
                    return jsonify({"status": "healthy", "mode": "http"}), 200
                
                # Add /worker endpoint (moved from adapter to shared app)
                @main_app.route("/worker", methods=["POST"])
                async def worker():
                    from quart import request, jsonify
                    payload = await request.get_json(silent=True) or {}
                    if payload.get("task_type") == "agent_execution":
                        result = await agent_worker_handler.handle_task(payload)
                        return jsonify(result), 200
                    elif payload.get("task_type") == "email_indexing":
                        job_id = payload.get("job_id")
                        logger.info(f"📧 [Worker] email_indexing received: job_id={job_id}")
                        if not job_id:
                            return jsonify({"error": "missing job_id"}), 400
                        job = await email_job_repo.get_job(job_id)
                        if not job:
                            return jsonify({"error": "job not found"}), 404
                        if job.status != "running":
                            logger.info(f"📧 [Worker] Job {job_id[:8]} is {job.status}, skipping")
                            return jsonify({"status": "skipped", "reason": job.status}), 200
                        creds = await oauth_credentials_port.get_credentials(job.user_id, job.provider)
                        if not creds:
                            await email_job_repo.update_job(job_id, {"status": "failed_auth", "updated_at": __import__("datetime").datetime.utcnow()})
                            return jsonify({"error": "oauth credentials missing"}), 400
                        job = await email_indexing_service.run_indexing_job(
                            job=job,
                            credentials=creds,
                            account_id=job.account_id,
                            max_pages=1,
                            mode=job.mode,
                            backfill_until=job.backfill_until,
                        )
                        if job.next_page_token and agent_task_queue:
                            await agent_task_queue.enqueue_email_indexing_task(job.job_id)
                            logger.info(f"📬 [Worker] Re-enqueued email indexing page for job {job.job_id[:8]}")
                        elif not job.next_page_token:
                            try:
                                await notification_service.notify(
                                    user_id=job.user_id,
                                    account_id=job.account_id,
                                    system_alert=email_indexing_service.completion_alert(job),
                                )
                            except Exception as notify_exc:
                                logger.warning(f"⚠️ [Worker] Notification failed (non-critical): {notify_exc}")
                        return jsonify({"status": "ok", "has_more": bool(job.next_page_token)}), 200
                    elif payload.get("task_type") == "email_indexing_watchdog":
                        # Marks stale "running" jobs as failed. Triggered by Cloud Scheduler.
                        import datetime as _dt
                        stale_threshold = _dt.datetime.utcnow() - _dt.timedelta(hours=2)
                        stale_jobs = await email_job_repo.get_stale_running_jobs(stale_threshold)
                        marked = 0
                        for stale_job in stale_jobs:
                            await email_job_repo.update_job(stale_job.job_id, {
                                "status": "failed",
                                "updated_at": _dt.datetime.utcnow(),
                            })
                            marked += 1
                            logger.warning(f"⏰ [Watchdog] Marked stale job {stale_job.job_id[:8]} as failed")
                        return jsonify({"status": "ok", "marked_failed": marked}), 200
                    elif payload.get("task_type") == "consolidation":
                        user_id = payload.get("user_id")
                        factory = _agent_factory_ref[0]
                        if not user_id or factory is None:
                            return jsonify({"error": "missing user_id or factory not ready"}), 400
                        from src.handlers.consolidation_handler import process_user_batches_on_overflow
                        # Process one batch per HTTP request → each task keeps full CPU on Cloud Run.
                        # Re-enqueue if more batches remain so Cloud Tasks chains them naturally.
                        has_more = await process_user_batches_on_overflow(
                            user_id=user_id,
                            coordinator=coordinator,
                            agent_factory=factory,
                            queue=consolidation_queue,
                            max_batches=1,
                        )
                        if has_more and agent_task_queue:
                            await agent_task_queue.enqueue_consolidation_task(user_id=user_id)
                            logger.info(f"📬 [Worker] Re-enqueued next consolidation task for user {user_id[:8]}")
                        return jsonify({"status": "ok"}), 200
                    return await slack_adapter._handle_worker_task()
                
                logger.info("✅ All blueprints registered on shared app (port 8080)")
                logger.info("   - /slack/events (Slack webhook)")
                logger.info("   - /worker (Cloud Tasks)")
                logger.info("   - /health (healthcheck)")
                logger.info("   - /auth/* (OAuth)")
                logger.info("   - /cabinet, /api/user/* (Cabinet)")
                
                # Override start() to launch shared app instead of individual adapter
                async def start_shared_app():
                    logger.info("🚀 Starting shared Quart app on port 8080...")
                    hypercorn_config = HypercornConfig()
                    hypercorn_config.bind = ["0.0.0.0:8080"]
                    hypercorn_config.use_reloader = False
                    hypercorn_config.accesslog = None
                    hypercorn_config.errorlog = "-"
                    await serve(main_app, hypercorn_config)
                
                # Replace adapter's start with shared app start
                slack_adapter.start = start_shared_app
                
            except Exception as e:
                logger.error(f"❌ Failed to create shared app: {e}", exc_info=True)
                logger.warning("🤖 Bot will continue without web features")
        else:
            logger.warning("⚠️ Socket Mode detected - OAuth + Cabinet not available (HTTP Mode only)")

        logger.info("🚀 Starting Alek Bot...")
        logger.info(f"🚀 Starting Slack Adapter in {slack_adapter.get_mode_name()} (Multi-Tenant)...")

        # Graceful shutdown: handle SIGTERM (Cloud Run) and SIGINT (local Ctrl+C)
        loop = asyncio.get_running_loop()
        shutdown_event = asyncio.Event()
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, shutdown_event.set)

        server_task = asyncio.create_task(slack_adapter.start())
        shutdown_waiter = asyncio.create_task(shutdown_event.wait())

        await asyncio.wait(
            [server_task, shutdown_waiter],
            return_when=asyncio.FIRST_COMPLETED,
        )

        for t in (server_task, shutdown_waiter):
            if not t.done():
                t.cancel()
                try:
                    await t
                except asyncio.CancelledError:
                    pass

        # Drain in-flight background tasks before exit
        logger.info("🛑 Draining background tasks...")
        if session_store._pending_tasks:
            await asyncio.gather(*list(session_store._pending_tasks), return_exceptions=True)
        await agent_factory.shutdown()
        await billing_agent.shutdown()
        await logger_agent.shutdown()
        if html_renderer:
            await html_renderer.stop()
        logger.info("✅ Graceful shutdown complete")

    except Exception as e:
        logger.error(f"❌ Runtime Error: {e}", exc_info=True)
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
