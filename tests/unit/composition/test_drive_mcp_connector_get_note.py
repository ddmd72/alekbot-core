"""The connector's `get_note` under connector access (DRIVE_MCP_CONNECTOR_RFC §7.5, §7.7): a `drive:` ref's item is read
under the connector audience before the note store, so an item outside access, a missing item and a `drive_note:` ref
answer one neutral line with no note read; a whole-drive note is readable only for a drive the connector sees. Over
HTTP on the production drive services (`drive_mcp_access_world`)."""
from src.domain import drive_connector_contract as c
from src.domain import drive_note_tool_texts as nt
from src.domain.drive_audience import CURRENT_DRIVE_AUDIENCE, DriveAudience
from src.domain.drive_connector_access import ConnectorAccess

from tests.support.drive_connector_texts import marker

from .drive_mcp_access_world import DRIVE_NOTE, NEUTRAL, PLAN_NOTE, World


async def rd_only() -> World:
    w = World()
    return await w.build(connector=ConnectorAccess.folders((w.top,)))


async def test_get_note_outside_access_is_the_neutral_refusal_and_reads_no_note():
    w = await rd_only()
    assert (await w.call("get_note", item_ref=w.ref(w.invoice))).split("\n") == NEUTRAL
    assert w.notes.reads == []
    assert PLAN_NOTE in await w.call("get_note", item_ref=w.ref(w.plan))


async def test_get_note_of_a_missing_item_answers_the_same_text():
    w = await rd_only()
    outside = await w.call("get_note", item_ref=w.ref(w.invoice))
    missing = await w.call("get_note", item_ref=w.ref("item-99"))
    assert outside == missing
    assert w.notes.reads == []


async def test_a_drive_note_ref_is_refused_without_a_read():
    w = await rd_only()
    assert (await w.call("get_note", item_ref="drive_note:n1")).split("\n") == NEUTRAL
    assert w.notes.reads == []


async def test_a_whole_drive_note_of_a_hidden_drive_answers_like_an_unknown_drive():
    w = await World(acme=True).build(connector=None, acme_access=ConnectorAccess.whole_drive())
    hidden = await w.call("get_note", whole_drive_name="Drive")
    never = await w.call("get_note", whole_drive_name="Acme Hamburg")
    assert DRIVE_NOTE not in hidden
    assert hidden.replace('"Drive"', '"Acme Hamburg"') == never
    assert "The drives are: Acme." in never


async def test_a_whole_drive_note_of_a_visible_drive_is_read():
    w = World()
    await w.build(connector=ConnectorAccess.whole_drive())
    assert DRIVE_NOTE in await w.call("get_note", whole_drive_name="Drive")


async def test_each_call_runs_under_the_connector_audience_and_leaves_it_unset():
    w = await rd_only()
    await w.call("get_note", item_ref=w.ref(w.plan))
    assert w.notes.audiences == [DriveAudience.CONNECTOR]
    assert CURRENT_DRIVE_AUDIENCE.get() is None


async def test_an_unexpected_failure_of_the_item_read_is_a_failure_and_reads_no_note():
    w = await rd_only()

    async def broken(handle, item_id):
        raise RuntimeError("provider down")
    w.drive.get_item = broken
    text = await w.call("get_note", item_ref=w.ref(w.plan))
    assert text.split("\n")[0] == marker(c.STATUS).format(status="failed")
    assert nt.UNEXPECTED in text and PLAN_NOTE not in text
    assert w.notes.reads == []
