"""The user notification path and the Slack platform kit it is built with (decisions/composition_root_consumers.md §4,
§5). Slack's kit is handed to the notification service and the channel factory at construction; Telegram's is bound
late by `register_late_platform`, once its adapter exists.

The Slack app is built here, before the notification service and the memory review, whose questions are posted through
its client; the Slack adapter is built later over the same app.
"""
from dataclasses import dataclass
from typing import Any, Optional

from slack_bolt.async_app import AsyncApp

from ..adapters.firestore_notification_state_adapter import FirestoreNotificationStateAdapter
from ..adapters.gcs_media_adapter import GcsMediaAdapter
from ..adapters.notification_channel_factory import NotificationChannelFactory
from ..adapters.playwright_html_renderer import PlaywrightHtmlRenderer
from ..adapters.slack.interactive_message_adapter import PLATFORM as SLACK_PLATFORM
from ..adapters.slack.media_adapter import SlackMediaAdapter
from ..adapters.slack.response_channel import SlackResponseChannel
from ..config.environment import EnvironmentConfig
from ..infrastructure.agent_coordinator import AgentCoordinator
from ..infrastructure.notification_sla import NOTIFICATION_SLA
from ..ports.html_renderer_port import HtmlRendererPort
from ..services.rich_content_service import RichContentService
from ..services.user_notification_service import UserNotificationService
from ..utils.logger import logger
from .language_setup import Languages
from .platform_kit import PlatformKit
from .sandbox_client_setup import build_html_renderer


@dataclass(frozen=True)
class Notifications:
    """`html_renderer` None: ENABLE_HTML_RENDERER is off (no widget images, on either platform)."""

    slack_app: AsyncApp
    html_renderer: Optional[HtmlRendererPort]
    slack_kit: PlatformKit
    channel_factory: NotificationChannelFactory
    state_repo: FirestoreNotificationStateAdapter
    service: UserNotificationService


def build_notifications(config: dict, db_client: Any, env_config: EnvironmentConfig, *, coordinator: AgentCoordinator,
                        session_store: Any, media_storage: Optional[GcsMediaAdapter],
                        languages: Languages) -> Notifications:
    """One media adapter and one RichContentService for Slack: the chat handler and the notification service deliver
    files through the same ones. The renderer lazy-starts Chromium on the first widget request."""
    state_repo = FirestoreNotificationStateAdapter(db_client=db_client, env_config=env_config)
    bot_token = config["SLACK_BOT_TOKEN"]
    logger.debug(f"Bot token starts with: {bot_token[:10]}...")
    app = AsyncApp(token=bot_token)

    html_renderer = build_html_renderer(
        enabled=bool(config.get("ENABLE_HTML_RENDERER")), inprocess_factory=PlaywrightHtmlRenderer)
    if html_renderer is not None:
        logger.info("✅ HTML renderer enabled (%s)", type(html_renderer).__name__)
    else:
        logger.info("ℹ️ HTML renderer disabled (ENABLE_HTML_RENDERER not set)")

    slack_media = SlackMediaAdapter(app_client=app.client, bot_token=bot_token)
    slack_kit = PlatformKit(
        platform=SLACK_PLATFORM,
        media=slack_media,
        rich_content=RichContentService(media_port=slack_media, storage_port=media_storage,
                                        html_renderer=html_renderer),
        channel=lambda channel_id: SlackResponseChannel(app_client=app.client, channel_id=channel_id,
                                                        bot_token=bot_token),
    )
    channel_factory = NotificationChannelFactory({slack_kit.platform: slack_kit.channel})
    service = UserNotificationService(
        state_repo=state_repo,
        channel_factory=channel_factory,
        coordinator=coordinator,
        notification_sla=NOTIFICATION_SLA,
        platform_media={slack_kit.platform: slack_kit.media},
        platform_rich_content={slack_kit.platform: slack_kit.rich_content},
        session_store=session_store,
        # The drive-note receipt posted from a call is rendered in the owner's UI language (DRIVE_FILE_NOTES_RFC §4.9).
        localization=languages.localization,
        language=languages.resolver,
    )
    return Notifications(slack_app=app, html_renderer=html_renderer, slack_kit=slack_kit,
                         channel_factory=channel_factory, state_repo=state_repo, service=service)
