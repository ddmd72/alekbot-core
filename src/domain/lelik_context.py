from dataclasses import dataclass
from typing import Dict, List


@dataclass(frozen=True)
class LelikContext:
    """What Lelik knows at call start (VOICE_COMPANION_RFC §4.8): the facts and the
    primary channel's recent history, before any prompt is assembled."""

    biographical_facts: List[Dict]
    conversation_history: List[Dict]
