"""Passages through the mount tree (multi-drive 2a, design §1 S-10, §3): canonical head, page, folder-note and scope
lines; `drives` and a mount prefix select the drive; `file_refs` ignore `drives`."""
from typing import List

from src.domain import drive_ref
from src.domain.drive_excerpt_range_request import ExcerptRangeRequest
from src.domain.drive_excerpt_render import _scope, render_excerpts
from src.domain.drive_excerpt_request import ExcerptRequest
from src.domain.drive_mounts import FOLDER_DRIVE_MISMATCH, ONE_DRIVE_LINE, UNKNOWN_DRIVE_LINE
from src.domain.drive_search import STATUS_DRIVE_REFUSED, STATUS_OK

from .drive_mounts_harness import MountedWorld, renamed_acme

QUESTION = "how does the review process work"


def toward(d: float) -> List[float]:
    return [1.0, d, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0]


class Passages(MountedWorld):
    def __init__(self, **kw) -> None:
        super().__init__(**kw)
        self.emb.pinned[QUESTION] = toward(0.0)

    def doc(self, name: str, texts, distances, *, parent: str = "root") -> str:
        item = self.file(name, parent=parent, kind="docx", chunks=texts)
        for index, d in enumerate(distances):
            chunk, _ = self.repo.chunks[("u", item, 1, index)]
            self.repo.chunks[("u", item, 1, index)] = (chunk, toward(d))
        return item

    async def passages(self, *, drives: str = "", **fields):
        return await self.service.excerpts("u", ExcerptRequest(question=QUESTION, **fields), drives=drives)


async def _world(**kw) -> Passages:
    w = await Passages(**kw).open()
    rd = w.folder("R&D")
    w.set(rd, owner_note_id="n-rd", owner_note_version=1, owner_note_text="Weekly reviews live here.",
          owner_note_merge_version=1)
    w.plan = w.doc("Tom's plan.docx", ["The review is held weekly.", "Then Acme signs off."], [0.1, 0.3], parent=rd)
    w.doc("menu.docx", ["Lunch options near the office."], [5.0])
    return w


def _text(w, result) -> str:
    return render_excerpts(result, nonce=lambda: "nonce0099").text


async def test_the_head_line_the_wrapper_and_the_folder_note_are_canonical():
    w = await _world(acme=False)
    result = await w.passages()
    assert sorted(f.path for f in result.files) == ["Drive/R&D/Tom's plan.docx", "Drive/menu.docx"]
    assert sorted(c.path for c in result.context.files) == ["Drive/R&D/Tom's plan.docx", "Drive/menu.docx"]
    text = _text(w, result)
    assert "· \"Drive/R&D/Tom's plan.docx\" · passages" in text and 'path="Drive/R&D/Tom\'s plan.docx"' in text
    assert [n.path for n in result.context.folders.values()] == ["Drive/R&D"]


async def test_the_scope_line_and_result_echo_the_canonical_folder():
    w = await _world()
    bare = await w.passages(folder="R&D")
    mounted = await w.passages(folder="Drive/R&D")
    assert [f.item_id for f in bare.files] == [f.item_id for f in mounted.files] != []
    assert bare.folder_path == mounted.folder_path == "Drive/R&D"
    assert _scope(mounted) == 'folder "Drive/R&D"'
    assert mounted.request.folder == "Drive/R&D"


async def test_a_page_prints_its_canonical_path():
    w = await _world(acme=False)
    page = await w.service.excerpt_range("u", ExcerptRangeRequest(drive_ref.format("a1", w.plan), 1, 2, page=True))
    assert page.page.path == "Drive/R&D/Tom's plan.docx" and page.files[0].path == page.page.path
    assert "of \"Drive/R&D/Tom's plan.docx\"" in render_excerpts(page, nonce=lambda: "nonce0099").text


async def test_a_file_with_no_text_is_named_by_its_canonical_path_on_a_folder_call():
    w = await _world(acme=False)
    bare = w.file("budget.ods", kind="ods", parent=next(i for (_, i), r in w.repo.records.items() if r.name == "R&D"))
    result = await w.passages(folder="R&D")
    assert result.no_text == 1 and result.no_text_paths == ("Drive/R&D/budget.ods",) and bare


async def test_drives_names_the_other_drive_and_reports_the_rest_as_not_searched():
    w = await _world()
    result = await w.passages(drives="Acme")
    assert result.status == STATUS_OK and result.ref_tag == "b2" and result.not_searched_drives == ("Drive",)
    default = await w.passages()
    assert default.ref_tag == "a1" and default.not_searched_drives == ("Acme",)


async def test_one_mount_reports_nothing_as_not_searched():
    w = await _world(acme=False)
    assert (await w.passages()).not_searched_drives == ()


async def test_two_names_and_an_unknown_name_are_refused():
    w = await _world()
    two = await w.passages(drives="Acme, Drive")
    assert two.status == STATUS_DRIVE_REFUSED and two.refusal == ONE_DRIVE_LINE.format(names="Drive, Acme")
    assert render_excerpts(two, nonce=lambda: "n").text == two.refusal
    unknown = await w.passages(drives="Acme Lviv")
    assert unknown.refusal == UNKNOWN_DRIVE_LINE.format(name="Acme Lviv", names="Drive, Acme")


async def test_a_folder_on_another_drive_than_drives_is_refused():
    w = await _world()
    result = await w.passages(drives="Drive", folder="Acme/R&D")
    assert result.status == STATUS_DRIVE_REFUSED
    assert result.refusal == FOLDER_DRIVE_MISMATCH.format(folder="Acme/R&D", mount_a="Acme", mount_b="Drive")


async def test_a_former_name_leads_the_rendered_result_with_the_notice():
    w = await _world(acme_history=renamed_acme())
    result = await w.passages(drives="Acme Hamburg")
    assert result.ref_tag == "b2" and result.notice == "Acme Hamburg is now called Acme."
    assert _text(w, result).splitlines()[0] == "Acme Hamburg is now called Acme."


async def test_file_refs_ignore_drives():
    w = await _world()
    ref = drive_ref.format("a1", w.plan)
    result = await w.service.excerpts("u", ExcerptRequest(question=QUESTION, file_refs=(ref,)), drives="Acme")
    assert result.status == STATUS_OK and result.ref_tag == "a1" and result.not_searched_drives == ()
    assert result.files[0].path == "Drive/R&D/Tom's plan.docx"
