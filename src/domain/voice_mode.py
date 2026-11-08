from enum import Enum


class VoiceMode(str, Enum):
    """What a voice call is for. LELIK is the companion call every caller gets today; the others are
    single-purpose modes started from their own page, each with its own prompt profile."""

    LELIK = "lelik"
    INTERPRETER = "interpreter"
    TUTOR = "tutor"


# Only the companion call ends with a summary to chat and memory; an interpreter or tutor session is a tool,
# not a conversation to remember.
VOICE_MODES_WITH_CALL_SUMMARY = frozenset({VoiceMode.LELIK})


class UnsupportedVoiceMode(ValueError):
    """The user's voice provider has no prompt profile for the requested mode."""
