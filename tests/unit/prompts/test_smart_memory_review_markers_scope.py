"""`PROTOCOL_MEMORY_REVIEW_MARKERS` applies only to messages that carry one of the three memory-review markers.

The markers it names are the domain builders' (`src/domain/fact_review_frame.py`), reduced to their prefix up to and
including `q=<id>`, so a builder change that breaks a marker breaks this test."""
from src.domain.fact_review_frame import not_delivered_frame, reply_part, review_frame
from tests.support import prompt_vocabulary as pv

TOKEN_ID = "PROTOCOL_MEMORY_REVIEW_MARKERS"
DOC = pv.ser.token_from_file(pv.token_path(TOKEN_ID).read_text(encoding="utf-8"))
BODY = DOC["content"]
PLACEHOLDER = "QID0"


def _prefix(builder, *rest):
    shaped = builder(PLACEHOLDER, *rest).replace(PLACEHOLDER, "<id>")
    return shaped[: shaped.index("q=<id>") + len("q=<id>")]


def _keys():
    return [line.split(":", 1)[0].strip() for line in BODY.split("\n") if line.startswith("    ") and not line.startswith("     ") and ":" in line]


def _value(key):
    line = next(line for line in BODY.split("\n") if line.strip().startswith(f"{key}:"))
    return line.split(":", 1)[1].strip().strip('"')


def test_apply_to_and_purpose_are_the_first_two_keys_in_that_order():
    assert _keys()[:2] == ["apply_to", "purpose"]


def test_apply_to_names_the_three_markers_the_builders_produce():
    apply_to = _value("apply_to")
    assert _prefix(review_frame) in apply_to
    assert _prefix(reply_part, "") in apply_to
    assert _prefix(not_delivered_frame) in apply_to


def test_apply_to_says_the_block_does_not_apply_without_a_marker():
    assert "this block does not apply" in _value("apply_to")


def test_talking_about_it_is_scoped_to_a_marked_question():
    assert "a marked question" in _value("talking_about_it")


def test_apply_to_pins_the_not_delivered_marker_and_exactly_three_markers():
    apply_to = _value("apply_to")
    assert "not delivered" in apply_to
    assert apply_to.count("memory review q=<id>") == 3


def test_apply_to_keeps_the_exclusion_clause():
    clause = "not even when the owner talks about his memory, his facts or what you remember"
    assert clause in _value("apply_to")


def test_purpose_says_the_messages_belong_to_the_system_which_applies_the_answers():
    purpose = _value("purpose")
    assert "belong to the system" in purpose
    assert "applies them itself" in purpose
