"""The plain-text layout (Facts S4 · T5b, L10): it moved from the domain's `as_plain_text` into the adapter, and what goes
out is byte for byte what `as_plain_text` produced. The strings below are the old method's output, pinned."""
from src.adapters.notification_channel_factory import NotificationChannelFactory
from src.adapters.plain_text_interactive_message_adapter import PlainTextInteractiveMessageAdapter
from src.domain.interactive_binding import InteractiveBinding
from src.domain.interactive_message import InteractiveMessage
from src.domain.interactive_option import InteractiveOption
from src.domain.messaging import MessageRef

LABELS = [("still_true", "yes"), ("ended", "no longer"), ("never", "never"), ("dont_know", "don't remember")]


class Channel:
    def __init__(self):
        self.sent = []

    async def send_message_ref(self, text):
        self.sent.append(text)
        return MessageRef("telegram", "555", "1")


def message(**over):
    fields = dict(
        prompt="Is this still true?", quote="Lives in Lisbon",
        options=[InteractiveOption(id=i, label=label) for i, label in LABELS],
        binding=InteractiveBinding(kind="fact_question", id="q1"), reply_hint="Reply with a digit.",
    )
    fields.update(over)
    return InteractiveMessage(**fields)


async def sent_text(msg):
    channel = Channel()
    factory = NotificationChannelFactory()
    factory.register_factory("telegram", lambda cid: channel)
    await PlainTextInteractiveMessageAdapter(factory).post("telegram", "555", msg)
    return channel.sent[0]


async def test_a_full_message_goes_out_as_the_old_as_plain_text_wrote_it():
    assert await sent_text(message()) == (
        "Is this still true?\n\n«Lives in Lisbon»\n\n1 — yes  2 — no longer  3 — never  4 — don't remember"
        "\n\nReply with a digit."
    )


async def test_without_a_quote_or_a_hint_those_sections_are_absent():
    text = await sent_text(message(quote="  ", reply_hint=" "))
    assert text == "Is this still true?\n\n1 — yes  2 — no longer  3 — never  4 — don't remember"


async def test_the_quote_is_kept_as_stored_inside_the_guillemets():
    text = await sent_text(message(quote="Живе в Лісабоні — «стара адреса»"))
    assert "«Живе в Лісабоні — «стара адреса»»" in text
