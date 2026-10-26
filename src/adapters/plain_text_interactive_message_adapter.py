"""PlainTextInteractiveMessageAdapter — interactive messages for a platform without controls: the options go out as
numbered text and the owner answers with a digit in a reply. Renders only; what a digit means is the caller's."""
from ..domain.choice_outcome import ChoiceOutcome
from ..domain.interactive_message import InteractiveMessage
from ..domain.messaging import MessageRef
from ..ports.interactive_message_port import InteractiveMessagePort
from ..ports.notification_channel_factory_port import NotificationChannelFactoryPort
from ..utils.logger import logger


ELLIPSIS = "…"
_SEPARATOR = "\n\n"


def _layout(message: InteractiveMessage, max_length: int) -> str:
    """Prompt, the quote in «», the numbered options on one line and last the reply hint. The hint is part of the text
    because on a platform without controls only a reply to the message counts. The text fits `max_length`: a quote that
    would overflow it is cut with an ellipsis, and the prompt, the options and the hint are never touched (the digits
    must survive)."""
    options = message.options_line()
    head = [message.prompt.strip()]
    tail = [options]
    if message.reply_hint.strip():
        tail.append(message.reply_hint.strip())
    quote = message.quote.strip()
    if quote:
        # the quote's section is «» around it and one separator before it
        budget = max_length - len(_SEPARATOR.join(head + tail)) - len(_SEPARATOR) - 2
        if len(quote) > budget:
            quote = quote[: budget - len(ELLIPSIS)].rstrip() + ELLIPSIS if budget > len(ELLIPSIS) else ""
    parts = head + ([f"«{quote}»"] if quote else []) + tail
    return _SEPARATOR.join(parts)


class PlainTextInteractiveMessageAdapter(InteractiveMessagePort):

    def __init__(self, channel_factory: NotificationChannelFactoryPort):
        self._channels = channel_factory

    async def post(self, platform: str, channel_id: str, message: InteractiveMessage) -> MessageRef:
        channel = self._channels.create(platform, channel_id)
        if channel is None:
            logger.error(f"[PlainTextInteractive] no channel for platform={platform} channel={channel_id}")
            raise RuntimeError(f"no channel for platform {platform}")
        try:
            return await channel.send_message_ref(_layout(message, channel.max_message_length))
        except Exception as exc:
            logger.error(
                f"[PlainTextInteractive] post failed platform={platform} channel={channel_id} "
                f"binding={message.binding.kind}:{message.binding.id}: {type(exc).__name__}"
            )
            raise

    async def show_choice(self, ref: MessageRef, outcome: ChoiceOutcome) -> None:
        """There are no controls to remove, so the message stays as it was posted."""
        return None
