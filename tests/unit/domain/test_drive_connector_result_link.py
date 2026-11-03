"""compose_connector_result with a one-time download link (DRIVE_MCP_CONNECTOR_RFC §6.2, §8): where the LINK line goes,
over marker texts. Invented items only."""
from src.domain import drive_connector_contract as c
from src.domain.drive_connector_result import compose_connector_result
from src.domain.drive_op import DriveOp
from src.domain.drive_outcome import DriveOutcome
from src.domain.drive_outcome_class import OutcomeClass
from src.domain.drive_withheld import Withheld
from src.domain.user_drive import DriveItem
from tests.support.drive_connector_texts import marker, marker_texts

TEXTS = marker_texts(LINK="<LINK {link}>")
URL = "https://mcp.example.test/d/Qk7rTz0aB1cD2eF3gH4iJ5"
IMAGE = DriveItem(item_id="i1", name="12 Oak Street.png", path="12 Oak Street/12 Oak Street.png", is_folder=False,
                  size_bytes=2000, mime_type="image/png")
LABEL = "[Drive: 12 Oak Street/FA-0137.pdf (12 KB) ref=drive:gd:f1]"


def lines(outcome: DriveOutcome, link=None) -> list:
    return compose_connector_result(outcome, TEXTS, link=link).split("\n")


def test_a_withheld_open_with_a_link_prints_the_link_line_after_its_label():
    outcome = DriveOutcome(op=DriveOp.OPEN, cls=OutcomeClass.OK, text="", success=True, items=(IMAGE,),
                           withheld=Withheld.IMAGE, ref_tag="gd")
    without = lines(outcome)
    assert lines(outcome, URL) == [*without, f"<LINK {URL}>"]
    assert without[1] == marker(c.WHAT_TO_DO).format(line=marker(c.text_key_of_withheld(Withheld.IMAGE)))


def test_a_link_with_no_withheld_code_is_the_what_to_do_line_and_the_label_only():
    outcome = DriveOutcome(op=DriveOp.OPEN, cls=OutcomeClass.OK, text=LABEL, success=True)
    assert lines(outcome, URL) == [marker(c.STATUS).format(status="ok"),
                                   marker(c.WHAT_TO_DO).format(line=f"<LINK {URL}>"), LABEL]


def test_no_link_keeps_the_result_as_before():
    outcome = DriveOutcome(op=DriveOp.OPEN, cls=OutcomeClass.OK, text=LABEL, success=True)
    assert lines(outcome) == compose_connector_result(outcome, TEXTS).split("\n")
    assert "<LINK" not in "\n".join(lines(outcome))


def test_a_refusal_never_prints_a_link():
    outcome = DriveOutcome(op=DriveOp.OPEN, cls=OutcomeClass.REFUSED, text="", success=False)
    assert URL not in "\n".join(lines(outcome, URL))
