"""
PassageHintPort — doc2query keywords and questions for a few chunks of one file, each with a verbatim evidence quote
(docs/10_rfcs/USER_DRIVE_INDEXING_RFC.md §25.4.2, frozen in §25.13.1). Implemented by the passages agent's runner in
`composition/` (P3).
"""
from abc import ABC, abstractmethod

from src.domain.drive_enrichment_calls import HintReply, HintRequest


class PassageHintPort(ABC):

    @abstractmethod
    async def hints(self, request: HintRequest) -> HintReply:
        """Items per chunk index, ungrounded (the caller runs `drive_hints.ground`). Every outcome is a reply status,
        with the model that served and the cost already billed through `user_id` and `account_id` (§25.4.4,
        §25.4.6)."""
