"""`reading_keys` is the §7.3 trigger logic as code names, `reading_lines` its chat texts byte for byte, and another
surface's map is applied verbatim (drive MCP connector concept §4.5)."""
import pytest

from src.domain import drive_answer_texts as t
from src.domain.drive_answer import reading_keys, reading_lines, reading_texts
from src.domain.drive_collected_result import CollectedResult
from src.domain.drive_op import DriveOp
from src.domain.drive_outcome import DriveOutcome
from src.domain.drive_outcome_class import OutcomeClass
from src.domain.drive_read_facts import ReadFacts
from src.domain.user_drive import DriveItem

PLAN = DriveItem(item_id="tom-plan", name="Tom's plan.docx", path="R&D/Tom's plan.docx", is_folder=False)
FIND = ("R_FIND", "R_TIERS", "R_TRUST", "R_RELATED", "R_COVERAGE", "R_MANY", "R_ALL")
NOT_FOUND = ("R_BUILDING", "R_NOT_SEARCHED", "R_FOLDER_CARD")
DOCUMENT = ("R_NUMBERS", "R_DOC_VS_MEMORY", "R_NOT_READ")


def _outcome(op, cls=OutcomeClass.OK, text="REMINDER\n<card: R&D/Tom's plan.docx>", **kw):
    return DriveOutcome(op=op, cls=cls, text=text, success=cls is OutcomeClass.OK, items=(PLAN,), ref_tag="a1", **kw)


def _block(outcome, kind=t.KIND_SEARCH, step=1):
    return CollectedResult(handle=f"r{step}", step=step, kind=kind, outcome=outcome)


def _search(**facts):
    return _block(_outcome(DriveOp.SEARCH, facts=ReadFacts(**facts)))


def _passages(**facts):
    return _block(_outcome(DriveOp.PASSAGES, text="invoice FA-0137 for 12 Oak Street", facts=ReadFacts(**facts)),
                  kind=t.KIND_PASSAGES)


def _opened(**kw):
    return _block(_outcome(DriveOp.OPEN, text="<drive_document>invoice FA-0137</drive_document>", **kw),
                  kind=t.KIND_OPEN)


OPENED_BODY = _opened(body=True)
MISS = _block(_outcome(DriveOp.SEARCH, cls=OutcomeClass.NOT_FOUND, text="No matching files or folders."))

# every trigger path of §7.3: (id, blocks, the keys in today's order)
PATHS = [
    ("one card", [_search(cards=1)], FIND),
    ("two cards", [_search(cards=2)], FIND + ("R_VERSIONS",)),
    ("passage", [_search(cards=1, passage=True)], FIND + ("R_PASSAGE",)),
    ("accuracy", [_search(cards=1, accuracy=True)], FIND + ("R_ACCURACY", "R_ACCURACY_FIND", "R_ACCURACY_VALUE")),
    ("low accuracy image", [_search(cards=1, low_accuracy_image=True)], FIND + ("R_ACCURACY_IMAGE",)),
    ("owner note", [_search(cards=1, owner_note=True)], FIND + ("R_OWNER_NOTE",)),
    ("scan", [_search(cards=1, scan=True)], FIND + ("R_SCAN",)),
    ("inaccessible", [_search(cards=1, inaccessible=True)], FIND + ("R_NO_ACCESS",)),
    ("image", [_search(cards=1, image=True)], FIND + ("R_PHOTOS",)),
    ("coverage incomplete", [_search(cards=1, coverage_incomplete=True)], FIND + NOT_FOUND),
    ("building", [_search(cards=1, building=True)], FIND + NOT_FOUND),
    ("not found", [MISS], NOT_FOUND),
    ("deleted search not found", [_block(MISS.outcome, kind=t.KIND_DELETED_SEARCH)], ()),
    ("excerpts", [_passages(excerpts=True)], ("R_EXCERPTS",)),
    ("paged", [_passages(excerpts=True, paged=True)], ("R_PAGED",)),
    ("open too long", [_opened(cls=OutcomeClass.REFUSED, facts=ReadFacts(too_long=True))], ("R_TOO_LONG",)),
    ("opened body", [OPENED_BODY], DOCUMENT),
    ("opened vision file", [_opened(file_data={"path": "/tmp/plan.pdf", "mime_type": "application/pdf"})], DOCUMENT),
    ("opened cut body", [_opened(body=True, facts=ReadFacts(cut=True))],
     ("R_TOO_LONG", "R_DOC_VS_MEMORY", "R_NOT_READ")),
    ("cut and whole bodies", [_opened(body=True, facts=ReadFacts(cut=True)), OPENED_BODY],
     ("R_TOO_LONG",) + DOCUMENT),
    ("open without content", [_opened()], ()),
    ("open owner note", [_opened(body=True, facts=ReadFacts(owner_note=True))], ("R_OWNER_NOTE",) + DOCUMENT),
    ("listing", [_block(_outcome(DriveOp.LIST, text="[Drive: R&D ref=drive:a1:rd]"), kind=t.KIND_LISTING)], ()),
    ("mixed run, each key once in the fixed order",
     [OPENED_BODY, _passages(excerpts=True), _search(cards=1, scan=True), _search(cards=1, scan=True), MISS],
     FIND + ("R_EXCERPTS", "R_SCAN", "R_VERSIONS") + NOT_FOUND + DOCUMENT),
]
IDS = [p[0] for p in PATHS]


@pytest.mark.parametrize("name, blocks, expected", PATHS, ids=IDS)
def test_each_trigger_path_gives_its_keys_in_todays_order(name, blocks, expected):
    assert reading_keys(blocks) == expected


@pytest.mark.parametrize("name, blocks, expected", PATHS, ids=IDS)
def test_the_chat_lines_are_the_chat_map_over_the_keys(name, blocks, expected):
    lines = reading_lines(blocks)
    assert isinstance(lines, list)
    assert lines == [t.CHAT_READING_TEXTS[k] for k in reading_keys(blocks)]
    assert lines == [getattr(t, k) for k in expected]   # today's constants, byte for byte


def test_the_chat_map_is_every_reading_constant_by_its_own_name():
    constants = {k: v for k, v in vars(t).items() if k.startswith("R_") and isinstance(v, str)}
    assert dict(t.CHAT_READING_TEXTS) == constants


def test_the_chat_texts_are_distinct_so_one_key_once_is_one_line_once():
    texts = list(t.CHAT_READING_TEXTS.values())
    assert len(set(texts)) == len(texts)


def test_a_connector_map_is_applied_verbatim():
    connector = {k: f"For the client: {k.lower()} about R&D/Tom's plan.docx" for k in t.CHAT_READING_TEXTS}
    blocks = [_search(cards=2, accuracy=True), OPENED_BODY]
    keys = reading_keys(blocks)
    lines = reading_texts(keys, connector)
    assert lines == [connector[k] for k in keys]
    assert lines[0] == "For the client: r_find about R&D/Tom's plan.docx"
    assert not set(lines) & set(t.CHAT_READING_TEXTS.values())


def test_a_map_without_a_key_fails_loudly():
    with pytest.raises(KeyError):
        reading_texts(("R_FIND", "R_EXCERPTS"), {"R_FIND": "Find."})
