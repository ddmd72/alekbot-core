import numpy as np

# 31-tap windowed-sinc low-pass at 11 kHz before 2:1 decimation (48 kHz -> 24 kHz); validated on
# the Cloudflare SFU spike (scripts/voice/cloudflare_sfu_poc/poc.py).
_TAPS = np.sinc(np.arange(-15, 16) * (11000 * 2 / 48000)) * np.hamming(31)
_TAPS = (_TAPS / _TAPS.sum()).astype(np.float32)
_STEREO_FRAME_BYTES = 4  # one s16le sample per channel


class PcmDownsampler:
    """48 kHz stereo s16le -> 24 kHz mono s16le. Filter state and any partial stereo sample
    are carried across calls, so chunk boundaries never change the output."""

    def __init__(self) -> None:
        self._tail = np.zeros(len(_TAPS) - 1, dtype=np.float32)
        self._phase = 0
        self._carry = b""

    def __call__(self, pcm: bytes) -> bytes:
        data = self._carry + pcm
        usable = len(data) - len(data) % _STEREO_FRAME_BYTES
        self._carry = data[usable:]
        if not usable:
            return b""
        stereo = np.frombuffer(data[:usable], dtype="<i2").astype(np.float32)
        mono = stereo.reshape(-1, 2).mean(axis=1)
        x = np.concatenate([self._tail, mono])
        filtered = np.convolve(x, _TAPS, mode="valid")
        self._tail = x[-(len(_TAPS) - 1):]
        out = filtered[self._phase::2]
        self._phase = (self._phase + len(filtered)) % 2
        return np.clip(np.round(out), -32768, 32767).astype("<i2").tobytes()
