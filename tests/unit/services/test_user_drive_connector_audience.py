"""`UserDriveService` under the connector audience (DRIVE_MCP_CONNECTOR_RFC §7.3, §7.4, E11): the production service and
`DriveConnectionService` over `FakeDriveAdapter` and the in-memory connection repository. Alex Doe's drive holds
`R&D/Tom's plan.docx` and `12 Oak Street/FA-0137.pdf`; a Google-like shared drive holds `Acme Hamburg/Tom's plan.docx`
with no reachable parent."""
from dataclasses import dataclass
from typing import Optional

import pytest

from src.domain.drive_access_op import AccessOp
from src.domain.drive_access_policy import CONNECTOR_REFUSED_LINE
from src.domain.drive_audience import connector_audience
from src.domain.drive_connector_access import ConnectorAccess
from src.domain.drive_feed_scope import FeedScope
from src.domain.user_drive import DriveAccessRefused
from src.services.drive_connection_service import DriveConnectionService
from src.services.user_drive_service import UserDriveService
from tests.support.drive_connection_repo import FakeDriveConnectionRepository
from tests.support.drive_connection_service_fake import connection
from tests.support.fake_drive_adapter import FakeDriveAdapter

USER = "usr-alex"


@dataclass
class World:
    adapter: FakeDriveAdapter
    repo: FakeDriveConnectionRepository
    svc: UserDriveService
    handle: object
    root: str
    rd: str = ""
    plan: str = ""
    oak: str = ""
    invoice: str = ""

    async def connector(self, access: Optional[ConnectorAccess]) -> None:
        await self.repo.update_connector_access(USER, "conn-1", access)

    async def scope(self, *folders: str, whole: bool = False) -> None:
        await self.repo.update_settings(USER, "conn-1", searched_by_default=True, write_folder_id=None,
                                        scope_folder_ids=folders, scope_whole_drive=whole)

    def calls(self, method: str) -> int:
        return sum(1 for m, _ in self.adapter.calls if m == method)


async def world(adapter: Optional[FakeDriveAdapter] = None, *, tree: bool = True) -> World:
    adapter = adapter or FakeDriveAdapter()
    repo = FakeDriveConnectionRepository()
    conn = connection(USER, "conn-1", ref_tag="a1")
    repo.connections[(USER, "conn-1")] = conn
    svc = UserDriveService(adapter, DriveConnectionService(repo, None))  # type: ignore[arg-type]
    root = (await adapter.get_root(conn.handle)).item_id
    w = World(adapter, repo, svc, conn.handle, root)
    if tree:
        w.rd = adapter.plant(conn.handle, root, "R&D", folder=True)
        w.plan = adapter.plant(conn.handle, w.rd, "Tom's plan.docx", b"plan")
        w.oak = adapter.plant(conn.handle, root, "12 Oak Street", folder=True)
        w.invoice = adapter.plant(conn.handle, w.oak, "FA-0137.pdf", b"invoice")
    return w


async def refused(coro) -> str:
    with pytest.raises(DriveAccessRefused) as caught:
        await coro
    return str(caught.value)


async def test_outside_the_audience_reads_ignore_connector_access():
    w = await world()
    await w.connector(None)
    assert (await w.svc.get_item(w.handle, w.invoice)).name == "FA-0137.pdf"
    assert [i.name for i in (await w.svc.list_folder(w.handle, None)).items] == ["12 Oak Street", "R&D"]
    assert [i.name for i in await w.svc.search(w.handle, "plan")] == ["Tom's plan.docx"]


async def test_under_the_audience_get_item_in_a_connector_folder_passes_and_outside_refuses():
    w = await world()
    await w.connector(ConnectorAccess.folders((w.rd,)))
    with connector_audience():
        assert (await w.svc.get_item(w.handle, w.plan)).name == "Tom's plan.docx"
        assert await refused(w.svc.get_item(w.handle, w.invoice)) == CONNECTOR_REFUSED_LINE
        assert await refused(w.svc.list_folder(w.handle, "12 Oak Street")) == CONNECTOR_REFUSED_LINE


async def test_a_drive_with_no_connector_access_refuses_every_read():
    w = await world()
    with connector_audience():
        assert await refused(w.svc.get_item(w.handle, w.plan)) == CONNECTOR_REFUSED_LINE
        assert await refused(w.svc.list_folder(w.handle, None)) == CONNECTOR_REFUSED_LINE
        assert await w.svc.search(w.handle, "plan") == []


async def test_whole_access_follows_the_scope():
    w = await world()
    await w.scope(w.rd)
    await w.connector(ConnectorAccess.whole_drive())
    with connector_audience():
        assert (await w.svc.get_item(w.handle, w.plan)).name == "Tom's plan.docx"
        assert await refused(w.svc.get_item(w.handle, w.invoice)) == CONNECTOR_REFUSED_LINE


async def test_a_connector_folder_moved_out_of_the_scope_is_refused():
    w = await world()
    await w.scope(w.oak)
    sub = w.adapter.plant(w.handle, w.oak, "R&D", folder=True)       # R&D inside the scope at first
    plan = w.adapter.plant(w.handle, sub, "Tom's plan.docx", b"plan")
    await w.connector(ConnectorAccess.folders((sub,)))
    with connector_audience():
        assert (await w.svc.get_item(w.handle, plan)).name == "Tom's plan.docx"
    await w.adapter.move(w.handle, sub, new_parent_id=w.root, new_name="R&D 2")
    with connector_audience():
        assert await refused(w.svc.get_item(w.handle, plan)) == CONNECTOR_REFUSED_LINE
    assert (await w.svc.get_item(w.handle, w.invoice)).name == "FA-0137.pdf"      # chat: unchanged


async def test_an_unknown_ancestor_is_refused():
    w = await world()
    await w.connector(ConnectorAccess.folders((w.rd,)))
    w.adapter.close(w.handle, w.rd)
    with connector_audience():
        assert await refused(w.svc.get_item(w.handle, w.plan)) == CONNECTOR_REFUSED_LINE
        assert await w.svc.search(w.handle, "plan") == []


async def test_a_shared_drive_folder_passes_without_reaching_the_root():
    w = await world(FakeDriveAdapter("fake_b", feed_scope=FeedScope.ACCOUNT), tree=False)
    hamburg = w.adapter.plant_parentless(w.handle, "Acme Hamburg", folder=True)
    plan = w.adapter.plant(w.handle, hamburg, "Tom's plan.docx", b"plan")
    other = w.adapter.plant(w.handle, w.root, "FA-0137.pdf", b"invoice")
    await w.scope(whole=True)
    await w.connector(ConnectorAccess.folders((hamburg,)))
    with connector_audience():
        assert (await w.svc.get_item(w.handle, plan)).name == "Tom's plan.docx"
        assert await refused(w.svc.get_item(w.handle, other)) == CONNECTOR_REFUSED_LINE
        assert [i.name for i in (await w.svc.list_folder(w.handle, "Acme Hamburg")).items] == ["Tom's plan.docx"]


async def test_the_drive_top_lists_the_connector_folders_when_its_root_is_outside():
    w = await world()
    await w.connector(ConnectorAccess.folders((w.rd,)))
    with connector_audience():
        out = await w.svc.list_folder(w.handle, None)
    assert out.connector_tops is True and out.scope_tops is False
    assert [i.name for i in out.items] == ["R&D"]
    assert [i.name for i in (await w.svc.list_folder(w.handle, None)).items] == ["12 Oak Street", "R&D"]


async def test_a_connector_top_moved_out_of_the_scope_is_not_listed():
    w = await world()
    await w.scope(w.oak)
    sub = w.adapter.plant(w.handle, w.oak, "R&D", folder=True)
    await w.connector(ConnectorAccess.folders((sub,)))
    with connector_audience():
        assert [i.name for i in (await w.svc.list_folder(w.handle, None)).items] == ["R&D"]
    await w.adapter.move(w.handle, sub, new_parent_id=w.root, new_name="R&D 2")
    with connector_audience():
        out = await w.svc.list_folder(w.handle, None)
    assert out.connector_tops is True and out.items == []


async def test_listing_above_a_connector_folder_is_refused():
    w = await world()
    sub = w.adapter.plant(w.handle, w.rd, "12 Oak Street", folder=True)
    await w.connector(ConnectorAccess.folders((sub,)))
    with connector_audience():
        assert await refused(w.svc.list_folder(w.handle, "R&D")) == CONNECTOR_REFUSED_LINE
        assert await refused(w.svc.list_folder_by_id(w.handle, w.rd)) == CONNECTOR_REFUSED_LINE
        assert (await w.svc.list_folder(w.handle, "R&D/12 Oak Street")).items == []


async def test_provider_search_keeps_only_results_on_a_connector_chain():
    w = await world()
    w.adapter.plant(w.handle, w.oak, "Tom's plan.docx", b"other plan")
    await w.connector(ConnectorAccess.folders((w.rd,)))
    with connector_audience():
        found = await w.svc.search(w.handle, "plan")
    assert [(i.item_id, i.name) for i in found] == [(w.plan, "Tom's plan.docx")]
    assert len(await w.svc.search(w.handle, "plan")) == 2


async def test_whole_access_on_a_whole_scope_walks_no_chain():
    w = await world()
    await w.scope(whole=True)
    await w.connector(ConnectorAccess.whole_drive())
    before = w.calls("get_change")
    with connector_audience():
        assert [i.name for i in await w.svc.search(w.handle, "FA-0137")] == ["FA-0137.pdf"]
        await w.svc.check_access(w.handle, AccessOp.READ, item_id=w.invoice)
    assert w.calls("get_change") == before
