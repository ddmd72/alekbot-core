"""
Slack Adapter Factory
Creates appropriate Slack adapter based on configuration.

Lives in composition/ so it can legally import from handlers/, infrastructure/,
services/, and adapters/. Creates ConversationHandler here and injects it as
ConversationHandlerPort into the platform adapter.
"""
import os
from typing import Optional
from slack_bolt.async_app import AsyncApp

from ..adapters.slack.base import SlackAdapter
from ..adapters.slack.http_adapter import HTTPModeAdapter
from ..adapters.slack.media_adapter import SlackMediaAdapter
from ..adapters.gcs_media_adapter import GcsMediaAdapter
from ..adapters.gcp_task_queue import GcpTaskQueue
from ..adapters.firestore_session_store import FirestoreSessionStore
from ..config.environment import EnvironmentConfig
from ..handlers.conversation_handler import ConversationHandler
from ..infrastructure.agent_coordinator import AgentCoordinator
from .user_agent_factory import UserAgentFactory
from ..services.consolidation_service import ConsolidationService
from ..services.iam_service import IAMService
from ..services.rich_content_service import RichContentService
from ..services.user_notification_service import UserNotificationService
from ..ports.file_service import FileService
from ..ports.html_renderer_port import HtmlRendererPort
from ..ports.language_service_port import LanguageServicePort
from ..services.localization_service import LocalizationService
from ..services.channel_binding_service import ChannelBindingService
from ..adapters.slack.channel_history import SlackChannelHistorySource
from ..utils.logger import logger


class SlackAdapterFactory:
    """
    Factory for creating Slack adapters based on configuration.
    Implements Factory pattern for dual-mode support.

    Composition root: creates ConversationHandler and injects it
    as ConversationHandlerPort into the platform adapter.
    """

    @staticmethod
    def create_adapter(
        app: AsyncApp,
        config: dict,
        env_config: EnvironmentConfig,
        coordinator: AgentCoordinator,
        agent_factory: UserAgentFactory,
        iam_service: IAMService,
        file_service: FileService,
        session_store: FirestoreSessionStore,
        db_client=None,
        consolidation_queue=None,
        consolidation_config=None,
        consolidation_service: Optional[ConsolidationService] = None,
        audio_service=None,
        html_renderer: Optional[HtmlRendererPort] = None,
        notification_service: Optional[UserNotificationService] = None,
        language_service: Optional[LanguageServicePort] = None,
        localization: Optional[LocalizationService] = None,
        file_conversion_service=None,
        channel_binding_service: Optional[ChannelBindingService] = None,
        fallback_service=None,
        short_link_service=None,
        skill_service=None,
        memory_fact_service=None,
        fact_answers=None,
        drive_note_service=None,
        long_turn_service=None,
        doc_delivery_service=None,
        worker_queue=None,
        rich_content_service: Optional[RichContentService] = None,
    ) -> SlackAdapter:
        """
        Create appropriate Slack adapter based on environment configuration.

        Args:
            app: Slack Bolt AsyncApp instance
            config: Configuration dictionary
            env_config: EnvironmentConfig instance
            coordinator: AgentCoordinator instance
            agent_factory: Factory for creating per-user agents
            iam_service: IAMService for centralized authorization
            file_service: FileService instance
            session_store: Session store (required for HTTP mode)
            db_client: Firestore AsyncClient (required for HTTP mode)
            consolidation_queue: Optional consolidation queue
            consolidation_config: Optional consolidation config
            consolidation_service: Optional shared ConsolidationService; its consolidate_now serves `$consolidate`
            audio_service: Optional audio transcription service
            worker_queue: The main task queue; a button press on an interactive message is queued on it as a `/worker` task

        Returns:
            HTTPModeAdapter instance
        """
        logger.info("🏭 Creating Slack adapter: http")

        # Create ConversationHandler here (composition root)
        if rich_content_service is None:   # the application graph passes the one it also registers on the notification service
            gcs_bucket = config.get("GCS_MEDIA_BUCKET", "")
            rich_content_service = RichContentService(
                media_port=SlackMediaAdapter(app_client=app.client, bot_token=config.get("SLACK_BOT_TOKEN", "")),
                storage_port=GcsMediaAdapter(bucket_name=gcs_bucket) if gcs_bucket else None,
                html_renderer=html_renderer,
            )

        # Channel history source for bound channel sessions (Slack API)
        channel_history_source = SlackChannelHistorySource(
            slack_client=app.client,
            bot_user_id=os.getenv("SLACK_BOT_USER_ID", ""),
        )

        conversation_handler = ConversationHandler(
            coordinator=coordinator,
            agent_factory=agent_factory,
            file_service=file_service,
            consolidation_queue=consolidation_queue,
            global_config=consolidation_config,
            audio_service=audio_service,
            rich_content_service=rich_content_service,
            notification_service=notification_service,
            overflow_callback=consolidation_service.consolidate_now if consolidation_service else None,
            localization=localization,
            file_conversion_service=file_conversion_service,
            channel_binding_service=channel_binding_service,
            channel_history_source=channel_history_source,
            fallback_service=fallback_service,
            short_link_service=short_link_service,
            skill_service=skill_service,
            memory_fact_service=memory_fact_service,
            fact_answers=fact_answers,
            drive_note_service=drive_note_service,   # `$drive-notes` (DRIVE_FILE_NOTES_RFC §4.8)
            long_turn_service=long_turn_service,
            doc_delivery_service=doc_delivery_service,
        )

        if not db_client:
            raise ValueError("db_client is required for HTTP mode (session persistence)")

        queue_suffix = "dev" if env_config.is_development else "prod"

        service_url = config.get("CLOUD_RUN_SERVICE_URL")
        if not service_url:
            logger.warning("⚠️ CLOUD_RUN_SERVICE_URL not set, defaulting to http://localhost:8080")
            service_url = "http://localhost:8080"

        task_service = GcpTaskQueue(
            project_id=config["GOOGLE_CLOUD_PROJECT"],
            location="europe-west1",
            queue_name=f"alek-bot-tasks-{queue_suffix}",
            service_url=service_url,
            service_account_email=config.get("SERVICE_ACCOUNT_EMAIL")
        )

        return HTTPModeAdapter(
            app=app,
            config=config,
            task_service=task_service,
            session_store=session_store,
            conversation_handler=conversation_handler,
            iam_service=iam_service,
            audio_service=audio_service,
            language_service=language_service,
            localization=localization,
            worker_queue=worker_queue,
        )
