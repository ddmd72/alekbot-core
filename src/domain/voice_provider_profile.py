from dataclasses import dataclass, field
from typing import Dict, Mapping, Optional

from src.domain.voice_mode import UnsupportedVoiceMode, VoiceMode
from src.domain.voice_session_spec import VoiceSessionSpec
from src.domain.voice_turn_ownership import TurnOwnership


@dataclass(frozen=True)
class VoiceProviderProfile:
    """Everything that differs per realtime provider for one voice call: the prompt profile Lelik's
    instructions are built from, plus the relay-side session spec (VOICE_MULTI_PROVIDER_RFC §4.1)."""

    prompt_profile: str
    session: VoiceSessionSpec
    # Prompt profile per non-default mode; a provider without an entry does not offer that mode.
    mode_profiles: Mapping[VoiceMode, str] = field(default_factory=dict)
    # The provider can run a web search of its own inside the session (xAI's server-side `web_search`,
    # XaiRealtimeAdapter). Used only when the LELIK_XAI_NATIVE_SEARCH switch is on: Lelik is then not offered the
    # delegated light web search, one search path rather than two (owner decision 2026-11-08; native vs delegated is
    # decided by a side-by-side quality comparison).
    builtin_web_search: bool = False

    def prompt_profile_for(self, mode: VoiceMode) -> str:
        if mode == VoiceMode.LELIK:
            return self.prompt_profile
        try:
            return self.mode_profiles[mode]
        except KeyError:
            raise UnsupportedVoiceMode(f"voice mode {mode.value!r} is not available for provider {self.session.provider!r}") from None


VOICE_PROVIDER_PROFILES: Dict[str, VoiceProviderProfile] = {
    # Owner's UAT call 4, 2026-09-30 (decisions/voice_xai_protocol_probe.md); its portrait split into
    # user-overridable voice tokens (VOICE_MULTI_PROVIDER_RFC §4.5).
    "xai": VoiceProviderProfile(
        prompt_profile="lelik_xai",
        session=VoiceSessionSpec("xai", TurnOwnership.PROVIDER, voice="castor", reasoning_effort="high"),
        mode_profiles={VoiceMode.INTERPRETER: "lelik_xai_interpreter", VoiceMode.TUTOR: "lelik_xai_tutor"},
        builtin_web_search=True,
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
