"""The deep-research copy under the primary's write folder (MULTI_DRIVE_RFC §11.5, D7; slice 2b P-B): the report folder is
created inside the write folder, a primary with no write folder reports the failed line (logged), no primary the
not-connected line. Real `UserDriveService` over `FakeDriveAdapter`."""
from dataclasses import replace
from datetime import datetime, timezone
from unittest.mock import AsyncMock

from src.adapters.file_localization_adapter import FileLocalizationAdapter
from src.domain.deep_research_drive_copy import report_title_from_html
from src.domain.drive_connection import DriveConnection
from src.domain.language import LanguageCode
from src.domain.ui_messages import UIMessage
from src.domain.user import UserBotConfig, UserProfile
from src.domain.drive_user_settings import DriveUserSettings
from src.ports.language_service_port import LanguageServicePort
from src.ports.user_repository import UserRepository
from src.services.deep_research_drive_copy_service import DeepResearchDriveCopyService
from src.services.drive_connection_service import DriveConnectionService
from src.services.localization_service import LocalizationService
from src.services.user_drive_service import UserDriveService
from tests.support.drive_connection_repo import FakeDriveConnectionRepository
from tests.support.fake_drive_adapter import FakeDriveAdapter

HTML = b"<html><head><title>Coimbra 2024 report</title></head><body>x</body></html>"
NOW = datetime(2026, 11, 2, 9, 30, tzinfo=timezone.utc)
L10N = LocalizationService(FileLocalizationAdapter())
FOLDER = L10N.get_ui_string(LanguageCode.EN, UIMessage.DEEP_RESEARCH_DRIVE_FOLDER)


class World:
    def __init__(self, primary=True):
        self.fake = FakeDriveAdapter("fake_b")
        self.repo = FakeDriveConnectionRepository()
        self.conn = DriveConnection(connection_id="conn-2", user_id="usr-1", account_id="acc-1", credential_id="c2",
                                    kind="fake_b", drive_identity="fake_b:d2", ref_tag="b2", mount_name="Acme",
                                    created_at=NOW)
        self.repo.connections[("usr-1", "conn-2")] = self.conn
        if primary:
            self.repo.settings["usr-1"] = DriveUserSettings(user_id="usr-1", primary_connection_id="conn-2")
        self.root = self.fake.tree(self.conn.handle).root.item_id
        self.rd = self.fake.plant(self.conn.handle, self.root, "R&D", folder=True)
        users = AsyncMock(spec=UserRepository)
        users.get_user.return_value = UserProfile(user_id="usr-1", account_id="acc-1",
                                                  config=UserBotConfig(drive_copy_deep_research=True, timezone="UTC"))
        language = AsyncMock(spec=LanguageServicePort)
        language.resolve_ui_language.return_value = LanguageCode.EN
        drive = UserDriveService(self.fake, DriveConnectionService(self.repo, None))
        self.service = DeepResearchDriveCopyService(users=users, drive=drive, localization=L10N, language=language,
                                                    now=lambda: NOW)

    def settings(self, **changes):
        self.repo.connections[("usr-1", "conn-2")] = replace(self.conn, **changes)

    def paths(self):
        tree = self.fake.tree(self.conn.handle)
        return {tree.path(n) for n in tree.nodes.values()}


def line(message, **fmt):
    return L10N.get_ui_string(LanguageCode.EN, message).format(**fmt)


async def test_the_report_folder_is_created_inside_the_write_folder():
    w = World()
    w.settings(write_folder_id=w.rd)
    out = await w.service.copy_report("usr-1", HTML, "q")
    title = report_title_from_html(HTML.decode())
    assert out == line(UIMessage.DEEP_RESEARCH_DRIVE_COPIED, path=f"Acme/R&D/{FOLDER}/2026-11-02 {title}.html")
    assert f"R&D/{FOLDER}" in w.paths()


async def test_the_root_write_folder_keeps_the_report_folder_at_the_top():
    w = World()
    out = await w.service.copy_report("usr-1", HTML, "q")
    assert out is not None and "R&D" not in out
    assert FOLDER in w.paths()


async def test_a_primary_without_a_write_folder_reports_failed_and_creates_nothing():
    w = World()
    w.settings(write_folder_id=None)
    before = w.paths()
    assert await w.service.copy_report("usr-1", HTML, "q") == line(UIMessage.DEEP_RESEARCH_DRIVE_COPY_FAILED)
    assert w.paths() == before


async def test_no_primary_is_not_connected():
    w = World(primary=False)
    assert await w.service.copy_report("usr-1", HTML, "q") == line(UIMessage.DEEP_RESEARCH_DRIVE_NOT_CONNECTED)
