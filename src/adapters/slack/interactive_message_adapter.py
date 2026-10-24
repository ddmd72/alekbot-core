"""SlackInteractiveMessageAdapter — an interactive message as Slack blocks: the prompt, the quoted subject, one button
per option. Builds from the `InteractiveMessage` alone and knows nothing about what is asked. Slack's escaping and
limits are applied here; a Slack error is raised, and the caller decides what it means."""
from typing import Any, Dict, List

from ...domain.choice_outcome import ChoiceOutcome
from ...domain.interactive_message import InteractiveMessage
from ...domain.messaging import MessageRef
from ...ports.interactive_message_port import InteractiveMessagePort
from ...utils.logger import logger
from .binding_codec import encode

PLATFORM = "slack"
# Slack's limit for the text of a section block (and of the other text objects we use).
MAX_TEXT_CHARS = 3000
# Slack's limit for the text of a button; a domain option label is at most this long (pinned by a test).
MAX_BUTTON_TEXT_CHARS = 75
ELLIPSIS = "…"

_ESCAPES = {"&": "&amp;", "<": "&lt;", ">": "&gt;"}  # Slack's documented rule for mrkdwn text


def _capped(pieces: List[str], limit: int = MAX_TEXT_CHARS) -> str:
    """The pieces joined, cut at `limit` with an ellipsis. A piece is never cut in half (an escape such as `&amp;` must
    stay whole)."""
    total = 0
    kept: List[str] = []
    for piece in pieces:
        if total + len(piece) > limit:
            while kept and total + len(ELLIPSIS) > limit:
                total -= len(kept.pop())
            return "".join(kept) + ELLIPSIS
        kept.append(piece)
        total += len(piece)
    return "".join(kept)


def _mrkdwn(text: str) -> str:
    """`text` as Slack `mrkdwn`: `&`, `<` and `>` escaped (so `<!channel>` or `<@U1>` is shown, not obeyed), then capped."""
    return _capped([_ESCAPES.get(ch, ch) for ch in text])


def _quote_mrkdwn(quote: str) -> str:
    """The quote as a Slack block quote: every line starts with `> `, after the text has been escaped."""
    pieces = ["> "]
    for ch in quote:
        pieces.append("\n> " if ch == "\n" else _ESCAPES.get(ch, ch))
    return _capped(pieces)


def _plain(text: str) -> str:
    return text if len(text) <= MAX_TEXT_CHARS else text[: MAX_TEXT_CHARS - len(ELLIPSIS)] + ELLIPSIS


def _section(text: str) -> Dict[str, Any]:
    return {"type": "section", "text": {"type": "mrkdwn", "text": text}}


def _subject_blocks(message: InteractiveMessage) -> List[Dict[str, Any]]:
    blocks = [_section(_mrkdwn(message.prompt.strip()))]
    if message.quote.strip():
        blocks.append(_section(_quote_mrkdwn(message.quote.strip())))
    return blocks


def _fallback_text(message: InteractiveMessage) -> str:
    """What a notification or a client without blocks shows: the prompt, the quoted subject, the numbered options and the
    reply hint. A long quote is cut to fit `MAX_TEXT_CHARS` so that the options and the hint always stay."""
    options = "  ".join(f"{number} — {option.label}" for number, option in message.numbered_options())
    head = _mrkdwn(message.prompt.strip())
    tail = _mrkdwn("\n".join([options, message.reply_hint.strip()] if message.reply_hint.strip() else [options]))
    quote = message.quote.strip()
    if not quote:
        return f"{head}\n{tail}"
    budget = MAX_TEXT_CHARS - len(head) - len(tail) - 2 - 2  # two newlines, two guillemets
    if budget <= len(ELLIPSIS):
        return f"{head}\n{tail}"
    shown = _capped([_ESCAPES.get(ch, ch) for ch in quote], budget)
    return f"{head}\n«{shown}»\n{tail}"


class SlackInteractiveMessageAdapter(InteractiveMessagePort):

    def __init__(self, client):
        """`client`: the Slack `AsyncWebClient` (`app.client`)."""
        self._client = client

    async def post(self, platform: str, channel_id: str, message: InteractiveMessage) -> MessageRef:
        if platform != PLATFORM:
            raise ValueError(f"the Slack adapter cannot post to platform {platform}")
        buttons = [
            {
                "type": "button",
                "action_id": option.id,
                "value": option.id,
                "text": {"type": "plain_text", "text": option.label, "emoji": False},
            }
            for _number, option in message.numbered_options()
        ]
        blocks = _subject_blocks(message)
        if message.reply_hint.strip():
            blocks.append({"type": "context", "elements": [{"type": "mrkdwn", "text": _mrkdwn(message.reply_hint.strip())}]})
        blocks.append({"type": "actions", "block_id": encode(message.binding), "elements": buttons})
        try:
            response = await self._client.chat_postMessage(
                channel=channel_id, blocks=blocks, text=_fallback_text(message),
            )
            return MessageRef(platform=PLATFORM, channel_id=response["channel"], message_id=response["ts"])
        except Exception as exc:
            logger.error(
                f"[SlackInteractive] post failed channel={channel_id} "
                f"binding={message.binding.kind}:{message.binding.id}: {type(exc).__name__}"
            )
            raise

    async def show_choice(self, ref: MessageRef, outcome: ChoiceOutcome) -> None:
        """Replace the message with its prompt and quote and, in place of the buttons, the option chosen or the note."""
        if ref.platform != PLATFORM:
            raise ValueError(f"the Slack adapter cannot update a message on platform {ref.platform}")
        chosen = outcome.chosen_option
        shown = f"✓ {chosen.label}" if chosen is not None else outcome.note.strip()
        blocks = _subject_blocks(outcome.message)
        blocks.append({"type": "context", "elements": [{"type": "plain_text", "text": _plain(shown), "emoji": False}]})
        try:
            await self._client.chat_update(
                channel=ref.channel_id, ts=ref.message_id, blocks=blocks,
                text=_mrkdwn(f"{outcome.message.prompt.strip()}\n{shown}"),
            )
        except Exception as exc:
            logger.error(
                f"[SlackInteractive] update failed channel={ref.channel_id} "
                f"binding={outcome.message.binding.kind}:{outcome.message.binding.id}: {type(exc).__name__}"
            )
            raise
