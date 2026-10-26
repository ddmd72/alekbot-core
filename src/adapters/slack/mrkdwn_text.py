"""Slack's escaping of literal text: the one place that turns text which must be SHOWN into text Slack will not interpret.

Slack reads `<...>` in message text as control syntax (`<!channel>`, `<@U1>`, `<https://x|label>`) and `&` as the start of an
entity. Its documented rule for text that is data, not markup, is to replace `&`, `<` and `>` by `&amp;`, `&lt;` and `&gt;`.
Everything that puts text of unknown origin (a stored fact, a model-translated label) on Slack goes through this module."""
from typing import List

ESCAPES = {"&": "&amp;", "<": "&lt;", ">": "&gt;"}


def escaped_pieces(text: str) -> List[str]:
    """`text` one piece per character, each escaped: a piece is never half an entity, so a caller that cuts to a length
    can cut between pieces."""
    return [ESCAPES.get(ch, ch) for ch in text]


def escape_literal(text: str) -> str:
    """`text` with `&`, `<` and `>` escaped, so Slack shows it as written."""
    return "".join(escaped_pieces(text))
