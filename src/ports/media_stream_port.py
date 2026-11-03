"""Streams a file's bytes from its media source (DRIVE_MCP_CONNECTOR_RFC §8: the one-time link always streams through
the main service, never a redirect to the provider's URL).

Implemented by HttpMediaStreamAdapter; used by DriveLinkService.
"""
from abc import ABC, abstractmethod
from typing import AsyncIterator

from ..domain.drive_media_source import MediaSource


class MediaStreamPort(ABC):
    @abstractmethod
    def chunks(self, source: MediaSource) -> AsyncIterator[bytes]:
        """The bytes at `source.url`, fetched with `source.headers`, in chunks, as an async generator.

        The request starts at the first iteration. Raises DriveItemNotFoundError when the upstream answers 404 or 410,
        MediaStreamFailed for any other failure (another status, a connection or read error). Closing the generator
        (a client that disconnected included) closes the upstream connection. The url and headers are secrets: never
        logged or put in an error (`drive_media_source.py`).
        """
