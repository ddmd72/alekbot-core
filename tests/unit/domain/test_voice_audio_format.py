"""AudioFormat — the byte arithmetic every transport and PlaybackTracker share."""
import base64

from src.domain.voice_audio_format import MULAW_8K, PCM16_24K, PCM16_48K_STEREO, AudioFormat
from src.domain.voice_playback_tracker import PlaybackTracker


def test_bytes_per_ms_of_known_formats():
    assert MULAW_8K.bytes_per_ms == 8
    assert PCM16_24K.bytes_per_ms == 48
    assert PCM16_48K_STEREO.bytes_per_ms == 192


def test_formats_carry_wire_encoding():
    assert MULAW_8K.encoding == "audio/pcmu"
    assert PCM16_24K.encoding == "audio/pcm"
    assert AudioFormat("audio/pcm", 16000, 1, 2).bytes_per_ms == 32


def test_playback_tracker_defaults_to_mulaw():
    tracker = PlaybackTracker()
    tracker.record_sent(base64.b64encode(b"\xff" * 800).decode())
    tracker.record_played("800")
    assert tracker.played_ms_since(0) == 100


def test_playback_tracker_measures_pcm24k_milliseconds():
    tracker = PlaybackTracker(bytes_per_ms=PCM16_24K.bytes_per_ms)
    tracker.record_sent(base64.b64encode(b"\x00" * 4800).decode())
    tracker.record_played("4800")
    assert tracker.played_ms_since(0) == 100
