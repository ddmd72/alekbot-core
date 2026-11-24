"""InteractiveMessagePort — post a message with options to a platform and re-render it once a choice is made."""
from abc import ABC, abstractmethod

from src.domain.choice_outcome import ChoiceOutcome
from src.domain.interactive_message import InteractiveMessage
from src.domain.messaging import MessageRef


class InteractiveMessagePort(ABC):

    @abstractmethod
    async def post(self, platform: str, channel_id: str, message: InteractiveMessage) -> MessageRef:
        """Post `message` in the channel, with whatever controls the platform has (plain text with digits where it has
        none). Returns where it landed. Raises when it was not delivered."""

    async def post_reply(
        self, platform: str, channel_id: str, message: InteractiveMessage, to_message_id: str,
    ) -> MessageRef:
        """Post `message` as `post` does, tied to the message `to_message_id` of the channel the way the platform ties a
        reply to a message (a thread reply on Slack), so it lands next to the turn it belongs to. Raises when it was not
        delivered.

        Not abstract: only a platform whose messages can be replied to implements it; the others refuse."""
        raise NotImplementedError(f"{platform} cannot post an interactive message as a reply")

    @abstractmethod
    async def show_choice(self, ref: MessageRef, outcome: ChoiceOutcome) -> None:
        """Re-render the message at `ref` as `outcome` says. Without `outcome.keep_options` the message loses its controls;
        with it the note is shown and the options stay. Nothing to do on a platform that never had any controls. Raises
        when a platform that had controls could not update the message."""
