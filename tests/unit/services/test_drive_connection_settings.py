"""The connection settings in `DriveConnectionService` (MULTI_DRIVE_RFC §11.3, §11.7, D4, D11, I2; design §1.6, E-17;
review N8): a new connection is read-only and not primary; its first write folder makes it the primary when the user has
none; `set_primary` refuses a read-only connection; `set_scope` refuses both or a scope that leaves the write folder and
drops nested folders; `set_write_folder` refuses a folder outside the scope and read-only on the primary; `disconnect`
hands the primary to the named heir (validated) or clears it."""
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from itertools import count

import pytest

from src.domain.drive_access_policy import (
    SCOPE_BOTH_LINE, SCOPE_LEAVES_WRITE_FOLDER_LINE, WRITE_FOLDER_OUTSIDE_SCOPE_LINE,
)
from src.domain.drive_ancestry import Ancestry
from src.domain.drive_capabilities import DriveCapabilities
from src.domain.drive_connection import ROOT_FOLDER, DriveConnection
from src.domain.drive_connection_state import ConnectionState
from src.domain.drive_feed_scope import FeedScope
from src.domain.drive_mounts import HEIR_INVALID_LINE, PRIMARY_NEEDS_WRITE_FOLDER_LINE
from src.domain.drive_setting_refused import DriveSettingRefused
from src.domain.drive_user_settings import DriveUserSettings
from src.domain.provider_account import ProviderAccount
from src.domain.user_drive import DriveNotConnectedError
from src.services.drive_connection_service import DriveConnectionService
from tests.support.drive_connection_repo import FakeDriveConnectionRepository
from tests.support.oauth_credentials_store import CredentialsStore, creds

KIND = "microsoft_onedrive"     # the credential store knows the registry's kinds only
T0 = datetime(2026, 11, 2, 9, 0, tzinfo=timezone.utc)
AREA = DriveCapabilities(max_path_chars=400)
ACCOUNT = DriveCapabilities(max_path_chars=400, feed_scope=FeedScope.ACCOUNT)
ROOT, RD, COIMBRA, SUB = "root", "f-rd", "f-coimbra", "f-rd-sub"
RD_CHAIN = Ancestry((RD, ROOT), reached_root=True)
COIMBRA_CHAIN = Ancestry((COIMBRA, ROOT), reached_root=True)
SUB_CHAIN = Ancestry((SUB, RD, ROOT), reached_root=True)
CHAINS = {RD: RD_CHAIN, COIMBRA: COIMBRA_CHAIN, SUB: SUB_CHAIN}


class World:
    def __init__(self) -> None:
        self.repo = FakeDriveConnectionRepository()
        self.store = CredentialsStore()
        ids, tags, ticks = (f"conn-{n}" for n in count(1)), (f"t{n}" for n in count(1)), count()
        self.service = DriveConnectionService(self.repo, self.store.repo,
                                              clock=lambda: T0 + timedelta(minutes=next(ticks)),
                                              new_id=lambda: next(ids), new_tag=lambda: next(tags))

    async def connect(self, subject="subject-a"):
        credential = creds("usr-1", KIND, account=ProviderAccount(realm="microsoft", subject=subject, label="x"))
        credential = replace(credential, credential_id=await self.store.repo.save(credential))
        return await self.service.activate(credential, f"fake_b:{subject}")

    async def seed(self, connection_id, *, write=ROOT_FOLDER, folders=(), whole=False,
                   state=ConnectionState.ACTIVE, minutes=0):
        connection = DriveConnection(
            connection_id=connection_id, user_id="usr-1", account_id="acc-1", credential_id=f"cred-{connection_id}",
            kind=KIND, drive_identity=f"fake_b:{connection_id}", ref_tag=f"a{minutes}", mount_name=f"Acme {minutes}"
            if minutes else "Drive", created_at=T0 + timedelta(minutes=minutes), state=state,
            disconnected_at=T0 if state is ConnectionState.DISCONNECTED else None, write_folder_id=write,
            scope_folder_ids=tuple(folders), scope_whole_drive=whole)
        await self.repo.create(connection)
        return connection

    def point(self, connection_id):
        self.repo.settings["usr-1"] = DriveUserSettings(user_id="usr-1", primary_connection_id=connection_id)

    def primary_id(self):
        settings = self.repo.settings.get("usr-1")
        return None if settings is None else settings.primary_connection_id

    def stored(self, connection_id):
        return self.repo.connections[("usr-1", connection_id)]


class TestNewConnection:
    async def test_a_new_connection_is_read_only_searched_and_not_primary(self):
        w = World()
        created = await w.connect()
        assert created.write_folder_id is None and created.searched_by_default is True
        assert created.scope_folder_ids == () and created.scope_whole_drive is False
        assert w.primary_id() is None

    async def test_its_first_write_folder_makes_it_the_primary(self):
        w = World()
        created = await w.connect()
        stored = await w.service.set_write_folder("usr-1", created.connection_id, ROOT_FOLDER, capabilities=AREA,
                                                  ancestry=None)
        assert stored.write_folder_id == ROOT_FOLDER and w.primary_id() == created.connection_id
        assert (await w.service.primary("usr-1")).connection_id == created.connection_id

    async def test_a_write_folder_of_a_second_drive_does_not_move_the_primary(self):
        w = World()
        await w.seed("conn-1")
        w.point("conn-1")
        await w.seed("conn-2", write=None, minutes=1)
        await w.service.set_write_folder("usr-1", "conn-2", RD, capabilities=AREA, ancestry=RD_CHAIN)
        assert w.primary_id() == "conn-1" and w.stored("conn-2").write_folder_id == RD

    async def test_a_reactivated_connection_with_a_write_folder_takes_a_lost_primary(self):
        w = World()
        created = await w.connect()
        await w.service.set_write_folder("usr-1", created.connection_id, ROOT_FOLDER, capabilities=AREA,
                                         ancestry=None)
        w.point("conn-missing")
        await w.connect()                       # the same account: its reconnect
        assert w.primary_id() == created.connection_id


class TestSetPrimary:
    async def test_refuses_a_read_only_connection(self):
        w = World()
        await w.seed("conn-1", write=None)
        with pytest.raises(DriveSettingRefused) as refused:
            await w.service.set_primary("usr-1", "conn-1")
        assert refused.value.line == PRIMARY_NEEDS_WRITE_FOLDER_LINE == \
            "Only a connected drive with a write folder can be the primary."
        assert w.primary_id() is None

    async def test_names_an_active_connection_with_a_write_folder(self):
        w = World()
        await w.seed("conn-1", write=RD)
        await w.service.set_primary("usr-1", "conn-1")
        assert w.primary_id() == "conn-1"

    async def test_a_connection_that_is_not_active_stays_not_connected(self):
        w = World()
        await w.seed("conn-1", state=ConnectionState.NEEDS_RECONNECT)
        with pytest.raises(DriveNotConnectedError):
            await w.service.set_primary("usr-1", "conn-1")


class TestSetScope:
    async def test_folders_and_the_whole_drive_are_refused(self):
        w = World()
        await w.seed("conn-1", write=None)
        with pytest.raises(DriveSettingRefused) as refused:
            await w.service.set_scope("usr-1", "conn-1", folder_ids=[RD], whole_drive=True, capabilities=AREA,
                                      write_folder_ancestry=None, folder_ancestries=CHAINS)
        assert refused.value.line == SCOPE_BOTH_LINE

    async def test_a_scope_that_leaves_the_write_folder_is_refused(self):
        w = World()
        await w.seed("conn-1", write=RD)
        with pytest.raises(DriveSettingRefused) as refused:
            await w.service.set_scope("usr-1", "conn-1", folder_ids=[COIMBRA], whole_drive=False, capabilities=AREA,
                                      write_folder_ancestry=RD_CHAIN, folder_ancestries=CHAINS)
        assert refused.value.line == SCOPE_LEAVES_WRITE_FOLDER_LINE.format(drive="Drive")
        assert w.stored("conn-1").scope_folder_ids == ()

    async def test_the_root_write_folder_admits_only_whole_or_the_area_default(self):
        w = World()
        await w.seed("conn-1")
        with pytest.raises(DriveSettingRefused):
            await w.service.set_scope("usr-1", "conn-1", folder_ids=[RD], whole_drive=False, capabilities=AREA,
                                      write_folder_ancestry=None, folder_ancestries=CHAINS)
        with pytest.raises(DriveSettingRefused):
            await w.service.set_scope("usr-1", "conn-1", folder_ids=[], whole_drive=False, capabilities=ACCOUNT,
                                      write_folder_ancestry=None, folder_ancestries={})
        whole = await w.service.set_scope("usr-1", "conn-1", folder_ids=[], whole_drive=True, capabilities=ACCOUNT,
                                          write_folder_ancestry=None, folder_ancestries={})
        assert whole.scope_whole_drive is True

    async def test_a_scope_holding_the_write_folder_is_stored_de_duplicated(self):
        w = World()
        await w.seed("conn-1", write=RD)
        stored = await w.service.set_scope("usr-1", "conn-1", folder_ids=[COIMBRA, RD, COIMBRA], whole_drive=False,
                                           capabilities=AREA, write_folder_ancestry=RD_CHAIN,
                                           folder_ancestries=CHAINS)
        assert stored.scope_folder_ids == (COIMBRA, RD) and w.stored("conn-1") == stored

    async def test_a_folder_inside_another_selected_one_is_dropped(self):
        w = World()
        await w.seed("conn-1", write=None)
        stored = await w.service.set_scope("usr-1", "conn-1", folder_ids=[SUB, RD, COIMBRA], whole_drive=False,
                                           capabilities=AREA, write_folder_ancestry=None, folder_ancestries=CHAINS)
        assert stored.scope_folder_ids == (RD, COIMBRA)

    async def test_a_selected_folder_without_its_chain_is_a_programming_error(self):
        w = World()
        await w.seed("conn-1", write=None)
        with pytest.raises(ValueError):
            await w.service.set_scope("usr-1", "conn-1", folder_ids=[RD], whole_drive=False, capabilities=AREA,
                                      write_folder_ancestry=None, folder_ancestries={})

    async def test_a_disconnected_connection(self):
        w = World()
        await w.seed("conn-1", state=ConnectionState.DISCONNECTED)
        with pytest.raises(DriveNotConnectedError):
            await w.service.set_scope("usr-1", "conn-1", folder_ids=[], whole_drive=True, capabilities=AREA,
                                      write_folder_ancestry=None, folder_ancestries={})


class TestSetWriteFolder:
    async def test_a_folder_outside_the_scope_is_refused(self):
        w = World()
        await w.seed("conn-1", write=None, folders=(RD,))
        with pytest.raises(DriveSettingRefused) as refused:
            await w.service.set_write_folder("usr-1", "conn-1", COIMBRA, capabilities=AREA, ancestry=COIMBRA_CHAIN)
        assert refused.value.line == WRITE_FOLDER_OUTSIDE_SCOPE_LINE.format(drive="Drive")
        assert w.stored("conn-1").write_folder_id is None

    async def test_a_folder_inside_the_scope_is_stored(self):
        w = World()
        await w.seed("conn-1", write=None, folders=(RD,))
        stored = await w.service.set_write_folder("usr-1", "conn-1", SUB, capabilities=AREA, ancestry=SUB_CHAIN)
        assert stored.write_folder_id == SUB and w.stored("conn-1").write_folder_id == SUB

    async def test_the_account_default_scope_admits_no_write_folder(self):
        w = World()
        await w.seed("conn-1", write=None)
        with pytest.raises(DriveSettingRefused):
            await w.service.set_write_folder("usr-1", "conn-1", ROOT_FOLDER, capabilities=ACCOUNT, ancestry=None)

    async def test_read_only_is_refused_on_the_primary(self):
        w = World()
        await w.seed("conn-1")
        w.point("conn-1")
        with pytest.raises(DriveSettingRefused) as refused:
            await w.service.set_write_folder("usr-1", "conn-1", None, capabilities=AREA, ancestry=None)
        assert refused.value.line == PRIMARY_NEEDS_WRITE_FOLDER_LINE
        assert w.stored("conn-1").write_folder_id == ROOT_FOLDER

    async def test_read_only_on_another_drive_is_stored(self):
        w = World()
        await w.seed("conn-1")
        w.point("conn-1")
        await w.seed("conn-2", minutes=1)
        assert (await w.service.set_write_folder("usr-1", "conn-2", None, capabilities=AREA,
                                                 ancestry=None)).write_folder_id is None

    async def test_an_ancestry_of_another_folder_is_a_programming_error(self):
        w = World()
        await w.seed("conn-1", write=None, folders=(RD,))
        with pytest.raises(ValueError):
            await w.service.set_write_folder("usr-1", "conn-1", SUB, capabilities=AREA, ancestry=RD_CHAIN)


class TestSearchedByDefault:
    async def test_the_flag_is_stored(self):
        w = World()
        await w.seed("conn-1")
        assert (await w.service.set_searched_by_default("usr-1", "conn-1", False)).searched_by_default is False
        assert w.stored("conn-1").searched_by_default is False

    async def test_a_disconnected_connection(self):
        w = World()
        await w.seed("conn-1", state=ConnectionState.DISCONNECTED)
        with pytest.raises(DriveNotConnectedError):
            await w.service.set_searched_by_default("usr-1", "conn-1", False)


class TestDisconnectHeir:
    async def test_the_primary_passes_to_the_named_heir(self):
        w = World()
        await w.seed("conn-1")
        await w.seed("conn-2", write=RD, minutes=1)
        w.point("conn-1")
        assert await w.service.disconnect("usr-1", "conn-1", heir_id="conn-2") is True
        assert w.primary_id() == "conn-2"

    async def test_without_an_heir_the_primary_is_cleared(self):
        w = World()
        await w.seed("conn-1")
        await w.seed("conn-2", minutes=1)
        w.point("conn-1")
        assert await w.service.disconnect("usr-1", "conn-1") is True
        assert w.primary_id() is None

    async def test_disconnecting_another_drive_keeps_the_primary(self):
        w = World()
        await w.seed("conn-1")
        await w.seed("conn-2", minutes=1)
        w.point("conn-1")
        await w.service.disconnect("usr-1", "conn-2", heir_id="conn-1")
        assert w.primary_id() == "conn-1"

    @pytest.mark.parametrize("heir", ["conn-1", "conn-ro", "conn-flagged", "conn-missing"])
    async def test_an_invalid_heir_is_refused_before_anything_is_written(self, heir):
        w = World()
        await w.seed("conn-1")
        await w.seed("conn-ro", write=None, minutes=1)
        await w.seed("conn-flagged", state=ConnectionState.NEEDS_RECONNECT, minutes=2)
        w.point("conn-1")
        with pytest.raises(DriveSettingRefused) as refused:
            await w.service.disconnect("usr-1", "conn-1", heir_id=heir)
        assert refused.value.line == HEIR_INVALID_LINE
        assert w.stored("conn-1").state is ConnectionState.ACTIVE and w.primary_id() == "conn-1"
