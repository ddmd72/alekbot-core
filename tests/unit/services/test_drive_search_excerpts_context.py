"""The "where it comes from" context a passages result carries (USER_DRIVE_INDEXING_RFC §28.3 A18; plan CTX.3): the
noted ancestor folders and each file's description and own note, built from the records and ancestors the call
already loaded. A counting repository shows that the notes cost no read."""
from __future__ import annotations

from src.domain.drive_card import note_line
from src.domain.drive_excerpt_folder_note import FolderNote
from src.domain.drive_index import DocumentBrief

from .test_drive_search_excerpts import PassageWorld, ref

RD_NOTE = "Acme specifications, the review process lives here"
ACME_NOTE = "work files from Acme"


def _tree(*, noted: bool):
    """Acme/R&D/Tom's plan.docx, with both folders noted or neither; a second file at the root."""
    w = PassageWorld()
    acme = w.folder("Acme")
    rd = w.folder("R&D", parent=acme)
    plan = w.doc("Tom's plan.docx", ["The first review is held by Lukas Weber."], [0.1], parent=rd)
    w.doc("menu.md", ["Lunch options near the office."], [3.0], kind="md")
    if noted:
        w.note(acme, ACME_NOTE)
        w.note(rd, RD_NOTE)
    return w, acme, rd, plan


def _line(text: str) -> str:
    return f"noted by the owner: «{text}»"


async def test_the_wide_path_reads_no_record_for_the_folder_notes():
    bare, *_ = _tree(noted=False)
    await bare.find()
    w, acme, rd, plan = _tree(noted=True)
    result = await w.find()
    assert len(w.repo.record_reads) == len(bare.repo.record_reads)
    assert result.context.folders == {
        acme: FolderNote(item_id=acme, path="Acme", depth=1, line=_line(ACME_NOTE)),
        rd: FolderNote(item_id=rd, path="Acme/R&D", depth=2, line=_line(RD_NOTE)),
    }
    plan_ctx = next(f for f in result.context.files if f.item_id == plan)
    assert plan_ctx.ancestors == (rd, acme) and plan_ctx.path == "Acme/R&D/Tom's plan.docx"


async def test_the_file_refs_path_reads_no_record_for_the_folder_notes():
    bare, _, _, bare_plan = _tree(noted=False)
    await bare.find(file_refs=(ref(bare_plan),))
    w, acme, rd, plan = _tree(noted=True)
    result = await w.find(file_refs=(ref(plan),))
    assert len(w.repo.record_reads) == len(bare.repo.record_reads)
    assert set(result.context.folders) == {acme, rd}
    assert [f.item_id for f in result.context.files] == [plan]


async def test_a_folder_scope_shows_its_own_and_its_ancestors_notes():
    w, acme, rd, plan = _tree(noted=True)
    result = await w.find(folder="Acme/R&D")
    assert [f.item_id for f in result.files] == [plan]
    assert set(result.context.folders) == {acme, rd}
    assert result.context.folders[acme].depth == 1


async def test_folders_without_a_note_get_no_line():
    w, *_ = _tree(noted=False)
    result = await w.find()
    assert result.context.folders == {} and len(result.context.files) == 2


async def test_a_chain_broken_by_an_unrecorded_ancestor_shows_its_known_part():
    w = PassageWorld()
    rd = w.folder("R&D", parent="ghost-1")   # its parent was never recorded
    w.note(rd, RD_NOTE)
    plan = w.doc("Tom's plan.docx", ["The first review is held by Lukas Weber."], [0.1], parent=rd)
    result = await w.find()
    ctx = next(f for f in result.context.files if f.item_id == plan)
    assert ctx.path == "R&D/Tom's plan.docx" and ctx.ancestors == (rd,)
    assert result.context.folders == {rd: FolderNote(item_id=rd, path="R&D", depth=1, line=_line(RD_NOTE))}


async def test_identical_copies_show_the_keepers_chain_only():
    w = PassageWorld()
    rd, old = w.folder("R&D"), w.folder("Old")
    w.note(rd, RD_NOTE)
    w.note(old, "kept from the previous office")
    w.doc("Tom's plan.docx", ["The review is held weekly."], [0.2], parent=rd, content_hash="same:500")
    keeper = w.doc("Tom's plan.docx", ["The review is held weekly."], [0.1], parent=old, content_hash="same:500")
    result = await w.find()
    assert [f.item_id for f in result.context.files] == [keeper]
    assert set(result.context.folders) == {old}


async def test_the_files_own_note_and_description_come_from_its_record():
    w = PassageWorld()
    plan = w.doc("Tom's plan.docx", ["The first review is held by Lukas Weber."], [0.1],
                 brief=DocumentBrief(description="A plan for the first review at Acme"))
    w.note(plan, "the version Tom sent")
    result = await w.find()
    ctx = result.context.files[0]
    assert ctx.description == "A plan for the first review at Acme"
    assert ctx.own_note == note_line(w.repo.records[("u", plan)]) == _line("the version Tom sent")


async def test_a_range_carries_the_context_of_its_file():
    w, acme, rd, plan = _tree(noted=True)
    result = await w.read(plan, 1, 1)
    assert [f.item_id for f in result.context.files] == [plan]
    assert set(result.context.folders) == {acme, rd}
