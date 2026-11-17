"""
Telegram Adapter Factory

Composition root for the Telegram integration.
Mirrors the pattern of SlackAdapterFactory: builds the ConversationHandler over the
shared ChatServices and the platform's RichContentService, then injects it into
TelegramWebhookAdapter as ConversationHandlerPort.
"""
from typing import Optional

from ..adapters.telegram.webhook_adapter import TelegramWebhookAdapter
from ..services.rich_content_service import RichContentService
from ..ports.platform_auth_port import PlatformAuthPort
from ..ports.language_service_port import LanguageServicePort
from ..utils.logger import logger
from .chat_services import ChatServices, build_conversation_handler


class TelegramAdapterFactory:
    """
    Factory for creating TelegramWebhookAdapter with full dependency wiring.

    Injects the RichContentService built by the application graph into ConversationHandler
    (the same instance the notification service uses).
    """

    @staticmethod
    def create_adapter(
        token: str,
        webhook_secret: str,
        dedup_store,
        session_store,
        chat: ChatServices,
        iam_service: PlatformAuthPort,
        language_service: Optional[LanguageServicePort] = None,
        task_queue=None,
        *,
        rich_content_service: RichContentService,
    ) -> TelegramWebhookAdapter:
        """
        Create TelegramWebhookAdapter around the given RichContentService.

        Args:
            token:               Telegram bot token
            webhook_secret:      HMAC secret for webhook verification
            dedup_store:         Deduplication store for update_id tracking
            session_store:       Session store for resolving user sessions
            chat:                The chat services both platforms' ConversationHandlers share; Telegram has no
                                 channel binding (P9-D9) and no history source (the Bot API has none)
            iam_service:         PlatformAuthPort for authorization
            rich_content_service: The platform's RichContentService (built once by the application graph, shared with the notification service)
            task_queue:          Optional TaskQueue — set, updates are handed off to a
                                 Cloud Task instead of processed inline (see
                                 TelegramWebhookAdapter.__init__)

        Returns:
            TelegramWebhookAdapter ready to handle webhook requests
        """
        conversation_handler = build_conversation_handler(chat, rich_content_service)

        logger.info("TelegramAdapterFactory: wiring complete")

        return TelegramWebhookAdapter(
            token=token,
            webhook_secret=webhook_secret,
            dedup_store=dedup_store,
            session_store=session_store,
            conversation_handler=conversation_handler,
            iam_service=iam_service,
            audio_service=chat.audio_service,
            language_service=language_service,
            localization=chat.localization,
            task_queue=task_queue,
        )
