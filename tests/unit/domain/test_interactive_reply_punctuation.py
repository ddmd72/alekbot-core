"""The punctuation a chat client puts around a lone digit is not words (Facts S4 · T1c review M2): one rule, in
`InteractiveMessage.option_for_reply`, the same on every platform and for every message."""
import pytest

from src.domain.interactive_binding import InteractiveBinding
from src.domain.interactive_message import InteractiveMessage
from src.domain.interactive_option import InteractiveOption


def message(count: int = 4) -> InteractiveMessage:
    return InteractiveMessage(
        prompt="Pick one",
        options=tuple(InteractiveOption(id=f"o{i}", label=f"option {i}") for i in range(1, count + 1)),
        binding=InteractiveBinding(kind="anything", id="x1"),
    )


@pytest.mark.parametrize("reply, option", [
    ("2.", "o2"), (" 2. ", "o2"), ("2 .", "o2"), ("`3`", "o3"), (" `3` ", "o3"), ("` 3 `", "o3"), ("`3.`", "o3"),
    ("``3``", "o3"), ("```\n4\n```", "o4"), ("`1.`", "o1"), ("1.\n", "o1"), ("\n`2`\n", "o2"),
])
def test_a_digit_in_a_full_stop_or_wrapping_backticks_still_picks_its_option(reply, option):
    assert message().option_for_reply(reply) == option


@pytest.mark.parametrize("reply", [
    "1..", "..", ".", "`", "``", "```", "`1", "1`", "`3`.", "`1` `2`", "`1`2`", "1 `", "`1 2`", "(1)", "1)", "1!", "1,",
    "1.5", "1. yes", "yes.", "`yes`", "5.", "`5`", "0.", "12.", "1.\n2.",
])
def test_anything_more_than_that_is_words(reply):
    assert message().option_for_reply(reply) is None


def test_the_rule_is_the_messages_whatever_it_asks():
    two = message(2)
    assert two.option_for_reply("2.") == "o2"
    assert two.option_for_reply("`3`") is None
