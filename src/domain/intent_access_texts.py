"""The refusal text of an intent that is not available on the caller's profile (domain/config_resolution.py).

It names no level: the same text holds whether the user, the account or a SYSTEM default turned the intent off. The
renderer of a tool result prefixes it with `Tool error: `.
"""


def intent_unavailable_text(intent: str) -> str:
    """The refusal of `intent`."""
    return f"{intent} is not available on this profile."
