"""PlainTextInteractiveMessageAdapter — interactive messages for a platform without controls: the options go out as
numbered text and the owner answers with a digit in a reply. Renders only; what a digit means is the caller's."""
from ..domain.choice_outcome import ChoiceOutcome
from ..domain.interactive_message import InteractiveMessage
from ..domain.messaging import MessageRef
from ..ports.interactive_message_port import InteractiveMessagePort
from ..ports.notification_channel_factory_port import NotificationChannelFactoryPort
from ..utils.logger import logger


class PlainTextInteractiveMessageAdapter(InteractiveMessagePort):

    def __init__(self, channel_factory: NotificationChannelFactoryPort):
        self._channels = channel_factory

    async def post(self, platform: str, channel_id: str, message: InteractiveMessage) -> MessageRef:
        channel = self._channels.create(platform, channel_id)
        if channel is None:
            logger.error(f"[PlainTextInteractive] no channel for platform={platform} channel={channel_id}")
            raise RuntimeError(f"no channel for platform {platform}")
        try:
            return await channel.send_message_ref(message.as_plain_text())
        except Exception as exc:
            logger.error(
                f"[PlainTextInteractive] post failed platform={platform} channel={channel_id} "
                f"binding={message.binding.token()}: {type(exc).__name__}"
            )
            raise

    async def show_choice(self, ref: MessageRef, outcome: ChoiceOutcome) -> None:
        """There are no controls to remove, so the message stays as it was posted."""
        return None
