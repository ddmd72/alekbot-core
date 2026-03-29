from abc import ABC, abstractmethod
from typing import List, Optional, Tuple

from ..domain.billing import BillingAccount


class AccountRepository(ABC):
    """Abstract interface for account-level billing operations."""

    @abstractmethod
    async def get_account(self, account_id: str) -> Optional[BillingAccount]:
        """Retrieve billing account by id."""
        pass

    @abstractmethod
    async def create_account(self, account: BillingAccount) -> BillingAccount:
        """Create a new billing account."""
        pass

    @abstractmethod
    async def update_account(self, account: BillingAccount) -> BillingAccount:
        """Update billing account details."""
        pass

    @abstractmethod
    async def increment_account_usage(self, account_id: str, tokens: int, cost: float) -> None:
        """Atomically increment account usage with transactional resets."""
        pass

    @abstractmethod
    async def check_quota(self, account_id: str) -> Tuple[bool, str]:
        """Return (has_quota, reason) based on account usage vs limits."""
        pass

    @abstractmethod
    async def list_all_accounts(self) -> List[BillingAccount]:
        """Return all active billing accounts."""
        pass

    # ========================================================================
    # OAuth Multi-Tenant Session 2: IAM Policy Operations
    # RFC: docs/10_rfcs/MULTI_TENANT_OAUTH_RFC.md
    # Note: IAM operations use existing get_account() and update_account()
    #       IAMPort adapter will modify BillingAccount.iam_policy and call update_account()
    # ========================================================================

    # No additional methods needed for MVP.
    # IAMPort adapter will use:
    #   - get_account(account_id) → read iam_policy
    #   - update_account(account) → write modified iam_policy
    #
    # Future optimization (Phase 2): Add atomic IAM policy updates
    #   - async def update_iam_policy(account_id: str, user_id: str, role: str) -> BillingAccount
