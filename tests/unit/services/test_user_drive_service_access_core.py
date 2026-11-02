"""The access-policy reads of `UserDriveService` (MULTI_DRIVE_RFC §11.3, design §1.9, E-3, E-13): `access` (a fresh
connection read), `ancestry` (memo, depth cap, a parentless item), `check_access` (the App Folder default walks no
chain: no `get_change`; a refusal carries the policy's line), `write_folder_path`, `picker_folders` (folders outside the
scope too) and `top_folder_names`. Over `FakeDriveAdapter` and the in-memory connection repository."""
from dataclasses import replace
from datetime import datetime, timezone

import pytest

from src.domain.drive_access_op import AccessOp
from src.domain.drive_access_policy import (
    ITEM_FORBIDS, NO_WRITE_FOLDER_LINE, OUTSIDE_SCOPE_LINE, OUTSIDE_WRITE_FOLDER_LINE,
)
from src.domain.drive_ancestry import Ancestry
from src.domain.drive_connection import ROOT_FOLDER, DriveConnection
from src.domain.drive_connection_state import ConnectionState
from src.domain.drive_feed_scope import FeedScope
from src.domain.drive_item_access import ItemAccess
from src.domain.drive_search import MAX_CHAIN_DEPTH
from src.domain.user_drive import DriveAccessRefused, DriveItemNotFoundError, DriveNotConnectedError
from src.services.drive_connection_service import DriveConnectionService
from src.services.user_drive_service import UserDriveService
from tests.support.drive_connection_repo import FakeDriveConnectionRepository
from tests.support.fake_drive_adapter import FakeDriveAdapter

T0 = datetime(2026, 11, 2, 9, 0, tzinfo=timezone.utc)


class World:
    def __init__(self, *, feed_scope=FeedScope.AREA, **settings):
        self.fake = FakeDriveAdapter("fake_b", feed_scope=feed_scope)
        self.repo = FakeDriveConnectionRepository()
        self.connection = DriveConnection(
            connection_id="conn-2", user_id="usr-1", account_id="acc-1", credential_id="cred-2", kind="fake_b",
            drive_identity="fake_b:d2", ref_tag="b2", mount_name="Acme", created_at=T0, **settings)
        self.repo.connections[("usr-1", "conn-2")] = self.connection
        self.handle = self.connection.handle
        self.service = UserDriveService(self.fake, DriveConnectionService(self.repo, None))
        self.root = self.fake.tree(self.handle).root.item_id
        self.rd = self.fake.plant(self.handle, self.root, "R&D", folder=True)
        self.plan = self.fake.plant(self.handle, self.rd, "Tom's plan.docx", b"plan")
        self.coimbra = self.fake.plant(self.handle, self.root, "Coimbra 2024", folder=True)

    def settings(self, **changes):
        self.repo.connections[("usr-1", "conn-2")] = replace(self.connection, **changes)

    def calls(self, method):
        return [m for m, _ in self.fake.calls if m == method]


class TestAccess:
    async def test_reads_the_connection_fresh(self):
        w = World()
        access, caps = await w.service.access(w.handle)
        assert (access.drive_name, access.root_item_id, access.write_folder_id) == ("Acme", w.root, ROOT_FOLDER)
        assert caps.feed_scope is FeedScope.AREA
        w.settings(write_folder_id=None, scope_folder_ids=(w.rd,))
        access, _ = await w.service.access(w.handle)
        assert access.write_folder_id is None and access.scope_folder_ids == frozenset({w.rd})

    async def test_a_disconnected_or_absent_connection(self):
        w = World()
        w.settings(state=ConnectionState.DISCONNECTED, disconnected_at=T0)
        with pytest.raises(DriveNotConnectedError):
            await w.service.access(w.handle)
        w.repo.connections.clear()
        with pytest.raises(DriveNotConnectedError):
            await w.service.access(w.handle)


class TestAncestry:
    async def test_the_chain_to_the_root(self):
        w = World()
        chain = await w.service.ancestry(w.handle, w.plan)
        assert chain == Ancestry((w.plan, w.rd, w.root), reached_root=True)

    async def test_the_root_itself(self):
        w = World()
        assert await w.service.ancestry(w.handle, w.root) == Ancestry((w.root,), reached_root=True)

    async def test_the_memo_is_shared_within_a_call(self):
        w = World()
        other = w.fake.plant(w.handle, w.rd, "lab-report.pdf", b"lab")
        memo = {}
        await w.service.ancestry(w.handle, w.plan, memo=memo)
        before = len(w.calls("get_change"))
        chain = await w.service.ancestry(w.handle, other, memo=memo)
        assert chain.ids == (other, w.rd, w.root)
        assert len(w.calls("get_change")) == before + 1        # only the new item; R&D's parent came from the memo

    async def test_a_parentless_item_does_not_reach_the_root(self):
        w = World(feed_scope=FeedScope.ACCOUNT)
        shared = w.fake.plant_parentless(w.handle, "lab-report.pdf", b"lab")
        assert await w.service.ancestry(w.handle, shared) == Ancestry((shared,), reached_root=False)

    async def test_a_deep_chain_is_cut_at_the_cap(self):
        w = World()
        parent = w.root
        for depth in range(MAX_CHAIN_DEPTH + 5):
            parent = w.fake.plant(w.handle, parent, f"Inbox {depth}", folder=True)
        chain = await w.service.ancestry(w.handle, parent)
        assert len(chain.ids) == MAX_CHAIN_DEPTH and chain.reached_root is False

    async def test_a_parent_closed_to_the_bot_ends_the_chain_unreached(self):
        w = World()
        w.fake.close(w.handle, w.rd)
        assert await w.service.ancestry(w.handle, w.plan) == Ancestry((w.plan, w.rd), reached_root=False)

    async def test_a_missing_item_is_not_found(self):
        w = World()
        with pytest.raises(DriveItemNotFoundError):
            await w.service.ancestry(w.handle, "item-missing")


class TestCheckAccess:
    async def test_the_app_folder_default_walks_no_chain(self):
        w = World()
        item = await w.fake.get_item(w.handle, w.plan)
        for op in AccessOp:
            await w.service.check_access(w.handle, op, item=item)
        assert w.calls("get_change") == []

    async def test_a_read_outside_the_chosen_folders_is_refused(self):
        w = World()
        w.settings(scope_folder_ids=(w.rd,), write_folder_id=w.rd)
        coimbra = await w.fake.get_item(w.handle, w.coimbra)
        with pytest.raises(DriveAccessRefused) as refused:
            await w.service.check_access(w.handle, AccessOp.LIST, item=coimbra)
        assert refused.value.line == OUTSIDE_SCOPE_LINE.format(drive="Acme")
        await w.service.check_access(w.handle, AccessOp.OPEN, item_id=w.plan)

    async def test_a_write_outside_the_write_folder_and_a_move_out_of_it(self):
        w = World()
        w.settings(scope_whole_drive=True, write_folder_id=w.rd)
        with pytest.raises(DriveAccessRefused) as refused:
            await w.service.check_access(w.handle, AccessOp.CREATE, item_id=w.coimbra)
        assert refused.value.line == OUTSIDE_WRITE_FOLDER_LINE.format(drive="Acme")
        coimbra = await w.fake.get_item(w.handle, w.coimbra)
        with pytest.raises(DriveAccessRefused):
            await w.service.check_access(w.handle, AccessOp.MOVE, item_id=w.plan, destination_item=coimbra)
        await w.service.check_access(w.handle, AccessOp.REPLACE, item_id=w.plan)

    async def test_a_read_only_drive(self):
        w = World(write_folder_id=None)
        with pytest.raises(DriveAccessRefused) as refused:
            await w.service.check_access(w.handle, AccessOp.SAVE, item_id=w.rd)
        assert refused.value.line == NO_WRITE_FOLDER_LINE

    async def test_the_items_own_access(self):
        w = World()
        w.fake.set_access(w.handle, w.plan, ItemAccess(can_delete=False))
        item = await w.fake.get_item(w.handle, w.plan)
        with pytest.raises(DriveAccessRefused) as refused:
            await w.service.check_access(w.handle, AccessOp.DELETE, item=item)
        assert refused.value.line == ITEM_FORBIDS[AccessOp.DELETE]

    async def test_a_given_chain_is_used_as_it_is(self):
        w = World()
        w.settings(scope_whole_drive=True, write_folder_id=w.rd)
        chain = Ancestry((w.plan, w.rd, w.root), reached_root=True)
        await w.service.check_access(w.handle, AccessOp.APPEND, item_id=w.plan, chain=chain)
        assert w.calls("get_change") == []


class TestCabinetReads:
    async def test_write_folder_path(self):
        w = World()
        assert await w.service.write_folder_path(w.handle) == ""
        w.settings(write_folder_id=None)
        assert await w.service.write_folder_path(w.handle) is None
        w.settings(write_folder_id=w.rd)
        assert await w.service.write_folder_path(w.handle) == "R&D"

    async def test_picker_folders_lists_folders_outside_the_scope(self):
        w = World()
        w.settings(scope_folder_ids=(w.rd,), write_folder_id=w.rd)
        w.fake.plant(w.handle, w.root, "birth-certificate.pdf", b"cert")
        top = await w.service.picker_folders(w.handle, None)
        assert [f.name for f in top] == ["Coimbra 2024", "R&D"]
        assert await w.service.picker_folders(w.handle, w.rd) == []

    async def test_top_folder_names(self):
        w = World()
        assert await w.service.top_folder_names(w.handle) == ["Coimbra 2024", "R&D"]
