"""The literal notice: how the owner is shown, byte for byte, text the bot stored and will act on later.

Memory facts the owner is asked to confirm, an external task's text that goes to a worker, a reminder's instruction that
runs when it fires: each is written by a model, and the owner must see it exactly as stored, whatever the model said in
its answer. Every such notice is built here, so their shape cannot drift: the exact text travels as a plain-text FILE,
and a short chat message carries the header and the short lines about it (a task id, a reminder's schedule).

Why a file: chat platforms cut or split long messages and read markup in them; a file holds the text byte for byte, so
nothing in it can be formatted, cut, or read as part of the message around it. The message is posted with
`ResponseChannel.send_literal` (the platform formats, links and obeys nothing in it), after the file; its header and
each line are flattened to one line, so a model-written field in it (a reminder's name, a task's title) cannot add a
line of its own. A command the owner pastes back stays a separate message, sent by the caller after this notice.

Public interface:
    LiteralNotice                                                        the message and the file
    render_literal_notice(header, text, *, filename, title, lines=())   builds one
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence


@dataclass(frozen=True)
class LiteralNotice:
    """`message` is sent literally after the file; the file is `filename`, shown under `title`, holding `text`."""
    message: str
    filename: str
    title: str
    text: str

    @property
    def content(self) -> bytes:
        """The file's bytes: `text` in UTF-8, unchanged."""
        return self.text.encode("utf-8")


def _one_line(line: str) -> str:
    """`line` with every line break (any of `str.splitlines`'s, not only `\\n`) turned into a space."""
    return " ".join(part for part in line.splitlines() if part)


def render_literal_notice(header: str, text: str, *, filename: str, title: str,
                          lines: Sequence[str] = ()) -> LiteralNotice:
    """The notice: the message is the header, then each non-empty entry of `lines`, one line each; the file holds
    `text` unchanged, under `title` flattened to one line."""
    message_lines = [_one_line(header), *(_one_line(line) for line in lines if line)]
    return LiteralNotice(message="\n".join(message_lines), filename=filename, title=_one_line(title), text=text)


__all__ = ["LiteralNotice", "render_literal_notice"]
