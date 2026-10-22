from abc import ABC, abstractmethod
from typing import List, Optional
from ..domain.invite_code import InviteCode


class InviteCodeRepository(ABC):
    """
    Port for invite codes. A code is a bearer secret (stored under the code itself): its holder joins the creator's
    account (TEAM_INVITE) or links a platform (SELF_LINK, no consumer in src/).

    Holds for every method:
    - Validity: never decided here. Reads return used and expired invites with used_at and expires_at as saved
      (tz-aware UTC); the caller refuses them with InviteCode.is_valid (invite_code_service.py:91).
    - Legacy documents: a missing role reads as "MEMBER"; a missing or unknown type, or another missing field,
      raises on read (observed); callers treat a raise as a refusal.
    - Errors: backend errors propagate.
    """

    @abstractmethod
    async def create(self, invite_code: InviteCode) -> InviteCode:
        """Store `invite_code` under its code and return it.

        Idempotency: callers need a new code never to replace another. Known defect: an existing code is overwritten
            (firestore_invite_code_repo.py:71), so a collision of the 9-hex-character code
            (invite_code_service.py:47-48) rebinds the older invite to the new creator's account.
        """
        pass

    @abstractmethod
    async def get_by_code(self, code: str) -> Optional[InviteCode]:
        """Return the invite stored under exactly `code` (case-sensitive), or None.

        Tenancy: none; holding the code is the authorization, the caller checks type and validity.
        """
        pass

    @abstractmethod
    async def update(self, invite_code: InviteCode) -> InviteCode:
        """Replace the stored invite with `invite_code` (e.g. marked used) and return it.

        Atomicity: a blind overwrite, last writer wins; an absent code is created (observed).
        Known defect: consumption must be one-shot, but consume_team_invite reads, checks is_valid and writes here
            with no precondition, so two concurrent consumers of one code both join the account
            (invite_code_service.py:85-159, firestore_invite_code_repo.py:89).
        """
        pass

    @abstractmethod
    async def list_by_user(self, user_id: str) -> List[InviteCode]:
        """Return every invite created by `user_id`, used and expired ones included.

        Tenancy: never another creator's invites. Order: none. Limit: none.
        """
        pass
