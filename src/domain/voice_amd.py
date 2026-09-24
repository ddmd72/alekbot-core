from typing import List, Optional

# A voicemail greeting can split into two caller turns while Lelik talks over it; a live
# caller who answers Lelik at all gets past two. Below this, a machine verdict stands.
_MIN_LIVE_CALLER_TURNS = 3


def is_voicemail(answered_by: Optional[str], caller_turn_texts: List[str]) -> bool:
    """Async AMD only suggests a machine (a caller who opens with a long request looks like a
    greeting), so the verdict counts only when nobody talked back to Lelik (RFC §4.6)."""
    if not (answered_by or "").startswith("machine"):
        return False
    spoken = sum(1 for text in caller_turn_texts if text.strip())
    return spoken < _MIN_LIVE_CALLER_TURNS
