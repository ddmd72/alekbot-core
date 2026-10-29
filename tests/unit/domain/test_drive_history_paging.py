"""History phrases for pages, a refused open and a cut open (USER_DRIVE_INDEXING_RFC §29.7)."""
from src.domain import drive_answer_texts as t
from src.domain.drive_excerpt_page import PageSpan
from src.domain.drive_excerpt_range_request import ExcerptRangeRequest
from src.domain.drive_history import drive_history_lines, history_entries
from src.domain.drive_op import DriveOp
from src.domain.drive_open_bound_info import OpenBoundInfo
from src.domain.drive_outcome import DriveOutcome
from src.domain.drive_outcome_class import OutcomeClass
from src.domain.drive_read_facts import ReadFacts
from src.domain.drive_text_extent import ExtentState, TextExtent
from src.domain.user_drive import DriveItem
from tests.unit.domain.drive_excerpt_builders import chunk, excerpt_file, result

PATH = "Docs/Oak Street parking rules.md"
ITEM = DriveItem(item_id="item-1", name="Oak Street parking rules.md", path=PATH, is_folder=False)
HEAD = f"[Drive: {PATH} ref=drive:item-1 — "


def _page(first, last, nxt, *, end=False, rows=None, version=2):
    span = PageSpan(first=first, last=last, total=130, version=version, next=nxt, end=end, path=PATH,
                    ref="drive:item-1")
    rows = rows if rows is not None else range(first - 1, last)
    file = excerpt_file("item-1", PATH, [chunk("item-1", i, f"Clause {i + 1}.") for i in rows])
    res = result(file, request=ExcerptRangeRequest("drive:item-1", first, first + 9, page=True), page=span)
    return DriveOutcome(op=DriveOp.PASSAGES, cls=OutcomeClass.OK, text="p", success=True, items=(ITEM,),
                        facts=ReadFacts(excerpts=True, paged=True), excerpts=res)


def _find():
    res = result(excerpt_file("item-1", PATH, [chunk("item-1", 4, "Clause 5."), chunk("item-1", 5, "Clause 6.")]))
    return DriveOutcome(op=DriveOp.PASSAGES, cls=OutcomeClass.OK, text="f", success=True, items=(ITEM,),
                        facts=ReadFacts(excerpts=True), excerpts=res)


def _lines(*outcomes):
    entries = [e for kind, o in outcomes for e in history_entries(kind, o)]
    return drive_history_lines(entries)


def test_a_page_has_its_own_phrase():
    assert _lines((t.KIND_PASSAGES, _page(1, 10, 11))) == [HEAD + "read pages 1–10 (next: from 11, version 2)]"]


def test_the_end_of_the_file():
    out = _lines((t.KIND_PASSAGES, _page(121, 130, None, end=True)))
    assert out == [HEAD + "read pages 121–130 (the end, version 2)]"]


def test_next_is_the_requests_not_the_last_rows():
    out = _lines((t.KIND_PASSAGES, _page(1, 10, 11, rows=[0, 1, 2])))
    assert "read pages 1–10 (next: from 11" in out[0]


def test_a_page_and_a_find_render_two_phrases_and_never_merge():
    out = _lines((t.KIND_PASSAGES, _page(1, 10, 11)), (t.KIND_PASSAGES, _find()))
    assert out == [HEAD + "read pages 1–10 (next: from 11, version 2); read passages 5–6]"]


def test_two_pages_are_two_phrases():
    out = _lines((t.KIND_PASSAGES, _page(1, 10, 11)), (t.KIND_PASSAGES, _page(11, 20, 21)))
    assert out[0].count("read pages") == 2


def test_a_refused_open():
    extent = TextExtent(ExtentState.PAGEABLE, text_chars=200_000, passages=130)
    refused = DriveOutcome(op=DriveOp.OPEN, cls=OutcomeClass.REFUSED, text="r", success=False, items=(ITEM,),
                           facts=ReadFacts(too_long=True), open_bound=OpenBoundInfo(extent))
    assert _lines((t.KIND_OPEN, refused)) == [HEAD + "too long to open whole (200,000 characters, 130 passages)]"]


def test_a_cut_open():
    info = OpenBoundInfo(TextExtent(ExtentState.NOT_INDEXED), chars=90_000, kept=59_000, cut_state=ExtentState.NOT_INDEXED)
    cut = DriveOutcome(op=DriveOp.OPEN, cls=OutcomeClass.OK, text="b", success=True, items=(ITEM,), body=True,
                       facts=ReadFacts(cut=True), open_bound=info)
    assert _lines((t.KIND_OPEN, cut)) == [HEAD + "opened, cut at 59,000 of 90,000 characters]"]


def test_a_whole_open_is_unchanged():
    whole = DriveOutcome(op=DriveOp.OPEN, cls=OutcomeClass.OK, text="b", success=True, items=(ITEM,), body=True)
    assert _lines((t.KIND_OPEN, whole)) == [HEAD + "opened whole]"]


def test_old_entries_without_the_new_keys_render_as_before():
    old = [{"path": PATH, "ref": "drive:item-1", "op": "passages", "passages": [3, 4, 5]},
           {"path": PATH, "ref": "drive:item-1", "op": "opened"}]
    assert drive_history_lines(old) == [HEAD + "read passages 3–5; opened whole]"]


def test_new_ops_with_missing_numbers_degrade_to_plain_phrases():
    entries = [{"path": PATH, "ref": "drive:item-1", "op": "paged"},
               {"path": PATH, "ref": "drive:item-1", "op": "too_long"},
               {"path": PATH, "ref": "drive:item-1", "op": "opened_cut"}]
    assert drive_history_lines(entries) == [HEAD + "read pages; too long to open whole; opened, cut]"]
