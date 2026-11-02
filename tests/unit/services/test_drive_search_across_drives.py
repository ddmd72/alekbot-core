"""Search across connections (multi-drive 2b P-C, RFC section 11.2, 11.4, E9): empty `drives` reads every drive
searched by default, one query per drive, merged; the same content on two drives is one card with `also on`."""
from dataclasses import replace

from src.domain.drive_content_identity import ContentIdentity
from src.domain.drive_mounts import FOLDER_DRIVE_MISMATCH, UNKNOWN_DRIVE_LINE
from src.domain.drive_search import STATUS_DISABLED, STATUS_DRIVE_REFUSED, STATUS_OK, render_results
from src.domain.drive_user_settings import DriveUserSettings

from .drive_two_drives_harness import ACME_CONN, TwoDrives

PLAN = ContentIdentity("fakehash", "hplan", 26)


async def _world(**kw) -> TwoDrives:
    w = await TwoDrives(**kw).open()
    rd = w.folder("R&D")
    w.plan = w.file("Tom's plan.docx", kind="docx", parent=rd, chunks=["The review is held weekly."])
    w.menu = w.file("menu.pdf", kind="pdf")
    acme_rd = w.on_acme(w.folder("R&D"))
    w.acme_plan = w.on_acme(w.file("Tom's plan.docx", kind="docx", parent=acme_rd, chunks=["Acme signs off weekly."]))
    w.acme_only = w.on_acme(w.file("Acme price list.docx", kind="docx", parent="root2", chunks=["Prices."]))
    return w


async def _set_flag(w, connection_id: str, flag: bool) -> None:
    stored = await w.conns.get("u", connection_id)
    await w.conns.update_settings("u", connection_id, searched_by_default=flag, write_folder_id=stored.write_folder_id,
                                  scope_folder_ids=stored.scope_folder_ids, scope_whole_drive=stored.scope_whole_drive)


async def test_empty_drives_reads_every_drive_searched_by_default():
    w = await _world()
    result = await w.find("plan")
    assert {c.ref_tag for c in result.cards} == {"a1", "b2"}
    assert result.envelope.not_searched_drives == ()


async def test_an_unflagged_drive_is_not_searched_and_is_named():
    w = await _world()
    await _set_flag(w, ACME_CONN, False)
    result = await w.find("plan")
    assert {c.ref_tag for c in result.cards} == {"a1"}
    assert result.envelope.not_searched_drives == ("Acme",)
    assert result.tiered.coverage.not_searched_drives == ("Acme",)
    assert "Acme" in render_results(result)


async def test_a_named_unflagged_drive_is_searched():
    w = await _world()
    await _set_flag(w, ACME_CONN, False)
    result = await w.find("plan", drives="Acme")
    assert {c.ref_tag for c in result.cards} == {"b2"} and result.ref_tag == "b2"
    assert result.envelope.not_searched_drives == ("Drive",)


async def test_two_names_are_searched_and_an_unknown_name_is_refused():
    w = await _world()
    both = await w.find("plan", drives="Acme, Drive")
    assert {c.ref_tag for c in both.cards} == {"a1", "b2"}
    unknown = await w.find("plan", drives="Acme Lviv")
    assert unknown.status == STATUS_DRIVE_REFUSED
    assert unknown.refusal == UNKNOWN_DRIVE_LINE.format(name="Acme Lviv", names="Drive, Acme")


async def test_identical_content_is_one_card_with_also_on_and_the_primarys_ref():
    w = await _world()
    w.set(w.plan, content_identity=PLAN)
    rec = w.repo.records[(ACME_CONN, w.acme_plan)]
    w.repo.records[(ACME_CONN, w.acme_plan)] = replace(rec, content_identity=PLAN)
    result = await w.find("plan")
    plans = [c for c in result.cards if c.name == "Tom's plan.docx"]
    assert len(plans) == 1
    assert plans[0].ref == f"drive:a1:{w.plan}" and plans[0].also_on == ("Acme/R&D/Tom's plan.docx",)
    assert "also on: Acme/R&D/Tom's plan.docx" in render_results(result)


async def test_a_hit_only_on_acme_carries_its_tag():
    w = await _world()
    result = await w.find("price")
    (card,) = [c for c in result.cards if c.name == "Acme price list.docx"]
    assert card.ref == f"drive:b2:{w.acme_only}" and card.path == "Acme/Acme price list.docx"


async def test_a_drive_whose_indexing_is_off_is_named_while_the_other_answers():
    w = TwoDrives()
    w.acme = replace(w.acme, indexing_enabled=False)
    await w.open()
    w.file("Tom's plan.docx", kind="docx", chunks=["The review is held weekly."])
    result = await w.find("plan")
    assert result.status == STATUS_OK and {c.ref_tag for c in result.cards} == {"a1"}
    assert result.envelope.not_searched_drives == ("Acme",)
    only = await w.find("plan", drives="Acme")
    assert only.status == STATUS_DISABLED


async def test_an_unprefixed_folder_is_on_the_primary_only_a_prefixed_one_on_its_mount():
    w = await _world()
    bare = await w.find("plan", folder="R&D")
    assert {c.ref_tag for c in bare.cards} == {"a1"} and bare.envelope.not_searched_drives == ("Acme",)
    prefixed = await w.find("plan", folder="Acme/R&D")
    assert {c.ref_tag for c in prefixed.cards} == {"b2"} and prefixed.folder_path == "Acme/R&D"


async def test_drives_with_an_unprefixed_folder_reads_the_folder_on_each_named_drive():
    w = await _world()
    result = await w.find("plan", folder="R&D", drives="Drive, Acme")
    assert {c.ref_tag for c in result.cards} == {"a1", "b2"}


async def test_a_folder_on_a_drive_that_drives_does_not_name_is_refused():
    w = await _world()
    result = await w.find("plan", folder="Acme/R&D", drives="Drive")
    assert result.status == STATUS_DRIVE_REFUSED
    assert result.refusal == FOLDER_DRIVE_MISMATCH.format(folder="Acme/R&D", mount_a="Acme", mount_b="Drive")


async def test_no_primary_still_searches_every_flagged_drive():
    w = await _world()
    await w.conns.save_settings(DriveUserSettings(user_id="u", primary_connection_id=None))
    result = await w.find("plan")
    assert {c.ref_tag for c in result.cards} == {"a1", "b2"}
