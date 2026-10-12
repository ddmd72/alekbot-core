"""
ImageTriagePort — which images show text to read, and a caption for the rest (docs/10_rfcs/USER_DRIVE_INDEXING_RFC.md
§5.3, §24.5). One call per batch of thumbnails; implemented by the triage agent's runner.
"""
from abc import ABC, abstractmethod
from typing import List, Sequence

from src.domain.drive_extraction import TriageImage, TriageResult


class ImageTriagePort(ABC):

    @abstractmethod
    async def triage(self, user_id: str, account_id: str, images: Sequence[TriageImage]) -> List[TriageResult]:
        """One result per image given (matched by `item_id`): `text` or `photo`, a photograph with a caption of at
        most ten words. Raises DriveModelUnavailable when the model cannot be used now (§24.8); the caller bills and
        configures through `user_id` and `account_id`."""
