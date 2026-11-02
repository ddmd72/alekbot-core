"""GOOGLE_DRIVE_RFC G4, G8.3, G11, G12 over the account-wide fake and the real `UserDriveService`: a folder outside the
root (a shared chain's top) resolves from its chain top under every scope; the root's listing names the chosen folders
when the root is outside the scope; a name the bot may not create is still readable by path. Invented cast only."""
from dataclasses import replace
from datetime import datetime, timezone

import pytest

from src.domain.drive_access_policy import OUTSIDE_SCOPE_LINE, OUTSIDE_WRITE_FOLDER_LINE
from src.domain.drive_connection import DriveConnection
from src.domain.drive_feed_scope import FeedScope
from src.domain.user_drive import DEFAULT_INBOX_FOLDER, NAME_SLASH, DriveAccessRefused, DrivePathError
from src.services.drive_connection_service import DriveConnectionService
from src.services.user_drive_service import LIST_LIMIT, UserDriveService
from tests.support.drive_connection_repo import FakeDriveConnectionRepository
from tests.support.fake_drive_adapter import FakeDriveAdapter

T0 = datetime(2026, 11, 2, 9, 0, tzinfo=timezone.utc)
OUT_SCOPE = OUTSIDE_SCOPE_LINE.format(drive="Acme")
OUT_WRITE = OUTSIDE_WRITE_FOLDER_LINE.format(drive="Acme")
GARDEN = "Garden plan 2026/27"


class World:
    def __init__(self, **settings):
        self.fake = FakeDriveAdapter("fake_b", feed_scope=FeedScope.ACCOUNT)
        self.repo = FakeDriveConnectionRepository()
        self.connection = DriveConnection(
            connection_id="conn-2", user_id="usr-1", account_id="acc-1", credential_id="cred-2", kind="fake_b",
            drive_identity="fake_b:d2", ref_tag="b2", mount_name="Acme", created_at=T0, **settings)
        self.repo.connections[("usr-1", "conn-2")] = self.connection
        self.handle = self.connection.handle
        self.service = UserDriveService(self.fake, DriveConnectionService(self.repo, None))
        self.root = self.fake.tree(self.handle).root.item_id
        self.rd = self.fake.plant(self.handle, self.root, "R&D", folder=True)
        self.hamburg = self.fake.plant_parentless(self.handle, "Acme Hamburg", folder=True)
        self.q3 = self.fake.plant(self.handle, self.hamburg, "Q3", folder=True)
        self.budget = self.fake.plant(self.handle, self.q3, "Q3 budget", b"lukas weber's share")
        self.fake.calls.clear()

    def settings(self, **changes):
        self.repo.connections[("usr-1", "conn-2")] = replace(self.connection, **changes)

    def names_in(self, folder_id):
        return {n.name for n in self.fake.tree(self.handle).nodes.values() if n.parent_id == folder_id}


async def listed(w, path):
    return [i.name for i in (await w.service.list_folder(w.handle, path)).items]


class TestOuterPathResolves:
    @pytest.mark.parametrize("settings", [
        {"scope_whole_drive": True},
        {"scope_folder_ids": ("hamburg",)},
        {"scope_folder_ids": ("q3",)},
    ], ids=["whole drive", "chosen top", "chosen inner"])
    async def test_a_path_from_the_outer_top_lists(self, settings):
        w = World()
        ids = {"hamburg": w.hamburg, "q3": w.q3}
        w.settings(**{k: tuple(ids[i] for i in v) if k == "scope_folder_ids" else v for k, v in settings.items()})
        assert await listed(w, "Acme Hamburg/Q3") == ["Q3 budget"]

    async def test_a_write_folder_on_the_outer_chain_takes_a_default_save_into_its_inbox(self):
        w = World(scope_whole_drive=True)
        w.settings(scope_whole_drive=True, write_folder_id=w.q3)
        outcome = await w.service.save(w.handle, b"x", "a.txt", "text/plain", None)
        assert outcome.item.path == f"Acme Hamburg/Q3/{DEFAULT_INBOX_FOLDER}/a.txt"
        assert DEFAULT_INBOX_FOLDER in w.names_in(w.q3)

    async def test_a_root_write_folder_refuses_a_save_to_an_outer_path(self):
        w = World(scope_whole_drive=True)
        with pytest.raises(DriveAccessRefused) as caught:
            await w.service.save(w.handle, b"x", "a.txt", "text/plain", "Acme Hamburg/Q3")
        assert caught.value.line == OUT_WRITE
        assert "Q3 budget" in w.names_in(w.q3) and "a.txt" not in w.names_in(w.q3)

    async def test_a_root_child_of_the_same_name_shadows_the_outer_top(self):
        w = World(scope_whole_drive=True)
        near = w.fake.plant(w.handle, w.root, "Acme Hamburg", folder=True)
        w.fake.plant(w.handle, near, "Own.txt", b"o")
        assert await listed(w, "Acme Hamburg") == ["Own.txt"]

    async def test_a_path_naming_neither_is_no_such_folder(self):
        w = World(scope_whole_drive=True)
        with pytest.raises(DrivePathError, match="No folder 'Acme/Acme Lviv' on the drive"):
            await w.service.list_folder(w.handle, "Acme Lviv")

    async def test_a_missing_folder_inside_the_chosen_scope_is_no_such_folder(self):
        w = World()
        w.settings(scope_folder_ids=(w.q3,))
        with pytest.raises(DrivePathError):
            await w.service.list_folder(w.handle, "Acme Hamburg/Q3/Q4")

    async def test_the_outer_top_is_outside_a_scope_chosen_deeper(self):
        w = World()
        w.settings(scope_folder_ids=(w.q3,))
        with pytest.raises(DriveAccessRefused) as caught:
            await w.service.list_folder(w.handle, "Acme Hamburg")
        assert caught.value.line == OUT_SCOPE


class TestScopeTopsListing:
    async def test_the_root_lists_the_chosen_folders_when_it_is_outside_the_scope(self):
        w = World()
        w.settings(scope_folder_ids=(w.q3,))
        out = await w.service.list_folder(w.handle, None)
        assert out.scope_tops is True and out.folder.item_id == w.root
        assert [(i.name, i.path) for i in out.items] == [("Q3", "Acme Hamburg/Q3")]

    async def test_a_chosen_folder_that_is_gone_is_skipped(self):
        w = World()
        w.settings(scope_folder_ids=(w.q3, "gone-1"))
        out = await w.service.list_folder(w.handle, None)
        assert [i.name for i in out.items] == ["Q3"]

    async def test_a_root_child_outside_the_scope_is_still_refused(self):
        w = World()
        w.settings(scope_folder_ids=(w.q3,))
        with pytest.raises(DriveAccessRefused) as caught:
            await w.service.list_folder(w.handle, "R&D")
        assert caught.value.line == OUT_SCOPE

    async def test_a_root_inside_the_scope_lists_as_before(self):
        w = World(scope_whole_drive=True)
        out = await w.service.list_folder(w.handle, None)
        assert out.scope_tops is False and [i.name for i in out.items] == ["R&D"]

    async def test_the_listing_is_bounded_by_the_list_limit(self):
        w = World()
        ids = tuple(w.fake.plant(w.handle, w.root, f"Plans {n:03d}", folder=True) for n in range(LIST_LIMIT + 2))
        w.settings(scope_folder_ids=ids)
        out = await w.service.list_folder(w.handle, None)
        assert len(out.items) == LIST_LIMIT and out.truncated is True


class TestScopeFolderPaths:
    async def test_seven_chosen_folders_name_five_and_two_more(self):
        w = World()
        ids = tuple(w.fake.plant(w.handle, w.root, f"Clients: {2020 + n}", folder=True) for n in range(7))
        w.settings(scope_folder_ids=ids)
        paths, more = await w.service.scope_folder_paths(w.handle)
        assert paths == [f"Clients: {2020 + n}" for n in range(5)] and more == 2

    async def test_a_limit_is_honoured(self):
        w = World()
        w.settings(scope_folder_ids=(w.q3, w.rd))
        assert await w.service.scope_folder_paths(w.handle, limit=1) == (["Acme Hamburg/Q3"], 1)

    async def test_whole_drive_and_no_choice_name_nothing(self):
        w = World(scope_whole_drive=True)
        assert await w.service.scope_folder_paths(w.handle) == ([], 0)
        w.settings(scope_whole_drive=False, scope_folder_ids=())
        assert await w.service.scope_folder_paths(w.handle) == ([], 0)

    async def test_a_folder_that_cannot_be_read_is_skipped(self):
        w = World()
        w.settings(scope_folder_ids=(w.q3, "gone-1"))
        assert await w.service.scope_folder_paths(w.handle) == (["Acme Hamburg/Q3"], 0)


class TestNamesTheBotMayNotCreate:
    """G12: only a segment the bot would create is validated."""

    async def test_a_root_folder_with_a_forbidden_character_resolves_lists_and_takes_a_save(self):
        w = World(scope_whole_drive=True)
        plans = w.fake.plant(w.handle, w.root, "Plans: 2026?", folder=True)
        w.fake.plant(w.handle, plans, "Beds.docx", b"b")
        assert await listed(w, "Plans: 2026?") == ["Beds.docx"]
        outcome = await w.service.save(w.handle, b"x", "a.txt", "text/plain", "Plans: 2026?")
        assert outcome.item.name == "a.txt" and "a.txt" in w.names_in(plans)

    async def test_creating_a_folder_with_a_forbidden_character_raises(self):
        w = World(scope_whole_drive=True)
        with pytest.raises(DrivePathError):
            await w.service.save(w.handle, b"x", "a.txt", "text/plain", "Plans: 2027?")
        assert "Plans: 2027?" not in w.names_in(w.root)


class TestSlashInsideAName:
    async def test_a_printed_path_resolves_back_to_the_folder(self):
        w = World(scope_whole_drive=True)
        garden = w.fake.plant(w.handle, w.root, GARDEN, folder=True)
        w.fake.plant(w.handle, garden, "Beds.docx", b"b")
        out = await w.service.list_folder(w.handle, f"Garden plan 2026{NAME_SLASH}27")
        assert out.folder.item_id == garden and [i.name for i in out.items] == ["Beds.docx"]
        assert out.folder.path == f"Garden plan 2026{NAME_SLASH}27"


class TestPickerOuterFolders:
    async def test_it_returns_the_outer_tops_only(self):
        w = World()
        w.fake.plant_parentless(w.handle, "Orphan.txt", b"o")
        w.fake.plant_parentless(w.handle, "Budget Archive", folder=True)
        got = await w.service.picker_outer_folders(w.handle)
        assert [f.name for f in got] == ["Acme Hamburg", "Budget Archive"]
        assert "R&D" not in [f.name for f in got] and "Q3" not in [f.name for f in got]

    async def test_a_forbidden_name_below_a_missing_folder_creates_nothing(self):
        w = World(scope_whole_drive=True)
        with pytest.raises(DrivePathError):
            await w.service.save(w.handle, b"x", "a.txt", "text/plain", "Clients: 2026/Plans: 2027?")
        assert "Clients: 2026" not in w.names_in(w.root)
