import base64
from dataclasses import dataclass

# μ-law at 8 kHz: one byte per sample, 8 samples per millisecond.
MULAW_8K_BYTES_PER_MS = 8


def mulaw_payload_bytes(b64_payload: str) -> int:
    """Byte length of a base64 μ-law payload, without decoding the audio twice."""
    return len(base64.b64decode(b64_payload))


@dataclass
class PlaybackTracker:
    """How much outbound audio the caller has actually heard, for one call.

    The relay writes audio into Twilio far ahead of playback, so "sent" is not
    "heard". Each outbound chunk carries a Twilio `mark` named by the running byte
    total; Twilio echoes a mark once everything before it has played. Barge-in
    truncation and the silence watchdog both read this, never the sent count.
    """

    sent_bytes: int = 0
    played_bytes: int = 0

    def record_sent(self, b64_payload: str) -> str:
        """Account one outbound chunk; returns the mark name to send after it."""
        self.sent_bytes += mulaw_payload_bytes(b64_payload)
        return str(self.sent_bytes)

    def record_played(self, mark_name: str) -> None:
        """Twilio echoed `mark_name`: audio up to that byte total has played.
        Marks for audio dropped by a `clear` echo too — the buffer is empty then."""
        try:
            played = int(mark_name)
        except ValueError:
            return
        self.played_bytes = max(self.played_bytes, min(played, self.sent_bytes))

    @property
    def caught_up(self) -> bool:
        return self.played_bytes >= self.sent_bytes

    def played_ms_since(self, start_bytes: int) -> int:
        """Milliseconds heard of the audio sent after byte offset `start_bytes`."""
        return max(self.played_bytes - start_bytes, 0) // MULAW_8K_BYTES_PER_MS
