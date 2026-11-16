"""
CallTextFileService — keeps an interpreter or tutor web call's text as one `.md` file, in the chat or in the drive
Inbox's voice-calls folder (VOICE_CALL_LIVE_TEXT_RFC §4.11).

The page sends the call's lines; this service asks the existing summarizer for `{title, summary}` (bounded: a slow
or failed summary still saves the file, with the fallback title and no summary), renders the Markdown, and delivers it:
`chat` through the notification service (one file upload, no session history written), `drive` through
`UserDriveService.save_to_primary` into `Inbox/<voice calls folder>` of the primary's write folder (never overwrites; the folder's name is the owner's
UI-language string, created when missing and reused when present). Neither `save`
nor `drive_available` raises: the route answers from the outcome. Logs carry counts, bytes and `user_id[:8]` only,
never the call's text or the title.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Callable, Optional

from ..domain.call_text_file import CallTextFileOutcome, CallTextRequest, summarizer_turns
from ..domain.call_text_markdown import (
    CALL_FILE_CONTENT_TYPE,
    call_text_filename,
    render_call_text_file,
)
from ..domain.local_time import local_moment
from ..domain.voice_mode import CALL_FILE_SUMMARIZER_TYPES
from ..domain.ui_messages import UIMessage
from ..domain.user_drive import DEFAULT_INBOX_FOLDER, DriveAccessRefused, DriveNotConnectedError, join_drive_path
from ..ports.companion_extractor_port import CompanionExtractorPort
from ..ports.language_service_port import LanguageServicePort
from ..ports.localization_port import LocalizationPort
from ..ports.prompt_content_store import PromptContentStore
from ..ports.user_repository import UserRepository
from ..utils.logger import logger

if TYPE_CHECKING:
    from .user_drive_service import UserDriveService
    from .user_notification_service import UserNotificationService


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class CallTextFileService:
    """drive_available(user_id) -> bool; save(user_id, account_id, request) -> CallTextFileOutcome."""

    def __init__(
        self,
        extractor: CompanionExtractorPort,
        drive: Optional["UserDriveService"],
        notifications: "UserNotificationService",
        users: UserRepository,
        *,
        localization: LocalizationPort,
        language: LanguageServicePort,
        prompt_content: Optional[PromptContentStore] = None,
        now: Callable[[], datetime] = _utcnow,
        summary_timeout_s: float = 30.0,
    ) -> None:
        self._extractor = extractor
        self._drive = drive
        self._notifications = notifications
        self._users = users
        self._localization = localization
        self._language = language
        self._prompt_content = prompt_content
        self._now = now
        self._summary_timeout_s = summary_timeout_s

    async def drive_available(self, user_id: str) -> bool:
        """Whether a file can be saved to the user's drive now. Never raises."""
        if self._drive is None:
            return False
        try:
            return await self._drive.can_save_to_primary(user_id)
        except Exception:
            logger.warning("[CallTextFile] drive availability unreadable for user=%s", user_id[:8], exc_info=True)
            return False

    async def save(self, user_id: str, account_id: str, request: CallTextRequest) -> CallTextFileOutcome:
        """Summarize, render and deliver the call's text. Never raises."""
        try:
            started = local_moment(self._now() - timedelta(seconds=request.duration_s),
                                   await self._timezone(user_id))
            title, summary = await self._summarize(user_id, account_id, request)
            name = call_text_filename(started, title)
            data = render_call_text_file(title, started, request.duration_s, summary, request.entries).encode("utf-8")
            logger.info("[CallTextFile] user=%s target=%s entries=%d bytes=%d", user_id[:8], request.target,
                        len(request.entries), len(data))
            if request.target == "chat":
                return await self._to_chat(user_id, account_id, data, name, title)
            return await self._to_drive(user_id, data, name)
        except Exception:
            logger.error("[CallTextFile] save failed for user=%s", user_id[:8], exc_info=True)
            return CallTextFileOutcome(saved=False, reason="failed")

    async def _timezone(self, user_id: str) -> str:
        try:
            user = await self._users.get_user(user_id)
            return user.config.timezone if user is not None else "UTC"
        except Exception:
            logger.warning("[CallTextFile] timezone unreadable for user=%s; using UTC", user_id[:8], exc_info=True)
            return "UTC"

    async def _summarize(self, user_id: str, account_id: str, request: CallTextRequest) -> tuple[str, str]:
        """(title, summary); both empty when the summarizer fails or runs past its budget."""
        try:
            extraction = await asyncio.wait_for(
                self._extractor.extract(CALL_FILE_SUMMARIZER_TYPES[request.mode], account_id, user_id,
                                        summarizer_turns(request.entries)),
                self._summary_timeout_s,
            )
            title, summary = extraction.get("title", ""), extraction.get("summary", "")
            if isinstance(title, str) and isinstance(summary, str):
                return title.strip(), summary.strip()
            logger.error("[CallTextFile] summarizer returned a malformed result for user=%s", user_id[:8])
        except Exception:
            logger.error("[CallTextFile] summary failed for user=%s; saving without it", user_id[:8], exc_info=True)
        finally:
            # The summarizer's prompt-content row is written in the background; Cloud Run throttles CPU once
            # the response is sent, so flush before answering.
            if self._prompt_content is not None:
                try:
                    await self._prompt_content.flush()
                except Exception:
                    logger.warning("[CallTextFile] prompt content flush failed", exc_info=True)
        return "", ""

    async def _to_chat(self, user_id: str, account_id: str, data: bytes, name: str,
                       title: str) -> CallTextFileOutcome:
        sent = await self._notifications.notify_file_bytes(user_id, account_id, data, name, title or name)
        return CallTextFileOutcome(saved=True) if sent else CallTextFileOutcome(saved=False, reason="no_chat")

    async def _to_drive(self, user_id: str, data: bytes, name: str) -> CallTextFileOutcome:
        if self._drive is None:
            return CallTextFileOutcome(saved=False, reason="not_connected")
        try:
            # The folder follows the owner's UI language; no other language's folder is ever a fallback.
            lang = await self._language.resolve_ui_language(user_id)
            folder = join_drive_path(DEFAULT_INBOX_FOLDER,
                                     self._localization.get_ui_string(lang, UIMessage.CALL_FILES_DRIVE_FOLDER))
        except Exception:
            logger.error("[CallTextFile] folder name unresolved for user=%s", user_id[:8], exc_info=True)
            return CallTextFileOutcome(saved=False, reason="failed")
        try:
            _, path = await self._drive.save_to_primary(user_id, data, name, CALL_FILE_CONTENT_TYPE, folder=folder)
        except DriveNotConnectedError:
            logger.info("[CallTextFile] drive not connected for user=%s", user_id[:8])
            return CallTextFileOutcome(saved=False, reason="not_connected")
        except DriveAccessRefused as refused:
            logger.warning("[CallTextFile] refused by the access policy for user=%s (%s)", user_id[:8], refused.line)
            return CallTextFileOutcome(saved=False, reason="refused")
        except Exception:
            logger.error("[CallTextFile] drive save failed for user=%s", user_id[:8], exc_info=True)
            return CallTextFileOutcome(saved=False, reason="failed")
        return CallTextFileOutcome(saved=True, path=path)
