"""
ImageTriagePort — which images show text to read, and a caption for the rest (docs/10_rfcs/USER_DRIVE_INDEXING_RFC.md
§5.3, §24.5). One call per batch of thumbnails; implemented by the triage agent's runner.
"""
from abc import ABC, abstractmethod
from typing import Sequence

from src.domain.drive_extraction import TriageBatchOutcome, TriageImage


class ImageTriagePort(ABC):

    @abstractmethod
    async def triage(self, user_id: str, account_id: str, images: Sequence[TriageImage]) -> TriageBatchOutcome:
        """One result per image answered (matched by `item_id`): `text` or `photo`, a photograph with a caption of at
        most ten words, plus the calls made and their cost for the tick summary (§24.9). An image whose call failed
        for its input other than by a refusal has no result: the caller retries it as a per-file failure (§24.8).
        Raises DriveModelUnavailable when the model cannot be used now (§24.8); the caller bills and configures
        through `user_id` and `account_id`."""
