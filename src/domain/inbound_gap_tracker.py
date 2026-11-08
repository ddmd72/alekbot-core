"""Detects gaps in a call's inbound audio packets (VOICE_CALL_CONTROL_CHANNEL_RFC §7.4.2).

Pure: time is passed in, nothing is logged. The caller feeds `packet(now)` on every inbound
packet and polls `gap_started(now)` on a timer; both return plain values the caller logs.
Not armed until the first packet, so pairing latency is never reported as a gap.
"""
from typing import Optional

DEFAULT_GAP_FLOOR_S = 2.0


class InboundGapTracker:
    def __init__(self, floor_s: float = DEFAULT_GAP_FLOOR_S) -> None:
        self._floor_s = floor_s
        self._last: Optional[float] = None
        self._announced = False

    @property
    def floor_s(self) -> float:
        return self._floor_s

    def packet(self, now: float) -> Optional[float]:
        """Record a packet. Returns the gap duration in seconds when this packet ends a gap that
        reached the floor, else None."""
        last, self._last = self._last, now
        self._announced = False
        if last is None:
            return None
        duration = now - last
        return duration if duration >= self._floor_s else None

    def gap_started(self, now: float) -> bool:
        """True exactly once per gap, when the silence first exceeds the floor."""
        if self._last is None or self._announced or now - self._last < self._floor_s:
            return False
        self._announced = True
        return True
