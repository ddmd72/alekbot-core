from dataclasses import dataclass
from typing import Literal

_VALID_TRACKS = {"inbound", "outbound"}


@dataclass(frozen=True)
class AudioFrame:
    """A carrier-agnostic slice of call audio crossing RealtimeSessionPort."""

    encoding: str
    sample_rate_hz: int
    payload: bytes
    track: Literal["inbound", "outbound"]

    def __post_init__(self) -> None:
        if self.track not in _VALID_TRACKS:
            raise ValueError(f"track must be one of {_VALID_TRACKS}, got {self.track!r}")
