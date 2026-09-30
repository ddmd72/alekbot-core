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

    def to_dict(self) -> Dict[str, Any]:
        return {**asdict(self), "turn_ownership": self.turn_ownership.value}

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "VoiceSessionSpec":
        return cls(
            provider=data["provider"],
            turn_ownership=TurnOwnership(data["turn_ownership"]),
            voice=data["voice"],
            reasoning_effort=data["reasoning_effort"],
        )
