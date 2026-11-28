"""The refusal text of an intent that is turned off for the caller (domain/config_resolution.py).

It names no level: the same text holds whether the user, the account or a SYSTEM default turned the intent off. The
renderer of a tool result prefixes it with `Tool error: `.
"""


def intent_unavailable_text(intent: str) -> str:
    """The refusal of `intent`."""
    return f"{intent} is turned off for this user; calling it again gets the same answer."


def turned_off_intents_note() -> str:
    """The sentence a delegate tool declaration ends with when the user's profile turned off at least one intent the
    orchestrator would otherwise offer. The system prompt's agents_registry is not filtered per user and still describes
    those intents, so the model is told the two lists differ and what to do about the difference."""
    return (
        "agents_registry also describes intents missing from this list: they are turned off for this user; "
        "do not call them, and if the user asks for one, say it is turned off."
    )


def no_intents_line() -> str:
    """The intents line of a delegate tool declaration when the profile turned off every intent on offer."""
    return "(none: every specialist is turned off for this user)"


def voice_intent_unavailable_text() -> str:
    """What a live voice call answers for an intent turned off on the caller's profile: the whole tool result, prefix
    included. It is read aloud, so it names no intent."""
    return "Tool error: this request is turned off for this caller; asking again gets the same answer."
