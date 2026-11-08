from dataclasses import asdict, dataclass
from typing import Any, Dict

from src.domain.voice_turn_ownership import TurnOwnership


@dataclass(frozen=True)
class VoiceSessionSpec:
    """What the relay needs to run one call on a provider. It travels in the session config
    under "voice" (VOICE_MULTI_PROVIDER_RFC §4.2)."""

    provider: str
    turn_ownership: TurnOwnership
    voice: str
    reasoning_effort: str
    # Offer the provider's own server-side search tools in the session (xAI: web_search, x_search). Set per call by
    # LelikAgent.session_config from the LELIK_XAI_NATIVE_SEARCH switch; the relay hands it to the adapter.
    builtin_search: bool = False

    def to_dict(self) -> Dict[str, Any]:
        wire = {**asdict(self), "turn_ownership": self.turn_ownership.value}
        if not self.builtin_search:
            # Absent when off: with the switch off the wire is what it was before the switch existed.
            del wire["builtin_search"]
        return wire

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "VoiceSessionSpec":
        return cls(
            provider=data["provider"],
            turn_ownership=TurnOwnership(data["turn_ownership"]),
            voice=data["voice"],
            reasoning_effort=data["reasoning_effort"],
            builtin_search=bool(data.get("builtin_search", False)),
        )
