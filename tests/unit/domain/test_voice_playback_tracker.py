"""PlaybackTracker — what the caller actually heard, from Twilio mark echoes."""
import base64

from src.domain.voice_playback_tracker import PlaybackTracker, mulaw_payload_bytes


def _b64(nbytes: int) -> str:
    return base64.b64encode(b"\xff" * nbytes).decode()


def test_payload_bytes_are_decoded_length():
    assert mulaw_payload_bytes(_b64(160)) == 160


def test_record_sent_returns_running_byte_total_as_mark_name():
    tracker = PlaybackTracker()
    assert tracker.record_sent(_b64(160)) == "160"
    assert tracker.record_sent(_b64(80)) == "240"
    assert tracker.sent_bytes == 240


def test_played_advances_only_forward_and_never_past_sent():
    tracker = PlaybackTracker()
    tracker.record_sent(_b64(800))
    tracker.record_played("400")
    tracker.record_played("160")  # out-of-order echo must not rewind
    assert tracker.played_bytes == 400
    tracker.record_played("9999")
    assert tracker.played_bytes == 800


def test_foreign_mark_names_are_ignored():
    tracker = PlaybackTracker()
    tracker.record_sent(_b64(80))
    tracker.record_played("not-ours")
    assert tracker.played_bytes == 0


def test_caught_up_when_everything_sent_has_played():
    tracker = PlaybackTracker()
    assert tracker.caught_up
    tracker.record_sent(_b64(80))
    assert not tracker.caught_up
    tracker.record_played("80")
    assert tracker.caught_up


def test_played_ms_since_counts_only_audio_after_the_offset():
    tracker = PlaybackTracker()
    tracker.record_sent(_b64(8000))   # earlier item: 1000 ms
    start = tracker.sent_bytes
    tracker.record_sent(_b64(16000))  # current item: 2000 ms
    tracker.record_played(str(start + 12000))
    assert tracker.played_ms_since(start) == 1500
    assert tracker.played_ms_since(start + 20000) == 0
