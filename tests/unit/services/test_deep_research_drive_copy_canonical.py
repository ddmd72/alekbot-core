"""The chat line of a deep research copy names the canonical path: the primary's mount name first (MULTI_DRIVE_RFC
§11.1, slice 2a S-16); the save itself still gets the drive-relative folder."""
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

from src.adapters.file_localization_adapter import FileLocalizationAdapter
from src.domain.language import LanguageCode
from src.domain.user import UserBotConfig, UserProfile
from src.domain.user_drive import DriveItem, SaveOutcome
from src.ports.language_service_port import LanguageServicePort
from src.ports.user_repository import UserRepository
from src.services.deep_research_drive_copy_service import DeepResearchDriveCopyService
from src.services.localization_service import LocalizationService
from src.services.user_drive_service import UserDriveService
from tests.support.drive_handles import drive_handle

USER = "user-1"
HANDLE = drive_handle(USER)
HTML = b"<html><head><title>Heat pumps in Springfield</title></head><body>x</body></html>"
NOW = datetime(2026, 10, 14, 9, 30, tzinfo=timezone.utc)


def _service(mount_name: str, path: str) -> DeepResearchDriveCopyService:
    users = AsyncMock(spec=UserRepository)
    users.get_user.return_value = UserProfile(user_id=USER, account_id="acc",
                                              config=UserBotConfig(drive_copy_deep_research=True, timezone="UTC"))
    drive = AsyncMock(spec=UserDriveService)
    drive.primary.return_value = MagicMock(handle=HANDLE, mount_name=mount_name)
    drive.save.return_value = SaveOutcome(item=DriveItem(item_id="i1", name=path.rsplit("/", 1)[-1], path=path,
                                                         is_folder=False), created=[])
    language = AsyncMock(spec=LanguageServicePort)
    language.resolve_ui_language.return_value = LanguageCode.EN
    return DeepResearchDriveCopyService(users=users, drive=drive, localization=LocalizationService(
        FileLocalizationAdapter()), language=language, now=lambda: NOW)


async def test_the_line_starts_with_the_primarys_mount_name():
    path = "Deep research/2026-10-14 Heat pumps in Springfield.html"
    line = await _service("Drive", path).copy_report(USER, HTML, "q")
    assert line == f"📁 Copied to your drive: Drive/{path}"


async def test_a_renamed_primary_prints_its_current_name():
    path = "Deep research/2026-10-14 Heat pumps in Springfield.html"
    line = await _service("Acme Hamburg", path).copy_report(USER, HTML, "q")
    assert line.endswith(f"Acme Hamburg/{path}")
