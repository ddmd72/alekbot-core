"""
LocalizationService
===================

Wraps LocalizationPort to provide UI string resolution to handlers and other
services. Handlers must not import LocalizationPort directly — they receive
this service via constructor injection.
"""
from __future__ import annotations

from typing import List

from ..domain.language import LanguageCode
from ..domain.ui_messages import StatusType
from ..ports.localization_port import LocalizationPort


class LocalizationService:
    """Thin service wrapper around LocalizationPort for UI string access."""

    def __init__(self, port: LocalizationPort) -> None:
        self._port = port

    def get_file_prompt(self, lang: LanguageCode, mime_type: str) -> str:
        """Return the localized prompt text shown when a user sends a file attachment."""
        return self._port.get_file_prompt(lang, mime_type)

    def get_status_phrases(self, lang: LanguageCode, status: StatusType) -> List[str]:
        """All phrase variants for a status type. Caller picks one at random."""
        return self._port.get_status_phrases(lang, status)

    def get_entertainment_intros(self, lang: LanguageCode) -> List[str]:
        """Intro phrases for the web-search entertainment message."""
        return self._port.get_entertainment_intros(lang)
