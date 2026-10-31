"""DeepResearchDriveCopyService — the opt-in copy of a deep research HTML report to the user's drive
(decisions/deep_research_drive_copy.md).

Contract: toggle off (or unreadable) → nothing, no line. Toggle on → one save into the per-language folder
under `YYYY-MM-DD <title>.html`, and one short localized line for the chat: copied (with the path), not
connected, or failed. Never raises.
"""
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.adapters.file_localization_adapter import FileLocalizationAdapter
from src.domain.language import LanguageCode
from src.domain.ui_messages import UIMessage
from src.domain.user import UserBotConfig, UserProfile
from src.domain.user_drive import (
    DriveItem,
    DriveNotConnectedError,
    DriveReauthRequired,
    SaveOutcome,
    sanitize_drive_filename,
)
from src.ports.language_service_port import LanguageServicePort
from src.ports.user_repository import UserRepository
from src.services.deep_research_drive_copy_service import DeepResearchDriveCopyService
from src.services.localization_service import LocalizationService
from src.services.user_drive_service import UserDriveService
from tests.support.drive_handles import drive_handle

USER = "user-1"
HANDLE = drive_handle(USER)
HTML = b"<!DOCTYPE html><html><head><title>Heat pumps in Springfield</title></head><body>x</body></html>"
NOW = datetime(2026, 10, 14, 9, 30, tzinfo=timezone.utc)
L10N = LocalizationService(FileLocalizationAdapter())


def _users(enabled=True, tz="UTC", missing=False):
    users = AsyncMock(spec=UserRepository)
    profile = UserProfile(user_id=USER, account_id="acc",
                          config=UserBotConfig(drive_copy_deep_research=enabled, timezone=tz))
    users.get_user.return_value = None if missing else profile
    return users


def _drive(path="Deep research/2026-10-14 Heat pumps in Springfield.html"):
    drive = AsyncMock(spec=UserDriveService)
    drive.primary.return_value = MagicMock(handle=HANDLE)      # the user's primary connection (MULTI_DRIVE_RFC D7)
    item = DriveItem(item_id="i1", name=path.rsplit("/", 1)[-1], path=path, is_folder=False)
    drive.save.return_value = SaveOutcome(item=item, created=[])
    return drive


def _language(lang=LanguageCode.EN):
    language = AsyncMock(spec=LanguageServicePort)
    language.resolve_ui_language.return_value = lang
    return language


def _service(users=None, drive=None, language=None, now=NOW, no_drive=False):
    return DeepResearchDriveCopyService(
        users=users or _users(), drive=None if no_drive else (drive or _drive()),
        localization=L10N, language=language or _language(), now=lambda: now,
    )


class TestConsent:
    async def test_toggle_off_copies_nothing_and_says_nothing(self):
        drive = _drive()
        assert await _service(users=_users(enabled=False), drive=drive).copy_report(USER, HTML, "q") is None
        drive.save.assert_not_called()

    async def test_unknown_user_copies_nothing(self):
        drive = _drive()
        assert await _service(users=_users(missing=True), drive=drive).copy_report(USER, HTML, "q") is None
        drive.save.assert_not_called()

    async def test_unreadable_toggle_copies_nothing(self):
        users = _users()
        users.get_user.side_effect = RuntimeError("firestore down")
        drive = _drive()
        assert await _service(users=users, drive=drive).copy_report(USER, HTML, "q") is None
        drive.save.assert_not_called()


class TestCopy:
    async def test_saves_the_html_once_in_the_deep_research_folder(self):
        drive = _drive()
        line = await _service(drive=drive).copy_report(USER, HTML, "heat pumps?")
        drive.save.assert_awaited_once_with(
            HANDLE, HTML, "2026-10-14 Heat pumps in Springfield.html", "text/html; charset=utf-8", "Deep research")
        assert line == "📁 Copied to your drive: Deep research/2026-10-14 Heat pumps in Springfield.html"

    async def test_the_line_shows_the_name_the_drive_actually_gave(self):
        drive = _drive(path="Deep research/2026-10-14 Heat pumps in Springfield 1.html")
        line = await _service(drive=drive).copy_report(USER, HTML, "q")
        assert line.endswith("Deep research/2026-10-14 Heat pumps in Springfield 1.html")

    async def test_no_title_names_the_file_after_the_question(self):
        drive = _drive()
        await _service(drive=drive).copy_report(USER, b"<html><body>no title</body></html>", "Best e-bikes")
        assert drive.save.call_args.args[2] == "2026-10-14 Best e-bikes.html"

    async def test_cyrillic_title(self):
        drive = _drive()
        html = "<title>Ринок оренди у Спрінгфілді</title>".encode("utf-8")
        await _service(drive=drive).copy_report(USER, html, "q")
        assert drive.save.call_args.args[2] == "2026-10-14 Ринок оренди у Спрінгфілді.html"

    async def test_the_date_is_the_users_local_day(self):
        drive = _drive()
        late = datetime(2026, 10, 13, 23, 30, tzinfo=timezone.utc)      # already the 14th in Europe/Madrid
        await _service(users=_users(tz="Europe/Madrid"), drive=drive, now=late).copy_report(USER, HTML, "q")
        assert drive.save.call_args.args[2].startswith("2026-10-14 ")

    async def test_an_unknown_timezone_falls_back_to_utc(self):
        drive = _drive()
        late = datetime(2026, 10, 13, 23, 30, tzinfo=timezone.utc)
        await _service(users=_users(tz="Mars/Olympus"), drive=drive, now=late).copy_report(USER, HTML, "q")
        assert drive.save.call_args.args[2].startswith("2026-10-13 ")


class TestFolderPerLanguage:
    @pytest.mark.parametrize("lang, folder", [
        (LanguageCode.EN, "Deep research"),
        (LanguageCode.UK, "Глибокі дослідження"),
        (LanguageCode.FR, "Recherches approfondies"),
        (LanguageCode.ES, "Investigaciones"),
    ])
    async def test_folder_name_follows_the_ui_language(self, lang, folder):
        drive = _drive()
        await _service(drive=drive, language=_language(lang)).copy_report(USER, HTML, "q")
        assert drive.save.call_args.args[4] == folder

    @pytest.mark.parametrize("lang", list(LanguageCode))
    def test_every_folder_name_is_a_valid_drive_name(self, lang):
        folder = L10N.get_ui_string(lang, UIMessage.DEEP_RESEARCH_DRIVE_FOLDER)
        assert folder and sanitize_drive_filename(folder) == folder and "/" not in folder

    async def test_language_lookup_failure_falls_back_to_english(self):
        language = _language()
        language.resolve_ui_language.side_effect = RuntimeError("boom")
        drive = _drive()
        await _service(drive=drive, language=language).copy_report(USER, HTML, "q")
        assert drive.save.call_args.args[4] == "Deep research"


class TestNotCopied:
    @pytest.mark.parametrize("exc", [DriveNotConnectedError("no creds"), DriveReauthRequired("expired")])
    async def test_not_connected(self, exc):
        drive = _drive()
        drive.save.side_effect = exc
        line = await _service(drive=drive).copy_report(USER, HTML, "q")
        assert line == "Not copied to your drive: the drive is not connected."

    async def test_no_drive_on_this_deployment_reads_as_not_connected(self):
        line = await _service(no_drive=True).copy_report(USER, HTML, "q")
        assert line == L10N.get_ui_string(LanguageCode.EN, UIMessage.DEEP_RESEARCH_DRIVE_NOT_CONNECTED)

    async def test_any_other_failure(self):
        drive = _drive()
        drive.save.side_effect = RuntimeError("graph 500")
        line = await _service(drive=drive).copy_report(USER, HTML, "q")
        assert line == "Not copied to your drive: saving failed."

    async def test_no_retry(self):
        drive = _drive()
        drive.save.side_effect = RuntimeError("graph 500")
        await _service(drive=drive).copy_report(USER, HTML, "q")
        assert drive.save.await_count == 1

    async def test_lines_are_localized(self):
        drive = _drive()
        drive.save.side_effect = DriveNotConnectedError("x")
        line = await _service(drive=drive, language=_language(LanguageCode.UK)).copy_report(USER, HTML, "q")
        assert line == L10N.get_ui_string(LanguageCode.UK, UIMessage.DEEP_RESEARCH_DRIVE_NOT_CONNECTED)
        assert line != L10N.get_ui_string(LanguageCode.EN, UIMessage.DEEP_RESEARCH_DRIVE_NOT_CONNECTED)

    async def test_never_raises_even_when_localization_breaks(self):
        broken = MagicMock(spec=LocalizationService)
        broken.get_ui_string.side_effect = KeyError("missing")
        service = DeepResearchDriveCopyService(users=_users(), drive=_drive(), localization=broken,
                                               language=_language(), now=lambda: NOW)
        assert await service.copy_report(USER, HTML, "q") is None
