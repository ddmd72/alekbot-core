"""`ChoiceOutcome.keep_options` (Facts S4 · T8d2, owner decision O3): a refused pick shows its note and the options stay.
The Slack adapter is mocked at the `slack_sdk` client boundary only; the plain-text adapter has nothing to keep."""
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.adapters.plain_text_interactive_message_adapter import PlainTextInteractiveMessageAdapter
from src.adapters.slack.binding_codec import decode
from src.adapters.slack.interactive_message_adapter import SlackInteractiveMessageAdapter
from src.domain.choice_outcome import ChoiceOutcome
from src.domain.interactive_binding import InteractiveBinding
from src.domain.interactive_message import InteractiveMessage
from src.domain.interactive_option import InteractiveOption
from src.domain.messaging import MessageRef

LABELS = [("still_true", "yes"), ("ended", "no longer"), ("never", "never"), ("dont_know", "don't remember")]
REF = MessageRef(platform="slack", channel_id="D0REAL", message_id="1760.01")
NOTE = "That fact has changed in the meantime, so I didn't apply your answer; I'll come back to it."


def question(**over) -> InteractiveMessage:
    fields = dict(
        prompt="Is this still true?", quote="Lives in Springfield",
        options=[InteractiveOption(id=i, label=label) for i, label in LABELS],
        binding=InteractiveBinding(kind="fact_question", id="q1"), reply_hint="Reply with a digit.",
    )
    fields.update(over)
    return InteractiveMessage(**fields)


def client() -> MagicMock:
    c = MagicMock()
    c.chat_postMessage = AsyncMock(return_value={"ok": True, "channel": "D0REAL", "ts": "1760.01"})
    c.chat_update = AsyncMock(return_value={"ok": True})
    return c


async def test_the_update_carries_the_note_and_the_same_actions_block_as_the_post():
    c = client()
    adapter = SlackInteractiveMessageAdapter(c)
    await adapter.post("slack", "D0REAL", question())
    posted = c.chat_postMessage.call_args.kwargs["blocks"]

    await adapter.show_choice(REF, ChoiceOutcome(message=question(), note=NOTE, keep_options=True))

    kwargs = c.chat_update.call_args.kwargs
    blocks = kwargs["blocks"]
    assert (kwargs["channel"], kwargs["ts"]) == ("D0REAL", "1760.01")
    assert [b["type"] for b in blocks] == ["section", "section", "context", "context", "actions"]
    assert blocks[0] == posted[0] and blocks[1] == posted[1]                         # prompt and quote as posted
    assert blocks[2] == {"type": "context", "elements": [{"type": "plain_text", "text": NOTE, "emoji": False}]}
    assert blocks[3:] == posted[2:]                                                  # the hint and the actions, identical
    assert blocks[-1]["block_id"] == "fact_question:q1" and decode(blocks[-1]["block_id"]) == question().binding
    assert [e["action_id"] for e in blocks[-1]["elements"]] == [i for i, _ in LABELS]
    assert "didn't apply your answer" in kwargs["text"] and "Lives in Springfield" in kwargs["text"]


async def test_a_message_without_a_hint_keeps_just_the_note_and_the_actions():
    c = client()
    await SlackInteractiveMessageAdapter(c).show_choice(
        REF, ChoiceOutcome(message=question(reply_hint=""), note=NOTE, keep_options=True))
    assert [b["type"] for b in c.chat_update.call_args.kwargs["blocks"]] == ["section", "section", "context", "actions"]


async def test_the_note_is_shown_as_plain_text_and_the_quote_is_escaped():
    c = client()
    await SlackInteractiveMessageAdapter(c).show_choice(
        REF, ChoiceOutcome(message=question(quote="<!channel> & co"), note="<!here> changed", keep_options=True))
    blocks = c.chat_update.call_args.kwargs["blocks"]
    assert blocks[1]["text"]["text"] == "> &lt;!channel&gt; &amp; co"
    assert blocks[2]["elements"][0] == {"type": "plain_text", "text": "<!here> changed", "emoji": False}
    assert "&lt;!here&gt; changed" in c.chat_update.call_args.kwargs["text"]


async def test_without_keep_options_the_actions_are_still_removed():
    c = client()
    await SlackInteractiveMessageAdapter(c).show_choice(REF, ChoiceOutcome(message=question(), note="Closed."))
    assert [b["type"] for b in c.chat_update.call_args.kwargs["blocks"]] == ["section", "section", "context"]


async def test_the_plain_text_adapter_has_nothing_to_keep_or_remove():
    factory = MagicMock()
    await PlainTextInteractiveMessageAdapter(factory).show_choice(
        MessageRef("telegram", "555", "9"), ChoiceOutcome(message=question(), note=NOTE, keep_options=True))
    factory.create.assert_not_called()


class TestTheOutcome:
    def test_options_are_dropped_unless_kept_and_the_default_is_off(self):
        assert ChoiceOutcome(message=question(), note="Closed.").keep_options is False
        assert ChoiceOutcome(message=question(), chosen_option_id="never").keep_options is False
        assert ChoiceOutcome(message=question(), note=NOTE, keep_options=True).keep_options is True

    def test_kept_options_need_a_note(self):
        with pytest.raises(ValueError):
            ChoiceOutcome(message=question(), keep_options=True)

    def test_kept_options_choose_none(self):
        with pytest.raises(ValueError):
            ChoiceOutcome(message=question(), chosen_option_id="never", note=NOTE, keep_options=True)
