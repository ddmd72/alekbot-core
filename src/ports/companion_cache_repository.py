"""
CompanionCacheRepository — port for the per-session context cache doc.

Session analog of FirestoreRepo.refresh_biographical_context_cache /
get_biographical_context_cached: one summary string per session_id,
refreshed by a companion's extractor (a later phase — the ConsolidationAgent
analog named in RFC §6). No AccountRepository/billing-config coupling —
that's Alek-specific, no session equivalent.
"""
from abc import ABC, abstractmethod
from typing import Optional


class CompanionCacheRepository(ABC):

    @abstractmethod
    async def get_summary(self, session_id: str) -> Optional[str]:
        """Fast read of the cached session summary. None if never written."""

    @abstractmethod
    async def save_summary(self, session_id: str, account_id: str, summary: str) -> None:
        """Overwrite the cached summary for this session (full replace, no merge)."""
