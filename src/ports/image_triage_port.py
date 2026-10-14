"""
ImageTriagePort — which images show text to read, and a description of each photograph
(docs/10_rfcs/USER_DRIVE_INDEXING_RFC.md §5.3, §24.5; triage v2: §25.4.3, §25.13.1). One call per batch of
thumbnails; implemented by the triage agent's runner.
"""
from abc import ABC, abstractmethod
from typing import Sequence

from src.domain.drive_enrichment_calls import TriageReply
from src.domain.drive_extraction import TriageImage


class ImageTriagePort(ABC):

    @abstractmethod
    async def triage(self, user_id: str, account_id: str, images: Sequence[TriageImage]) -> TriageReply:
        """One `TriageResultV2` per image answered (matched by `item_id`): `text`, or `photo` with its description.
        `capped_item_ids` / `refused_item_ids` were still stopped at the output cap / refused alone and get no
        description; an image in none of the three was not settled and is retried. Every outcome is a `status`, never
        an exception (an outage included, §25.4.6), with the calls made, the model that served, the cost and the
        fallback's share, already billed through `user_id` and `account_id`."""
