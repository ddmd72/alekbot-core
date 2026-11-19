"""The services every chat platform's `ConversationHandler` gets, held once (`ChatServices`), and the one builder both
platform factories use. What differs per platform is explicit: the rich-content delivery of its kit, and Slack's channel
binding and history source (Telegram has neither: owner ruling P9-D9, and the Bot API has no history)."""
from dataclasses import dataclass
from typing import Any, Optional

from ..domain.settings import ConsolidationSettings
from ..handlers.command_services import CommandServices
from ..handlers.conversation_handler import ConversationHandler
from ..infrastructure.agent_coordinator import AgentCoordinator
from ..services.channel_binding_service import ChannelBindingService
from ..services.consolidation_service import ConsolidationService
from ..services.document_delivery_service import DocumentDeliveryService
from ..services.geo_mark_service import GeoMarkService
from ..services.localization_service import LocalizationService
from ..services.rich_content_service import RichContentService
from ..services.user_notification_service import UserNotificationService
from .user_agent_factory import UserAgentFactory


@dataclass(frozen=True)
class ChatServices:
    """The `ConversationHandler` dependencies both platforms share, built once by the application graph."""
    coordinator: AgentCoordinator
    agent_factory: UserAgentFactory
    consolidation_queue: Optional[Any] = None
    consolidation_config: Optional[ConsolidationSettings] = None
    consolidation_service: Optional[ConsolidationService] = None   # its consolidate_now serves `$consolidate`
    audio_service: Optional[Any] = None
    notification_service: Optional[UserNotificationService] = None
    localization: Optional[LocalizationService] = None
    file_conversion_service: Optional[Any] = None
    fallback_service: Optional[Any] = None
    short_link_service: Optional[Any] = None
    skill_service: Optional[Any] = None
    memory_fact_service: Optional[Any] = None
    fact_answers: Optional[Any] = None
    drive_note_service: Optional[Any] = None   # `$drive-notes` (DRIVE_FILE_NOTES_RFC §4.8)
    long_turn_service: Optional[Any] = None
    doc_delivery_service: Optional[DocumentDeliveryService] = None
    geo_mark_service: Optional[GeoMarkService] = None   # `$geo` and the Telegram pin (GEO_MARKS_RFC §4.5, §4.6)


def build_conversation_handler(chat: ChatServices, rich_content: RichContentService, *,
                               channel_binding: Optional[ChannelBindingService] = None,
                               channel_history: Optional[Any] = None) -> ConversationHandler:
    """One platform's chat handler. `rich_content` is the platform's own (shared with the notification path), not the
    whole kit: Telegram's kit is completed only after its adapter, which takes this handler."""
    return ConversationHandler(
        coordinator=chat.coordinator,
        agent_factory=chat.agent_factory,
        consolidation_queue=chat.consolidation_queue,
        global_config=chat.consolidation_config,
        audio_service=chat.audio_service,
        rich_content_service=rich_content,
        notification_service=chat.notification_service,
        overflow_callback=chat.consolidation_service.consolidate_now if chat.consolidation_service else None,
        localization=chat.localization,
        file_conversion_service=chat.file_conversion_service,
        channel_binding_service=channel_binding,
        channel_history_source=channel_history,
        fallback_service=chat.fallback_service,
        short_link_service=chat.short_link_service,
        fact_answers=chat.fact_answers,
        long_turn_service=chat.long_turn_service,
        doc_delivery_service=chat.doc_delivery_service,
        commands=CommandServices(skills=chat.skill_service, memory_facts=chat.memory_fact_service,
                                 drive_notes=chat.drive_note_service, geo_marks=chat.geo_mark_service),
    )
