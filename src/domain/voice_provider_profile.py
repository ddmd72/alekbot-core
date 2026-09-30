from dataclasses import dataclass
from typing import Dict, Optional

from src.domain.voice_session_spec import VoiceSessionSpec
from src.domain.voice_turn_ownership import TurnOwnership


@dataclass(frozen=True)
class VoiceProviderProfile:
    """Everything that differs per realtime provider for one voice call: the prompt profile Lelik's
    instructions are built from, plus the relay-side session spec (VOICE_MULTI_PROVIDER_RFC §4.1)."""

    prompt_profile: str
    session: VoiceSessionSpec


VOICE_PROVIDER_PROFILES: Dict[str, VoiceProviderProfile] = {
    # Owner's UAT call 4, 2026-09-30 (decisions/voice_xai_protocol_probe.md).
    "xai": VoiceProviderProfile(
        prompt_profile="lelik_you",
        session=VoiceSessionSpec("xai", TurnOwnership.PROVIDER, voice="castor", reasoning_effort="high"),
    ),
    # Lelik as tuned through 2026-09-29 (VOICE_COMPANION_RFC §4.15).
    "openai": VoiceProviderProfile(
        prompt_profile="lelik",
        session=VoiceSessionSpec("openai", TurnOwnership.RELAY, voice="verse", reasoning_effort="medium"),
    ),
}
# Owner, 2026-09-30: users without a choice get the call-4 xAI setup.
DEFAULT_VOICE_PROVIDER = "xai"
# A session config without a "voice" entry was minted before per-user providers existed, when every
# call ran on OpenAI with relay-owned turns.
LEGACY_VOICE_SESSION = VOICE_PROVIDER_PROFILES["openai"].session


def resolve_voice_profile(voice_provider: Optional[str]) -> VoiceProviderProfile:
    """The user's profile, or the default when unset or unknown (the caller logs an unknown value)."""
    return VOICE_PROVIDER_PROFILES.get(voice_provider or DEFAULT_VOICE_PROVIDER,
                                       VOICE_PROVIDER_PROFILES[DEFAULT_VOICE_PROVIDER])
