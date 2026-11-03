"""
TranscriptCachePort — transcripts of opened audio, kept by content key (docs/10_rfcs/EXTERNAL_FILES_RFC.md §4.4 P21,
"Cached by content key").

The same bytes are transcribed once: the caller builds the key from the content hash and the transcriber's model, so a
repeated open or a re-delegation reads the cached text. Entries are per account and expire at `purge_after` (a chat
upload's life, `document_read.TRANSCRIPT_CACHE_DAYS`); an expired entry is never returned, even before the store
removes it.
"""
from abc import ABC, abstractmethod
from datetime import datetime
from typing import Optional


class TranscriptCachePort(ABC):

    @abstractmethod
    async def get(self, account_id: str, key: str) -> Optional[str]:
        """The cached transcript of `key` in the account, or None when there is none or it has expired."""

    @abstractmethod
    async def put(self, account_id: str, key: str, text: str, *, purge_after: datetime) -> None:
        """Keep `text` under `key` in the account until `purge_after` (an aware datetime); a second put replaces it."""
