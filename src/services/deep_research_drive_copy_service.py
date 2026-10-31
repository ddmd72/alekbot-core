"""
DeepResearchDriveCopyService — the opt-in copy of a deep research report to the user's drive
(docs/04_solution_strategy/decisions/deep_research_drive_copy.md).

Zero-LLM and deterministic. Called once per delivered deep research HTML report, after its link reached
the user: when the user turned on "Copy deep research reports to my drive" in the Cabinet (the explicit
consent, read uncached here), the HTML is saved through UserDriveService (never overwrites; same bytes →
the existing file; otherwise a suffixed name) into a folder named in the user's UI language, as
`YYYY-MM-DD <title>.html`. The result is one short localized line for the chat: copied (with the path),
not connected, or failed. No retry, no queue, and it never raises: the delivery must not fail because
of the copy.

The copy is an ordinary drive file: with drive indexing on, the indexer picks it up like any HTML (the
save nudges it). It goes to the primary connection (MULTI_DRIVE_RFC §11.5, D7): with no primary it is
"not connected", never written elsewhere.
"""
from __future__ import annotations

from datetime import date, datetime, timezone
from typing import TYPE_CHECKING, Callable, Optional
from zoneinfo import ZoneInfo

from ..domain.deep_research_drive_copy import (
    REPORT_CONTENT_TYPE,
    deep_research_drive_filename,
    report_title_from_html,
)
from ..domain.language import LanguageCode
from ..domain.ui_messages import UIMessage
from ..domain.user_drive import DriveNotConnectedError
from ..ports.language_service_port import LanguageServicePort
from ..ports.user_repository import UserRepository
from ..utils.logger import logger

if TYPE_CHECKING:
    from .localization_service import LocalizationService
    from .user_drive_service import UserDriveService


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class DeepResearchDriveCopyService:
    """copy_report(user_id, html, query) -> the chat line, or None when nothing is to be said."""

    def __init__(
        self,
        users: UserRepository,
        drive: Optional["UserDriveService"],
        localization: "LocalizationService",
        language: LanguageServicePort,
        now: Callable[[], datetime] = _utcnow,
    ) -> None:
        self._users = users
        self._drive = drive
        self._localization = localization
        self._language = language
        self._now = now

    async def copy_report(self, user_id: str, html: bytes, query: str) -> Optional[str]:
        """Copy the report if the user opted in. Returns the localized line to post after the report's
        link, or None (toggle off, consent unreadable, or the line itself could not be built)."""
        try:
            user = await self._users.get_user(user_id)
        except Exception:
            # Consent unknown → no copy (and nothing to tell: the user may never have asked for one).
            logger.error("[DeepResearchDriveCopy] toggle unreadable for user=%s; not copied", user_id[:8],
                         exc_info=True)
            return None
        if user is None or not user.config.drive_copy_deep_research:
            return None

        try:
            lang = await self._language.resolve_ui_language(user_id)
        except Exception:
            logger.warning("[DeepResearchDriveCopy] UI language lookup failed for user=%s", user_id[:8],
                           exc_info=True)
            lang = LanguageCode.EN

        try:
            folder = self._localization.get_ui_string(lang, UIMessage.DEEP_RESEARCH_DRIVE_FOLDER)
            outcome = await self._save(user_id, html, query, folder, self._local_day(user.config.timezone))
            return self._localization.get_ui_string(lang, outcome[0]).format(path=outcome[1])
        except Exception:
            logger.error("[DeepResearchDriveCopy] copy line could not be built for user=%s", user_id[:8],
                         exc_info=True)
            return None

    async def _save(self, user_id: str, html: bytes, query: str, folder: str,
                    day: date) -> tuple[UIMessage, str]:
        """(the line's message, its path) — the save's outcome, never an exception."""
        if self._drive is None:
            logger.warning("[DeepResearchDriveCopy] no drive on this deployment; not copied for user=%s",
                           user_id[:8])
            return UIMessage.DEEP_RESEARCH_DRIVE_NOT_CONNECTED, ""
        title = report_title_from_html(html.decode("utf-8", errors="replace"))
        filename = deep_research_drive_filename(day, title, query)
        try:
            primary = await self._drive.primary(user_id)
            saved = await self._drive.save(primary.handle, html, filename, REPORT_CONTENT_TYPE, folder)
        except DriveNotConnectedError:
            logger.info("[DeepResearchDriveCopy] drive not connected for user=%s; not copied", user_id[:8])
            return UIMessage.DEEP_RESEARCH_DRIVE_NOT_CONNECTED, ""
        except Exception:
            logger.error("[DeepResearchDriveCopy] save failed for user=%s; not copied", user_id[:8],
                         exc_info=True)
            return UIMessage.DEEP_RESEARCH_DRIVE_COPY_FAILED, ""
        path = saved.item.path or f"{folder}/{saved.item.name}"
        logger.info("[DeepResearchDriveCopy] copied for user=%s (%d bytes, existed=%s)", user_id[:8],
                    len(html), saved.already_existed)
        return UIMessage.DEEP_RESEARCH_DRIVE_COPIED, path

    def _local_day(self, tz_name: str) -> date:
        """Today in the user's timezone (the date in the file name); UTC when the zone is unknown."""
        now = self._now()
        try:
            return now.astimezone(ZoneInfo(tz_name or "UTC")).date()
        except Exception:
            logger.warning("[DeepResearchDriveCopy] unknown timezone %r; using UTC", tz_name)
            return now.astimezone(timezone.utc).date()
