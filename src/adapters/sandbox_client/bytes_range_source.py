"""
BytesRangeSource: a `RangeSource` over bytes already in memory (docs/10_rfcs/UNTRUSTED_CONTENT_SANDBOX_RFC.md S5b).

The main service holds a user's video in memory; the sandbox asks for offsets and gets slices of that object, so the
service never decodes it.

Public interface:
    BytesRangeSource(data)
        .size() -> int
        .stream(offset) -> AsyncIterator[bytes]   ValueError for an offset outside [0, len(data)]
        .aclose()                                  no-op
"""
from __future__ import annotations

from typing import AsyncIterator

from ...domain.sandbox_tunnel import DATA_MAX


class BytesRangeSource:
    def __init__(self, data: bytes) -> None:
        self._data = data

    async def size(self) -> int:
        return len(self._data)

    async def stream(self, offset: int) -> AsyncIterator[bytes]:
        if not 0 <= offset <= len(self._data):
            raise ValueError("range outside the source")
        for at in range(offset, len(self._data), DATA_MAX):
            yield self._data[at:at + DATA_MAX]

    async def aclose(self) -> None:
        return None
