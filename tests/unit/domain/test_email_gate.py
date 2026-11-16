"""The email gate's closed reasons and the code rule over its answer (email index v2 §4, slice 9b): exactly the RFC's
reasons minus `document`; whatever the answer does not settle cleanly is kept and marked forced; an id not on the page is
ignored; a duplicate id keeps when either answer keeps; the block renders one `- value: meaning` line per value."""
import pytest

from src.domain.email_gate import (
    DROP_REASONS, KEEP_REASONS, GateDecision, gate_decisions, render_gate_reasons_block,
)

PAGE = ["msg_rome01", "msg_school02", "msg_shop03"]


def test_the_reason_sets_are_exactly_the_rfcs_without_document():
    assert list(KEEP_REASONS) == ["record", "correspondence", "notice", "unsure"]
    assert list(DROP_REASONS) == ["promotion", "newsletter", "social", "sign_in", "automated"]
    assert "document" not in KEEP_REASONS and not set(KEEP_REASONS) & set(DROP_REASONS)
    assert all(meaning.strip() for meaning in [*KEEP_REASONS.values(), *DROP_REASONS.values()])


def test_clean_answers_decide_as_given_and_are_not_forced():
    got = gate_decisions([
        {"email_id": "msg_rome01", "decision": "keep", "reason": "record"},
        {"email_id": "msg_school02", "decision": "keep", "reason": "notice"},
        {"email_id": "msg_shop03", "decision": "drop", "reason": "newsletter"},
    ], PAGE)
    assert got == {
        "msg_rome01": GateDecision(keep=True, reason="record", forced=False),
        "msg_school02": GateDecision(keep=True, reason="notice", forced=False),
        "msg_shop03": GateDecision(keep=False, reason="newsletter", forced=False),
    }
    assert list(got) == PAGE


def test_a_listed_mail_missing_from_the_answer_is_kept_and_forced():
    got = gate_decisions([{"email_id": "msg_shop03", "decision": "drop", "reason": "promotion"}], PAGE)
    assert got["msg_rome01"] == got["msg_school02"] == GateDecision(keep=True, reason=None, forced=True)
    assert got["msg_shop03"] == GateDecision(keep=False, reason="promotion", forced=False)


@pytest.mark.parametrize("reason", ["document", "spam", "", None, 3, "Promotion"])
def test_a_reason_outside_both_lists_keeps_and_is_forced(reason):
    got = gate_decisions([{"email_id": "msg_shop03", "decision": "drop", "reason": reason}], ["msg_shop03"])
    assert got == {"msg_shop03": GateDecision(keep=True, reason=None, forced=True)}


def test_a_drop_with_a_keep_reason_keeps_and_is_forced():
    got = gate_decisions([{"email_id": "msg_rome01", "decision": "drop", "reason": "record"}], ["msg_rome01"])
    assert got == {"msg_rome01": GateDecision(keep=True, reason="record", forced=True)}


def test_a_keep_with_a_drop_reason_keeps_and_is_forced():
    got = gate_decisions([{"email_id": "msg_shop03", "decision": "keep", "reason": "promotion"}], ["msg_shop03"])
    assert got == {"msg_shop03": GateDecision(keep=True, reason="promotion", forced=True)}


@pytest.mark.parametrize("decision", ["Drop", "maybe", None, True])
def test_a_decision_outside_keep_and_drop_keeps_and_is_forced(decision):
    got = gate_decisions([{"email_id": "msg_shop03", "decision": decision, "reason": "promotion"}], ["msg_shop03"])
    assert got["msg_shop03"].keep is True and got["msg_shop03"].forced is True


def test_an_id_not_on_the_page_is_ignored():
    got = gate_decisions([
        {"email_id": "msg_elsewhere", "decision": "keep", "reason": "record"},
        {"email_id": 7, "decision": "keep", "reason": "record"},
        {"decision": "drop", "reason": "social"},
        {"email_id": "msg_shop03", "decision": "drop", "reason": "social"},
    ], ["msg_shop03"])
    assert got == {"msg_shop03": GateDecision(keep=False, reason="social", forced=False)}


@pytest.mark.parametrize("order", [0, 1])
def test_a_duplicate_id_keeps_when_either_answer_keeps(order):
    answers = [{"email_id": "msg_school02", "decision": "drop", "reason": "automated"},
               {"email_id": "msg_school02", "decision": "keep", "reason": "notice"}]
    if order:
        answers.reverse()
    got = gate_decisions(answers, ["msg_school02"])
    assert got == {"msg_school02": GateDecision(keep=True, reason="notice", forced=False)}


def test_a_duplicate_of_two_drops_stays_dropped_with_the_first_reason():
    got = gate_decisions([{"email_id": "msg_shop03", "decision": "drop", "reason": "promotion"},
                          {"email_id": "msg_shop03", "decision": "drop", "reason": "newsletter"}], ["msg_shop03"])
    assert got == {"msg_shop03": GateDecision(keep=False, reason="promotion", forced=False)}


def test_the_block_has_one_line_per_value_under_its_heading():
    lines = render_gate_reasons_block().splitlines()
    keep_at, drop_at = lines.index("KEEP REASONS (decision keep):"), lines.index("DROP REASONS (decision drop):")
    assert keep_at == 0 and drop_at == len(KEEP_REASONS) + 1 and len(lines) == len(KEEP_REASONS) + len(DROP_REASONS) + 2
    assert lines[1:drop_at] == [f"- {v}: {m}" for v, m in KEEP_REASONS.items()]
    assert lines[drop_at + 1:] == [f"- {v}: {m}" for v, m in DROP_REASONS.items()]
