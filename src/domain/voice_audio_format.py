from dataclasses import dataclass


@dataclass(frozen=True)
class AudioFormat:
    """What a stream of call audio is, so byte counts can be turned into time.

    `encoding` is the wire name a realtime provider understands ("audio/pcmu", "audio/pcm")."""

    encoding: str
    sample_rate_hz: int
    channels: int = 1
    bytes_per_sample: int = 1

    @property
    def bytes_per_ms(self) -> int:
        return self.sample_rate_hz * self.channels * self.bytes_per_sample // 1000


# Twilio Media Streams and the telephony provider session: G.711 μ-law, 8 kHz mono.
MULAW_8K = AudioFormat("audio/pcmu", 8000, 1, 1)
# Web calls to the realtime provider: 16-bit PCM, 24 kHz mono.
PCM16_24K = AudioFormat("audio/pcm", 24000, 1, 2)
# Cloudflare Realtime SFU WebSocket adapters: 16-bit PCM, 48 kHz, stereo interleaved.
PCM16_48K_STEREO = AudioFormat("audio/pcm", 48000, 2, 2)
