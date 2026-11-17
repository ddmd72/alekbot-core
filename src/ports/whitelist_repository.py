"""
Repository port (interface) for whitelist data.

Part of Hexagonal Architecture - defines contract for whitelist storage.
"""
from abc import ABC, abstractmethod
from ..domain.whitelist import WhitelistEntry


class WhitelistRepository(ABC):
    """
    Port for the access whitelist: the emails and domains that may use the product.

    The chat gate (IAMService.authorize), the Cabinet sign-in gate (AuthenticationService) and team invites
    (InviteCodeService.consume_team_invite) decide access with WhitelistEntry.is_allowed on get_whitelist.

    Holds for every method:
    - Tenancy: one global whitelist; no account or user scope.
    - Case: entries are lowercase and stripped; callers compare a lowercased email (WhitelistEntry.is_allowed).
    - Atomicity: a write adds or removes one entry and keeps every other; concurrent writes of different entries
      are all kept.
    - Errors: backend errors propagate; every caller treats a raise as a refusal, never as access.
    """

    @abstractmethod
    async def get_whitelist(self) -> WhitelistEntry:
        """Return the whitelist as stored now; no cache, so a removal refuses on the next call.

        Missing config: an empty whitelist (no one allowed), never an error.
        Legacy: a missing allowed_emails or allowed_domains reads as empty; a null one raises (observed).
        Case: entries are returned lowercased and stripped (an entry that is blank after stripping is dropped), so one
            written in mixed case outside this port (scripts/admin/init_whitelist.py:22) still matches.
        """
        pass
