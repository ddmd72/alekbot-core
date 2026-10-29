"""The ledger keeps what a find matched apart from what a page read (USER_DRIVE_INDEXING_RFC §29.6 L11)."""
from src.domain import drive_excerpt_render as R
from src.domain.drive_excerpt_context_plan import ContextPlan
from src.domain.drive_excerpt_ledger import ExcerptLedger
from src.domain.drive_excerpt_page import PageSpan
from src.domain.drive_excerpt_range_request import ExcerptRangeRequest
from tests.unit.domain.drive_excerpt_builders import chunk, excerpt_file, nonces, result

PATH = "Docs/Oak Street parking rules.md"


def _file(*indexes):
    return excerpt_file("item-1", PATH, [chunk("item-1", i, f"Clause {i + 1}. The gate stays closed.") for i in indexes])


def _page(first, last, nxt, total=25, **kw):
    span = PageSpan(first=first, last=last, total=total, version=1, next=nxt, path=PATH, ref="drive:item-1", **kw)
    return result(_file(*range(first - 1, last)), request=ExcerptRangeRequest("drive:item-1", first, first + 9, page=True),
                  page=span)


def _read(block):
    return [line for line in block.splitlines() if line.startswith("pages read:")]


def test_page_then_find_marks_only_the_finds_picks():
    ledger = ExcerptLedger()
    ledger.add(_page(1, 10, 11), ContextPlan.EMPTY)
    shown = ledger.keys()
    find = result(_file(12, 13), repeated=(("item-1", 1, 4),))
    assert not (shown & {c.key for f in find.files for c in f.chunks})
    ledger.add(find, ContextPlan.EMPTY)
    block = ledger.block(nonce=nonces())
    assert R.OPENING_BLOCK_MIXED in block.splitlines()
    assert R.NEAREST.format(numbers="5, 13–14") in _read(block)[0]
    assert block.count("Clause 5.") == 1


def test_find_then_page_delivers_the_page_whole():
    ledger = ExcerptLedger()
    ledger.add(result(_file(2, 3)), ContextPlan.EMPTY)
    ledger.add(_page(1, 10, 11), ContextPlan.EMPTY)
    block = ledger.block(nonce=nonces())
    for n in range(1, 11):
        assert block.count(f"Clause {n}.") == 1
    assert "nearest the question: 3–4" in _read(block)[0]


def test_two_pages_give_one_line_with_merged_ranges_and_the_latest_next():
    ledger = ExcerptLedger()
    ledger.add(_page(1, 10, 11), ContextPlan.EMPTY)
    ledger.add(_page(11, 20, 21), ContextPlan.EMPTY)
    reads = _read(ledger.block(nonce=nonces()))
    assert len(reads) == 1 and "passages 1–20 of 25" in reads[0] and reads[0].endswith(R.PAGES_READ_NEXT.format(next=21))


def test_a_find_only_run_has_no_pages_and_the_old_opening():
    ledger = ExcerptLedger()
    ledger.add(result(_file(0, 1)), ContextPlan.EMPTY)
    block = ledger.block(nonce=nonces())
    assert R.OPENING_BLOCK in block.splitlines() and not _read(block)


def test_a_past_the_end_page_adds_no_line():
    ledger = ExcerptLedger()
    ledger.add(result(_file(0), request=ExcerptRangeRequest("drive:item-1", 1, 1, page=False)), ContextPlan.EMPTY)
    past = PageSpan(first=30, last=29, total=25, version=1, past_end=True, path=PATH, ref="drive:item-1")
    ledger.add(result(request=ExcerptRangeRequest("drive:item-1", 30, 39, page=True), page=past), ContextPlan.EMPTY)
    assert not _read(ledger.block(nonce=nonces()))
