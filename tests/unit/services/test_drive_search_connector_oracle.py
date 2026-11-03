"""No existence oracle in the search service under the connector audience (DRIVE_MCP_CONNECTOR_RFC §7.7; slice 3
security review): a folder outside connector access is never named, not even as "contents not indexed", by path or by
ref; the cut note of an opened transcript never prints the mirror's path. With no audience every answer is chat's,
unchanged. The `MountedWorld` index: `R&D` (the connector folder) and `12 Oak Street` (contents not indexed)."""
from dataclasses import replace

from src.domain import drive_ref
from src.domain.drive_audience import connector_audience
from src.domain.drive_connector_access import ConnectorAccess
from src.domain.drive_index import Scope
from src.domain.drive_search import STATUS_FOLDER_NOT_FOUND, STATUS_OK, render_results

from .drive_mounts_harness import MountedWorld
from .test_drive_search_transcript_live_cut import _async, _text, _world as _transcript_world


async def world(connector):
    w = MountedWorld(acme=False)
    w.rd = w.folder("R&D")
    w.file("Tom's plan.docx", kind="docx", parent=w.rd, chunks=["The first review is held weekly."])
    w.oak = w.folder("12 Oak Street")
    w.set(w.oak, scope=Scope.OPAQUE_ROOT)                     # its contents are not indexed
    w.inner = w.folder("R&D", parent=w.oak)
    w.set(w.inner, scope=Scope.INSIDE_OPAQUE)
    if connector == "rd":
        connector = ConnectorAccess.folders((w.rd,))
    w.primary = replace(w.primary, scope_whole_drive=True, connector_access=connector)
    return await w.open()


def not_named(result) -> None:
    assert result.status == STATUS_FOLDER_NOT_FOUND and not result.folder_not_indexed
    assert "12 Oak Street" not in (result.folder_path or "")


async def test_a_path_through_a_hidden_folder_outside_access_is_not_found_under_the_audience():
    w = await world("rd")
    with connector_audience():
        hidden = await w.find("plan", folder="12 Oak Street/R&D")
        absent = await w.find("plan", folder="Acme Hamburg/R&D")          # a folder the drive never had
    not_named(hidden)
    # the line echoes only the path the client wrote: the same answer as for a folder that does not exist
    assert render_results(hidden).replace("12 Oak Street", "Acme Hamburg") == render_results(absent)


async def test_a_ref_of_a_hidden_folder_outside_access_is_not_found_under_the_audience():
    w = await world("rd")
    with connector_audience():
        result = await w.find("plan", folder=drive_ref.format("a1", w.inner))
    not_named(result)
    assert "R&D" not in (result.folder_path or "") and "R&D" not in render_results(result)


async def test_chat_still_answers_not_indexed_with_the_audience_unset():
    w = await world("rd")
    by_path = await w.find("plan", folder="12 Oak Street/R&D")
    by_ref = await w.find("plan", folder=drive_ref.format("a1", w.inner))
    assert (by_path.status, by_path.folder_not_indexed, by_path.folder_path) == (STATUS_OK, True, "Drive/12 Oak Street")
    assert (by_ref.status, by_ref.folder_not_indexed, by_ref.folder_path) == (STATUS_OK, True, "Drive/R&D")


async def test_a_hidden_folder_inside_access_still_answers_not_indexed_under_the_audience():
    w = await world(ConnectorAccess.whole_drive())
    with connector_audience():
        result = await w.find("plan", folder="12 Oak Street/R&D")
    assert (result.status, result.folder_not_indexed) == (STATUS_OK, True)


async def test_the_cut_transcript_note_names_no_mirror_path_under_the_audience():
    w, item = await _transcript_world()
    w.service._active_mirror_path = _async("_Transcripts/Tom's plan.md")
    assert "Drive/_Transcripts/Tom's plan.md" in await _text(w, item)          # chat: unchanged
    with connector_audience():
        text = await _text(w, item)
    assert "_Transcripts" not in text and "[Cut by the system: " in text
