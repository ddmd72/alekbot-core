"""The fake-provider acceptance of slice 3 (DRIVE_MCP_CONNECTOR_RFC §7, §11 slice 3 "Done"): six tools × six rows of
connector access, over HTTP on the production drive services (`drive_mcp_access_world`). "in" is `R&D/Tom's plan.docx`
(or `Acme Hamburg/Tom's plan.docx` on the shared drive), "out" is `12 Oak Street/FA-0137.pdf` (or `FA-0137.pdf` at the
shared drive's root). Each cell asserts the result text."""
from typing import Dict

from src.domain import drive_connector_contract as c
from src.domain.drive_connector_access import ConnectorAccess
from tests.support.drive_connector_texts import marker

from .drive_mcp_access_world import INVOICE_TEXT, NEUTRAL, PLAN_NOTE, PLAN_TEXT, World, what_to_do

OUT_NOTE = "The locksmith's invoice."


def assert_reached(w: World, out: Dict[str, str]) -> None:
    """All six tools reach the plan."""
    assert w.plan_path in out["search_drive"]
    assert PLAN_TEXT in out["find_passages"]
    assert PLAN_TEXT in out["read_passages"]
    assert PLAN_TEXT in out["open_drive_file"]
    assert "Tom's plan.docx" in out["list_folder"] and out["list_folder"].startswith(marker(c.STATUS).format(status="ok"))
    assert PLAN_NOTE in out["get_note"]


def assert_reached_invoice(w: World, out: Dict[str, str]) -> None:
    assert w.invoice_path in out["search_drive"]
    assert INVOICE_TEXT in out["find_passages"]
    assert INVOICE_TEXT in out["read_passages"]
    assert INVOICE_TEXT in out["open_drive_file"]
    assert "FA-0137.pdf" in out["list_folder"]
    assert OUT_NOTE in out["get_note"]


def assert_withheld(path: str, text: str, out: Dict[str, str], note: str) -> None:
    """Not found, not in passages, neutral refusal on open, list and note."""
    assert path not in out["search_drive"]
    assert text not in out["find_passages"]
    assert text not in out["read_passages"]
    assert out["open_drive_file"].split("\n") == NEUTRAL
    assert out["list_folder"].split("\n") == NEUTRAL
    assert out["get_note"].split("\n") == NEUTRAL
    assert note not in out["get_note"]


class TestNothing:
    async def test_every_tool_sees_no_drive(self):
        w = await World().build(connector=None)
        root = await w.call("list_folder", folder="/")
        assert what_to_do(root) == marker(c.WHAT_TO_DO).format(line=marker(c.NOT_CONNECTED))
        assert "Drive:" not in root
        for item, path, text, note, folder in ((w.plan, w.plan_path, PLAN_TEXT, PLAN_NOTE, "Drive/R&D"),
                                               (w.invoice, w.invoice_path, INVOICE_TEXT, OUT_NOTE,
                                                "Drive/12 Oak Street")):
            out = await w.six(item, folder)
            assert path not in out["search_drive"] and text not in out["find_passages"]
            assert text not in out["read_passages"] and text not in out["open_drive_file"]
            assert "Tom's plan.docx" not in out["list_folder"] and "FA-0137.pdf" not in out["list_folder"]
            for tool in ("read_passages", "open_drive_file", "get_note"):     # refs: one neutral line
                assert out[tool].split("\n") == NEUTRAL, tool
        assert w.notes.reads == [] and w.conversion.opened == []


class TestFoldersRD:
    async def test_in_reaches_every_tool_and_out_reaches_none(self):
        w = World()
        await w.build(connector=ConnectorAccess.folders((w.top,)))
        assert_reached(w, await w.six(w.plan, "Drive/R&D"))
        assert_withheld(w.invoice_path, INVOICE_TEXT, await w.six(w.invoice, "Drive/12 Oak Street"), OUT_NOTE)
        root = await w.call("list_folder", folder="/")
        assert "- Drive: primary; connected; connector reads only Drive/R&D; searched by default" in root
        assert w.conversion.opened == [w.ref(w.plan)]


class TestWhole:
    async def test_all_six_reach_both(self):
        w = await World().build(connector=ConnectorAccess.whole_drive())
        assert_reached(w, await w.six(w.plan, "Drive/R&D"))
        assert_reached_invoice(w, await w.six(w.invoice, "Drive/12 Oak Street"))
        assert "connector reads everything the bot sees on it" in await w.call("list_folder", folder="/")


class TestMovedOutOfTheScope:
    async def test_a_connector_folder_moved_out_of_the_scope_folder_is_refused_everywhere(self):
        w = World()
        await w.build(connector=ConnectorAccess.folders((w.top,)), scope_folders=(w.oak,))
        await w.adapter.move(w.handle, w.top, new_parent_id=w.oak)          # R&D inside the scope folder
        w.index.set(w.top, parent_id=w.oak)
        assert PLAN_TEXT in await w.call("open_drive_file", file_ref=w.ref(w.plan))
        await w.adapter.move(w.handle, w.top, new_parent_id="root")       # then moved out of it on the drive
        w.index.set(w.top, parent_id="root")
        assert_withheld(w.plan_path, PLAN_TEXT, await w.six(w.plan, "Drive/R&D"), PLAN_NOTE)
        assert_withheld(w.invoice_path, INVOICE_TEXT, await w.six(w.invoice, "Drive/12 Oak Street"), OUT_NOTE)


class TestUnknownAncestor:
    async def test_r_and_d_closed_to_id_reads_refuses_everywhere(self):
        w = World()
        await w.build(connector=ConnectorAccess.folders((w.top,)))
        w.adapter.close(w.handle, w.top)                  # the provider answers "not found" for R&D
        del w.index.repo.records[("u", w.top)]           # so the index never recorded it
        assert_withheld(w.plan_path, PLAN_TEXT, await w.six(w.plan, "Drive/R&D"), PLAN_NOTE)
        assert_withheld(w.invoice_path, INVOICE_TEXT, await w.six(w.invoice, "Drive/12 Oak Street"), OUT_NOTE)


class TestSharedDrive:
    async def test_a_shared_drive_folder_is_reachable_by_all_six_and_a_root_file_is_refused(self):
        w = World(shared=True)
        await w.build(connector=ConnectorAccess.folders((w.top,)))
        assert_reached(w, await w.six(w.plan, "Drive/Acme Hamburg"))
        out = await w.six(w.invoice, "Drive")
        assert w.invoice_path not in out["search_drive"]
        assert INVOICE_TEXT not in out["find_passages"] and INVOICE_TEXT not in out["read_passages"]
        assert out["open_drive_file"].split("\n") == NEUTRAL and out["get_note"].split("\n") == NEUTRAL
        assert "FA-0137.pdf" not in out["list_folder"]                      # the top lists the connector folders
        assert "Folders shared with this connector on Drive:" in out["list_folder"]


async def test_the_refusal_text_is_the_same_for_absent_and_outside():
    w = World()
    await w.build(connector=ConnectorAccess.folders((w.top,)))
    for tool, arg in (("open_drive_file", "file_ref"), ("get_note", "item_ref")):
        outside = await w.call(tool, **{arg: w.ref(w.invoice)})
        absent = await w.call(tool, **{arg: w.ref("item-99")})
        assert outside == absent and outside.split("\n") == NEUTRAL, tool


async def test_a_hidden_drive_is_absent_from_every_listing_and_search():
    w = await World(acme=True).build(connector=None, acme_access=ConnectorAccess.whole_drive())
    root = await w.call("list_folder", folder="/")
    assert "- Acme:" in root and "- Drive:" not in root
    texts = [root, await w.call("list_folder", folder=""), await w.call("list_folder", folder="Drive"),
             await w.call("search_drive", query="Tom's plan"), await w.call("search_drive", query="FA-0137"),
             await w.call("search_drive", query="Tom's plan", drives="Drive"),
             await w.call("find_passages", question="When is the first review?")]
    for text in texts:
        assert "Tom's plan.docx" not in text and "FA-0137.pdf" not in text and PLAN_TEXT not in text
    assert 'There is no drive named "Drive". The drives are: Acme.' in texts[5]
