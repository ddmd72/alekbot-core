"""The refusal text of an intent that is turned off for the caller (domain/config_resolution.py).

It names no level: the same text holds whether the user, the account or a SYSTEM default turned the intent off. The
renderer of a tool result prefixes it with `Tool error: `.
"""


def intent_unavailable_text(intent: str) -> str:
    """The refusal of `intent`."""
    return f"{intent} is turned off for this user; calling it again gets the same answer."
