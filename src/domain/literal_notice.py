"""The literal notice: how the owner is shown, word for word, text the bot stored and will act on later.

A reminder runs its instruction when it fires; an external task's text goes to a worker. Both texts are written by a
model, and the owner must see each exactly as stored, whatever the model said in its answer. Every such notice is
rendered here, so their shape cannot drift: one header line, the exact text set apart line by line, then footer lines.

The notice is posted with `ResponseChannel.send_literal`, so the platform formats, links and obeys nothing in it
(Slack escapes `<`, `>` and `&` and turns mrkdwn off; Telegram sends without a parse mode). What this renderer adds is
the boundary between the stored text and the notice around it: every line of the text starts with `TEXT_MARK`, and the
header and each footer entry are flattened to one line. So no line of the stored text, and nothing a model wrote into
a header field (a reminder's name, a task's title), can stand as a line of the notice itself, such as a fake footer
or a second header. The text is never cut: `send_literal` splits an over-long post into several messages.

Public interface:
    TEXT_MARK                                              starts every line of the exact text
    render_literal_notice(header, text, footer=()) -> str  the notice, ready for `send_literal`
"""
from __future__ import annotations

from typing import Sequence

TEXT_MARK = "│"


def _one_line(line: str) -> str:
    """`line` with every line break (any of `str.splitlines`'s, not only `\\n`) turned into a space."""
    return " ".join(part for part in line.splitlines() if part)


def render_literal_notice(header: str, text: str, footer: Sequence[str] = ()) -> str:
    """The header line, a blank line, the text with `TEXT_MARK` before each of its lines (an empty line is the mark
    alone), then — after a blank line — each non-empty footer entry on its own line. An empty text leaves its block
    out; the text's line breaks are kept, written as `\\n`."""
    blocks = [_one_line(header)]
    if text:
        blocks.append("\n".join(f"{TEXT_MARK} {line}" if line else TEXT_MARK for line in text.splitlines()))
    footer_lines = [_one_line(entry) for entry in footer if entry]
    if footer_lines:
        blocks.append("\n".join(footer_lines))
    return "\n\n".join(blocks)


__all__ = ["TEXT_MARK", "render_literal_notice"]
