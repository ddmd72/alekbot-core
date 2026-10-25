"""ChoiceHandlerPort — what answers a choice made on an interactive message of one binding kind."""
from abc import ABC, abstractmethod
from typing import Optional

from src.domain.choice_outcome import ChoiceOutcome
from src.domain.interactive_choice import InteractiveChoice


class ChoiceHandlerPort(ABC):

    @property
    @abstractmethod
    def kind(self) -> str:
        """The `InteractiveBinding.kind` this handler answers."""

    @abstractmethod
    async def handle(self, choice: InteractiveChoice, user_id: str, account_id: str) -> Optional[ChoiceOutcome]:
        """Act on the choice for the resolved user. The outcome to show on the message, or None when nothing is to be
        shown (the choice was not the user's, or not for this message)."""
