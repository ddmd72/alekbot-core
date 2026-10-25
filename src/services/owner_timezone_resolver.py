"""OwnerTimezoneResolver: the account owner's IANA zone, read once and decided by `domain/owner_timezone.py`.

The one place the reads happen (the account, then the first owner's profile); every service that needs the owner's zone
takes this resolver instead of repeating the sequence. It never raises: a store hiccup is a WARNING and UTC.
"""

from typing import Optional

from ..domain.owner_timezone import FALLBACK_TIMEZONE, ResolvedOwnerZone, failure_reason, owner_to_read, owner_zone
from ..ports.account_repository import AccountRepository
from ..ports.user_repository import UserRepository
from ..utils.logger import logger


class OwnerTimezoneResolver:
    """`resolve(account_id)`: the account's first owner and that owner's zone.

    The account comes from the caller (the profile's account) and the zone never decides it. Several owners: the first
    one's zone and one WARNING. Any missing step, an invalid zone or a raising repository: `FALLBACK_TIMEZONE` and one
    WARNING.
    """

    def __init__(self, account_repo: AccountRepository, user_repo: UserRepository) -> None:
        self._account_repo = account_repo
        self._user_repo = user_repo

    async def resolve(self, account_id: str) -> ResolvedOwnerZone:
        owner_id: Optional[str] = None
        try:
            step = owner_to_read(await self._account_repo.get_account(account_id))
            owner_id = step.owner_id
            if step.owner_id is None:
                return self._unresolved(account_id, None, step.unresolved or "")
            if step.owner_count > 1:
                logger.warning(
                    "[OwnerZone] account %s has %d owners; the timezone of the first is used", account_id[:8], step.owner_count,
                )
            resolved = owner_zone(await self._user_repo.get_user(step.owner_id))
            if resolved.unresolved is not None:
                return self._unresolved(account_id, step.owner_id, resolved.unresolved)
            return ResolvedOwnerZone(step.owner_id, resolved.zone)
        except Exception as exc:
            return self._unresolved(account_id, owner_id, failure_reason(exc))

    @staticmethod
    def _unresolved(account_id: str, owner_id: Optional[str], why: str) -> ResolvedOwnerZone:
        logger.warning(
            "[OwnerZone] owner timezone unresolved for account %s: %s; using %s", account_id[:8], why, FALLBACK_TIMEZONE,
        )
        return ResolvedOwnerZone(owner_id, FALLBACK_TIMEZONE)
