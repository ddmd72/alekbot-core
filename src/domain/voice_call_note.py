from datetime import datetime
from typing import List, Optional
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

# Marks the delivered call note in chat and in history; PROTOCOL_VOICE_PARTNER names it.
CALL_NOTE_PREFIX = "📞 "


def call_event_text(started_at: Optional[datetime], ended_at: Optional[datetime], timezone: str) -> str:
    """The system half of a call's history pair: an unmistakable event, so Alek never
    mistakes the note that follows for a reply of his own. Times in the user's zone."""
    if started_at is None or ended_at is None:
        return "[System: phone call with Lelik ended. The note below was posted to the user's chat.]"
    try:
        tz = ZoneInfo(timezone or "UTC")
    except (ZoneInfoNotFoundError, KeyError):
        tz = ZoneInfo("UTC")
    minutes = max((ended_at - started_at).total_seconds(), 0) / 60
    duration = "under 1 min" if minutes < 1 else f"{round(minutes)} min"
    window = f"{started_at.astimezone(tz):%H:%M}–{ended_at.astimezone(tz):%H:%M}"
    return (
        f"[System: phone call with Lelik, {window} ({duration}). "
        "The note below was posted to the user's chat.]"
    )


def call_event_from_turns(turns: List[dict], timezone: str) -> str:
    """Same event, windowed by the relay's turn segments (ISO `started_at`/`ended_at`)."""
    starts = [datetime.fromisoformat(t["started_at"]) for t in turns if t.get("started_at")]
    ends = [datetime.fromisoformat(t["ended_at"]) for t in turns if t.get("ended_at")]
    return call_event_text(min(starts) if starts else None, max(ends) if ends else None, timezone)
