"""
LanguageServicePort — platform adapter boundary for language preference resolution.

Adapters call resolve_ui_language() to get the effective UI language for a user,
and get_preference() to obtain the agent language settings to forward into
MessageContext metadata.
"""
from abc import ABC, abstractmethod
from typing import Optional, Tuple

from ..domain.language import LanguageCode


class LanguageServicePort(ABC):
    """Read-only language resolution interface used by platform adapters."""

    @abstractmethod
    async def resolve_ui_language(self, user_id: str) -> LanguageCode:
        """Resolve effective UI language for a user (USER → ACCOUNT → SYSTEM chain)."""

    @abstractmethod
    async def get_preference(self, user_id: str) -> Tuple[Optional[LanguageCode], bool]:
        """Return (preferred_language, agent_mirror) for a user."""
