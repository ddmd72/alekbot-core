from collections import deque
from typing import Callable, Deque, List, Tuple


class PacedAudioOutlet:
    """Provider audio -> fixed-size transport frames, one per tick (VOICE_WEB_TRANSPORT_RFC §5.4).

    Provider audio arrives faster than real time and in arbitrary chunk sizes; a real-time
    transport wants exactly one frame per tick. A response's last partial frame is zero-padded
    and sent at once — held back, the tail would never play and playback would never catch up.
    With nothing queued the frame is silence, which also keeps an idle track alive.

    Each pushed chunk carries a mark (PlaybackTracker's running byte total); the mark is released
    once the chunk has been fully handed on, which is what the transport reports as played."""

    def __init__(self, frame_bytes: int, convert: Callable[[bytes], bytes] = lambda data: data) -> None:
        self._frame_bytes = frame_bytes
        self._convert = convert
        self._pending = bytearray()
        self._marks: Deque[Tuple[int, str]] = deque()  # (stream offset where the chunk ends, mark)
        self._enqueued = 0
        self._emitted = 0

    @property
    def pending_bytes(self) -> int:
        return len(self._pending)

    def push(self, provider_bytes: bytes, mark: str) -> None:
        converted = self._convert(provider_bytes)
        self._pending.extend(converted)
        self._enqueued += len(converted)
        self._marks.append((self._enqueued, mark))

    def next_frame(self) -> Tuple[bytes, List[str]]:
        take = min(len(self._pending), self._frame_bytes)
        frame = bytes(self._pending[:take]) + bytes(self._frame_bytes - take)
        del self._pending[:take]
        self._emitted += take
        released = []
        while self._marks and self._marks[0][0] <= self._emitted:
            released.append(self._marks.popleft()[1])
        return frame, released

    def clear(self) -> None:
        self._emitted += len(self._pending)
        self._pending.clear()
        self._marks.clear()
