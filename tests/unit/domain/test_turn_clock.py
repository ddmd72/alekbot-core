"""TurnClock: one absolute deadline per chat turn (LONG_RUNNING_TURNS_RFC §5.1)."""
from src.domain.llm import Message, MessagePart
from src.domain.turn_clock import (
    CURRENT_TURN_CLOCK,
    LONG_TURN_BUDGET_S,
    MEANWHILE_HEADER,
    TurnClock,
    WRAP_UP_RESERVE_S,
    render_chat_since,
    render_late_answer_note,
)


def test_budget_constants_match_the_rfc():
    assert LONG_TURN_BUDGET_S == 1500
    assert WRAP_UP_RESERVE_S == 120


def test_remaining_counts_down_from_the_start():
    clock = TurnClock.start(budget_s=100, wrap_up_reserve_s=10, now=1000.0)
    assert clock.remaining(now=1000.0) == 100
    assert clock.remaining(now=1060.0) == 40
    assert clock.remaining(now=2000.0) == 0


def test_call_timeout_leaves_the_wrap_up_reserve():
    clock = TurnClock.start(budget_s=100, wrap_up_reserve_s=10, now=0.0)
    assert clock.call_timeout(now=0.0) == 90
    assert clock.call_timeout(now=50.0) == 40


def test_call_timeout_never_below_one_second():
    clock = TurnClock.start(budget_s=100, wrap_up_reserve_s=10, now=0.0)
    assert clock.call_timeout(now=95.0) == 1


def test_wrap_up_timeout_uses_the_whole_remainder():
    # Final review I2: the remainder (5 s here) is raised to the 30 s wrap-up floor.
    clock = TurnClock.start(budget_s=100, wrap_up_reserve_s=10, now=0.0)
    assert clock.wrap_up_timeout(now=95.0) == 30


def test_in_reserve_and_can_retry_are_complements():
    clock = TurnClock.start(budget_s=100, wrap_up_reserve_s=10, now=0.0)
    assert not clock.in_reserve(now=89.0) and clock.can_retry(now=89.0)
    assert clock.in_reserve(now=90.0) and not clock.can_retry(now=90.0)


def test_context_var_defaults_to_none():
    assert CURRENT_TURN_CLOCK.get() is None


def test_render_chat_since_lists_roles_and_summaries():
    msgs = [
        Message(role="user", parts=[MessagePart(text="what about Tuesday?")], created_at=0.0),
        Message(role="model", parts=[MessagePart(text="Tuesday is free.", full_text="LONG")], created_at=1.0),
    ]
    out = render_chat_since(msgs)
    assert out.startswith(MEANWHILE_HEADER)
    assert "- user: what about Tuesday?" in out
    assert "- you: Tuesday is free." in out
    assert "LONG" not in out


def test_render_late_answer_note_quotes_and_links():
    note = render_late_answer_note("compare the three offers", "14:02", "https://x/p1")
    assert note == '[System: late answer to "compare the three offers" (asked 14:02, https://x/p1)]'


def test_render_late_answer_note_without_link_and_long_question():
    note = render_late_answer_note("q" * 300, "14:02", None)
    assert "no link" in note
    assert len(note) < 260


# --- Final review I2: the wrap-up call gets a floor --------------------------------


def test_wrap_up_floor_constant_fits_inside_the_hard_stop_margin():
    from src.domain.turn_clock import HARD_STOP_MARGIN_S, WRAP_UP_FLOOR_S
    assert WRAP_UP_FLOOR_S == 30
    assert WRAP_UP_FLOOR_S < HARD_STOP_MARGIN_S


def test_wrap_up_timeout_never_below_the_floor():
    from src.domain.turn_clock import WRAP_UP_FLOOR_S
    clock = TurnClock.start(budget_s=100, wrap_up_reserve_s=10, now=0.0)
    assert clock.wrap_up_timeout(now=99.0) == WRAP_UP_FLOOR_S
    assert clock.wrap_up_timeout(now=500.0) == WRAP_UP_FLOOR_S   # deadline already passed


def test_wrap_up_timeout_above_the_floor_uses_the_remainder():
    clock = TurnClock.start(budget_s=100, wrap_up_reserve_s=10, now=0.0)
    assert clock.wrap_up_timeout(now=20.0) == 80
