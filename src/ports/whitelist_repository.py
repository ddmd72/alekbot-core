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
        Legacy: a missing allowed_emails or allowed_domains reads as empty; a null one raises (observed). Known defect: entries are returned as stored, so one written in mixed case outside this
            port (scripts/admin/init_whitelist.py:22) never matches (firestore_whitelist_repo.py:90-91).
        """
        pass

    @abstractmethod
    async def add_email(self, email: str) -> None:
        """Add `email`, lowercased and stripped, to the allowed emails.

        Contract (observed; no caller in src/).
        Idempotency: adding a present email changes nothing; a missing config is created.
        Errors: no format validation.
        """
        pass

    @abstractmethod
    async def remove_email(self, email: str) -> None:
        """Remove `email`, lowercased and stripped, from the allowed emails.

        Contract (observed; no caller in src/).
        Idempotency: removing an absent email changes nothing.
        An email whose domain is allowed still passes WhitelistEntry.is_allowed after removal.
        """
        pass

    @abstractmethod
    async def add_domain(self, domain: str) -> None:
        """Add `domain` (e.g. "company.com"), lowercased and stripped, to the allowed domains.

        Contract (observed; no caller in src/).
        Idempotency: adding a present domain changes nothing; a missing config is created.
        Errors: no format validation.
        """
        pass

    @abstractmethod
    async def remove_domain(self, domain: str) -> None:
        """Remove `domain`, lowercased and stripped, from the allowed domains.

        Contract (observed; no caller in src/).
        Idempotency: removing an absent domain changes nothing. Allowed emails of that domain still pass.
        """
        pass
