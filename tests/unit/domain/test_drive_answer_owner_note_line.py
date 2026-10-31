"""The owner-note reading line (DRIVE_SPECIALIST_RFC §9.3 B10; U2 design §4.5): added once when any block carries a
note, on a search block with a noted card and on an opened file's note block; never otherwise. Invented items only."""
from src.domain import drive_answer_texts as t
from src.domain.drive_answer import compose_answer, reading_lines
from src.domain.drive_op import DriveOp
from src.domain.drive_outcome import DriveOutcome
from src.domain.drive_outcome_class import OutcomeClass
from src.domain.drive_pass_through import PassThroughCollector
from src.domain.drive_read_facts import ReadFacts
from src.domain.drive_report import DriveReport
from src.domain.user_drive import DriveItem

ITEM = DriveItem(item_id="a", name="bathroom-invoice.md", path="Home/bathroom-invoice.md", is_folder=False)


def _blocks(kind, *outcomes):
    c = PassThroughCollector()
    for o in outcomes:
        c.add(1, kind, o)
    return list(c.results)


def _search(**facts):
    return DriveOutcome(op=DriveOp.SEARCH, cls=OutcomeClass.OK, text="cards", success=True, items=(ITEM,),
                        facts=ReadFacts(cards=1, **facts), ref_tag="a1")


def _open(note: bool):
    return DriveOutcome(op=DriveOp.OPEN, cls=OutcomeClass.OK, text="body", success=True, items=(ITEM,), body=True,
                        facts=ReadFacts(owner_note=note), ref_tag="a1")


def test_a_search_block_with_a_noted_card_adds_the_line_once():
    lines = reading_lines(_blocks(t.KIND_SEARCH, _search(owner_note=True), _search(owner_note=True)))
    assert lines.count(t.R_OWNER_NOTE) == 1


def test_an_open_block_with_a_note_adds_the_line():
    assert t.R_OWNER_NOTE in reading_lines(_blocks(t.KIND_OPEN, _open(True)))


def test_neither_adds_no_line():
    assert t.R_OWNER_NOTE not in reading_lines(_blocks(t.KIND_SEARCH, _search()))
    assert t.R_OWNER_NOTE not in reading_lines(_blocks(t.KIND_OPEN, _open(False)))


def test_the_line_reaches_the_answer_once_with_the_accuracy_line_untouched():
    c = PassThroughCollector()
    c.add(1, t.KIND_SEARCH, _search(owner_note=True, accuracy=True))
    answer = compose_answer(c, DriveReport())
    assert answer.text.count(t.READING_PREFIX + t.R_OWNER_NOTE) == 1
    assert t.R_OWNER_NOTE in answer.reading and t.R_ACCURACY in answer.reading


def test_the_line_says_what_the_owner_note_outranks_and_where_it_sits():
    assert t.R_OWNER_NOTE.startswith("A `noted by the owner:` line is what the user told you about the file.")
    assert "not part of `card accuracy`" in t.R_OWNER_NOTE and "its own block after the file's text" in t.R_OWNER_NOTE
