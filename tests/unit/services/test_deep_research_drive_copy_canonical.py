"""The chat line of a deep research copy names the canonical path: the primary's mount name first (MULTI_DRIVE_RFC
§11.1, slice 2a S-16), then the path the drive actually gave the file. Since VOICE_CALL_LIVE_TEXT_RFC §4.11 that path is
built inside `UserDriveService.save_to_primary`, so these run the real service over `FakeDriveAdapter`, the root as
the write folder (the report folder stays at the top)."""
from datetime import datetime, timezone
from unittest.mock import AsyncMock

from src.adapters.file_localization_adapter import FileLocalizationAdapter
from src.domain.drive_connection import DriveConnection
from src.domain.drive_user_settings import DriveUserSettings
from src.domain.language import LanguageCode
from src.domain.user import UserBotConfig, UserProfile
from src.ports.language_service_port import LanguageServicePort
from src.ports.user_repository import UserRepository
from src.services.deep_research_drive_copy_service import DeepResearchDriveCopyService
from src.services.drive_connection_service import DriveConnectionService
from src.services.localization_service import LocalizationService
from src.services.user_drive_service import UserDriveService
from tests.support.drive_connection_repo import FakeDriveConnectionRepository
from tests.support.fake_drive_adapter import FakeDriveAdapter

USER = "user-1"
HTML = b"<html><head><title>Heat pumps in Springfield</title></head><body>x</body></html>"
NOW = datetime(2026, 10, 14, 9, 30, tzinfo=timezone.utc)


class World:
    def __init__(self, mount_name: str = "Drive"):
        self.fake = FakeDriveAdapter("fake_b")
        repo = FakeDriveConnectionRepository()
        self.conn = DriveConnection(connection_id="conn-1", user_id=USER, account_id="acc", credential_id="c1",
                                    kind="fake_b", drive_identity="fake_b:d1", ref_tag="b1", mount_name=mount_name,
                                    created_at=NOW)
        repo.connections[(USER, "conn-1")] = self.conn
        repo.settings[USER] = DriveUserSettings(user_id=USER, primary_connection_id="conn-1")
        users = AsyncMock(spec=UserRepository)
        users.get_user.return_value = UserProfile(user_id=USER, account_id="acc",
                                                  config=UserBotConfig(drive_copy_deep_research=True, timezone="UTC"))
        language = AsyncMock(spec=LanguageServicePort)
        language.resolve_ui_language.return_value = LanguageCode.EN
        drive = UserDriveService(self.fake, DriveConnectionService(repo, None))
        self.service = DeepResearchDriveCopyService(users=users, drive=drive, localization=LocalizationService(
            FileLocalizationAdapter()), language=language, now=lambda: NOW)


async def test_the_line_starts_with_the_primarys_mount_name():
    path = "Deep research/2026-10-14 Heat pumps in Springfield.html"
    line = await World("Drive").service.copy_report(USER, HTML, "q")
    assert line == f"📁 Copied to your drive: Drive/{path}"


async def test_a_renamed_primary_prints_its_current_name():
    path = "Deep research/2026-10-14 Heat pumps in Springfield.html"
    line = await World("Acme Hamburg").service.copy_report(USER, HTML, "q")
    assert line.endswith(f"Acme Hamburg/{path}")


async def test_the_line_shows_the_name_the_drive_actually_gave():
    # Moved from test_deep_research_drive_copy_service.py: the drive's own name now reaches the line through
    # save_to_primary, so a real clash (same name, other bytes) makes the drive suffix it.
    w = World()
    tree = w.fake.tree(w.conn.handle)
    folder = w.fake.plant(w.conn.handle, tree.root.item_id, "Deep research", folder=True)
    w.fake.plant(w.conn.handle, folder, "2026-10-14 Heat pumps in Springfield.html", b"an older report")
    line = await w.service.copy_report(USER, HTML, "q")
    assert line.endswith("Deep research/2026-10-14 Heat pumps in Springfield 1.html")
