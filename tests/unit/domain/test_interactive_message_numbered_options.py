"""InteractiveMessage.numbered_options (Facts S4 · T5b, L10): the one digit rule, shared by whatever shows the digits and
by `option_for_reply`."""
from src.domain.interactive_binding import InteractiveBinding
from src.domain.interactive_message import InteractiveMessage
from src.domain.interactive_option import InteractiveOption


def message():
    return InteractiveMessage(
        prompt="Is this still true?",
        options=[InteractiveOption(id=i, label=i) for i in ("a", "b", "c")],
        binding=InteractiveBinding(kind="fact_question", id="q1"),
    )


def test_the_options_are_numbered_from_one_in_order():
    numbered = message().numbered_options()
    assert [(n, o.id) for n, o in numbered] == [(1, "a"), (2, "b"), (3, "c")]


def test_every_digit_it_gives_is_the_reply_that_picks_that_option():
    msg = message()
    for number, option in msg.numbered_options():
        assert msg.option_for_reply(str(number)) == option.id


def test_the_domain_no_longer_lays_the_message_out():
    assert not hasattr(message(), "as_plain_text")
