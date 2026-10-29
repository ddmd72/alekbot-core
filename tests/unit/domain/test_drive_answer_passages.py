"""Smart's side of a passages run (USER_DRIVE_INDEXING_RFC §28.3 A11-A13, A18): every retrieved passage once, in one
consolidated block from the collector's ledger, the Searched line instead of the specialist's note, and the language
line last. Lines are referenced by constant, never by sentence. Invented items only."""
import re

from src.domain import drive_answer_texts as t
from src.domain.drive_answer import compose_answer, reading_lines
from src.domain.drive_excerpt_render import OPENING_BLOCK, searched_line
from src.domain.drive_op import DriveOp
from src.domain.drive_outcome import DriveOutcome
from src.domain.drive_outcome_class import OutcomeClass
from src.domain.drive_pass_through import PassThroughCollector
from src.domain.drive_read_facts import ReadFacts
from src.domain.drive_report import DriveReport
from src.domain.drive_status import DriveStatus
from src.domain.user_drive import DriveItem
from tests.unit.domain.drive_excerpt_builders import LEASE, TOMS_PLAN, chunk, excerpt_file, nonces, result

PLAN_ITEM = DriveItem(item_id="item-1", name="Tom's plan.docx", path=TOMS_PLAN, is_folder=False)
LEASE_ITEM = DriveItem(item_id="item-2", name=LEASE, path=LEASE, is_folder=False)
NOTE = "Look in the R&D folder first."


def _text(item_id, index):
    return f"Passage {index} of {item_id}: Alex Doe reviews the Acme draft."


def _file(*indexes, item_id="item-1", path=TOMS_PLAN):
    return excerpt_file(item_id, path, [chunk(item_id, i, _text(item_id, i)) for i in indexes])


def _passages(*files, owner_note=False, question="How does the review process work?"):
    items = tuple(PLAN_ITEM if f.item_id == "item-1" else LEASE_ITEM for f in files)
    return DriveOutcome(op=DriveOp.PASSAGES, cls=OutcomeClass.OK, text=f"PER-CALL TEXT {question}", success=True,
                        items=items, facts=ReadFacts(excerpts=bool(files), owner_note=owner_note),
                        excerpts=result(*files, question=question), request_text=question)


def _answer(*entries, report=DriveReport(), **kw):
    c = PassThroughCollector()
    for step, kind, outcome in entries:
        c.add(step, kind, outcome)
    return c, compose_answer(c, report, nonce=nonces(), **kw)


SEARCH = DriveOutcome(op=DriveOp.SEARCH, cls=OutcomeClass.OK, text="REMINDER\n<card>", success=True,
                      items=(PLAN_ITEM,), facts=ReadFacts(cards=1), request_text="Tom's plan")


def test_two_passages_calls_give_one_block_and_no_per_call_block():
    _, answer = _answer((1, t.KIND_PASSAGES, _passages(_file(0))), (2, t.KIND_PASSAGES, _passages(_file(4))))
    assert answer.text.count(OPENING_BLOCK) == 1
    assert answer.text.count(t.RESULT_HEAD_PASSAGES.format(handles="r1, r2", kind=t.KIND_PASSAGES)) == 1
    assert "PER-CALL TEXT" not in answer.text
    assert t.RESULT_HEAD.format(handle="r1", step=1, kind=t.KIND_PASSAGES) not in answer.text
    assert answer.status is DriveStatus.OK


def test_files_in_first_retrieval_order_chunks_in_index_order_each_once():
    _, answer = _answer((1, t.KIND_PASSAGES, _passages(_file(5), _file(0, item_id="item-2", path=LEASE))),
                        (2, t.KIND_PASSAGES, _passages(_file(3, item_id="item-2", path=LEASE), _file(1, 5))))
    order = [(m.group(2), int(m.group(1))) for m in re.finditer(r"Passage (\d+) of (item-\d)", answer.text)]
    assert order == [("item-1", 1), ("item-1", 5), ("item-2", 0), ("item-2", 3)]


def test_adjacent_chunks_print_as_one_run():
    _, answer = _answer((1, t.KIND_PASSAGES, _passages(_file(2))), (2, t.KIND_PASSAGES, _passages(_file(3))))
    assert "[passages 3–4]" in answer.text and "[passages 3–3]" not in answer.text


def test_a_cut_run_delivers_every_passage_collected():
    _, answer = _answer((1, t.KIND_PASSAGES, _passages(_file(0))), (2, t.KIND_PASSAGES, _passages(_file(7))),
                        cut=True)
    assert answer.status is DriveStatus.UNFINISHED
    assert _text("item-1", 0) in answer.text and _text("item-1", 7) in answer.text
    assert answer.text.splitlines()[-1] == t.LANGUAGE_LINE


def test_passages_facts_still_choose_the_reading_lines():
    _, answer = _answer((1, t.KIND_PASSAGES, _passages(_file(0), owner_note=True)))
    assert t.R_EXCERPTS in answer.reading and t.R_OWNER_NOTE in answer.reading
    assert answer.text.count(t.READING_PREFIX + t.R_EXCERPTS) == 1


def test_r_excerpts_fires_only_from_the_excerpts_fact():
    c = PassThroughCollector()
    c.add(1, t.KIND_SEARCH, SEARCH)
    assert t.R_EXCERPTS not in reading_lines(list(c.results))
    c.add(2, t.KIND_PASSAGES, _passages(_file(0)))
    assert reading_lines(list(c.results)).count(t.R_EXCERPTS) == 1


def test_the_owner_note_line_fires_from_a_passages_outcome_alone():
    c = PassThroughCollector()
    c.add(1, t.KIND_PASSAGES, _passages(_file(0), owner_note=True))
    assert t.R_OWNER_NOTE in reading_lines(list(c.results))
    c2 = PassThroughCollector()
    c2.add(1, t.KIND_PASSAGES, _passages(_file(0)))
    assert t.R_OWNER_NOTE not in reading_lines(list(c2.results))


def test_the_language_line_is_last_after_the_other_blocks():
    _, answer = _answer((1, t.KIND_SEARCH, SEARCH), (2, t.KIND_PASSAGES, _passages(_file(0))))
    lines = answer.text.splitlines()
    assert lines[-1] == t.LANGUAGE_LINE and answer.text.count(t.LANGUAGE_LINE) == 1
    search_head = lines.index(t.RESULT_HEAD.format(handle="r1", step=1, kind=t.KIND_SEARCH))
    block_head = lines.index(t.RESULT_HEAD_PASSAGES.format(handles="r2", kind=t.KIND_PASSAGES))
    assert search_head < block_head


def test_the_searched_line_replaces_the_note_when_the_ledger_holds_chunks():
    c, answer = _answer((1, t.KIND_SEARCH, SEARCH), (2, t.KIND_PASSAGES, _passages(_file(0))),
                        report=DriveReport(note=NOTE))
    searched = searched_line(c.results)
    assert searched.startswith(t.SEARCHED_PREFIX)
    assert searched in answer.text.splitlines()
    assert t.NOTE_PREFIX not in answer.text and NOTE not in answer.text


def test_a_passages_call_with_no_passage_keeps_the_note_and_its_own_block():
    empty = _passages()
    _, answer = _answer((1, t.KIND_PASSAGES, empty), report=DriveReport(note=NOTE))
    assert t.NOTE_PREFIX + NOTE in answer.text
    assert t.SEARCHED_PREFIX not in answer.text and t.LANGUAGE_LINE not in answer.text
    assert t.RESULT_HEAD.format(handle="r1", step=1, kind=t.KIND_PASSAGES) in answer.text
    assert empty.text in answer.text and OPENING_BLOCK not in answer.text


def test_a_run_without_passages_keeps_the_note_and_has_no_passages_lines():
    _, answer = _answer((1, t.KIND_SEARCH, SEARCH), report=DriveReport(note=NOTE))
    assert t.NOTE_PREFIX + NOTE in answer.text
    for absent in (t.SEARCHED_PREFIX, t.LANGUAGE_LINE, OPENING_BLOCK, t.R_EXCERPTS):
        assert absent not in answer.text


def test_drive_context_has_the_files_and_the_passage_numbers_read():
    """A11 as revised 2026-10-29: each file, in order, with the display numbers read, a record for the history line."""
    _, answer = _answer((1, t.KIND_PASSAGES, _passages(_file(0, 1), _file(2, item_id="item-2", path=LEASE))))
    assert answer.drive_context == [{"path": TOMS_PLAN, "ref": PLAN_ITEM.ref, "op": "passages", "passages": [1, 2]},
                                    {"path": LEASE, "ref": LEASE_ITEM.ref, "op": "passages", "passages": [3]}]


def test_the_default_nonce_wraps_each_file():
    c = PassThroughCollector()
    c.add(1, t.KIND_PASSAGES, _passages(_file(0), _file(1, item_id="item-2", path=LEASE)))
    answer = compose_answer(c, DriveReport())
    assert len(re.findall(r"<drive_document [^>]*nonce=\"[A-Za-z0-9]{8,64}\"", answer.text)) == 2
