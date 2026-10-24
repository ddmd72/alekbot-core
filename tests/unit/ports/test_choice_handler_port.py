"""ChoiceHandlerPort and InteractiveMessagePort (Facts S4 · T5a): the contracts a handler and an adapter must fill."""
from datetime import datetime, timezone

import pytest

from src.domain.choice_outcome import ChoiceOutcome
from src.domain.interactive_binding import InteractiveBinding
from src.domain.interactive_choice import InteractiveChoice
from src.domain.interactive_message import InteractiveMessage
from src.domain.interactive_option import InteractiveOption
from src.domain.messaging import MessageRef
from src.ports.choice_handler_port import ChoiceHandlerPort
from src.ports.interactive_message_port import InteractiveMessagePort


def message():
    return InteractiveMessage(
        prompt="Pick one", options=[InteractiveOption(id="a", label="A"), InteractiveOption(id="b", label="B")],
        binding=InteractiveBinding(kind="demo", id="1"),
    )


class DemoHandler(ChoiceHandlerPort):
    @property
    def kind(self) -> str:
        return "demo"

    async def handle(self, choice, user_id, account_id):
        if choice.platform_user_id != "owner":
            return None
        return ChoiceOutcome(message=message(), chosen_option_id=choice.option_id)


def choice(who):
    return InteractiveChoice(
        binding=InteractiveBinding(kind="demo", id="1"), option_id="b", platform="slack", platform_user_id=who,
        message_ref=MessageRef("slack", "D1", "1"), at=datetime(2026, 10, 24, tzinfo=timezone.utc),
    )


def test_neither_port_can_be_used_without_its_methods():
    with pytest.raises(TypeError):
        ChoiceHandlerPort()
    with pytest.raises(TypeError):
        InteractiveMessagePort()

    class NoKind(ChoiceHandlerPort):
        async def handle(self, choice, user_id, account_id):
            return None

    with pytest.raises(TypeError):
        NoKind()


async def test_a_handler_is_found_by_the_kind_of_the_choices_binding_and_may_show_nothing():
    handlers = {h.kind: h for h in [DemoHandler()]}
    picked = choice("owner")
    outcome = await handlers[picked.binding.kind].handle(picked, "u", "a")
    assert outcome.chosen_option.label == "B"
    assert await handlers["demo"].handle(choice("stranger"), "u", "a") is None
