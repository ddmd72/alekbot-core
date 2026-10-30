import sys
import os
import signal
import asyncio

from src.config.settings import load_settings
from src.web.worker_oidc_verifier import build_oidc_gate, parse_sa_emails
from src.web.twilio_signature_verifier import verify_twilio_signature
from src.web.voice_control_plane_app import create_voice_control_plane_blueprint
from src.web.voice_webhook_app import create_voice_webhook_blueprint
from src.web.mcp_error_logger import McpErrorLogger
from src.adapters.firestore_ephemeral_store import FirestoreEphemeralStore
from src.composition.application_graph import build_application_graph
from src.utils.logger import logger, quiet_third_party_loggers
from src.utils.telemetry import init_telemetry
from src.composition.memory_telemetry_setup import build_memory_telemetry
from src.domain.memory_telemetry import SCOPE_WORKER_TASK
from src.utils.memory_scope import memory_scope
from src.domain.cloud_task_delivery import CloudTaskDelivery
from src.web.oauth_app import create_oauth_blueprint
from src.web.user_cabinet_app import create_user_cabinet_blueprint
from src.web.deep_research_webhooks import create_deep_research_webhooks_blueprint
from src.web.microsoft_tasks_webhook import create_microsoft_tasks_webhook_blueprint
from src.web.mcp_consent_app import build_mcp_consent_blueprint
from src.composition.mcp_setup import build_mcp_components


async def main():
    """Main function to start the bot with clean dependency injection."""

    print("=" * 80)
    print("🚀 ALEK BOT STARTING...")
    print("=" * 80)

    init_telemetry("alek-core")

    memory_telemetry = build_memory_telemetry("alek-core")
    if memory_telemetry:
        await memory_telemetry.start()

    # Set external libraries to WARNING to reduce noise, but keep our logs at DEBUG
    quiet_third_party_loggers()

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
        database_id = env_config.firestore_database_id
        
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
        logger.info("🔥 Warming up Firestore connection...")
        try:
            # 1. Trigger OAuth and connection initialization
            test_col = db_client.collection("_warmup")
            await test_col.limit(1).get()
            logger.info("✅ Firestore connection initialized")
        except Exception as e:
            logger.warning(f"⚠️ Firestore Warmup failed (non-critical): {e}")

        # NOTE: Embedding initialization moved to UserAgentFactory.ensure_agents_for_user()
        # where repository is created with proper BiographicalContextService injection

        app = build_application_graph(config, db_client)
        await app.start()

        # Create blueprints (will be registered on slack_adapter.quart_app)
        oauth_bp = create_oauth_blueprint(
            auth_service=app.auth_service,
            session_service=app.session_service,
            auth_registry=app.auth_registry,
            auth_config=app.auth_config,
            invite_service=app.invite_service,
            gmail_oauth_service=app.gmail_oauth_service,
            oauth_credentials_port=app.container.oauth_credentials,
            google_tasks_oauth_service=app.google_tasks_oauth_service,
            ms_todo_client_id=config.get("MICROSOFT_TODO_CLIENT_ID", ""),
            ms_todo_client_secret=config.get("MICROSOFT_TODO_CLIENT_SECRET", ""),
            ms_todo_redirect_uri=config.get("MICROSOFT_TODO_REDIRECT_URI", ""),
            onedrive_redirect_uri=config.get("ONEDRIVE_REDIRECT_URI", ""),
            task_queue=app.agent_task_queue,
            reconnect=app.reconnect_service,
            connectors=app.container.connectors,
        )

        cabinet_bp = create_user_cabinet_blueprint(
            invite_service=app.invite_service,
            session_service=app.session_service,
            user_repo=app.user_repo,
            fact_repo=app.container.repository,
            embedding_service=app.container.embedding_service,
            oauth_credentials_port=app.container.oauth_credentials,
            gmail_oauth_service=app.gmail_oauth_service,
            google_tasks_oauth_service=app.google_tasks_oauth_service,
            indexed_email_repo=app.container.indexed_email_repo,
            email_indexing_service=app.container.email_indexing_service,
            email_job_repo=app.container.email_job_repo,
            task_queue=app.agent_task_queue,
            task_setup=app.task_setup_service,
            tasks_provider=app.container.ms_todo_adapter,
            language_service=app.language_service,
            agent_note_port=app.container.notes_adapter,
            recurrence_port=app.container.recurrence_adapter,
            twilio_verify_client=app.twilio_verify_client,
            user_drive=app.container.user_drive,
            drive_search_service=app.container.drive_search_service,
            drive_task_queue=app.container.drive_task_queue,
            drive_transcribe_switch_on=app.container.drive_transcribe_switch_on,
            cabinet_facts=app.cabinet_facts,
            fact_review_store=app.container.fact_review_store,
            fact_answer_service=app.fact_answers,
            localization=app.localization,
            reconnect=app.reconnect_service,
            connectors=app.container.connectors,
        )

        deep_research_webhooks_bp = create_deep_research_webhooks_blueprint(
            webhook_secret=config.get("OPENAI_DEEP_RESEARCH_WEBHOOK_SECRET"),
            task_queue=app.task_dispatch,
            fetch_response=app.deep_research_fetch,
            dedup_store=app.deep_research_dedup_store,
        )

        ms_tasks_webhook_bp = create_microsoft_tasks_webhook_blueprint(
            task_indexing=app.container.task_indexing,
            task_setup=app.task_setup_service,
            webhook_secret=config.get("MICROSOFT_TASKS_WEBHOOK_SECRET"),
        )
        logger.info("✅ OAuth + Cabinet services initialized")

    except Exception as e:
        logger.error(f"❌ Infrastructure Initialization Error: {e}", exc_info=True)
        sys.exit(1)

    try:
        slack_adapter = app.slack_adapter
        telegram_adapter = app.telegram_adapter
        worker_handler = app.worker_handler

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
                main_app.register_blueprint(deep_research_webhooks_bp)
                main_app.register_blueprint(ms_tasks_webhook_bp)

                # /f/<token> capability route for private file access
                if app.media_storage:
                    from src.web.file_access_app import create_file_access_blueprint
                    main_app.register_blueprint(
                        create_file_access_blueprint(
                            token_service=app.file_token_service,
                            media_storage=app.media_storage,
                            session_service=app.session_service,
                        )
                    )
                    logger.info("✅ File access blueprint registered at /f/<token>")

                # /s/<code> short-link redirect, wraps /f/<token> for shorter chat links
                if app.short_link_service:
                    from src.web.short_link_app import create_short_link_blueprint
                    main_app.register_blueprint(
                        create_short_link_blueprint(short_links=app.short_link_service)
                    )
                    logger.info("✅ Short link blueprint registered at /s/<code>")

                # /voice/session-config, /voice/submit-transcript — main-service
                # control-plane endpoints consumed by the relay's
                # CallControlPlanePort (Task 6). OIDC-protected the same way
                # /worker is: verify_worker_oidc + the SERVICE_ACCOUNT_EMAIL
                # local-dev bypass, wrapped as an async callable so the route
                # stays testable without a real Google token.
                # Collection name resolves through EnvironmentConfig like every
                # other Firestore collection (env prefix, REQ multi-tenant
                # convention) — it was hardcoded and unprefixed until now.
                voice_ephemeral_store = FirestoreEphemeralStore(
                    db_client, collection=env_config.voice_tickets_collection
                )

                # Accepted: the invoker identity, the relay's runtime SA (it posts here with its own
                # metadata-server token) and the optional transition list (IAM migration). Empty set:
                # closed on Cloud Run, open locally.
                _voice_oidc_verifier = build_oidc_gate(
                    parse_sa_emails(
                        config.get("SERVICE_ACCOUNT_EMAIL"),
                        config.get("VOICE_RELAY_SA_EMAIL"),
                        os.getenv("OIDC_TRANSITION_SA_EMAILS"),
                    ),
                    on_cloud_run=bool(os.getenv("K_SERVICE")),
                    label="Voice control plane",
                )

                async def _voice_twilio_signature_verifier(
                    url: str, form_params: dict, signature: str
                ) -> bool:
                    # Same bypass shape as _voice_oidc_verifier above and as
                    # /worker's SERVICE_ACCOUNT_EMAIL gate: with no auth token
                    # configured (local dev) there is nothing to verify against,
                    # so the gate opens. In Cloud Run TWILIO_AUTH_TOKEN is always
                    # present (cloudbuild-dev.yaml --set-secrets).
                    auth_token = config.get("TWILIO_AUTH_TOKEN")
                    if not auth_token:
                        return True
                    return verify_twilio_signature(auth_token, url, form_params, signature)

                async def _voice_summary_consumer(*, call_id, user_id, account_id, transcript_text, turns):
                    # LelikSummarizerAgent-backed consumer (Task 15). Reuses
                    # companion_extractor_runner (built by build_application_graph,
                    # "Initializing Companion Extraction pipeline") the same way tutor batches do,
                    # just with companion_type="voice" — turns are already
                    # {"request_text", "response_text", ...} dicts (see
                    # HttpCallControlPlaneAdapter.submit_transcript), the same
                    # List[dict] shape CompanionExtractorRunner.extract() expects,
                    # so no reshaping is needed before handing them over.
                    from src.domain.secret_digest import fingerprint
                    extraction = await app.companion_extractor_runner.extract(
                        companion_type="voice",
                        account_id=account_id,
                        created_by_user_id=user_id,
                        messages=turns,
                    )
                    summary = extraction.get("summary") or ""
                    if not summary:
                        logger.warning(f"voice call {fingerprint(call_id)}: summarizer returned an empty summary, nothing to deliver")
                        return
                    from src.domain.voice_call_note import call_event_from_turns
                    from src.domain.voice_ticket_keys import call_kind_key
                    profile = await app.user_repo.get_user(user_id)
                    try:
                        kind_record = await voice_ephemeral_store.get(call_kind_key(app.digest_key, call_id))
                        call_kind = (kind_record or {}).get("call_kind", "phone")
                    except Exception:
                        # The summary is the last stop of the call pipeline — a
                        # lookup failure here must not lose it. Fall back to the
                        # default header and keep delivering.
                        logger.warning(f"voice call {fingerprint(call_id)}: failed to read call kind, defaulting to phone", exc_info=True)
                        call_kind = "phone"
                    await app.notification_service.notify_call_summary(
                        user_id, account_id, summary,
                        call_event=call_event_from_turns(
                            turns, profile.config.timezone if profile else "UTC", call_kind=call_kind
                        ),
                    )

                async def _voice_late_answer_sink(*, user_id, account_id, request, output, failed=False):
                    # An answer the relay stopped waiting for (300 s timeout, or the call ended):
                    # Lelik told the caller it would come to chat. The label is "intent: query".
                    # A failed delegation posts a short localized line, never its error text.
                    from src.domain.voice_call_note import late_answer_text
                    from src.domain.ui_messages import UIMessage
                    from src.domain.language import LanguageCode
                    if failed:
                        try:
                            lang = await app.language_service.resolve_ui_language(user_id)
                        except Exception:
                            # The line still goes out, in English, rather than being lost.
                            logger.warning(f"voice late answer: UI language lookup failed for {user_id}", exc_info=True)
                            lang = LanguageCode.EN
                        output = app.localization.get_ui_string(lang, UIMessage.VOICE_REQUEST_FAILED)
                    await app.notification_service.notify_raw(user_id, account_id, late_answer_text(request, output))

                from urllib.parse import urlsplit
                _relay_parts = urlsplit(config.get("VOICE_RELAY_STREAM_URL", ""))
                _relay_http_url = (
                    f"{'https' if _relay_parts.scheme == 'wss' else 'http'}://{_relay_parts.netloc}/"
                    if _relay_parts.netloc else ""
                )

                async def _relay_warmup() -> int:
                    # A plain GET with no body: the relay answers it with 200 (relay_main.process_request).
                    import httpx
                    async with httpx.AsyncClient(timeout=120.0) as client:
                        response = await client.get(_relay_http_url)
                    return response.status_code

                main_app.register_blueprint(
                    create_voice_control_plane_blueprint(
                        ephemeral_store=voice_ephemeral_store,
                        quota_service=app.quota_service,
                        prompt_content_store=app.container.prompt_content_store,
                        summary_consumer=_voice_summary_consumer,
                        oidc_verifier=_voice_oidc_verifier,
                        # A summary-pipeline failure is otherwise completely
                        # silent: the call completes, usage is billed, and
                        # nothing reaches chat or memory.
                        alert_sink=app.alert_sink,
                        lelik_agent_provider=app.agent_factory.get_lelik,
                        late_answer_sink=_voice_late_answer_sink,
                        relay_warmup=_relay_warmup if _relay_http_url else None,
                        digest_key=app.digest_key,
                    )
                )
                logger.info(
                    "✅ Voice control-plane blueprint registered at "
                    "/voice/session-config, /voice/submit-transcript, /voice/delegate, "
                    "/voice/delegate/abandon"
                )

                # /voice/auth, /voice/answer — Twilio's own webhooks (Task 8/9),
                # never registered until now (Task 10b: found as a real gap during
                # Task 10's review). Reuses voice_ephemeral_store minted just above
                # (same ticket/one-call-marker store the control plane reads/releases,
                # not a second FirestoreEphemeralStore instance) plus other
                # already-constructed instances: user_repo, the alert sink,
                # notification_service, agent_factory.

                main_app.register_blueprint(
                    create_voice_webhook_blueprint(
                        user_repository=app.user_repo,
                        ephemeral_store=voice_ephemeral_store,
                        alert_sink=app.alert_sink,
                        notification_service=app.notification_service,
                        lelik_agent_provider=app.agent_factory.get_lelik,
                        answer_url=f"{config.get('CLOUD_RUN_SERVICE_URL') or 'http://localhost:8080'}/voice/answer",
                        signature_verifier=_voice_twilio_signature_verifier,
                        relay_stream_url=config.get("VOICE_RELAY_STREAM_URL", ""),
                        cipher=app.cipher,
                        digest_key=app.digest_key,
                    )
                )
                logger.info(
                    "✅ Voice webhook blueprint registered at "
                    "/voice/auth, /voice/inbound-status, /voice/answer, /voice/status"
                )

                # Cabinet web call (VOICE_WEB_TRANSPORT_RFC §5.2) — browser-to-Lelik calls over
                # Cloudflare's Calls SFU, the second call kind next to the Twilio phone path
                # above. MVP/dev-only like the rest of Voice Companion: skipped (not a boot
                # failure) when either the Cloudflare app credentials or the relay's own
                # wss:// URL isn't configured, so the app boots exactly as before this feature
                # existed. `app.alert_sink` is passed through unguarded, the same risk the
                # Twilio webhook blueprint just above already carries: with no alert webhook
                # configured it is None, and a failed `VoiceCallSetupService.prepare` releases
                # the ticket and marker first, then crashes on `None.post` — the request ends in
                # a 500 instead of its handled error response, but nothing stays held.
                if config.get("CLOUDFLARE_SFU_APP_ID") and config.get("CLOUDFLARE_SFU_APP_SECRET") \
                        and config.get("VOICE_RELAY_STREAM_URL"):
                    from urllib.parse import urlsplit
                    from src.adapters.cloudflare_sfu_adapter import CloudflareSfuAdapter
                    from src.services.voice_call_setup_service import VoiceCallSetupService
                    from src.web.voice_web_call_app import create_voice_web_call_blueprint

                    relay_parts = urlsplit(config.get("VOICE_RELAY_STREAM_URL", ""))
                    main_app.register_blueprint(create_voice_web_call_blueprint(
                        session_service=app.session_service,
                        call_setup=VoiceCallSetupService(
                            voice_ephemeral_store, app.alert_sink, app.agent_factory.get_lelik,
                            digest_key=app.digest_key,
                        ),
                        media_room=CloudflareSfuAdapter(config["CLOUDFLARE_SFU_APP_ID"], config["CLOUDFLARE_SFU_APP_SECRET"]),
                        ephemeral_store=voice_ephemeral_store,
                        relay_base_url=f"{relay_parts.scheme}://{relay_parts.netloc}",
                        cipher=app.cipher,
                        digest_key=app.digest_key,
                    ))
                    logger.info(
                        "✅ Voice web-call blueprint registered at /cabinet/call, /api/voice/web-call"
                    )
                else:
                    logger.warning(
                        "⚠️ Voice web-call blueprint skipped — CLOUDFLARE_SFU_APP_ID/"
                        "CLOUDFLARE_SFU_APP_SECRET/VOICE_RELAY_STREAM_URL not fully configured"
                    )

                # Telegram adapter (built by the application graph when configured): its blueprint goes on the shared app.
                if telegram_adapter is not None:
                    try:
                        telegram_bp = telegram_adapter.get_blueprint()
                        main_app.register_blueprint(telegram_bp, url_prefix="/telegram")
                        logger.info("✅ Telegram adapter registered at /telegram/webhook")
                    except Exception as e:
                        logger.error(f"❌ Failed to initialize Telegram adapter: {e}", exc_info=True)
                        logger.warning("🤖 Bot will continue without Telegram support")
                
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
                
                # Public landing at `/` (signed-in owner → /cabinet), /og.png, /robots.txt
                from src.web.landing_app import create_landing_blueprint
                main_app.register_blueprint(create_landing_blueprint(app.session_service))

                # Add health endpoint
                @main_app.route("/health", methods=["GET"])
                async def health():
                    from quart import jsonify
                    return jsonify({"status": "healthy", "mode": "http"}), 200

                # Real browsers + link-preview bots request these by default; serving them
                # stops the recurring 404 noise (prod log audit, P2 hygiene batch).
                @main_app.route("/favicon.ico", methods=["GET"])
                async def favicon():
                    from quart import send_file
                    static_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "src", "web", "static")
                    return await send_file(os.path.join(static_dir, "alek-logo.png"), mimetype="image/png")

                # Accepted /worker identities, computed once: SERVICE_ACCOUNT_EMAIL plus the optional
                # transition list. Empty set: every call refused on Cloud Run (ERROR logged once, here),
                # open locally (manual curl triggers).
                _worker_oidc_gate = build_oidc_gate(
                    parse_sa_emails(config.get("SERVICE_ACCOUNT_EMAIL"), os.getenv("OIDC_TRANSITION_SA_EMAILS")),
                    on_cloud_run=bool(os.getenv("K_SERVICE")),
                    label="Worker",
                )

                # Add /worker endpoint — delegates to WorkerHandler
                @main_app.route("/worker", methods=["POST"])
                async def worker():
                    from quart import request, jsonify
                    # OIDC gate. Cloud Tasks / Cloud Scheduler attach an OIDC
                    # token signed by an accepted identity (_worker_oidc_gate);
                    # no identity configured: closed on Cloud Run, open locally,
                    # keeping manual curl triggers working (CLAUDE.md "Manual
                    # Triggers"). Identity-only check — audience is not pinned
                    # (callers use inconsistent audiences); see verifier docstring.
                    if not await _worker_oidc_gate(request.headers.get("Authorization")):
                        logger.warning("Rejected unauthenticated /worker request")
                        return jsonify({"error": "unauthorized"}), 401
                    payload = await request.get_json(silent=True) or {}
                    delivery = CloudTaskDelivery.from_headers(request.headers)
                    if delivery is not None:
                        logger.info(
                            "📥 /worker delivery: task_type=%s task=%s retry_count=%s execution_count=%s",
                            payload.get("task_type"), delivery.task_name,
                            delivery.retry_count, delivery.execution_count,
                        )
                    with memory_scope(SCOPE_WORKER_TASK, str(payload.get("task_type") or "slack_event")):
                        body, status = await worker_handler.handle(
                            payload, delivery=delivery, headers=dict(request.headers),
                        )
                        return jsonify(body), status

                # ====================================================================
                # Remote MCP connector for claude.ai Custom Connectors
                # AS+RS in-process via the `mcp` SDK's FastMCP.
                # Mounted under /mcp in a Starlette parent alongside main_app.
                # ====================================================================
                mcp_components = None
                try:
                    # SearchEnrichmentService isn't a container singleton —
                    # UserAgentFactory builds one per user with tier-specific
                    # total_limit. For MCP we need a shared instance: default
                    # SearchConfig limits, sensible total_limit=30. Same
                    # repository + embedding_service as everyone else.
                    from src.services.search_enrichment_service import (
                        SearchEnrichmentService,
                    )
                    from src.domain.settings import SearchConfig
                    _mcp_search_config = SearchConfig()
                    mcp_search_enrichment = SearchEnrichmentService(
                        repository=app.container.repository,
                        embedding_service=app.container.embedding_service,
                        keyword_limit=_mcp_search_config.DEFAULT_KEYWORD_LIMIT,
                        phrase_one_limit=_mcp_search_config.DEFAULT_PHRASE_ONE_LIMIT,
                        phrase_two_limit=_mcp_search_config.DEFAULT_PHRASE_TWO_LIMIT,
                        total_limit=30,
                    )
                    mcp_components = build_mcp_components(
                        db_client=db_client,
                        env_config=env_config,
                        auth_config=app.auth_config,
                        search_enrichment_service=mcp_search_enrichment,
                        cipher=app.cipher,
                        digest_key=app.digest_key,
                    )
                    # Consent blueprint is built in main.py (not in
                    # composition/) because composition must not depend
                    # on the web layer. We pass the authorization service
                    # built by build_mcp_components into the factory.
                    mcp_consent_bp = build_mcp_consent_blueprint(
                        mcp_service=mcp_components.authorization_service,
                        session_service=app.session_service,
                    )
                    main_app.register_blueprint(mcp_consent_bp)
                    logger.info("✅ MCP connector initialized (/mcp + /mcp/consent)")
                except Exception as mcp_err:
                    logger.error(
                        f"❌ Failed to initialize MCP connector: {mcp_err}",
                        exc_info=True,
                    )
                    logger.warning("🤖 Bot will continue without MCP connector")

                logger.info("✅ All blueprints registered on shared app (port 8080)")
                logger.info("   - /slack/events (Slack webhook)")
                logger.info("   - /worker (Cloud Tasks)")
                logger.info("   - /health (healthcheck)")
                logger.info("   - /auth/* (OAuth)")
                logger.info("   - /cabinet, /api/user/* (Cabinet)")
                if mcp_components is not None:
                    logger.info("   - /mcp (remote MCP for claude.ai)")
                    logger.info("   - /mcp/consent (OAuth consent UI)")

                # Override start() to launch shared app instead of individual adapter
                async def start_shared_app():
                    logger.info("🚀 Starting shared app on port 8080...")
                    hypercorn_config = HypercornConfig()
                    hypercorn_config.bind = ["0.0.0.0:8080"]
                    hypercorn_config.use_reloader = False
                    hypercorn_config.accesslog = None
                    hypercorn_config.errorlog = "-"

                    if mcp_components is not None:
                        # Plain ASGI dispatcher routing by path.
                        #
                        # Starlette's Mount("/mcp", ...) strips the prefix and
                        # requires a trailing slash — it does NOT match exact
                        # "/mcp" for POST, which is what claude.ai sends. It
                        # also conflicts with the SDK's absolute-path routes
                        # (SDK builds /authorize, /token, /register and PRM at
                        # server root, not under a mount prefix).
                        #
                        # So we forward the FastMCP-owned paths to the SDK
                        # sub-app WITHOUT path rewriting, and everything else
                        # falls through to the Quart app. Quart handles
                        # /mcp/consent (the consent UI blueprint), /auth/*,
                        # /api/*, /slack/*, /worker, /health, /cabinet.
                        _fastmcp = mcp_components.fastmcp
                        _mcp_asgi = McpErrorLogger(_fastmcp.streamable_http_app())

                        def _is_mcp_path(path: str) -> bool:
                            if path == "/mcp" or path == "/mcp/":
                                return True
                            if path in ("/authorize", "/token", "/register", "/revoke"):
                                return True
                            if path == "/.well-known/oauth-authorization-server":
                                return True
                            if path.startswith("/.well-known/oauth-protected-resource"):
                                return True
                            return False

                        async def parent_asgi(scope, receive, send):
                            if scope["type"] == "lifespan":
                                async with _fastmcp.session_manager.run():
                                    while True:
                                        message = await receive()
                                        if message["type"] == "lifespan.startup":
                                            await send({"type": "lifespan.startup.complete"})
                                        elif message["type"] == "lifespan.shutdown":
                                            await send({"type": "lifespan.shutdown.complete"})
                                            return
                            path = scope.get("path", "")
                            if _is_mcp_path(path):
                                await _mcp_asgi(scope, receive, send)
                            else:
                                await main_app(scope, receive, send)

                        await serve(parent_asgi, hypercorn_config)
                    else:
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
        await app.shutdown()
        if memory_telemetry:
            await memory_telemetry.stop()
        logger.info("✅ Graceful shutdown complete")

    except Exception as e:
        logger.error(f"❌ Runtime Error: {e}", exc_info=True)
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
