"""ChoiceHandlerPort and InteractiveMessagePort (Facts S4 · T5a): the contracts a handler and an adapter must fill."""
import pytest

from src.ports.choice_handler_port import ChoiceHandlerPort
from src.ports.interactive_message_port import InteractiveMessagePort


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
