from src.domain.voice_audio_frame import AudioFrame


def test_audio_frame_is_frozen_value_object():
    frame = AudioFrame(encoding="audio/pcmu", sample_rate_hz=8000, payload=b"\x00\x01", track="inbound")
    assert frame.encoding == "audio/pcmu"
    assert frame.sample_rate_hz == 8000
    assert frame.payload == b"\x00\x01"
    assert frame.track == "inbound"


def test_audio_frame_rejects_unknown_track():
    import pytest
    with pytest.raises(ValueError):
        AudioFrame(encoding="audio/pcmu", sample_rate_hz=8000, payload=b"\x00", track="sideways")
