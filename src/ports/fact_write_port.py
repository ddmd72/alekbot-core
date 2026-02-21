"""
FactWritePort — abstract interface for writing facts with embedding generation.

Justification for port promotion:
- Used in ConsolidationAgent (agents layer) and FactManagementAdapter (adapters layer).
- Encapsulates multi-vector embedding generation + deduplication logic.
- Enables test doubles without real Firestore + Gemini embedding calls.
"""
from abc import ABC, abstractmethod
from typing import Dict, List, Optional, Tuple


class FactWritePort(ABC):
    """Abstract interface for writing facts with automatic multi-vector generation."""

    @abstractmethod
    async def add_facts_batch(
        self,
        account_id: str,
        user_id: str,
        facts_data: List[Dict],
        skip_deduplication: bool = False,
    ) -> Tuple[int, int]:
        """
        Add a batch of facts with automatic multi-vector embedding and deduplication.

        Args:
            account_id: Account ID (billing entity).
            user_id: User ID (attribution).
            facts_data: List of fact dicts from LLM (text/content, tags, type, metadata).
            skip_deduplication: Skip semantic deduplication (for deliberate fact management).

        Returns:
            Tuple of (saved_count, skipped_count).
        """
