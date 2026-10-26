"""`unwrapped` (the whole-text backtick rule) and `InteractiveMessage.options_line` (S4 review D-L4, D-L1)."""
import pytest

from src.domain.chat_text import unwrapped
from src.domain.interactive_binding import InteractiveBinding
from src.domain.interactive_message import InteractiveMessage
from src.domain.interactive_option import InteractiveOption


@pytest.mark.parametrize("text, expected", [
    ("  hello  ", "hello"),
    ("`$skill save abc`", "$skill save abc"),
    ("```\n$skill save abc\n```", "$skill save abc"),
    ("  `` $consolidate ``  ", "$consolidate"),
    ("`$HOME` is wrong", "`$HOME` is wrong"),
    ("see `$HOME`", "see `$HOME`"),
    ("`", ""),
    ("", ""),
])
def test_unwrapped_strips_backticks_only_when_they_wrap_the_whole_text(text, expected):
    assert unwrapped(text) == expected


def _message(*labels: str) -> InteractiveMessage:
    return InteractiveMessage(
        prompt="Keep it?", options=tuple(InteractiveOption(id=f"o{i}", label=label) for i, label in enumerate(labels, 1)),
        binding=InteractiveBinding(kind="memory_review", id="q1"),
    )


def test_options_line_numbers_the_options_from_one_on_a_single_line():
    assert _message("Keep", "Drop", "Later").options_line() == "1 — Keep  2 — Drop  3 — Later"


def test_the_reply_rule_and_the_options_line_share_one_digit_rule():
    message = _message("Keep", "Drop")
    assert message.option_for_reply("`2`") == "o2" and message.option_for_reply(" 1. ") == "o1"
    assert message.option_for_reply("3") is None
