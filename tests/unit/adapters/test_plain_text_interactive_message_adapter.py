"""PlainTextInteractiveMessageAdapter (Facts S4 · T5a): the message goes out as numbered text through the platform's own
channel, the ref comes from the channel, and nothing is decided here."""
import logging

import pytest

from src.adapters.notification_channel_factory import NotificationChannelFactory
from src.adapters.plain_text_interactive_message_adapter import PlainTextInteractiveMessageAdapter
from src.domain.choice_outcome import ChoiceOutcome
from src.domain.interactive_binding import InteractiveBinding
from src.domain.interactive_message import InteractiveMessage
from src.domain.interactive_option import InteractiveOption
from src.domain.messaging import MessageRef, ResponseChannel


class RefChannel:
    """A channel that numbers what it sends and answers the ref, as Telegram's does."""

    max_message_length = 4096  # the ResponseChannel contract's property; Telegram's value

    def __init__(self, platform, channel_id):
        self.platform = platform
        self.channel_id = channel_id
        self.sent = []
        self.fail = False

    async def send_message_ref(self, text):
        if self.fail:
            raise RuntimeError("telegram is down")
        self.sent.append(text)
        return MessageRef(self.platform, self.channel_id, str(100 + len(self.sent)))


class NoRefChannel(RefChannel):
    """A channel whose platform returns no ref: the base method of ResponseChannel."""

    async def send_message_ref(self, text):
        return await ResponseChannel.send_message_ref(self, text)


def question():
    return InteractiveMessage(
        prompt="Is this still true?",
        quote="Lives in Lisbon",
        options=[InteractiveOption(id=i, label=label) for i, label in
                 [("still_true", "yes"), ("ended", "no longer"), ("never", "never"), ("dont_know", "don't remember")]],
        binding=InteractiveBinding(kind="fact_question", id="q1"),
        reply_hint="Reply to this message with a digit.",
    )


# What the adapter lays `question()` out as (the old `InteractiveMessage.as_plain_text` output, pinned; T5b reviewer, L10).
QUESTION_TEXT = (
    "Is this still true?\n\n«Lives in Lisbon»\n\n1 — yes  2 — no longer  3 — never  4 — don't remember"
    "\n\nReply to this message with a digit."
)


@pytest.fixture
def channels():
    made = {}
    factory = NotificationChannelFactory()
    factory.register_factory("telegram", lambda cid: made.setdefault(cid, RefChannel("telegram", cid)))
    factory.register_factory("bare", lambda cid: made.setdefault(cid, NoRefChannel("bare", cid)))
    return factory, made


async def test_the_text_goes_to_the_platforms_channel_and_the_channels_ref_comes_back(channels):
    factory, made = channels
    ref = await PlainTextInteractiveMessageAdapter(factory).post("telegram", "555", question())
    assert ref == MessageRef("telegram", "555", "101")
    assert made["555"].sent == [QUESTION_TEXT]


async def test_each_post_is_its_own_message_with_its_own_ref(channels):
    factory, _ = channels
    adapter = PlainTextInteractiveMessageAdapter(factory)
    first = await adapter.post("telegram", "555", question())
    second = await adapter.post("telegram", "555", question())
    assert (first.message_id, second.message_id) == ("101", "102")


async def test_an_unconfigured_platform_is_an_error_not_a_silent_drop(channels):
    factory, made = channels
    with pytest.raises(RuntimeError):
        await PlainTextInteractiveMessageAdapter(factory).post("whatsapp", "1", question())
    assert made == {}


async def test_a_send_failure_is_raised_and_logged_without_the_text(channels, caplog):
    factory, made = channels
    made["555"] = RefChannel("telegram", "555")
    made["555"].fail = True
    with caplog.at_level(logging.ERROR):
        with pytest.raises(RuntimeError, match="telegram is down"):
            await PlainTextInteractiveMessageAdapter(factory).post("telegram", "555", question())
    logged = " ".join(r.getMessage() for r in caplog.records)
    assert "fact_question:q1" in logged and "Lisbon" not in logged


async def test_a_channel_without_refs_refuses_through_the_base_method(channels):
    factory, _ = channels
    with pytest.raises(NotImplementedError):
        await PlainTextInteractiveMessageAdapter(factory).post("bare", "1", question())


async def test_showing_a_choice_changes_nothing_because_there_are_no_controls(channels):
    factory, made = channels
    adapter = PlainTextInteractiveMessageAdapter(factory)
    ref = await adapter.post("telegram", "555", question())
    await adapter.show_choice(ref, ChoiceOutcome(message=question(), chosen_option_id="never"))
    assert made["555"].sent == [QUESTION_TEXT]
