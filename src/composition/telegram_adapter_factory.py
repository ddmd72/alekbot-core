"""
Telegram Adapter Factory

Composition root for the Telegram integration.
Mirrors the pattern of SlackAdapterFactory: creates ConversationHandler with
RichContentService fully wired, then injects it into TelegramWebhookAdapter
as ConversationHandlerPort.
"""
from typing import Optional
from telegram import Bot

from ..adapters.telegram.webhook_adapter import TelegramWebhookAdapter
from ..adapters.telegram.media_adapter import TelegramMediaAdapter
from ..handlers.conversation_handler import ConversationHandler
from ..handlers.consolidation_handler import process_user_batches_on_overflow
from ..infrastructure.agent_coordinator import AgentCoordinator
from .user_agent_factory import UserAgentFactory
from ..services.rich_content_service import RichContentService
from ..services.user_notification_service import UserNotificationService
from ..ports.html_renderer_port import HtmlRendererPort
from ..ports.conversation_handler_port import ConversationHandlerPort
from ..ports.platform_auth_port import PlatformAuthPort
from ..ports.file_service import FileService
from ..ports.language_service_port import LanguageServicePort
from ..ports.localization_port import LocalizationPort
from ..utils.logger import logger


class TelegramAdapterFactory:
    """
    Factory for creating TelegramWebhookAdapter with full dependency wiring.

    Injects RichContentService (with html_renderer) into ConversationHandler
    so widget rich content is rendered to PNG and sent as Telegram photos.
    """

    @staticmethod
    def create_adapter(
        token: str,
        webhook_secret: str,
        dedup_store,
        session_store,
        coordinator: AgentCoordinator,
        agent_factory: UserAgentFactory,
        iam_service: PlatformAuthPort,
        file_service: FileService,
        consolidation_queue=None,
        consolidation_config=None,
        audio_service=None,
        html_renderer: Optional[HtmlRendererPort] = None,
        notification_service: Optional[UserNotificationService] = None,
        indexed_email_repo=None,
        user_repo=None,
        language_service: Optional[LanguageServicePort] = None,
        localization: Optional[LocalizationPort] = None,
    ) -> TelegramWebhookAdapter:
        """
        Create TelegramWebhookAdapter with RichContentService wired in.

        Args:
            token:               Telegram bot token
            webhook_secret:      HMAC secret for webhook verification
            dedup_store:         Deduplication store for update_id tracking
            session_store:       Session store for resolving user sessions
            coordinator:         AgentCoordinator instance
            agent_factory:       Factory for per-user agents
            iam_service:         PlatformAuthPort for authorization
            file_service:        FileService for attachment handling
            consolidation_queue: Optional consolidation queue
            consolidation_config: Optional consolidation config
            audio_service:       Optional audio transcription port
            html_renderer:       Optional HtmlRendererPort for widget → PNG

        Returns:
            TelegramWebhookAdapter ready to handle webhook requests
        """
        # Bot instance for media delivery (TelegramMediaAdapter).
        # TelegramWebhookAdapter creates its own Bot from the same token — that is fine,
        # python-telegram-bot Bot objects are stateless HTTP clients.
        bot = Bot(token)

        media_adapter = TelegramMediaAdapter(bot=bot)
        rich_content_service = RichContentService(
            media_port=media_adapter,
            html_renderer=html_renderer,
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
            indexed_email_repo=indexed_email_repo,
            user_repo=user_repo,
            overflow_callback=process_user_batches_on_overflow,
            localization=localization,
        )

        logger.info("TelegramAdapterFactory: wiring complete (html_renderer=%s)", html_renderer is not None)

        return TelegramWebhookAdapter(
            token=token,
            webhook_secret=webhook_secret,
            dedup_store=dedup_store,
            session_store=session_store,
            conversation_handler=conversation_handler,
            iam_service=iam_service,
            audio_service=audio_service,
            language_service=language_service,
            localization=localization,
        )
