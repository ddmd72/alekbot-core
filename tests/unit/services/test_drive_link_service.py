"""The slice-4 acceptance matrix (DRIVE_MCP_CONNECTOR_RFC §8, §11 slice 4): the link opens once, HEAD does not consume
it, it dies at five minutes, when its folder leaves connector access, when the drive is set to nothing or disconnected,
and when the grant is revoked. Production services over the fake drive; see `drive_link_world`."""
import logging
from datetime import datetime, timezone

import pytest

from src.domain.drive_audience import connector_audience
from src.domain.drive_connection import DriveConnection
from src.domain.drive_connection_state import ConnectionState
from src.domain.drive_connector_access import ConnectorAccess
from src.domain.drive_link import LINK_MAX_READS, LINK_TTL_S
from src.domain.drive_link_outcome import LinkFile
from src.domain.exceptions import DriveLinkFolder, DriveLinkRefused, MediaStreamFailed
from src.domain.user_drive import DriveAccessRefused, DriveRefRefused
from tests.unit.services.drive_link_world import ACCOUNT, BASE, CONN, GRANT, INVOICE_BYTES, PLAN_BYTES, USER, \
    LinkWorld, drain


@pytest.fixture
async def world():
    return await LinkWorld().open()


async def issue(world: LinkWorld, item: str, *, max_reads: int = 1, grant=GRANT):
    with connector_audience():
        return await world.service(max_reads).issue(USER, ACCOUNT, grant, world.ref(item))


def raw_of(url: str) -> str:
    return url.rsplit("/", 1)[1]


def refused_for(caplog, reason: str) -> bool:
    return all(f"reason={reason} " in line for line in caplog.text.splitlines() if "[DriveLink]" in line and
               "refused" in line) and f"reason={reason} " in caplog.text


async def test_the_link_opens_once(world):
    issued = await issue(world, world.invoice)
    assert issued.url.startswith(f"{BASE}/d/") and len(raw_of(issued.url)) == 22
    assert issued.label.startswith("[Drive: ") and "12 Oak Street/FA-0137.pdf" in issued.label
    links = world.service()
    opened = await links.open(raw_of(issued.url))
    assert (opened.name, opened.mime) == ("FA-0137.pdf", "application/pdf")
    assert await drain(opened.chunks) == INVOICE_BYTES
    assert await links.open(raw_of(issued.url)) is None
    assert len(world.media.calls) == 1


async def test_head_does_not_consume(world):
    issued = await issue(world, world.plan)
    links = world.service()
    raw = raw_of(issued.url)
    assert await links.head(raw) == LinkFile(name="Tom's plan.docx", mime=(await links.head(raw)).mime)
    assert next(iter(world.store.links.values())).reads == 0
    assert world.media.calls == []
    assert await drain((await links.open(raw)).chunks) == PLAN_BYTES
    assert await links.head(raw) is None


async def test_it_dies_at_five_minutes(world):
    first = await issue(world, world.invoice)
    world.later(LINK_TTL_S - 1)
    assert await drain((await world.service().open(raw_of(first.url))).chunks) == INVOICE_BYTES
    world.now = datetime(2026, 11, 3, 9, 0, tzinfo=timezone.utc)
    second = await issue(world, world.invoice)
    world.later(LINK_TTL_S)
    assert await world.service().head(raw_of(second.url)) is None
    assert await world.service().open(raw_of(second.url)) is None
    assert len(world.media.calls) == 1


async def test_it_dies_when_its_folder_leaves_connector_access(world, caplog):
    issued = await issue(world, world.invoice)
    await world.share(ConnectorAccess.folders((world.rnd,)))
    caplog.set_level(logging.INFO)
    assert await world.service().head(raw_of(issued.url)) is None
    assert await world.service().open(raw_of(issued.url)) is None
    assert world.media.calls == []
    assert refused_for(caplog, "access")


@pytest.mark.parametrize("how", ["nothing", "disconnected"])
async def test_it_dies_when_the_drive_is_set_to_nothing_or_disconnected(world, how, caplog):
    issued = await issue(world, world.plan)
    if how == "nothing":
        await world.share(None)
    else:
        await world.conns.transition(USER, CONN, ConnectionState.DISCONNECTED, world.now)
    caplog.set_level(logging.INFO)
    assert await world.service().head(raw_of(issued.url)) is None
    assert await world.service().open(raw_of(issued.url)) is None
    assert world.media.calls == []
    assert refused_for(caplog, "connection")


async def test_it_dies_on_grant_revoke(world, caplog):
    issued = await issue(world, world.invoice)
    world.grants[(USER, GRANT)] = False
    caplog.set_level(logging.INFO)
    assert await world.service().head(raw_of(issued.url)) is None
    assert await world.service().open(raw_of(issued.url)) is None
    assert world.media.calls == []
    assert refused_for(caplog, "grant")


@pytest.mark.parametrize("max_reads", [1, 3])
async def test_n_reads_is_one_constant(world, max_reads):
    assert LINK_MAX_READS == 1
    issued = await issue(world, world.plan, max_reads=max_reads)
    links = world.service(max_reads)
    for _ in range(max_reads):
        assert await drain((await links.open(raw_of(issued.url))).chunks) == PLAN_BYTES
    assert await links.open(raw_of(issued.url)) is None
    assert len(world.media.calls) == max_reads


async def test_issue_refuses_a_file_outside_access_a_hidden_drive_and_a_missing_grant_id(world):
    await world.share(ConnectorAccess.folders((world.rnd,)))
    with pytest.raises(DriveAccessRefused):
        await issue(world, world.invoice)
    await world.share(None)
    with pytest.raises((DriveRefRefused, DriveAccessRefused)):
        await issue(world, world.plan)
    await world.share(ConnectorAccess.whole_drive())
    with pytest.raises(DriveLinkRefused):
        await issue(world, world.plan, grant=None)
    assert world.store.links == {}


async def test_issue_refuses_a_folder_with_its_line(world):
    with pytest.raises(DriveLinkFolder) as info:
        await issue(world, world.oak)
    assert info.value.path.endswith("12 Oak Street")
    assert world.store.links == {}


async def test_a_source_that_fails_before_its_first_byte_is_refused(world):
    issued = await issue(world, world.invoice)
    world.media.fail = MediaStreamFailed("the source answered 503")
    assert await world.service().open(raw_of(issued.url)) is None
    assert world.media.closed == world.media.calls


async def test_issue_and_get_log_no_path_name_or_raw_id(world, caplog):
    caplog.set_level(logging.DEBUG)
    issued = await issue(world, world.invoice)
    raw = raw_of(issued.url)
    links = world.service()
    await links.head(raw)
    await drain((await links.open(raw)).chunks)
    await links.open(raw)
    text = caplog.text
    assert "[DriveLink] issued user=usr-alex key=" in text
    assert "[DriveLink] get ok user=usr-alex" in text
    assert "[DriveLink] get refused reason=unknown_or_spent" in text
    for secret in (raw, issued.url, "FA-0137", "12 Oak Street", world.invoice):
        assert secret not in text


async def test_a_reconnected_connection_keeps_its_links_and_another_connection_with_the_tag_does_not(world, caplog):
    issued = await issue(world, world.plan)
    await world.conns.transition(USER, CONN, ConnectionState.NEEDS_RECONNECT, world.now)
    await world.conns.transition(USER, CONN, ConnectionState.ACTIVE, world.now)
    assert await world.service().head(raw_of(issued.url)) is not None

    again = await issue(world, world.plan)
    stored: DriveConnection = world.conns.connections.pop((USER, CONN))
    other = DriveConnection(**{**stored.__dict__, "connection_id": "conn-9", "credential_id": "cred-conn-9"})
    world.conns.connections[(USER, "conn-9")] = other
    caplog.set_level(logging.INFO)
    caplog.clear()
    assert await world.service().head(raw_of(again.url)) is None
    assert await world.service().open(raw_of(again.url)) is None
    assert world.media.calls == []
    assert refused_for(caplog, "connection")
