"""InteractiveMessageRouter — picks the interactive-message adapter by platform. Composition builds the mapping;
this class imports no concrete adapter and holds no rule beyond the lookup."""
from typing import Mapping

from ..domain.choice_outcome import ChoiceOutcome
from ..domain.interactive_message import InteractiveMessage
from ..domain.messaging import MessageRef
from ..ports.interactive_message_port import InteractiveMessagePort
from ..utils.logger import logger


class InteractiveMessageRouter(InteractiveMessagePort):

    def __init__(self, adapters: Mapping[str, InteractiveMessagePort]):
        self._adapters = dict(adapters)

    def _for(self, platform: str) -> InteractiveMessagePort:
        adapter = self._adapters.get(platform)
        if adapter is None:
            logger.error(f"[InteractiveRouter] no interactive-message adapter for platform={platform}")
            raise ValueError(f"no interactive-message adapter for platform {platform}")
        return adapter

    async def post(self, platform: str, channel_id: str, message: InteractiveMessage) -> MessageRef:
        return await self._for(platform).post(platform, channel_id, message)

    async def show_choice(self, ref: MessageRef, outcome: ChoiceOutcome) -> None:
        await self._for(ref.platform).show_choice(ref, outcome)
