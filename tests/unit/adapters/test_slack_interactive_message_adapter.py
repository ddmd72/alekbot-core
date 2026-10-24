"""SlackInteractiveMessageAdapter (Facts S4 · T5c): mocked at the slack_sdk client boundary only."""
from unittest.mock import AsyncMock, MagicMock

import pytest
from slack_sdk.errors import SlackApiError

from src.adapters.slack.binding_codec import decode
from src.adapters.slack.interactive_message_adapter import (
    MAX_BUTTON_TEXT_CHARS, MAX_TEXT_CHARS, SlackInteractiveMessageAdapter,
)
from src.domain.choice_outcome import ChoiceOutcome
from src.domain.interactive_binding import InteractiveBinding
from src.domain.interactive_message import InteractiveMessage
from src.domain.interactive_option import MAX_LABEL_CHARS, InteractiveOption
from src.domain.messaging import MessageRef

LABELS = [("still_true", "yes"), ("ended", "no longer"), ("never", "never"), ("dont_know", "don't remember")]


def question(**over):
    fields = dict(
        prompt="Is this still true?", quote="Lives in Lisbon",
        options=[InteractiveOption(id=i, label=label) for i, label in LABELS],
        binding=InteractiveBinding(kind="fact_question", id="q1"), reply_hint="Reply with a digit.",
    )
    fields.update(over)
    return InteractiveMessage(**fields)


def client(**responses):
    c = MagicMock()
    c.chat_postMessage = AsyncMock(return_value=responses.get("post", {"ok": True, "channel": "D0OWNER", "ts": "1760.01"}))
    c.chat_update = AsyncMock(return_value={"ok": True})
    return c


def slack_error():
    return SlackApiError("channel_not_found", {"ok": False, "error": "channel_not_found"})


def posted(c):
    return c.chat_postMessage.call_args.kwargs


def mrkdwn_fields(blocks):
    out = []
    for block in blocks:
        if block["type"] == "section":
            out.append(block["text"])
        for element in block.get("elements", []):
            if element["type"] == "mrkdwn":
                out.append(element)
    return out


async def test_post_sends_blocks_and_a_text_fallback_to_the_channel():
    c = client()
    await SlackInteractiveMessageAdapter(c).post("slack", "D0OWNER", question())
    kwargs = posted(c)
    assert kwargs["channel"] == "D0OWNER"
    prompt, quote, hint, actions = kwargs["blocks"]
    assert prompt == {"type": "section", "text": {"type": "mrkdwn", "text": "Is this still true?"}}
    assert quote == {"type": "section", "text": {"type": "mrkdwn", "text": "> Lives in Lisbon"}}
    assert hint["type"] == "context" and hint["elements"][0]["text"] == "Reply with a digit."
    assert actions["type"] == "actions"
    assert kwargs["text"] == (
        "Is this still true?\n«Lives in Lisbon»\n1 — yes  2 — no longer  3 — never  4 — don't remember\nReply with a digit."
    )


async def test_the_buttons_carry_the_option_ids_as_plain_text_labels_and_the_binding_as_the_block_id():
    c = client()
    await SlackInteractiveMessageAdapter(c).post("slack", "D0OWNER", question())
    actions = posted(c)["blocks"][-1]
    assert decode(actions["block_id"]) == InteractiveBinding(kind="fact_question", id="q1")
    assert [(b["action_id"], b["value"], b["text"]["type"], b["text"]["text"]) for b in actions["elements"]] == [
        ("still_true", "still_true", "plain_text", "yes"),
        ("ended", "ended", "plain_text", "no longer"),
        ("never", "never", "plain_text", "never"),
        ("dont_know", "dont_know", "plain_text", "don't remember"),
    ]


async def test_a_message_without_a_quote_or_a_hint_has_only_the_prompt_and_the_actions():
    c = client()
    await SlackInteractiveMessageAdapter(c).post("slack", "D0OWNER", question(quote="  ", reply_hint=""))
    assert [b["type"] for b in posted(c)["blocks"]] == ["section", "actions"]


async def test_the_ref_is_built_from_the_channel_and_ts_slack_answered():
    c = client(post={"ok": True, "channel": "D0REAL", "ts": "1760000000.000100"})
    ref = await SlackInteractiveMessageAdapter(c).post("slack", "D0OWNER", question())
    assert ref == MessageRef(platform="slack", channel_id="D0REAL", message_id="1760000000.000100")


async def test_slack_control_syntax_in_the_quote_arrives_escaped():
    c = client()
    await SlackInteractiveMessageAdapter(c).post("slack", "D0OWNER", question(quote="Told <!channel> & <@U1> a > b"))
    quote = posted(c)["blocks"][1]["text"]["text"]
    assert quote == "> Told &lt;!channel&gt; &amp; &lt;@U1&gt; a &gt; b"
    assert "<!channel>" not in quote


async def test_the_prompt_the_hint_and_the_fallback_text_are_escaped_too():
    c = client()
    msg = question(prompt="Still <b>true</b> & right?", reply_hint="Reply <now>", options=[
        InteractiveOption(id="a", label="yes <b>"), InteractiveOption(id="b", label="no & more")])
    await SlackInteractiveMessageAdapter(c).post("slack", "D0OWNER", msg)
    kwargs = posted(c)
    for field in mrkdwn_fields(kwargs["blocks"]):
        assert "<" not in field["text"].replace("&lt;", "").replace("&gt;", "")
    assert kwargs["blocks"][0]["text"]["text"] == "Still &lt;b&gt;true&lt;/b&gt; &amp; right?"
    assert kwargs["text"] == (
        "Still &lt;b&gt;true&lt;/b&gt; &amp; right?\n«Lives in Lisbon»\n1 — yes &lt;b&gt;  2 — no &amp; more\nReply &lt;now&gt;"
    )
    # labels are plain_text: escaping them would show the entities
    assert [b["text"]["text"] for b in kwargs["blocks"][-1]["elements"]] == ["yes <b>", "no & more"]


async def test_a_quote_of_3500_characters_is_capped_at_the_section_limit_with_an_ellipsis():
    c = client()
    await SlackInteractiveMessageAdapter(c).post("slack", "D0OWNER", question(quote="x" * 3500))
    quote = posted(c)["blocks"][1]["text"]["text"]
    assert len(quote) == MAX_TEXT_CHARS
    assert quote.startswith("> xxx") and quote.endswith("x…")


async def test_the_cap_counts_the_escaped_text_and_never_cuts_an_entity_in_half():
    c = client()
    await SlackInteractiveMessageAdapter(c).post("slack", "D0OWNER", question(quote="&" * 1000))
    quote = posted(c)["blocks"][1]["text"]["text"]
    assert len(quote) <= MAX_TEXT_CHARS
    assert quote.endswith("&amp;…")


async def test_a_multi_line_quote_is_quoted_on_every_line():
    c = client()
    await SlackInteractiveMessageAdapter(c).post("slack", "D0OWNER", question(quote="line one\nline two"))
    assert posted(c)["blocks"][1]["text"]["text"] == "> line one\n> line two"


async def test_a_slack_error_on_post_is_raised():
    c = client()
    c.chat_postMessage = AsyncMock(side_effect=slack_error())
    with pytest.raises(SlackApiError):
        await SlackInteractiveMessageAdapter(c).post("slack", "D0OWNER", question())


async def test_another_platform_is_refused_before_anything_is_sent():
    c = client()
    with pytest.raises(ValueError):
        await SlackInteractiveMessageAdapter(c).post("telegram", "555", question())
    c.chat_postMessage.assert_not_called()


REF = MessageRef(platform="slack", channel_id="D0REAL", message_id="1760.01")


async def test_show_choice_updates_the_message_without_the_actions_and_shows_the_chosen_label():
    c = client()
    outcome = ChoiceOutcome(message=question(), chosen_option_id="ended")
    await SlackInteractiveMessageAdapter(c).show_choice(REF, outcome)
    kwargs = c.chat_update.call_args.kwargs
    assert (kwargs["channel"], kwargs["ts"]) == ("D0REAL", "1760.01")
    blocks = kwargs["blocks"]
    assert [b["type"] for b in blocks] == ["section", "section", "context"]  # no actions, no hint
    assert blocks[0]["text"]["text"] == "Is this still true?"
    assert blocks[1]["text"]["text"] == "> Lives in Lisbon"
    assert blocks[-1] == {"type": "context", "elements": [{"type": "plain_text", "text": "✓ no longer", "emoji": False}]}
    assert "no longer" in kwargs["text"]


async def test_show_choice_without_a_chosen_option_shows_the_note():
    c = client()
    outcome = ChoiceOutcome(message=question(), note="This question is closed.")
    await SlackInteractiveMessageAdapter(c).show_choice(REF, outcome)
    blocks = c.chat_update.call_args.kwargs["blocks"]
    assert [b["type"] for b in blocks] == ["section", "section", "context"]  # no actions, no hint
    assert blocks[-1]["elements"][0]["text"] == "This question is closed."


async def test_show_choice_escapes_the_quote_too():
    c = client()
    outcome = ChoiceOutcome(message=question(quote="<!channel> & co"), note="Closed.")
    await SlackInteractiveMessageAdapter(c).show_choice(REF, outcome)
    assert c.chat_update.call_args.kwargs["blocks"][1]["text"]["text"] == "> &lt;!channel&gt; &amp; co"


async def test_a_slack_error_on_update_is_raised():
    c = client()
    c.chat_update = AsyncMock(side_effect=slack_error())
    with pytest.raises(SlackApiError):
        await SlackInteractiveMessageAdapter(c).show_choice(REF, ChoiceOutcome(message=question(), note="Closed."))


async def test_another_platforms_ref_is_refused_before_anything_is_sent():
    c = client()
    with pytest.raises(ValueError):
        await SlackInteractiveMessageAdapter(c).show_choice(
            MessageRef("telegram", "555", "9"), ChoiceOutcome(message=question(), note="Closed."))
    c.chat_update.assert_not_called()


# ------------------------------------------------------------------------------------------------ limits

def test_the_domains_label_bound_fits_slacks_button_text_limit():
    assert MAX_BUTTON_TEXT_CHARS == 75
    assert MAX_LABEL_CHARS <= MAX_BUTTON_TEXT_CHARS


async def test_a_label_of_the_domains_longest_goes_out_unchanged_as_the_button_text():
    c = client()
    label = "x" * MAX_LABEL_CHARS
    msg = question(options=[InteractiveOption(id="a", label=label), InteractiveOption(id="b", label="no")])
    await SlackInteractiveMessageAdapter(c).post("slack", "D0OWNER", msg)
    button = posted(c)["blocks"][-1]["elements"][0]
    assert button["text"]["text"] == label
    assert len(button["text"]["text"]) <= MAX_BUTTON_TEXT_CHARS


async def test_the_notification_text_carries_the_escaped_quote():
    c = client()
    await SlackInteractiveMessageAdapter(c).post("slack", "D0OWNER", question(quote="Told <!channel> & co"))
    assert "«Told &lt;!channel&gt; &amp; co»" in posted(c)["text"]


async def test_a_long_quote_is_cut_in_the_notification_text_and_the_options_and_hint_stay():
    c = client()
    await SlackInteractiveMessageAdapter(c).post("slack", "D0OWNER", question(quote="&" * 5000))
    text = posted(c)["text"]
    assert len(text) <= MAX_TEXT_CHARS
    assert "&amp;…»" in text
    assert text.endswith("1 — yes  2 — no longer  3 — never  4 — don't remember\nReply with a digit.")
