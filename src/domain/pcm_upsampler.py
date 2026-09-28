import numpy as np


class PcmUpsampler:
    """24 kHz mono s16le -> 48 kHz stereo s16le by linear interpolation; the last sample and any
    odd trailing byte are carried across calls."""

    def __init__(self) -> None:
        self._last = 0.0
        self._carry = b""

    def __call__(self, pcm: bytes) -> bytes:
        data = self._carry + pcm
        usable = len(data) - len(data) % 2
        self._carry = data[usable:]
        if not usable:
            return b""
        mono = np.frombuffer(data[:usable], dtype="<i2").astype(np.float32)
        previous = np.concatenate([[self._last], mono[:-1]])
        self._last = float(mono[-1])
        doubled = np.empty(len(mono) * 2, dtype=np.float32)
        doubled[0::2] = (previous + mono) / 2
        doubled[1::2] = mono
        return np.clip(np.round(np.repeat(doubled, 2)), -32768, 32767).astype("<i2").tobytes()
