"""`overflow_window`, `valid_plan`, `plan_after_cut`: the batches of a session's overflow and the plan that keeps their
boundaries between appends (`src/domain/consolidation_window.py`)."""
import pytest

from src.domain.consolidation import handoff_key
from src.domain.consolidation_window import overflow_window, plan_after_cut, valid_plan
from src.domain.llm import Message, MessagePart

SID = "u1:C1"
TEXTS = ["Is the car service still on 2026-11-05?", "Yes, bring the logbook.", "Bring the logbook.",
         "Noted: the logbook.", "Is the gym membership valid until November?", "Yes, until Nov 2026.",
         "Do you still play chess on Sundays?"]
HISTORY = [Message(role="user", parts=[MessagePart(text=t)], created_at=1000.0 + i) for i, t in enumerate(TEXTS)]


def planned(start, end):
    return (end - start, handoff_key(SID, HISTORY[start:end]))


def texts(batches):
    return [[m.parts[0].text for m in batch] for batch in batches]


def test_without_a_plan_the_batches_follow_the_current_window():
    window = overflow_window(SID, HISTORY, [], max_length=3, batch_size=2, ceiling=None)
    assert texts(window.batches) == [TEXTS[0:2], TEXTS[2:4]]
    assert texts([window.rest]) == [TEXTS[4:7]] and window.plan == [planned(0, 2), planned(2, 4)]


def test_planned_batches_come_first_with_their_own_size_then_the_current_window():
    window = overflow_window(SID, HISTORY, [planned(0, 2)], max_length=2, batch_size=3, ceiling=None)
    assert texts(window.batches) == [TEXTS[0:2], TEXTS[2:5]]
    assert window.plan == [planned(0, 2), planned(2, 5)] and texts([window.rest]) == [TEXTS[5:7]]


def test_a_planned_batch_is_formed_even_when_the_window_grew_past_the_history():
    window = overflow_window(SID, HISTORY, [planned(0, 2)], max_length=10, batch_size=5, ceiling=None)
    assert texts(window.batches) == [TEXTS[0:2]] and window.plan == [planned(0, 2)]


@pytest.mark.parametrize("plan, kept", [
    ([planned(0, 2), planned(2, 4)], [planned(0, 2), planned(2, 4)]),
    ([planned(1, 3), planned(3, 5)], []),                              # the head moved: the whole plan is gone
    ([planned(0, 2), planned(3, 5)], [planned(0, 2)]),                 # a batch off its place ends the plan there
    ([(0, handoff_key(SID, []))], []),
    ([(8, "past-the-history")], []),
])
def test_valid_plan_keeps_the_leading_batches_still_at_their_place(plan, kept):
    assert valid_plan(SID, HISTORY, plan) == kept


def test_the_ceiling_cuts_a_planned_batch_by_its_own_size():
    window = overflow_window(SID, HISTORY, [planned(0, 3)], max_length=3, batch_size=2, ceiling=6)
    assert window.dropped == [3] and texts([window.history]) == [TEXTS[3:7]]
    assert texts(window.batches) == [TEXTS[3:5]] and window.plan == [planned(3, 5)]


@pytest.mark.parametrize("cut, kept", [
    (2, [planned(2, 4)]),
    (4, []),
    (3, []),                                                           # a cut through a batch clears the plan
    (6, []),
])
def test_plan_after_cut_keeps_the_batches_behind_a_cut_on_a_boundary(cut, kept):
    assert plan_after_cut([planned(0, 2), planned(2, 4)], cut) == kept
