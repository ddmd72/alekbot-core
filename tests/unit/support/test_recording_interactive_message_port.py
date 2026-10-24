"""The recording InteractiveMessagePort (tests/support/fact_review_world.py) answers like a platform does."""
import pytest

from src.domain.choice_outcome import ChoiceOutcome
from src.domain.interactive_binding import InteractiveBinding
from src.domain.interactive_message import InteractiveMessage
from src.domain.interactive_option import InteractiveOption
from src.domain.messaging import MessageRef
from tests.support.fact_review_world import ReviewWorld


def question(ident="q1"):
    return InteractiveMessage(
        prompt="Is this still true?",
        options=[InteractiveOption(id="a", label="yes"), InteractiveOption(id="b", label="no")],
        binding=InteractiveBinding(kind="fact_question", id=ident),
    )


async def test_refs_are_numbered_per_channel_and_the_message_is_kept():
    port = ReviewWorld().interactive
    first = await port.post("slack", "D1", question("q1"))
    second = await port.post("slack", "D1", question("q2"))
    other = await port.post("slack", "D2", question("q3"))
    assert [r.message_id for r in (first, second, other)] == ["m1", "m2", "m1"]
    assert port.message_at(second).binding.id == "q2" and port.has_controls(second)


async def test_a_shown_choice_removes_the_controls_and_an_unknown_message_is_refused():
    port = ReviewWorld().interactive
    ref = await port.post("slack", "D1", question())
    await port.show_choice(ref, ChoiceOutcome(message=question(), chosen_option_id="b"))
    assert not port.has_controls(ref) and port.shown[ref].chosen_option.label == "no"
    with pytest.raises(RuntimeError, match="message_not_found"):
        await port.show_choice(MessageRef("slack", "D1", "m9"), ChoiceOutcome(message=question(), note="Closed."))


async def test_failures_are_settable_and_leave_nothing_recorded():
    port = ReviewWorld().interactive
    port.fail = True
    with pytest.raises(RuntimeError):
        await port.post("slack", "D1", question())
    port.fail = False
    port.unreachable.add("telegram")
    with pytest.raises(RuntimeError):
        await port.post("telegram", "1", question())
    assert port.posted == []
    ref = await port.post("slack", "D1", question())
    port.fail_show = True
    with pytest.raises(RuntimeError):
        await port.show_choice(ref, ChoiceOutcome(message=question(), note="Closed."))
    assert port.has_controls(ref)
