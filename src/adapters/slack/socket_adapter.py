"""
Socket Mode Adapter
Enterprise-level implementation using platform-agnostic ConversationHandler
"""
from typing import Optional, Any
from slack_bolt.async_app import AsyncApp
from slack_bolt.adapter.socket_mode.async_handler import AsyncSocketModeHandler

from .base import SlackAdapter
from .response_channel import SlackResponseChannel
from ...domain.messaging import MessageContext, FileAttachment
from ...domain.prompt import ANONYMOUS_ACCOUNT_ID  # SESSION_26
from ...infrastructure.agent_coordinator import AgentCoordinator
from ...services.user_agent_factory import UserAgentFactory
from ...services.iam_service import IAMService
from ...ports.file_service import FileService
from ...utils.logger import logger


class SocketModeAdapter(SlackAdapter):
    """
    Socket Mode adapter for Slack integration.
    Uses persistent WebSocket connection - suitable for local development.

    Translates Slack events into platform-agnostic MessageContext and
    delegates processing to ConversationHandler.
    
    Updated (2026-02-05): Replaced IdentityResolver with IAMService.
    """

    def __init__(
        self,
        app: AsyncApp,
        config: dict,
        coordinator: AgentCoordinator,
        agent_factory: UserAgentFactory,
        iam_service: IAMService,
        file_service: FileService,
        consolidation_queue: Optional[Any] = None,
        consolidation_config: Optional[Any] = None,
        audio_service: Optional[Any] = None,
    ):
        super().__init__(
            app,
            config,
            coordinator,
            agent_factory,
            iam_service,
            file_service,
            consolidation_queue,
            consolidation_config,
            audio_service=audio_service,
        )

        self.slack_app_token = config.get("SLACK_APP_TOKEN")
        self.slack_bot_token = config.get("SLACK_BOT_TOKEN")
        self.handler: Optional[AsyncSocketModeHandler] = None

        if not self.slack_app_token:
            raise ValueError("SLACK_APP_TOKEN is required for Socket Mode")

        logger.info("🔌 Socket Mode adapter initialized (Multi-Tenant)")

    def _translate_files(self, slack_files: list) -> list:
        attachments = []
        for f in slack_files:
            attachments.append(FileAttachment(
                url=f.get("url_private"),
                mime_type=f.get("mimetype", "application/octet-stream"),
                filename=f.get("name", "unknown"),
                size_bytes=f.get("size")
            ))
        return attachments

    def register_handlers(self) -> None:
        logger.info("🔍 DEBUG: Registering message handler...")

        @self.app.event("message")
        async def handle_message(message, say):
            logger.info(f"🔍 DEBUG: handle_message called with message keys: {list(message.keys())}")
            logger.info(f"📨 Received message event: {message.get('type')} in {message.get('channel_type')} channel")

            subtype = message.get("subtype")
            if message.get("bot_id") or (subtype and subtype != "file_share"):
                return

            if message.get("channel_type") != "im":
                logger.debug(f"⏭️ Skipping non-IM message in channel {message.get('channel')}")
                return

            text = message.get("text", "")
            slack_user_id = message.get("user", "unknown")

            # IAM Authorization (NEW - 2026-02-05)
            decision = await self.iam_service.authorize("slack", platform_user_id=slack_user_id)
            
            if decision.action == "reject":
                # User NOT authorized → send registration message
                logger.warning(f"⛔ Unauthorized Slack user: {slack_user_id}")
                await say(decision.message)
                return
            
            # User authorized → continue
            user_profile = decision.user
            user_id = user_profile.user_id
            account_id = user_profile.account_id or ANONYMOUS_ACCOUNT_ID  # SESSION_26
            logger.info(f"👤 Processing message for user {user_id} ({user_profile.display_name})")

            await self.agent_factory.ensure_agents_for_user(user_id)

            if text.startswith("$"):
                command = text.lstrip("$").strip().lower()
                context = MessageContext(
                    text=text,
                    session_id=user_id,
                    user_id=user_id,
                    account_id=account_id,  # SESSION_26
                    metadata={"event_type": "command", "slack_user_id": slack_user_id}
                )
                response_channel = SlackResponseChannel(
                    self.app.client,
                    message["channel"],
                    self.slack_bot_token
                )
                await self.conversation_handler.handle_command(command, context, response_channel)
                return

            context = MessageContext(
                text=text,
                session_id=user_id,
                user_id=user_id,
                account_id=account_id,  # SESSION_26
                attachments=self._translate_files(message.get("files", [])),
                thread_id=message.get("thread_ts"),
                metadata={
                    "event_type": "message",
                    "channel_type": message.get("channel_type"),
                    "slack_user_id": slack_user_id
                }
            )

            response_channel = SlackResponseChannel(
                self.app.client,
                message["channel"],
                self.slack_bot_token
            )

            await self.conversation_handler.handle_message(context, response_channel)

        @self.app.event("app_mention")
        async def handle_mention(event, say):
            logger.info(f"📨 Received app_mention event in channel {event.get('channel')}")

            text = event["text"].split(">", 1)[-1].strip()
            slack_user_id = event.get("user", "unknown")

            # IAM Authorization (NEW - 2026-02-05)
            decision = await self.iam_service.authorize("slack", platform_user_id=slack_user_id)
            
            if decision.action == "reject":
                # User NOT authorized → send registration message
                logger.warning(f"⛔ Unauthorized Slack user: {slack_user_id}")
                await say(decision.message)
                return
            
            # User authorized → continue
            user_profile = decision.user
            user_id = user_profile.user_id
            account_id = user_profile.account_id or ANONYMOUS_ACCOUNT_ID  # SESSION_26
            logger.info(f"👤 Processing mention for user {user_id} ({user_profile.display_name})")

            await self.agent_factory.ensure_agents_for_user(user_id)

            context = MessageContext(
                text=text,
                session_id=user_id,
                user_id=user_id,
                account_id=account_id,  # SESSION_26
                attachments=self._translate_files(event.get("files", [])),
                thread_id=event.get("ts"),
                metadata={
                    "event_type": "app_mention",
                    "slack_user_id": slack_user_id
                }
            )

            response_channel = SlackResponseChannel(
                self.app.client,
                event["channel"],
                self.slack_bot_token
            )

            await self.conversation_handler.handle_message(context, response_channel)

        @self.app.event("file_shared")
        async def handle_file_shared_events():
            pass

        logger.info("✅ Socket Mode handlers registered")

    async def start(self) -> None:
        try:
            logger.info("🚀 Starting Socket Mode handler...")
            logger.debug(f"App token starts with: {self.slack_app_token[:10]}...")
            logger.debug(f"Bot token starts with: {self.slack_bot_token[:10]}...")

            logger.debug("Creating AsyncSocketModeHandler...")
            self.handler = AsyncSocketModeHandler(
                self.app,
                self.slack_app_token,
                ping_interval=30
            )
            logger.debug("AsyncSocketModeHandler created")

            logger.info("⏳ Calling start_async() - this will block until stopped")
            await self.handler.start_async()
            logger.info("✅ start_async() completed (bot stopped)")

        except Exception as e:
            logger.error(f"❌ Socket Mode handler failed: {e}", exc_info=True)
            raise

    async def stop(self) -> None:
        try:
            if self.handler:
                logger.info("🛑 Stopping Socket Mode handler...")
                await self.handler.close_async()
                logger.info("✅ Socket Mode handler stopped")
        except Exception as e:
            logger.error(f"❌ Error stopping Socket Mode handler: {e}")

    def get_mode_name(self) -> str:
        return "SocketMode"
