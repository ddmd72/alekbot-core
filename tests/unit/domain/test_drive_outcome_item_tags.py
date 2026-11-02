"""`DriveOutcome.item_tags` (MULTI_DRIVE_RFC §11.4; design §1.9): a search across connections tags each item with its
own connection; without it every item takes `ref_tag` (today). Also the frozen `ref_tag` / `also_on` fields of a search
card and an excerpt file."""
import pytest

from src.domain.drive_excerpt_file import ExcerptFile
from src.domain.drive_op import DriveOp
from src.domain.drive_outcome import DriveOutcome
from src.domain.drive_outcome_class import OutcomeClass
from src.domain.drive_search import DriveSearchCard
from src.domain.user_drive import DriveItem

PLAN = DriveItem(item_id="item-1", name="Tom's plan.docx", path="R&D/Tom's plan.docx", is_folder=False)
LAB = DriveItem(item_id="item-1", name="lab-report.pdf", path="lab-report.pdf", is_folder=False)   # same id, drive B


def outcome(**over):
    return DriveOutcome(op=DriveOp.SEARCH, cls=OutcomeClass.OK, text="", success=True, **over)


def test_without_item_tags_every_item_uses_ref_tag():
    out = outcome(items=(PLAN, LAB), ref_tag="a1")
    assert [e["ref"] for e in out.drive_context()] == ["drive:a1:item-1", "drive:a1:item-1"]
    assert out.ref_of(LAB) == "drive:a1:item-1"


def test_item_tags_give_each_item_its_own_connection():
    out = outcome(items=(PLAN, LAB), item_tags=("a1", "b2"))
    assert out.drive_context() == [{"path": "R&D/Tom's plan.docx", "ref": "drive:a1:item-1"},
                                   {"path": "lab-report.pdf", "ref": "drive:b2:item-1"}]
    assert out.ref_of(LAB) == "drive:b2:item-1" and out.ref_of(PLAN) == "drive:a1:item-1"
    assert out.label_of(LAB) == "[Drive: lab-report.pdf (0B) ref=drive:b2:item-1]"


def test_an_equal_item_object_finds_its_tag():
    copy = DriveItem(item_id="item-1", name="lab-report.pdf", path="lab-report.pdf", is_folder=False)
    assert outcome(items=(PLAN, LAB), item_tags=("a1", "b2")).ref_of(copy) == "drive:b2:item-1"


@pytest.mark.parametrize("tags", [("a1",), ("a1", ""), ("a1", "not a tag")])
def test_item_tags_need_one_valid_tag_per_item(tags):
    with pytest.raises(ValueError):
        outcome(items=(PLAN, LAB), item_tags=tags)


def test_items_without_any_tag_are_still_refused():
    with pytest.raises(ValueError):
        outcome(items=(PLAN,))


def test_card_and_excerpt_file_carry_their_tag():
    card = DriveSearchCard(ref="drive:b2:item-1", item_id="item-1", name="lab-report.pdf", path="lab-report.pdf",
                           kind="document", is_folder=False, summary="")
    assert (card.ref_tag, card.also_on) == ("", ())
    excerpt = ExcerptFile(item_id="item-1", ref="drive:b2:item-1", name="lab-report.pdf", path="lab-report.pdf",
                          kind="document", generation=1, chunk_count=1, ref_tag="b2")
    assert excerpt.ref_tag == "b2"


def test_make_card_stamps_the_connections_tag():
    from src.domain.drive_index import DriveIndexRecord, RecordState, Scope
    from src.domain.drive_search import make_card
    rec = DriveIndexRecord(user_id="usr-1", connection_id="conn-2", item_id="item-1", name="lab-report.pdf",
                           is_folder=False, kind="pdf", scope=Scope.NORMAL, has_card=True, state=RecordState.PENDING)
    card = make_card(rec, "lab-report.pdf", "lab-report.pdf | PDF document", "n0000000", "b2")
    assert (card.ref, card.ref_tag, card.also_on) == ("drive:b2:item-1", "b2", ())
