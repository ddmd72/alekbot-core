"""The drive's history lines (DRIVE_SPECIALIST_RFC §7.4): each collected result's entries name what was done, and the
turn's entries render as one line per file, deduplicated, operations combined, the ref kept. Invented items only."""
import pytest

from src.domain import drive_answer_texts as t
from src.domain.drive_history import drive_history_lines, history_entries
from src.domain.drive_op import DriveOp
from src.domain.drive_outcome import DriveOutcome
from src.domain.drive_outcome_class import OutcomeClass
from src.domain.drive_pass_through import PassThroughCollector
from src.domain.user_drive import DriveItem, parse_drive_ref
from tests.unit.domain.drive_excerpt_builders import chunk, excerpt_file, result

LAB = DriveItem(item_id="abc123", name="lab-report.pdf", path="Health/lab-report.pdf", is_folder=False)
LEASE = DriveItem(item_id="def456", name="Lisbon lease.pdf", path="Home/Lisbon lease.pdf", is_folder=False)
HOME = DriveItem(item_id="fold01", name="Home", path="Home", is_folder=True)


def _out(op, *items, **kw):
    return DriveOutcome(op=op, cls=OutcomeClass.OK, text="x", success=True, items=items, **kw)


def _search(*items, query="lab report"):
    return _out(DriveOp.SEARCH, *items, request_text=query)


def _passages(item, *indexes):
    f = excerpt_file(item.item_id, item.path, [chunk(item.item_id, i, f"passage {i}") for i in indexes])
    return _out(DriveOp.PASSAGES, item, excerpts=result(f, question="LAB-2025-03-0417 values"))


def _lines(*results):
    c = PassThroughCollector()
    for kind, outcome in results:
        c.add(1, kind, outcome)
    return drive_history_lines([c.drive_context()])   # Smart's engine wraps one commission one level


def test_a_search_names_its_query_and_file_count_and_keeps_the_ref():
    assert _lines((t.KIND_SEARCH, _search(LAB, LEASE))) == [
        '[Drive: Health/lab-report.pdf ref=drive:abc123 — searched for "lab report" (2 files)]',
        '[Drive: Home/Lisbon lease.pdf ref=drive:def456 — searched for "lab report" (2 files)]']


def test_one_file_touched_three_times_is_one_line_with_its_operations_combined():
    lines = _lines((t.KIND_SEARCH, _search(LAB)), (t.KIND_PASSAGES, _passages(LAB, 2, 3, 4)),
                   (t.KIND_PASSAGES, _passages(LAB, 7)), (t.KIND_SEARCH, _search(LAB)))
    assert lines == ['[Drive: Health/lab-report.pdf ref=drive:abc123 — searched for "lab report" (1 file); '
                     'read passages 3–5, 8]']


def test_passage_numbers_are_display_numbers_merged_across_calls():
    assert _lines((t.KIND_PASSAGES, _passages(LAB, 4)), (t.KIND_PASSAGES, _passages(LAB, 3)))[0].endswith(
        "— read passages 4–5]")
    assert _lines((t.KIND_PASSAGES, _passages(LAB, 0)))[0].endswith("— read passage 1]")


def test_a_passages_call_that_showed_nothing_new_still_names_the_read():
    assert _lines((t.KIND_PASSAGES, _passages(LAB)))[0].endswith("— read passages]")


@pytest.mark.parametrize("kind, outcome, phrase", [
    (t.KIND_OPEN, _out(DriveOp.OPEN, LAB, body=True), "opened whole"),
    (t.KIND_OPEN, _out(DriveOp.OPEN, LAB, file_data={"path": "/tmp/x.pdf", "mime_type": "application/pdf"}),
     "opened whole"),
    (t.KIND_OPEN, _out(DriveOp.OPEN, LAB), "open answered without its content"),
    (t.KIND_LINK, _out(DriveOp.OPEN, LAB), "recording link given"),
    (t.KIND_LISTING, _out(DriveOp.LIST, LAB), "listed"),
    (t.KIND_DELETED_SEARCH, _out(DriveOp.SEARCH, LAB), "searched deleted recordings and notes (1 file)"),
    (DriveOp.SAVE_TEXT.value, _out(DriveOp.SAVE_TEXT, LAB), "changed: saved"),
    (DriveOp.COPY.value, _out(DriveOp.COPY, LAB), "changed: copied in"),
    (DriveOp.APPEND.value, _out(DriveOp.APPEND, LAB), "changed: appended to"),
    (DriveOp.REPLACE.value, _out(DriveOp.REPLACE, LAB), "changed: content replaced"),
    (DriveOp.CREATE_FOLDER.value, _out(DriveOp.CREATE_FOLDER, LAB), "changed: created"),
    (DriveOp.NOTE_READ.value, _out(DriveOp.NOTE_READ, LAB), "note_read"),
])
def test_each_operation_is_named(kind, outcome, phrase):
    assert _lines((kind, outcome)) == [f"[Drive: Health/lab-report.pdf ref=drive:abc123 — {phrase}]"]


def test_a_move_keeps_one_line_under_the_latest_path():
    moved = DriveItem(item_id="abc123", name="lab-report.pdf", path="Archive/lab-report.pdf", is_folder=False)
    assert _lines((t.KIND_SEARCH, _search(LAB)), (DriveOp.MOVE.value, _out(DriveOp.MOVE, moved))) == [
        '[Drive: Archive/lab-report.pdf ref=drive:abc123 — searched for "lab report" (1 file); '
        'changed: moved or renamed]']


def test_a_listing_names_the_folder_and_each_file_once():
    assert _lines((t.KIND_LISTING, _out(DriveOp.LIST, HOME, LEASE))) == [
        "[Drive: Home ref=drive:fold01 — listed]", "[Drive: Home/Lisbon lease.pdf ref=drive:def456 — listed]"]


def test_a_failed_or_not_started_operation_touches_no_file():
    failed = DriveOutcome(op=DriveOp.MOVE, cls=OutcomeClass.FAILED, text="no", success=False)
    assert _lines((DriveOp.MOVE.value, failed)) == []


def test_a_search_without_query_words_and_a_long_query():
    assert _lines((t.KIND_SEARCH, _search(LAB, query="")))[0].endswith("— searched (1 file)]")
    long = "lab " * 60
    line = _lines((t.KIND_SEARCH, _search(LAB, query=f'{long} "x"')))[0]
    assert '"x"' not in line and "…\" (1 file)]" in line and line.count('"') == 2


def test_two_commissions_in_one_turn_render_in_order_deduplicated():
    first = PassThroughCollector()
    first.add(1, t.KIND_SEARCH, _search(LAB, LEASE))
    second = PassThroughCollector()
    second.add(1, t.KIND_OPEN, _out(DriveOp.OPEN, LEASE, body=True))
    assert drive_history_lines([first.drive_context(), second.drive_context()]) == [
        '[Drive: Health/lab-report.pdf ref=drive:abc123 — searched for "lab report" (2 files)]',
        '[Drive: Home/Lisbon lease.pdf ref=drive:def456 — searched for "lab report" (2 files); opened whole]']


def test_the_older_flat_shape_renders_each_file_once_with_its_ref():
    legacy = [{"path": "Home/Lisbon lease.pdf", "ref": "drive:def456"},
              {"path": "Home/Lisbon lease.pdf", "ref": "drive:def456"}]
    assert drive_history_lines(legacy) == ["[Drive: Home/Lisbon lease.pdf ref=drive:def456]"]
    assert drive_history_lines([legacy]) == ["[Drive: Home/Lisbon lease.pdf ref=drive:def456]"]


@pytest.mark.parametrize("value", [None, "", "text", 7, [], [[]], [None, 3, "x"], [{}], [{"op": "listed"}],
                                   [[[[[{"path": "a", "ref": "drive:a"}]]]]]])
def test_shapes_no_writer_produces_never_crash_and_render_nothing(value):
    assert drive_history_lines(value) == []


def test_odd_fields_are_tolerated():
    entries = [{"path": "Home/Lisbon lease.pdf", "ref": "drive:def456", "op": "passages", "passages": "3"},
               {"path": "Home/Lisbon lease.pdf", "ref": "drive:def456", "op": "passages", "passages": [2, "x", None]},
               {"path": "Home/Lisbon lease.pdf", "ref": "drive:def456", "op": "searched", "count": "many"},
               {"path": "Home/Lisbon lease.pdf", "ref": "drive:def456", "op": "changed"},
               {"path": "Home/Lisbon lease.pdf", "ref": "drive:def456", "op": "changed", "change": "shred"},
               {"path": "Home/Lisbon lease.pdf", "ref": "drive:def456", "op": "  "},
               {"path": "Notes/a.txt"}, {"path": "", "ref": "drive:root", "op": "listed"}]
    assert drive_history_lines(entries) == [
        "[Drive: Home/Lisbon lease.pdf ref=drive:def456 — read passage 2; searched; changed; changed: shred]",
        "[Drive: Notes/a.txt]", "[Drive: / ref=drive:root — listed]"]


def test_the_ref_parses_back_from_the_line_and_from_its_ref_token():
    line = _lines((t.KIND_SEARCH, _search(LAB)), (t.KIND_PASSAGES, _passages(LAB, 2)))[0]
    token = next(w for w in line.split() if w.startswith("ref="))
    assert parse_drive_ref(line) == parse_drive_ref(token) == parse_drive_ref(token[len("ref="):]) == "abc123"


def test_entries_keep_todays_path_and_ref():
    entry = history_entries(t.KIND_SEARCH, _search(LAB))[0]
    assert (entry["path"], entry["ref"], entry["op"], entry["query"], entry["count"]) == (
        "Health/lab-report.pdf", "drive:abc123", "searched", "lab report", 1)
