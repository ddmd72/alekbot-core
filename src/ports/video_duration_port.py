"""
VideoDurationPort: the duration of a user-supplied video, told without decoding it in the calling process
(docs/10_rfcs/UNTRUSTED_CONTENT_SANDBOX_RFC.md S5b).

Public interface:
    VideoDurationPort.duration_s(video) -> Optional[float]
"""
from abc import ABC, abstractmethod
from typing import Optional


class VideoDurationPort(ABC):
    @abstractmethod
    async def duration_s(self, video: bytes) -> Optional[float]:
        """The duration in seconds of the video's container; `None` whenever it cannot be told (the decoder is busy
        or unreachable, the file is unreadable, the container carries no duration, the wall was hit).

        Never raises except `asyncio.CancelledError`. The caller must not block a request on `None`.
        """
