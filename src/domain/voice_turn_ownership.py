from enum import Enum


class TurnOwnership(str, Enum):
    """Who starts a voice reply and stops it when the caller talks over it
    (VOICE_MULTI_PROVIDER_RFC §4.3)."""

    # The relay places the persona anchor, starts every reply, and owns barge-in and silence notes.
    RELAY = "relay"
    # The provider replies and handles interruptions itself; the relay adds only the greeting and
    # delegation answers.
    PROVIDER = "provider"
