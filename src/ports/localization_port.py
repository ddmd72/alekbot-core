"""
LocalizationPort — abstract interface for UI string localization.

RFC: docs/10_rfcs/MULTILINGUAL_SUPPORT_RFC.md §8

Scope: status messages ("Thinking..."), file prompts, entertainment intros.
NOT for agent response language — that is PromptBuilderPort's concern.

Justification for port:
- 2+ implementations plausible: file-based (now), Firestore-based (future).
- Application layer must not depend on locale file structure.
- Enables deterministic test doubles.
"""
from abc import ABC, abstractmethod
from typing import List

from ..domain.language import LanguageCode
from ..domain.ui_messages import StatusType


class LocalizationPort(ABC):
    """Abstract interface for UI string localization."""

    @abstractmethod
    def get_status_phrases(self, lang: LanguageCode, status: StatusType) -> List[str]:
        """All phrase variants for a status type. Caller picks one at random."""

    @abstractmethod
    def get_entertainment_intros(self, lang: LanguageCode) -> List[str]:
        """Intro phrases for the web-search entertainment message."""

    @abstractmethod
    def get_file_prompt(self, lang: LanguageCode, mime_type: str) -> str:
        """Prompt to use when user sends a file without text."""
