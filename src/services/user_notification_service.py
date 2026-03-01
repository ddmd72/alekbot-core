"""
UserNotificationService — sends background notifications to a user via their last active channel.

Flow:
  1. Load last active channel from NotificationStatePort.
  2. Create a ResponseChannel via NotificationChannelFactoryPort.
  3. Send system_alert to QuickAgent (via AgentCoordinator) to format in user's communication style.
  4. Deliver the formatted response via ResponseChannel.

Callers: email indexing worker (batch progress), future background tasks.
"""
import uuid
from typing import Optional

from ..domain.agent import AgentMessage, AgentIntent, AgentStatus
from ..domain.llm import MessagePart
from ..domain.messaging import SmartResponse
from ..infrastructure.agent_coordinator import AgentCoordinator
from ..ports.notification_channel_factory_port import NotificationChannelFactoryPort
from ..ports.notification_state_port import NotificationStatePort
from ..utils.logger import logger


class UserNotificationService:

    def __init__(
        self,
        state_repo: NotificationStatePort,
        channel_factory: NotificationChannelFactoryPort,
        coordinator: AgentCoordinator,
    ):
        self._state_repo = state_repo
        self._channel_factory = channel_factory
        self._coordinator = coordinator

    async def save_channel(self, user_id: str, platform: str, channel_id: str) -> None:
        """
        Persist last active channel for a user.
        Called by ConversationHandler on every incoming message. Best-effort: never raises.
        """
        try:
            await self._state_repo.save(user_id, platform, channel_id)
        except Exception as exc:
            logger.warning(f"[Notification] Failed to save channel for {user_id[:8]}: {exc}")

    async def notify(
        self,
        user_id: str,
        account_id: str,
        system_alert: str,
        agent_id_override: Optional[str] = None,
    ) -> None:
        """
        Send a background notification to the user's last active channel.
        The system_alert is formatted by QuickAgent in the user's communication style.
        Silently skips if no channel is stored or platform not configured.
        """
        try:
            channel_info = await self._state_repo.get(user_id)
        except Exception as exc:
            logger.warning(f"[Notification] Failed to load channel for {user_id[:8]}: {exc}")
            return

        if not channel_info:
            logger.info(f"[Notification] No channel stored for user {user_id[:8]}, skipping")
            return

        response_channel = self._channel_factory.create(
            platform=channel_info.platform,
            channel_id=channel_info.channel_id,
        )
        if not response_channel:
            logger.warning(
                f"[Notification] Cannot create channel: platform={channel_info.platform}"
            )
            return

        recipient = agent_id_override or f"quick_response_agent_{user_id}"
        framed = MessagePart(text=f"[System: {system_alert} Your response to this message will be read by the user. Inform them of the event details in your usual manner of communication.]")
        message = AgentMessage.create(
            sender="notification_service",
            recipient=recipient,
            intent=AgentIntent.QUERY,
            payload={"text": ""},
            context={
                "user_id": user_id,
                "account_id": account_id,
                "session_id": str(uuid.uuid4()),
                "thread_id": None,
                "current_message_parts": [framed],
            },
        )

        try:
            response = await self._coordinator.route_message(message)
            if response.status != AgentStatus.SUCCESS:
                logger.warning(
                    f"[Notification] Agent returned {response.status} for user {user_id[:8]}"
                )
                return

            result = response.result
            if isinstance(result, SmartResponse):
                text = result.text
            else:
                text = str(result) if result else ""

            if text:
                await response_channel.send_message(text)
                logger.info(
                    f"📬 [Notification] Sent to {channel_info.platform} "
                    f"channel={channel_info.channel_id} user={user_id[:8]}"
                )
        except Exception as exc:
            logger.error(
                f"[Notification] Failed to send notification for {user_id[:8]}: {exc}",
                exc_info=True,
            )
