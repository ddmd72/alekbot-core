"""The relay's optional per-call knobs, read once at relay boot (`relay_main`, from `os.getenv`) and handed to
`VoiceSessionService` as one frozen bundle, so the session loop's constructor does not grow a parameter per knob
(composition-root assessment D1)."""
from dataclasses import dataclass
from typing import FrozenSet


@dataclass(frozen=True)
class VoiceRelayCallOptions:
    """`live_text_lag_ms` (web calls only, interpreter and tutor mode calls with a control link,
    VOICE_CALL_LIVE_TEXT_RFC §4.3): how much audio, in ms, the live text waits behind the played audio.

    `debug_transcript_users` (debug only, temporary; empty = off): a call of a listed user, in any mode, logs its turn
    events, notes, tool calls and both sides' transcript text as `voice debug <fingerprint>:` INFO lines. Text never
    appears in any other line, and nothing changes for other users (docs/07_deployment/README.md, relay knob 8)."""

    live_text_lag_ms: int = 0
    debug_transcript_users: FrozenSet[str] = frozenset()
