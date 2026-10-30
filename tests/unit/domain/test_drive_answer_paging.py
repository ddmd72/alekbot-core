"""Smart's side of paging and the open bound (USER_DRIVE_INDEXING_RFC §29.6 L13): facts choose the reading lines, a
refusal is REFUSED and never settled away, a page arms nothing a refusal does not. Lines referenced by constant."""
from src.domain import drive_answer_texts as t
from src.domain import drive_tool_texts as tools
from src.domain.drive_answer import compose_answer
from src.domain.drive_excerpt_page import PageSpan
from src.domain.drive_excerpt_range_request import ExcerptRangeRequest
from src.domain.drive_index_params import OPEN_MAX_CHARS
from src.domain.drive_op import DriveOp
from src.domain.drive_open_bound import refusal_text
from src.domain.drive_open_bound_info import OpenBoundInfo
from src.domain.drive_outcome import DriveOutcome
from src.domain.drive_outcome_class import OutcomeClass
from src.domain.drive_pass_through import PassThroughCollector
from src.domain.drive_read_facts import ReadFacts
from src.domain.drive_report import DriveReport
from src.domain.drive_status import DriveStatus
from src.domain.drive_text_extent import ExtentState, TextExtent
from src.domain.user_drive import DriveItem
from tests.unit.domain.drive_excerpt_builders import chunk, excerpt_file, nonces, result

PATH = "Docs/Oak Street parking rules.md"
ITEM = DriveItem(item_id="item-1", name="Oak Street parking rules.md", path=PATH, is_folder=False)
OTHER = DriveItem(item_id="item-2", name="Garage notes.md", path="Docs/Garage notes.md", is_folder=False)
EXTENT = TextExtent(ExtentState.PAGEABLE, text_chars=200_000, passages=130, version=1, pages_total=130)


def _file(*indexes):
    return excerpt_file("item-1", PATH, [chunk("item-1", i, f"Clause {i + 1}. The gate stays closed.") for i in indexes])


def _find():
    res = result(_file(2, 3))
    return DriveOutcome(op=DriveOp.PASSAGES, cls=OutcomeClass.OK, text="x", success=True, items=(ITEM,),
                        facts=ReadFacts(excerpts=True), excerpts=res, request_text="gate hours", ref_tag="a1")


def _page(first=1, last=10, nxt=11):
    span = PageSpan(first=first, last=last, total=130, version=1, next=nxt, path=PATH, ref="drive:item-1")
    res = result(_file(*range(first - 1, last)), request=ExcerptRangeRequest("drive:item-1", first, first + 9, page=True),
                 page=span)
    return DriveOutcome(op=DriveOp.PASSAGES, cls=OutcomeClass.OK, text="page", success=True, items=(ITEM,),
                        facts=ReadFacts(excerpts=True, paged=True), excerpts=res, ref_tag="a1")


def _refusal(item=ITEM):
    return DriveOutcome(op=DriveOp.OPEN, cls=OutcomeClass.REFUSED, text=refusal_text(item.path, EXTENT), success=False,
                        items=(item,), facts=ReadFacts(too_long=True), open_bound=OpenBoundInfo(EXTENT), ref_tag="a1")


def _open(item, *, cut=False):
    return DriveOutcome(op=DriveOp.OPEN, cls=OutcomeClass.OK, text="BODY of " + item.name, success=True,
                        items=(item,), body=True, facts=ReadFacts(cut=cut), ref_tag="a1")


def _answer(*entries):
    c = PassThroughCollector()
    for step, kind, outcome in entries:
        c.add(step, kind, outcome)
    return compose_answer(c, DriveReport(), nonce=nonces())


def _reading(answer):
    return [line[len(t.READING_PREFIX):] for line in answer.text.splitlines() if line.startswith(t.READING_PREFIX)]


def test_find_only_has_excerpts_and_no_paged():
    reading = _reading(_answer((1, t.KIND_PASSAGES, _find())))
    assert t.R_EXCERPTS in reading and t.R_PAGED not in reading and t.R_TOO_LONG not in reading


def test_page_only_has_paged_and_no_excerpts():
    reading = _reading(_answer((1, t.KIND_PASSAGES, _page())))
    assert t.R_PAGED in reading and t.R_EXCERPTS not in reading


def test_a_mixed_run_has_both():
    reading = _reading(_answer((1, t.KIND_PASSAGES, _find()), (2, t.KIND_PASSAGES, _page())))
    assert t.R_EXCERPTS in reading and t.R_PAGED in reading


def test_a_past_the_end_page_still_reads_as_paged():
    span = PageSpan(first=300, last=299, total=130, version=1, past_end=True, path=PATH, ref="drive:item-1")
    past = DriveOutcome(op=DriveOp.PASSAGES, cls=OutcomeClass.OK, text="past", success=True, items=(ITEM,),
                        facts=ReadFacts(paged=True),
                        excerpts=result(request=ExcerptRangeRequest("drive:item-1", 300, 309, page=True), page=span), ref_tag="a1")
    answer = _answer((1, t.KIND_PASSAGES, past))
    assert t.R_PAGED in _reading(answer) and answer.status is DriveStatus.OK


def test_a_refusal_alone_is_refused_with_its_text_as_what_to_do():
    answer = _answer((1, t.KIND_OPEN, _refusal()))
    assert answer.status is DriveStatus.REFUSED
    assert t.WHAT_TO_DO.format(line=refusal_text(PATH, EXTENT)) in answer.text
    assert t.R_TOO_LONG in _reading(answer) and t.R_NUMBERS not in _reading(answer)


def test_a_refusal_then_a_page_is_ok_and_keeps_the_refusal_block():
    answer = _answer((1, t.KIND_OPEN, _refusal()), (2, t.KIND_PASSAGES, _page()))
    assert answer.status is DriveStatus.OK
    assert answer.text.count(refusal_text(PATH, EXTENT)) == 1   # the block only: "what to do" does not come from it
    reading = _reading(answer)
    assert t.R_PAGED in reading and t.R_TOO_LONG in reading


def test_a_refusal_is_not_settled_by_another_files_open():
    answer = _answer((1, t.KIND_OPEN, _refusal()), (2, t.KIND_OPEN, _open(OTHER)))
    assert refusal_text(PATH, EXTENT) in answer.text and "BODY of Garage notes.md" in answer.text
    assert answer.status is DriveStatus.OK


def test_a_refusal_is_not_settled_by_the_same_files_open_either():
    answer = _answer((1, t.KIND_OPEN, _refusal()), (2, t.KIND_OPEN, _open(ITEM)))
    assert refusal_text(PATH, EXTENT) in answer.text


def test_a_cut_open_says_too_long_and_not_numbers():
    reading = _reading(_answer((1, t.KIND_OPEN, _open(ITEM, cut=True))))
    assert t.R_TOO_LONG in reading and t.R_NUMBERS not in reading
    assert t.R_DOC_VS_MEMORY in reading and t.R_NOT_READ in reading


def test_a_whole_open_keeps_numbers_and_not_too_long():
    reading = _reading(_answer((1, t.KIND_OPEN, _open(ITEM))))
    assert t.R_NUMBERS in reading and t.R_TOO_LONG not in reading


def test_a_whole_open_beside_a_cut_one_keeps_numbers():
    reading = _reading(_answer((1, t.KIND_OPEN, _open(ITEM, cut=True)), (2, t.KIND_OPEN, _open(OTHER))))
    assert t.R_NUMBERS in reading and t.R_TOO_LONG in reading


def test_the_printed_refusal_names_no_tool():
    for name in (tools.FIND_PASSAGES, tools.READ_PASSAGES, tools.OPEN_DRIVE_FILE):
        assert name not in refusal_text(PATH, EXTENT)


def test_the_reading_lines_carry_the_cap():
    assert f"{OPEN_MAX_CHARS:,}" in t.R_TOO_LONG
