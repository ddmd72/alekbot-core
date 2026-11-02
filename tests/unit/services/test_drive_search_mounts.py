"""Search through the mount tree (multi-drive 2a, design §1 S-7, S-8, S-14 and §3): canonical card paths, a folder
written with or without its mount, `drives` selection, the refusals and the coverage part."""
from src.domain.drive_mounts import FOLDER_DRIVE_MISMATCH, ONE_DRIVE_LINE, UNKNOWN_DRIVE_LINE
from src.domain.drive_search import STATUS_DRIVE_REFUSED, STATUS_OK, render_results

from .drive_mounts_harness import MountedWorld, renamed_acme


async def _world(**kw) -> MountedWorld:
    w = await MountedWorld(**kw).open()
    rd = w.folder("R&D")
    w.file("Tom's plan.docx", kind="docx", parent=rd, chunks=["The review is held weekly."])
    w.file("Tom's budget.xlsx", kind="xlsx", parent=rd)
    w.file("menu.pdf", kind="pdf")
    return w


async def test_a_card_prints_the_canonical_path_in_its_line_and_wrapper():
    w = await _world(acme=False)
    result = await w.find("plan")
    card = next(c for c in result.cards if c.name == "Tom's plan.docx")
    assert card.path == "Drive/R&D/Tom's plan.docx"
    assert 'path="Drive/R&D/Tom\'s plan.docx"' in render_results(result)
    listed = await w.find("", kind="docx")
    assert "path: Drive/R&D/Tom's plan.docx" in render_results(listed)


async def test_a_folder_with_its_mount_equals_the_folder_without_it():
    w = await _world()
    bare = await w.find("", kind="docx", folder="R&D")
    mounted = await w.find("", kind="docx", folder="Drive/R&D")
    assert [c.item_id for c in bare.cards] == [c.item_id for c in mounted.cards] != []
    assert bare.matched_total == mounted.matched_total
    assert bare.folder_path == mounted.folder_path == "Drive/R&D"
    assert mounted.request.folder == "Drive/R&D" and bare.request.folder == "R&D"   # the model's own text is kept
    assert [c.path for c in mounted.cards] == ["Drive/R&D/Tom's plan.docx"]


async def test_a_query_in_a_mounted_folder_finds_the_same_files():
    w = await _world()
    bare = await w.find("plan", folder="R&D")
    mounted = await w.find("plan", folder="Drive/R&D")
    assert [c.item_id for c in bare.cards] == [c.item_id for c in mounted.cards] != []
    assert mounted.folder_path == "Drive/R&D"


async def test_a_mount_name_alone_searches_the_whole_mount():
    w = await _world()
    whole = await w.find("plan", folder="Drive")
    assert whole.status == STATUS_OK and [c.name for c in whole.cards] == ["Tom's plan.docx"]


async def test_drives_names_the_acme_connection_and_reports_the_primary_as_not_searched():
    w = await _world()
    result = await w.find("plan", drives="Acme")
    assert result.status == STATUS_OK and result.ref_tag == "b2"
    assert result.envelope.not_searched_drives == ("Drive",)


async def test_empty_drives_with_two_mounts_searches_the_primary_and_names_the_other():
    w = await _world()
    result = await w.find("plan")
    assert result.ref_tag == "a1" and result.envelope.not_searched_drives == ("Acme",)
    assert result.tiered is not None and result.tiered.coverage.not_searched_drives == ("Acme",)
    text = render_results(result)
    assert "Coverage: incomplete — drives not searched: Acme." in text


async def test_one_mount_adds_no_coverage_part():
    w = await _world(acme=False)
    result = await w.find("plan")
    assert result.envelope.not_searched_drives == () and result.tiered.coverage.not_searched_drives == ()
    text = render_results(result)
    assert "not searched" not in text and "Coverage: complete." in text


async def test_two_names_are_refused_with_the_list():
    w = await _world()
    result = await w.find("plan", drives="Acme, Drive")
    assert result.status == STATUS_DRIVE_REFUSED and result.cards == ()
    assert result.refusal == ONE_DRIVE_LINE.format(names="Drive, Acme")
    assert render_results(result) == result.refusal


async def test_an_unknown_name_is_refused_without_guessing():
    w = await _world()
    result = await w.find("plan", drives="Acme Lviv")
    assert result.status == STATUS_DRIVE_REFUSED
    assert result.refusal == UNKNOWN_DRIVE_LINE.format(name="Acme Lviv", names="Drive, Acme")
    assert render_results(result) == 'There is no drive named "Acme Lviv". The drives are: Drive, Acme.'


async def test_a_former_name_selects_the_drive_and_leads_with_the_notice():
    w = await _world(acme_history=renamed_acme())
    result = await w.find("plan", drives="Acme Hamburg")
    assert result.ref_tag == "b2" and result.notice == "Acme Hamburg is now called Acme."
    assert render_results(result).splitlines()[0] == "Acme Hamburg is now called Acme."


async def test_a_folder_naming_another_drive_than_drives_is_refused():
    w = await _world()
    result = await w.find("plan", drives="Drive", folder="Acme/R&D")
    assert result.status == STATUS_DRIVE_REFUSED
    assert result.refusal == FOLDER_DRIVE_MISMATCH.format(folder="Acme/R&D", mount_a="Acme", mount_b="Drive")
    assert result.refusal == "The folder Acme/R&D is on Acme, not on Drive."


async def test_a_folder_naming_its_mount_searches_that_mount_whatever_the_default():
    w = await _world()
    assert (await w.find("plan", folder="Acme/R&D")).ref_tag == "b2"
    assert (await w.find("plan", drives="Acme", folder="Acme/R&D")).ref_tag == "b2"


async def test_an_unprefixed_folder_is_resolved_in_the_named_drive():
    w = await _world()
    result = await w.find("plan", drives="Acme", folder="R&D")
    assert result.ref_tag == "b2"          # not the primary, where R&D exists


async def test_a_former_name_in_a_folder_prefix_adds_the_notice():
    w = await _world(acme_history=renamed_acme())
    result = await w.find("plan", folder="Acme Hamburg/R&D")
    assert result.ref_tag == "b2" and result.notice == "Acme Hamburg is now called Acme."


async def test_a_filter_only_result_prints_the_envelope_sentence():
    w = await _world()
    result = await w.find("", kind="docx")
    text = render_results(result)
    assert "Not searched: the drives Acme; name one in drives to search it." in text
    assert [c.path for c in result.cards] == ["Drive/R&D/Tom's plan.docx"]


async def test_the_grouped_rest_prints_canonical_folders():
    w = await _world(acme=False)
    for n in range(8):
        w.file(f"Plan {n}.pdf", kind="pdf", parent=w.folder(f"Plans {n % 2}"), chunks=["The plan names a topic."])
    result = await w.find("plan topic")
    rest = [m for block in result.tiered.blocks for m in block.rest]
    assert all(m.folder.startswith("Drive") for m in rest)
