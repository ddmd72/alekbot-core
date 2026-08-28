"""
CompanionExtractorPort — boundary between CompanionExtractionService
(services/, domain+ports imports only) and the concrete per-companion-type
extractor agent (agents/), which only composition/ may import directly.

Raises on failure (any Exception) rather than returning a status object —
CompanionExtractionService treats any exception the same way it would treat
an AgentResponse.failure(): increment attempts, retry/backoff.
"""
from abc import ABC, abstractmethod
from typing import Any, Dict, List


class CompanionExtractorPort(ABC):

    @abstractmethod
    async def extract(
        self,
        companion_type: str,
        account_id: str,
        created_by_user_id: str,
        messages: List[dict],
    ) -> Dict[str, Any]:
        """Run the companion_type's extractor over a batch of messages.

        Returns {"records": [{"text": str, "domain": str, "tags": [str]}], "summary": str}.
        Raises on failure (unknown companion_type, missing user, LLM/parse error)."""
