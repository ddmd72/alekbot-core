"""
DocumentProfilerPort — one text file's profile, written once at index time (docs/10_rfcs/USER_DRIVE_INDEXING_RFC.md
§25.4.1, frozen in §25.13.1). Implemented by the profiler agent's runner in `composition/` (P3).
"""
from abc import ABC, abstractmethod

from src.domain.drive_enrichment_calls import ProfileReply, ProfileRequest


class DocumentProfilerPort(ABC):

    @abstractmethod
    async def profile(self, request: ProfileRequest) -> ProfileReply:
        """The model's profile of `request.input`, unverified (the caller runs `drive_profile.verify`). Every outcome
        is a reply status, with the model that served and the cost already billed through `user_id` and
        `account_id` (§25.4.4, §25.4.6)."""
