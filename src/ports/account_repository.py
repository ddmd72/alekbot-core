from abc import ABC, abstractmethod
from typing import List, Optional, Tuple

from ..domain.billing import BillingAccount, UsageIncrement


class AccountRepository(ABC):
    """Billing accounts by account_id.

    - Tenancy: the account id is the scope; no user filter.
    - Legacy: get_account and list_all_accounts build BillingAccount: a missing field takes the model default; unknown
      fields are dropped. An unknown `tier`: no writer produces one; behaviour pending the data audit.
    - Errors: backend errors propagate.
    """

    @abstractmethod
    async def get_account(self, account_id: str) -> Optional[BillingAccount]:
        """The account, or None when absent."""
        pass

    @abstractmethod
    async def create_account(self, account: BillingAccount) -> BillingAccount:
        """Store the account as given, replacing any document with the same id. Returns it."""
        pass

    @abstractmethod
    async def update_account(self, account: BillingAccount) -> BillingAccount:
        """Store the account's fields as given. Usage counters are not the caller's to write: a usage
        increment that landed since the caller's get_account is kept. Errors: an absent account raises (NotFound)."""
        pass

    @abstractmethod
    async def set_member_role(self, account_id: str, user_id: str, role: str) -> None:
        """Store `role` as user_id's entry of the account's iam_policy; every other entry and field is kept as stored.

        Atomicity: one entry is written, never the whole map, so concurrent calls for different members of one account
        all land (two team invites accepted at once). Idempotency: a repeat with the same role changes nothing.
        Errors: an absent account raises (NotFound), nothing written.
        """
        pass

    @abstractmethod
    async def increment_account_usage(
        self, account_id: str, tokens: int, cost: float
    ) -> UsageIncrement:
        """Add tokens, cost and one request to the account's total, daily and monthly counters.

        Atomicity: no concurrent increment is lost. When the UTC day or month has turned since the stored reset stamp,
        the window rotates exactly once under concurrent callers (prev_daily_* snapshot of the ended day, its date in
        prev_daily_date; prev_monthly_* snapshot of the ended month, its month in
        prev_monthly_month) and this increment is applied to the new window.
        Returns the day's spend before and after this increment, so concurrent callers get disjoint, ordered slices
        and a caller can detect a budget limit crossing without re-reading the account.
        Legacy: an account without `usage` starts its windows on this call; a missing daily_cost_limit is the default
        ($5). A non-datetime reset stamp: no writer produces one; behaviour pending the data audit.
        Errors: ValueError, nothing written, for an absent account.
        """
        pass

    @abstractmethod
    async def check_quota(self, account_id: str) -> Tuple[bool, str]:
        """Return (has_quota, reason) based on account usage vs limits.

        Dead by decision (no caller in src/; the hard cap was dropped 2026-07-26).
        """
        pass

    @abstractmethod
    async def list_all_accounts(self) -> List[BillingAccount]:
        """Accounts whose stored is_active is true. Order: none promised. No limit. A document without is_active:
        no writer produces one; behaviour pending the data audit."""
        pass
