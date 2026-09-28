"""48 kHz stereo (SFU) <-> 24 kHz mono (provider): a tone must survive both ways, chunking must not matter."""
import numpy as np

from src.domain.pcm_downsampler import PcmDownsampler
from src.domain.pcm_upsampler import PcmUpsampler


def _tone(rate: int, seconds: float = 1.0, hz: float = 1000.0) -> np.ndarray:
    t = np.arange(int(rate * seconds)) / rate
    return (np.sin(2 * np.pi * hz * t) * 10000).astype("<i2")


def _peak_hz(samples: np.ndarray, rate: int) -> float:
    spectrum = np.abs(np.fft.rfft(samples.astype(np.float32)))
    return float(np.argmax(spectrum) * rate / len(samples))


def test_downsampler_halves_rate_to_mono_and_keeps_the_tone():
    stereo = np.repeat(_tone(48000), 2).astype("<i2").tobytes()
    out = PcmDownsampler()(stereo)
    mono = np.frombuffer(out, dtype="<i2")
    assert abs(len(mono) - 24000) <= 16
    assert abs(_peak_hz(mono[1000:], 24000) - 1000) < 5


def test_downsampler_chunking_matches_one_shot():
    stereo = np.repeat(_tone(48000, 0.2), 2).astype("<i2").tobytes()
    whole = PcmDownsampler()(stereo)
    chunked_resampler = PcmDownsampler()
    chunked = b"".join(chunked_resampler(stereo[i:i + 3838]) for i in range(0, len(stereo), 3838))
    assert chunked == whole


def test_upsampler_doubles_rate_to_identical_channels_and_keeps_the_tone():
    out = PcmUpsampler()(_tone(24000).tobytes())
    stereo = np.frombuffer(out, dtype="<i2").reshape(-1, 2)
    assert len(stereo) == 48000
    assert (stereo[:, 0] == stereo[:, 1]).all()
    assert abs(_peak_hz(stereo[2000:, 0], 48000) - 1000) < 5


def test_upsampler_carries_an_odd_byte_between_chunks():
    mono = _tone(24000, 0.1).tobytes()
    whole = PcmUpsampler()(mono)
    split = PcmUpsampler()
    assert split(mono[:101]) + split(mono[101:]) == whole
