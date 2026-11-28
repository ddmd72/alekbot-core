"""A bound channel's turn delegates straight to its bound intent (`conversation_handler.py`, `calling_agent_id="bound_channel"`):
when the user's profile turns that intent off the specialist never runs. What the user is told is another part's.
The real `ConversationHandler` and coordinator over the seam world; the bound specialist is a recording stand-in."""
from __future__ import annotations

from src.domain.channel_binding import ChannelBinding
from tests.support.denied_profile import denying
from tests.support.seam_world import OTHER_SLACK, USER, ChatChannel, SeamWorld, says, slack_turn


class _Bindings:
    """`ChannelBindingService.get` for one bound channel."""

    def __init__(self, binding: ChannelBinding) -> None:
        self._binding = binding

    async def get(self, channel_id: str):
        return self._binding if channel_id == self._binding.channel_id else None


async def _bound_turn(monkeypatch, *, deny: tuple):
    world = SeamWorld(monkeypatch)
    if deny:
        world.coordinator._effective_config = denying(*deny)
    tutor = world.agent("tutor_agent").always(says("a lesson"))
    binding = ChannelBinding(channel_id="C2", agent_type="tutor", intent="tutor_chat", created_by=USER)
    chat = ChatChannel(*OTHER_SLACK)
    await world.chat_handler(channel_binding_service=_Bindings(binding)).handle_message(slack_turn("teach me"), chat)
    return tutor


async def test_a_bound_turn_to_a_denied_intent_never_runs_the_specialist(monkeypatch):
    tutor = await _bound_turn(monkeypatch, deny=("tutor_chat",))

    assert tutor.received == []


async def test_a_bound_turn_to_an_available_intent_runs_the_specialist(monkeypatch):
    tutor = await _bound_turn(monkeypatch, deny=())

    assert len(tutor.received) == 1
