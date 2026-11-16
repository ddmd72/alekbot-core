from enum import Enum
from types import MappingProxyType
from typing import Mapping


class VoiceMode(str, Enum):
    """What a voice call is for. LELIK is the companion call every caller gets today; the others are
    single-purpose modes started from their own page, each with its own prompt profile."""

    LELIK = "lelik"
    INTERPRETER = "interpreter"
    TUTOR = "tutor"


# Only the companion call ends with a summary to chat and memory; an interpreter or tutor session is a tool,
# not a conversation to remember.
VOICE_MODES_WITH_CALL_SUMMARY = frozenset({VoiceMode.LELIK})

# The mode pages show the call's text live (VOICE_CALL_LIVE_TEXT_RFC §4.1); the main page is unchanged.
VOICE_MODES_WITH_LIVE_TEXT = frozenset({VoiceMode.INTERPRETER, VoiceMode.TUTOR})

# The modes whose web call page lets the caller keep the call's text as a file (VOICE_CALL_LIVE_TEXT_RFC §4.11).
VOICE_MODES_WITH_CALL_FILE = frozenset({VoiceMode.INTERPRETER, VoiceMode.TUTOR})


class UnsupportedVoiceMode(ValueError):
    """The user's voice provider has no prompt profile for the requested mode."""


# A call kept as a file (VOICE_CALL_LIVE_TEXT_RFC §4.11) is summarised with its own mode's prompt: an interpreter's
# turns are two people and the translations, a tutor's are a learner and the lesson. The value is the companion type
# CompanionExtractorPort.extract takes for that mode; LELIK has none, its summary goes to chat under "voice".
CALL_FILE_SUMMARIZER_TYPES: Mapping[VoiceMode, str] = MappingProxyType({
    VoiceMode.INTERPRETER: "voice_interpreter",
    VoiceMode.TUTOR: "voice_tutor",
})
