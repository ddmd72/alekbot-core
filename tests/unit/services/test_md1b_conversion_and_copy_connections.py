"""FileConversionService and DeepResearchDriveCopyService by connection (MULTI_DRIVE_RFC §9, §11.5, D7; slice 1b part D):
a drive ref's tag is resolved to its connection's handle before any drive call, a ref of no live connection raises its
code-built line, and the deep-research copy lands on the primary (none: "not connected", nothing written)."""
from datetime import datetime, timezone
from unittest.mock import AsyncMock

import pytest

from src.adapters.file_localization_adapter import FileLocalizationAdapter
from src.domain.language import LanguageCode
from src.domain.ui_messages import UIMessage
from src.domain.drive_ref_refusal import stale_ref_line
from src.domain.user import UserBotConfig, UserProfile
from src.domain.user_drive import DriveRefRefused
from src.ports.file_storage_port import FileStoragePort
from src.ports.language_service_port import LanguageServicePort
from src.ports.user_repository import UserRepository
from src.services.deep_research_drive_copy_service import DeepResearchDriveCopyService
from src.services.file_conversion_service import FileConversionService
from src.services.localization_service import LocalizationService
from src.services.user_drive_service import UserDriveService
from tests.support.drive_connection_repo import FakeDriveConnectionRepository
from tests.support.drive_connections import connection_service, seed
from tests.support.fake_drive_adapter import FakeDriveAdapter

T1 = datetime(2026, 1, 2, tzinfo=timezone.utc)
NOW = datetime(2026, 10, 14, 9, 30, tzinfo=timezone.utc)


@pytest.fixture
async def world():
    class World:
        pass
    w = World()
    w.repo = FakeDriveConnectionRepository()
    w.drive = FakeDriveAdapter("fake_a")
    w.connections = connection_service(w.repo)
    w.one = await seed(w.repo, connection_id="conn-1", tag="a1")
    w.two = await seed(w.repo, connection_id="conn-2", tag="b2", kind="fake_b", primary=False, created_at=T1)
    assert w.drive.plant(w.one.handle, "root", "one.txt", b"from one") == "item-1"
    assert w.drive.plant(w.two.handle, "root", "two.txt", b"from two") == "item-1"   # the same item id on purpose (T-4)
    w.conversion = FileConversionService(storage=AsyncMock(spec=FileStoragePort), drive=w.drive,
                                         connections=w.connections)
    return w


class TestConversionResolvesTheTag:
    async def test_bytes_and_metadata_come_from_the_tags_connection(self, world):
        assert await world.conversion.resolve_bytes("drive:b2:item-1", "usr-1") == b"from two"
        assert (await world.conversion.get_drive_item("ref=drive:a1:item-1]", "usr-1")).name == "one.txt"
        assert "from two" in await world.conversion.resolve_content("drive:b2:item-1", "usr-1")

    async def test_a_stale_ref_raises_its_line(self, world):
        with pytest.raises(DriveRefRefused) as refused:
            await world.conversion.resolve_bytes("drive:item-1", "usr-1")
        assert refused.value.line == stale_ref_line("drive:item-1")

    async def test_another_users_tag_is_stale(self, world):
        with pytest.raises(DriveRefRefused):
            await world.conversion.get_drive_item("drive:a1:item-1", "usr-2")

    async def test_without_connections_drive_refs_are_unavailable(self, world):
        bare = FileConversionService(storage=AsyncMock(spec=FileStoragePort), drive=world.drive)
        with pytest.raises(FileNotFoundError):
            await bare.get_drive_item("drive:a1:item-1", "usr-1")


def _names(world, connection):
    return {node.name for node in world.drive.tree(connection.handle).nodes.values()}


def _copy_service(world) -> DeepResearchDriveCopyService:
    users = AsyncMock(spec=UserRepository)
    users.get_user.side_effect = lambda user_id: UserProfile(
        user_id=user_id, account_id="acc-1", config=UserBotConfig(drive_copy_deep_research=True, timezone="UTC"))
    language = AsyncMock(spec=LanguageServicePort)
    language.resolve_ui_language.return_value = LanguageCode.EN
    return DeepResearchDriveCopyService(users=users, drive=UserDriveService(world.drive, world.connections),
                                        localization=LocalizationService(FileLocalizationAdapter()),
                                        language=language, now=lambda: NOW)


class TestDeepResearchCopyGoesToThePrimary:
    async def test_the_copy_lands_on_the_primary_only(self, world):
        line = await _copy_service(world).copy_report("usr-1", b"<html><title>Plan</title></html>", "plan")
        assert line is not None and "2026-10-14" in line
        assert any(name.endswith(".html") for name in _names(world, world.one))
        assert not any(name.endswith(".html") for name in _names(world, world.two))

    async def test_no_primary_is_not_connected_and_writes_nothing(self, world):
        line = await _copy_service(world).copy_report("usr-2", b"<html><title>Plan</title></html>", "plan")
        assert line == LocalizationService(FileLocalizationAdapter()).get_ui_string(
            LanguageCode.EN, UIMessage.DEEP_RESEARCH_DRIVE_NOT_CONNECTED)
        assert not any(node.name.endswith(".html")                     # on any connection's tree the fake holds
                       for tree in world.drive._trees.values() for node in tree.nodes.values())
