"""The relay's optional per-call knobs as one frozen bundle: everything off by default."""
import dataclasses

import pytest

from src.domain.voice_relay_call_options import VoiceRelayCallOptions


def test_the_defaults_are_off():
    options = VoiceRelayCallOptions()
    assert options.live_text_lag_ms == 0 and options.debug_transcript_users == frozenset()


def test_the_bundle_is_frozen():
    with pytest.raises(dataclasses.FrozenInstanceError):
        VoiceRelayCallOptions().live_text_lag_ms = 5  # type: ignore[misc]
