"""`DriveConnectionService` and the connector (DRIVE_MCP_CONNECTOR_RFC §7.4, §7.6, E12; slice 3 DELTA 1, 6): a drive
with no connector access is hidden from every connection read under the audience and from none outside it;
`set_connector_access` and the scope change that would strand a connector folder. Alex Doe's drives `Drive` and
`Acme`; folders `R&D` and `12 Oak Street`."""
from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from src.domain.drive_access_policy import (
    CONNECTOR_FOLDER_OUTSIDE_SCOPE_LINE, CONNECTOR_ROOT_LINE, SCOPE_STRANDS_CONNECTOR_LINE,
)
from src.domain.drive_ancestry import Ancestry
from src.domain.drive_audience import connector_audience
from src.domain.drive_capabilities import DriveCapabilities
from src.domain.drive_connection_state import ConnectionState
from src.domain.drive_connection_status import DriveConnectionStatus
from src.domain.drive_connector_access import ConnectorAccess
from src.domain.drive_feed_scope import FeedScope
from src.domain.drive_ref_refusal import DriveRefRefusal
from src.domain.drive_resolved_ref import ResolvedDriveRef
from src.domain.drive_setting_refused import DriveSettingRefused
from src.domain.drive_user_settings import DriveUserSettings
from src.domain.user_drive import DriveNotConnectedError
from src.services.drive_connection_service import DriveConnectionService
from tests.support.drive_connection_repo import FakeDriveConnectionRepository
from tests.support.drive_connection_service_fake import T_CREATED, connection

USER = "usr-alex"
ROOT, RD, OAK = "root", "f-rd", "f-oak"
AREA = DriveCapabilities(max_path_chars=400)
ACCOUNT = DriveCapabilities(max_path_chars=400, feed_scope=FeedScope.ACCOUNT)
RD_CHAIN = Ancestry((RD, ROOT), reached_root=True)
OAK_CHAIN = Ancestry((OAK, ROOT), reached_root=True)
PLAN_DIR_CHAIN = Ancestry(("f-plans", RD, ROOT), reached_root=True)      # R&D/Plans, nested in R&D


def setup(*, drive_access=None, acme_access=ConnectorAccess.whole_drive(), primary="conn-1"):
    """Both drives read-only (no write folder), so a scope change is refused only for the connector folders."""
    repo = FakeDriveConnectionRepository()
    drive = connection(USER, "conn-1", ref_tag="a1")
    acme = connection(USER, "conn-2", ref_tag="b2", drive_identity="fake_a:drive-2", credential_id="cred-2",
                      created_at=T_CREATED + timedelta(days=1))
    repo.connections[(USER, "conn-1")] = replace(drive, connector_access=drive_access, write_folder_id=None)
    repo.connections[(USER, "conn-2")] = replace(acme, mount_name="Acme", connector_access=acme_access,
                                                 write_folder_id=None)
    repo.settings[USER] = DriveUserSettings(user_id=USER, primary_connection_id=primary)
    return repo, DriveConnectionService(repo, None, clock=lambda: datetime(2026, 11, 3, tzinfo=timezone.utc))  # type: ignore[arg-type]


async def test_mounts_under_the_audience_leave_out_a_drive_with_no_connector_access():
    _, svc = setup()
    with connector_audience():
        table = await svc.mounts(USER)
    assert [m.name for m in table.mounts] == ["Acme"]
    assert table.primary() is not None and table.primary().name == "Acme"
    assert [m.name for m in (await svc.mounts(USER)).mounts] == ["Drive", "Acme"]


async def test_the_primary_falls_back_to_the_first_visible_mount_then_to_none():
    repo, svc = setup()
    with connector_audience():
        assert (await svc.primary(USER)).connection_id == "conn-2"
    assert (await svc.primary(USER)).connection_id == "conn-1"
    repo.connections[(USER, "conn-1")] = replace(repo.connections[(USER, "conn-1")],
                                                 connector_access=ConnectorAccess.whole_drive())
    with connector_audience():
        assert (await svc.primary(USER)).connection_id == "conn-1"
    _, hidden = setup(acme_access=None)
    with connector_audience():
        assert await hidden.primary(USER) is None
        assert (await hidden.mounts(USER)).mounts == ()
        assert (await hidden.status(USER)) == DriveConnectionStatus.of(None)


async def test_list_for_user_status_and_connection_of_tag_hide_it():
    _, svc = setup()
    with connector_audience():
        assert [c.connection_id for c in await svc.list_for_user(USER)] == ["conn-2"]
        assert [c.connection_id for c in await svc.list_for_user(USER, include_disconnected=True)] == ["conn-2"]
        assert await svc.connection_of_tag(USER, "a1") is None
        assert (await svc.connection_of_tag(USER, "b2")).connection_id == "conn-2"
        status = await svc.status(USER)
    acme = await svc.get(USER, "conn-2")
    assert status == DriveConnectionStatus.of(acme)
    assert (await svc.get(USER, "conn-1")) is not None          # `get` is never filtered


async def test_a_ref_of_a_hidden_drive_resolves_like_an_unknown_tag():
    _, svc = setup()
    with connector_audience():
        hidden = await svc.resolve_ref(USER, "drive:a1:item-7")
        unknown = await svc.resolve_ref(USER, "drive:z9:item-7")
        visible = await svc.resolve_ref(USER, "drive:b2:item-7")
    assert isinstance(hidden, DriveRefRefusal)
    assert hidden.line.replace("a1", "z9") == unknown.line
    assert isinstance(visible, ResolvedDriveRef) and visible.connection.connection_id == "conn-2"


async def test_outside_the_audience_every_read_is_unchanged():
    _, svc = setup(acme_access=None)
    assert [c.connection_id for c in await svc.list_for_user(USER)] == ["conn-1", "conn-2"]
    assert (await svc.primary(USER)).connection_id == "conn-1"
    assert [m.name for m in (await svc.mounts(USER)).mounts] == ["Drive", "Acme"]
    assert isinstance(await svc.resolve_ref(USER, "drive:a1:item-7"), ResolvedDriveRef)
    assert (await svc.connection_of_tag(USER, "b2")).connection_id == "conn-2"


async def test_set_connector_access_stores_nothing_whole_and_outermost_folders():
    repo, svc = setup()
    chains = {RD: RD_CHAIN, "f-plans": PLAN_DIR_CHAIN, OAK: OAK_CHAIN}
    stored = await svc.set_connector_access(USER, "conn-1", ConnectorAccess.folders(("f-plans", RD, OAK)),
                                            capabilities=AREA, root_item_id=ROOT, folder_ancestries=chains)
    assert stored.connector_access == ConnectorAccess.folders((RD, OAK))
    assert repo.connections[(USER, "conn-1")].connector_access == ConnectorAccess.folders((RD, OAK))
    whole = await svc.set_connector_access(USER, "conn-1", ConnectorAccess.whole_drive(), capabilities=AREA,
                                           root_item_id=ROOT, folder_ancestries={})
    assert whole.connector_access == ConnectorAccess.whole_drive()
    nothing = await svc.set_connector_access(USER, "conn-1", None, capabilities=AREA, root_item_id=ROOT,
                                             folder_ancestries={})
    assert nothing.connector_access is None


async def test_a_connector_folder_outside_the_scope_and_the_root_are_refused_with_their_lines():
    repo, svc = setup()
    await svc.set_scope(USER, "conn-1", folder_ids=[RD], whole_drive=False, capabilities=AREA,
                        write_folder_ancestry=None, folder_ancestries={RD: RD_CHAIN})
    with pytest.raises(DriveSettingRefused) as outside:
        await svc.set_connector_access(USER, "conn-1", ConnectorAccess.folders((OAK,)), capabilities=AREA,
                                       root_item_id=ROOT, folder_ancestries={OAK: OAK_CHAIN})
    assert outside.value.line == CONNECTOR_FOLDER_OUTSIDE_SCOPE_LINE.format(drive="Drive")
    assert outside.value.line == "A folder the connector reads must be inside the folders Drive indexes."
    with pytest.raises(DriveSettingRefused) as root:
        await svc.set_connector_access(USER, "conn-1", ConnectorAccess.folders((ROOT, RD)), capabilities=AREA,
                                       root_item_id=ROOT,
                                       folder_ancestries={ROOT: Ancestry((ROOT,), reached_root=True), RD: RD_CHAIN})
    assert root.value.line == CONNECTOR_ROOT_LINE
    assert root.value.line == 'Choose "Everything the bot sees on this drive" instead of the drive\'s root.'
    assert repo.connections[(USER, "conn-1")].connector_access is None


async def test_an_outer_folder_inside_a_whole_scope_is_accepted():
    _, svc = setup()
    await svc.set_scope(USER, "conn-1", folder_ids=[], whole_drive=True, capabilities=ACCOUNT,
                        write_folder_ancestry=None, folder_ancestries={})
    hamburg = Ancestry(("f-hamburg",), reached_root=False)                 # Acme Hamburg, a shared drive's root
    stored = await svc.set_connector_access(USER, "conn-1", ConnectorAccess.folders(("f-hamburg",)),
                                            capabilities=ACCOUNT, root_item_id=ROOT,
                                            folder_ancestries={"f-hamburg": hamburg})
    assert stored.connector_access == ConnectorAccess.folders(("f-hamburg",))


async def test_a_disconnected_connection_is_not_connected():
    repo, svc = setup()
    await repo.transition(USER, "conn-1", ConnectionState.DISCONNECTED, datetime(2026, 11, 3, tzinfo=timezone.utc))
    with pytest.raises(DriveNotConnectedError):
        await svc.set_connector_access(USER, "conn-1", ConnectorAccess.whole_drive(), capabilities=AREA,
                                       root_item_id=ROOT, folder_ancestries={})
    with pytest.raises(DriveNotConnectedError):
        await svc.set_connector_access(USER, "conn-9", None, capabilities=AREA, root_item_id=ROOT,
                                       folder_ancestries={})


async def test_a_scope_change_that_strands_a_connector_folder_is_refused_with_the_approved_line():
    repo, svc = setup(drive_access=ConnectorAccess.folders((RD,)))
    with pytest.raises(DriveSettingRefused) as refused:
        await svc.set_scope(USER, "conn-1", folder_ids=[OAK], whole_drive=False, capabilities=AREA,
                            write_folder_ancestry=None, folder_ancestries={OAK: OAK_CHAIN},
                            connector_ancestries={RD: RD_CHAIN})
    assert refused.value.line == SCOPE_STRANDS_CONNECTOR_LINE
    assert refused.value.line == ("This would leave folders the connector reads outside what the bot sees: change "
                                  "the connector access first.")
    assert repo.connections[(USER, "conn-1")].scope_folder_ids == ()
    with pytest.raises(ValueError):
        await svc.set_scope(USER, "conn-1", folder_ids=[OAK], whole_drive=False, capabilities=AREA,
                            write_folder_ancestry=None, folder_ancestries={OAK: OAK_CHAIN})


async def test_a_scope_change_that_keeps_every_connector_folder_stores():
    repo, svc = setup(drive_access=ConnectorAccess.folders(("f-plans",)))
    stored = await svc.set_scope(USER, "conn-1", folder_ids=[RD], whole_drive=False, capabilities=AREA,
                                 write_folder_ancestry=None, folder_ancestries={RD: RD_CHAIN},
                                 connector_ancestries={"f-plans": PLAN_DIR_CHAIN})
    assert stored.scope_folder_ids == (RD,)
    assert stored.connector_access == ConnectorAccess.folders(("f-plans",))


async def test_whole_access_never_strands():
    repo, svc = setup(drive_access=ConnectorAccess.whole_drive())
    stored = await svc.set_scope(USER, "conn-1", folder_ids=[OAK], whole_drive=False, capabilities=AREA,
                                 write_folder_ancestry=None, folder_ancestries={OAK: OAK_CHAIN})
    assert stored.scope_folder_ids == (OAK,) and stored.connector_access == ConnectorAccess.whole_drive()
