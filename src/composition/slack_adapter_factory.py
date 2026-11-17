"""
Slack Adapter Factory
Creates appropriate Slack adapter based on configuration.

Lives in composition/ so it can legally import from handlers/, infrastructure/,
services/, and adapters/. Builds the ConversationHandler over the shared ChatServices
(`build_conversation_handler`) and injects it as ConversationHandlerPort into the platform adapter.
"""
import os
from typing import Optional
from slack_bolt.async_app import AsyncApp

from ..adapters.slack.base import SlackAdapter
from ..adapters.slack.http_adapter import HTTPModeAdapter
from ..adapters.gcp_task_queue import GcpTaskQueue
from ..adapters.firestore_session_store import FirestoreSessionStore
from .deploy_topology import DeployTopology
from ..services.iam_service import IAMService
from ..services.rich_content_service import RichContentService
from ..ports.language_service_port import LanguageServicePort
from ..services.channel_binding_service import ChannelBindingService
from ..adapters.slack.channel_history import SlackChannelHistorySource
from .chat_services import ChatServices, build_conversation_handler
from ..utils.logger import logger


class SlackAdapterFactory:
    """
    Factory for creating Slack adapters based on configuration.
    Implements Factory pattern for dual-mode support.

    Composition root: builds the ConversationHandler and injects it
    as ConversationHandlerPort into the platform adapter.
    """

    @staticmethod
    def create_adapter(
        app: AsyncApp,
        config: dict,
        deploy_topology: DeployTopology,
        chat: ChatServices,
        iam_service: IAMService,
        session_store: FirestoreSessionStore,
        db_client=None,
        language_service: Optional[LanguageServicePort] = None,
        channel_binding_service: Optional[ChannelBindingService] = None,
        worker_queue=None,
        *,
        rich_content_service: RichContentService,
    ) -> SlackAdapter:
        """
        Create appropriate Slack adapter based on environment configuration.

        Args:
            app: Slack Bolt AsyncApp instance
            config: Configuration dictionary
            deploy_topology: The service URL, queue suffix and regions
            chat: The chat services both platforms' ConversationHandlers share
            iam_service: IAMService for centralized authorization
            session_store: Session store (required for HTTP mode)
            db_client: Firestore AsyncClient (required for HTTP mode)
            channel_binding_service: Slack only (P9-D9): `$agent` / `$primary` and bound channel sessions
            rich_content_service: The platform's RichContentService (built once by the application graph, shared with the notification service)
            worker_queue: The main task queue; a button press on an interactive message is queued on it as a `/worker` task

        Returns:
            HTTPModeAdapter instance
        """
        logger.info("🏭 Creating Slack adapter: http")

        # Channel history source for bound channel sessions (Slack API)
        channel_history_source = SlackChannelHistorySource(
            slack_client=app.client,
            bot_user_id=os.getenv("SLACK_BOT_USER_ID", ""),
        )

        conversation_handler = build_conversation_handler(
            chat, rich_content_service,
            channel_binding=channel_binding_service,
            channel_history=channel_history_source,
        )

        if not db_client:
            raise ValueError("db_client is required for HTTP mode (session persistence)")

        task_service = GcpTaskQueue(
            project_id=config["GOOGLE_CLOUD_PROJECT"],
            location=deploy_topology.slack_intake_region,
            queue_name=f"alek-bot-tasks-{deploy_topology.queue_suffix}",
            service_url=deploy_topology.service_url,
            service_account_email=config.get("SERVICE_ACCOUNT_EMAIL")
        )

        return HTTPModeAdapter(
            app=app,
            config=config,
            task_service=task_service,
            session_store=session_store,
            conversation_handler=conversation_handler,
            iam_service=iam_service,
            audio_service=chat.audio_service,
            language_service=language_service,
            localization=chat.localization,
            worker_queue=worker_queue,
        )
