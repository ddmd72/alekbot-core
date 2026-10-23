"""An account store for `ConsolidationService`'s owner-timezone read (Facts S3 T7, plan §3.10).

Holds one account per id with the given user as its owner, records every id asked for, and answers None for any
other id. Fixtures that build the service only to test something else hand it this store, so the run resolves the
owner's zone cleanly and logs nothing about it.
"""
from typing import Dict, List, Optional

from src.domain.billing import BillingAccount


class OwnedAccounts:
    def __init__(self, owners: Dict[str, str]) -> None:
        self._owners = dict(owners)          # account id → the owner's user id
        self.asked: List[str] = []

    async def get_account(self, account_id: str) -> Optional[BillingAccount]:
        self.asked.append(account_id)
        owner = self._owners.get(account_id)
        return None if owner is None else BillingAccount(account_id=account_id, iam_policy={owner: "owner"})
