"""Voice call resumption: what is kept after a call, and under which key (VOICE_CALL_CONTROL_CHANNEL_RFC §6.2)."""
from typing import Optional

from src.domain.voice_mode import VoiceMode

# Only xAI keeps a conversation server-side that a new connection can continue (live probe P3, 2026-11-08).
RESUME_PROVIDER = "xai"
# Owner, 2026-11-08: a re-dial within 25 minutes resumes (under xAI's own 30 minutes of idle history).
RESUME_TTL_S = 25 * 60
# How long the button-hangup marker outlives the hangup: the relay's end-of-call report follows within seconds.
BUTTON_END_TTL_S = 10 * 60
# The session-config field carrying the handle to the relay.
RESUME_CONFIG_FIELD = "resume"


def resume_key(user_id: str) -> str:
    """One record per user; the record names the mode and provider it belongs to."""
    return f"voice_resume:{user_id}"


def resume_applies(mode: Optional[str], provider: Optional[str]) -> bool:
    """A call's handle is kept only for a known mode on a provider that can resume."""
    return provider == RESUME_PROVIDER and mode in {m.value for m in VoiceMode}
