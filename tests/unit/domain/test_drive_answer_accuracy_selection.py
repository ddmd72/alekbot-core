"""OQ-A2 on Smart's side (USER_DRIVE_INDEXING_RFC §28.3 A13, §28.6): code adds `R_ACCURACY_IMAGE` only when a shown
card scores below 90% and its text is not known to be born-digital (`ReadFacts.low_accuracy_image`), right after the
accuracy lines. Lines are referenced by constant, never by sentence."""
import pytest

from src.domain import drive_answer_texts as t
from src.domain.drive_answer import compose_answer, reading_lines
from src.domain.drive_op import DriveOp
from src.domain.drive_outcome import DriveOutcome
from src.domain.drive_outcome_class import OutcomeClass
from src.domain.drive_pass_through import PassThroughCollector
from src.domain.drive_read_facts import ReadFacts
from src.domain.drive_report import DriveReport
from src.domain.user_drive import DriveItem

SCAN = DriveItem(item_id="s", name="scan_0012.pdf", path="R&D/scan_0012.pdf", is_folder=False)


def _search(**facts):
    return DriveOutcome(op=DriveOp.SEARCH, cls=OutcomeClass.OK, text="cards", success=True, items=(SCAN,),
                        facts=ReadFacts(cards=1, **facts), ref_tag="a1")


def _lines(*outcomes):
    c = PassThroughCollector()
    for o in outcomes:
        c.add(1, t.KIND_SEARCH, o)
    return reading_lines(list(c.results))


def test_a_low_accuracy_card_read_from_an_image_adds_the_image_line_after_the_value_line():
    lines = _lines(_search(accuracy=True, low_accuracy_image=True))
    assert lines.count(t.R_ACCURACY_IMAGE) == 1
    value = lines.index(t.R_ACCURACY_VALUE)
    assert lines[value + 1] == t.R_ACCURACY_IMAGE
    assert lines[value - 2:value] == [t.R_ACCURACY, t.R_ACCURACY_FIND]


@pytest.mark.parametrize("facts", [
    {"accuracy": True},                       # a card at 90% or more, or a born-digital card below it
    {"accuracy": True, "scan": True},         # a scan whose card is trusted: the scan line, not the image line
    {},
])
def test_without_the_fact_there_is_no_image_line(facts):
    lines = _lines(_search(**facts))
    assert t.R_ACCURACY_IMAGE not in lines


def test_the_value_line_stays_with_a_digital_low_accuracy_card():
    lines = _lines(_search(accuracy=True))
    assert t.R_ACCURACY_VALUE in lines and t.R_ACCURACY_IMAGE not in lines


def test_two_searches_add_the_image_line_once():
    lines = _lines(_search(accuracy=True, low_accuracy_image=True), _search(accuracy=True, low_accuracy_image=True))
    assert lines.count(t.R_ACCURACY_IMAGE) == 1


def test_the_image_line_reaches_the_answer_once_with_its_prefix():
    c = PassThroughCollector()
    c.add(1, t.KIND_SEARCH, _search(accuracy=True, low_accuracy_image=True))
    answer = compose_answer(c, DriveReport())
    assert answer.text.count(t.READING_PREFIX + t.R_ACCURACY_IMAGE) == 1
    assert t.R_ACCURACY_IMAGE in answer.reading
