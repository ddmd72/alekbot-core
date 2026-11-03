"""The connector's index admit (DRIVE_MCP_CONNECTOR_RFC §7.3, E16; slice 3 DELTA 4): under the audience a record is
seen only when the scope and the connector access hold on its index chain; an unrecorded ancestor and a failed
capabilities lookup admit nothing (chat's admit fails open and stays so); `access_note` never tells the connector that
an item outside its access exists. The `MountedWorld` index: `R&D/Tom's plan.docx` and `12 Oak Street/FA-0137.pdf`."""
from dataclasses import replace

from src.domain import drive_ref
from src.domain.drive_audience import connector_audience
from src.domain.drive_capabilities import DriveCapabilities
from src.domain.drive_connector_access import ConnectorAccess
from src.domain.drive_excerpt_range_request import ExcerptRangeRequest
from src.domain.drive_excerpt_request import ExcerptRequest
from src.domain.drive_search_texts import ACCESS_NOTE_FILE

from .drive_mounts_harness import MountedWorld
from .drive_search_v2_fixture import AS_OF


async def world(*, connector=None, scope_folders=False, whole=True):
    """`whole`: the drive's scope is the whole drive (no capabilities read); `scope_folders`: R&D is the scope (the
    fixture drive answers no capabilities, so the scope test cannot be run)."""
    w = MountedWorld(acme=False)
    w.rd = w.folder("R&D")
    w.plan = w.file("Tom's plan.docx", kind="docx", parent=w.rd, chunks=["The first review is held weekly."])
    w.oak = w.folder("12 Oak Street")
    w.invoice = w.file("FA-0137.pdf", kind="pdf", parent=w.oak, chunks=["Invoice FA-0137 for the lock cylinder and two key copies."])
    if connector == "rd":
        connector = ConnectorAccess.folders((w.rd,))
    w.primary = replace(w.primary, scope_whole_drive=whole and not scope_folders,
                        scope_folder_ids=(w.rd,) if scope_folders else (), connector_access=connector)
    return await w.open()


def names(result):
    return sorted(c.name for c in result.cards)


def passage_names(result):
    return sorted(f.path.rsplit("/", 1)[-1] for f in result.files)


async def excerpts(w, question="What happens after the first review and the locksmith invoice?"):
    return await w.service.excerpts("u", ExcerptRequest(question=question))


async def read(w, item):
    return await w.service.excerpt_range("u", ExcerptRangeRequest(drive_ref.format("a1", item), 1, 10))


async def test_under_the_audience_only_files_below_a_connector_folder_are_found():
    w = await world(connector="rd")
    assert names(await w.find("", kind="docx")) == ["Tom's plan.docx"]
    assert names(await w.find("", kind="pdf")) == ["FA-0137.pdf"]
    assert passage_names(await excerpts(w)) == ["FA-0137.pdf", "Tom's plan.docx"]
    with connector_audience():
        assert names(await w.find("", kind="docx")) == ["Tom's plan.docx"]
        assert names(await w.find("", kind="pdf")) == []
        assert passage_names(await excerpts(w)) == ["Tom's plan.docx"]
        assert [c.text for f in (await read(w, w.plan)).files for c in f.chunks] == ["The first review is held weekly."]
        assert (await read(w, w.invoice)).files == ()


async def test_an_unrecorded_ancestor_is_refused_under_the_audience():
    w = await world(connector="rd")
    del w.repo.records[("u", w.rd)]
    assert names(await w.find("", kind="docx")) == ["Tom's plan.docx"]
    with connector_audience():
        assert names(await w.find("", kind="docx")) == []
        assert (await read(w, w.plan)).files == ()


async def test_a_failed_capabilities_lookup_admits_nothing_under_the_audience_and_everything_in_chat():
    w = await world(connector=ConnectorAccess.whole_drive(), scope_folders=True)
    assert not hasattr(w.drive, "capabilities")          # the lookup raises
    assert names(await w.find("", kind="pdf")) == ["FA-0137.pdf"]          # chat: fail open, by design
    with connector_audience():
        assert names(await w.find("", kind="docx")) == []
        assert names(await w.find("", kind="pdf")) == []


async def test_read_passages_of_a_file_outside_answers_like_a_missing_file():
    w = await world(connector="rd")
    with connector_audience():
        outside = await read(w, w.invoice)
        missing = await read(w, "I999")
    assert outside.no_text_paths == (drive_ref.format("a1", w.invoice),)          # the asked ref, echoed
    assert missing.no_text_paths == (drive_ref.format("a1", "I999"),)
    assert replace(outside, request=missing.request, no_text_paths=missing.no_text_paths) == missing


async def test_a_folder_note_above_a_connector_folder_is_not_shown():
    w = MountedWorld(acme=False)
    oak = w.folder("12 Oak Street")
    rd = w.folder("R&D", parent=oak)
    w.file("Tom's plan.docx", kind="docx", parent=rd, chunks=["The first review is held weekly."])
    w.set(oak, owner_note_id="n-oak", owner_note_version=1, owner_note_text="Alex Doe's house, 12 Oak Street",
          owner_note_merge_version=1)
    w.set(rd, owner_note_id="n-rd", owner_note_version=1, owner_note_text="Acme R&D plans",
          owner_note_merge_version=1)
    w.primary = replace(w.primary, scope_whole_drive=True, connector_access=ConnectorAccess.folders((rd,)))
    await w.open()
    chat = await excerpts(w, "When is the first review?")
    assert sorted(n.path for n in chat.context.folders.values()) == ["Drive/12 Oak Street", "Drive/12 Oak Street/R&D"]
    with connector_audience():
        seen = await excerpts(w, "When is the first review?")
    assert [n.path for n in seen.context.folders.values()] == ["Drive/12 Oak Street/R&D"]


async def test_access_note_outside_the_connector_is_none_under_the_audience():
    w = await world(connector="rd")
    w.set(w.invoice, inaccessible_since=AS_OF)
    w.set(w.plan, inaccessible_since=AS_OF)
    conn = await w.connections.get("u", "u")
    assert await w.service.access_note(conn, item_id=w.invoice) == ACCESS_NOTE_FILE
    with connector_audience():
        assert await w.service.access_note(conn, item_id=w.invoice) is None
        assert await w.service.access_note(conn, item_id=w.plan) == ACCESS_NOTE_FILE


async def test_the_area_default_scope_needs_no_scope_test_under_the_audience():
    w = await world(connector="rd", whole=False)            # no folders chosen on an area feed: everything in scope

    async def capabilities(handle):
        return DriveCapabilities(max_path_chars=400)
    w.service._drive.capabilities = capabilities
    with connector_audience():
        assert names(await w.find("", kind="docx")) == ["Tom's plan.docx"]
        assert names(await w.find("", kind="pdf")) == []
