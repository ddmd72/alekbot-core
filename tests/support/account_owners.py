"""An account store for the owner-timezone read of `ConsolidationService` (Facts S3 T7, plan §3.10), and the production
resolver over it (Facts S4 T7c).

Holds one account per id with the given user as its owner, records every id asked for, and answers None for any
other id. Fixtures that build the service only to test something else hand it `owner_zones(...)`: the production
`OwnerTimezoneResolver` over this store and the service's own user store, so the run resolves the owner's zone cleanly
and logs nothing about it.
"""
from typing import Any, Dict, List, Optional

from src.domain.billing import BillingAccount
from src.services.owner_timezone_resolver import OwnerTimezoneResolver


class OwnedAccounts:
    def __init__(self, owners: Dict[str, str]) -> None:
        self._owners = dict(owners)          # account id → the owner's user id
        self.asked: List[str] = []

    async def get_account(self, account_id: str) -> Optional[BillingAccount]:
        self.asked.append(account_id)
        owner = self._owners.get(account_id)
        return None if owner is None else BillingAccount(account_id=account_id, iam_policy={owner: "owner"})


def owner_zones(owners: Dict[str, str], user_repo: Any) -> OwnerTimezoneResolver:
    """The production resolver over `OwnedAccounts(owners)` and `user_repo` (pass the service's own user store, so the
    owner's profile is read where the service reads profiles, as before T7c)."""
    return OwnerTimezoneResolver(OwnedAccounts(owners), user_repo)
