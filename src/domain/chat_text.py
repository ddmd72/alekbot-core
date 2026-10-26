"""Rules about the text a chat user sends, shared by everything that reads it."""


def unwrapped(text: str) -> str:
    """`text` trimmed and, when backticks wrap the WHOLE trimmed text (any number of them, e.g. a code block), without them.

    A copied `` `$skill save CODE` `` is still the command, and a reply `` `2` `` is still the digit; backticks around
    a part of the text (`` "`$HOME` is wrong" ``) leave it untouched."""
    trimmed = text.strip()
    if trimmed.startswith("`") and trimmed.endswith("`"):
        return trimmed.strip("`").strip()
    return trimmed
