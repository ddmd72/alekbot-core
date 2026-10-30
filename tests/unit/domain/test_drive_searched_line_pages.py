"""The Searched line files a page under `pages:`, never under `neighbours:` (USER_DRIVE_INDEXING_RFC §29.7)."""
from src.domain import drive_excerpt_render as R
from src.domain.drive_collected_result import CollectedResult
from src.domain.drive_excerpt_context import quoted
from src.domain.drive_excerpt_page import PageSpan
from src.domain.drive_excerpt_range_request import ExcerptRangeRequest
from src.domain.drive_op import DriveOp
from src.domain.drive_outcome import DriveOutcome
from src.domain.drive_outcome_class import OutcomeClass
from src.domain.drive_read_facts import ReadFacts
from src.domain.user_drive import DriveItem
from tests.unit.domain.drive_excerpt_builders import chunk, excerpt_file, result

PATH = "Docs/Oak Street parking rules.md"
ITEM = DriveItem(item_id="item-1", name="Oak Street parking rules.md", path=PATH, is_folder=False)


def _collected(res, handle="r1"):
    outcome = DriveOutcome(op=DriveOp.PASSAGES, cls=OutcomeClass.OK, text="x", success=True, items=(ITEM,),
                           facts=ReadFacts(excerpts=True), excerpts=res, request_text="", ref_tag="a1")
    return CollectedResult(handle=handle, step=1, kind="passages", outcome=outcome)


def _request(first, last, page):
    return ExcerptRangeRequest("drive:item-1", first, last, page=page)


def _file():
    return excerpt_file("item-1", PATH, [chunk("item-1", 0, "Clause 1.")])


def _span(**kw):
    base = dict(first=1, last=10, total=25, version=1, next=11, path=PATH, ref="drive:item-1")
    base.update(kw)
    return PageSpan(**base)


def test_a_page_with_a_next():
    line = R.searched_line([_collected(result(_file(), request=_request(1, 10, True), page=_span()))])
    assert "pages: " + R.SEARCHED_PAGE_NEXT.format(path=quoted(PATH), first=1, last=10, next=11) in line
    assert "neighbours" not in line


def test_the_end_of_the_file():
    span = _span(first=21, last=25, next=None, end=True)
    line = R.searched_line([_collected(result(_file(), request=_request(21, 30, True), page=span))])
    assert R.SEARCHED_PAGE_END.format(path=quoted(PATH), first=21, last=25) in line


def test_past_the_end():
    span = _span(first=30, last=29, next=None, past_end=True)
    line = R.searched_line([_collected(result(request=_request(30, 39, True), page=span))])
    assert R.SEARCHED_PAGE_PAST_END.format(path=quoted(PATH), first=30) in line


def test_a_neighbour_range_stays_under_neighbours():
    line = R.searched_line([_collected(result(_file(), request=_request(6, 8, False)))])
    assert f"neighbours: {quoted(PATH)} passages 6–8" in line and "pages:" not in line


def test_both_in_one_run_keep_the_part_order():
    page = _collected(result(_file(), request=_request(1, 10, True), page=_span()), "r1")
    near = _collected(result(_file(), request=_request(6, 8, False)), "r2")
    line = R.searched_line([page, near])
    assert line.index("neighbours:") < line.index("pages:")
