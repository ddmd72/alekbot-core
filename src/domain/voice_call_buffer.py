from dataclasses import dataclass, field
from datetime import datetime
from typing import Dict, List


@dataclass
class VoiceTurnSegment:
    """One request/response pair, segmented on the provider's
    response.created -> response.done boundary (RFC §4.12)."""

    request_text: str
    response_text: str
    started_at: datetime
    ended_at: datetime
    finish_reason: str = "completed"


@dataclass
class VoiceCallBuffer:
    """Call-scoped accumulator held by VoiceSessionService, flushed once via
    CallControlPlanePort.submit_transcript at call end (RFC §4.5)."""

    call_id: str
    turns: List[VoiceTurnSegment] = field(default_factory=list)
    usage_by_model: Dict[str, Dict[str, int]] = field(default_factory=dict)
    transcript_text: str = ""

    def add_turn(self, segment: VoiceTurnSegment) -> None:
        self.turns.append(segment)
        self.transcript_text += f"\nUser: {segment.request_text}\nLelik: {segment.response_text}"

    def add_usage(self, model: str, **token_kwargs: int) -> None:
        bucket = self.usage_by_model.setdefault(model, {})
        for key, value in token_kwargs.items():
            bucket[key] = bucket.get(key, 0) + value
