from datetime import datetime, timezone
from typing import List, Optional, Tuple

from google.cloud import firestore
from google.cloud.firestore import FieldFilter

from ..domain.billing import BillingAccount
from ..ports.account_repository import AccountRepository
from ..utils.logger import logger


class FirestoreAccountRepository(AccountRepository):
    """Firestore adapter for account-level billing operations."""

    def __init__(self, db_client, collection_name: str):
        """
        Initialize FirestoreAccountRepository.

        Args:
            db_client: Firestore client
            collection_name: Full collection name (e.g., "dev_accounts_oauth")
        """
        self.db_client = db_client
        # ADR-006: collection_name is passed from environment.py (e.g. domain_accounts_v2)
        self.accounts_collection = db_client.collection(collection_name)

    async def get_account(self, account_id: str) -> Optional[BillingAccount]:
        doc_ref = self.accounts_collection.document(account_id)
        snapshot = await doc_ref.get()
        if not snapshot.exists:
            return None
        return BillingAccount(**snapshot.to_dict())

    async def create_account(self, account: BillingAccount) -> BillingAccount:
        doc_ref = self.accounts_collection.document(account.account_id)
        data = account.model_dump()
        await doc_ref.set(data)
        return account

    async def update_account(self, account: BillingAccount) -> BillingAccount:
        doc_ref = self.accounts_collection.document(account.account_id)
        data = account.model_dump()
        await doc_ref.set(data)
        return account

    async def increment_account_usage(self, account_id: str, tokens: int, cost: float) -> None:
        doc_ref = self.accounts_collection.document(account_id)
        now = datetime.now(timezone.utc)

        @firestore.async_transactional
        async def _transaction(transaction):
            snapshot = await doc_ref.get(transaction=transaction)
            if not snapshot.exists:
                raise ValueError(f"Account {account_id} not found")

            data = snapshot.to_dict()
            usage = data.get("usage", {})
            daily_reset_at = usage.get("daily_reset_at")
            monthly_reset_at = usage.get("monthly_reset_at")

            daily_needs_reset = True
            if daily_reset_at:
                daily_needs_reset = (now.date() != daily_reset_at.date())

            monthly_needs_reset = True
            if monthly_reset_at:
                monthly_needs_reset = (now.year, now.month) != (monthly_reset_at.year, monthly_reset_at.month)

            updates = {
                "usage.total_tokens": firestore.Increment(tokens),
                "usage.total_cost": firestore.Increment(cost),
                "usage.total_requests": firestore.Increment(1),
            }

            if daily_needs_reset:
                # Snapshot yesterday's totals before resetting
                updates.update({
                    "usage.prev_daily_tokens": usage.get("daily_tokens", 0),
                    "usage.prev_daily_cost": usage.get("daily_cost", 0.0),
                    "usage.daily_tokens": tokens,
                    "usage.daily_cost": cost,
                    "usage.daily_reset_at": now,
                })
            else:
                updates.update({
                    "usage.daily_tokens": firestore.Increment(tokens),
                    "usage.daily_cost": firestore.Increment(cost),
                })

            if monthly_needs_reset:
                updates.update({
                    "usage.monthly_tokens": tokens,
                    "usage.monthly_cost": cost,
                    "usage.monthly_reset_at": now,
                })
            else:
                updates.update({
                    "usage.monthly_tokens": firestore.Increment(tokens),
                    "usage.monthly_cost": firestore.Increment(cost),
                })

            transaction.update(doc_ref, updates)

        transaction = self.db_client.transaction()
        await _transaction(transaction)

    async def list_all_accounts(self) -> List[BillingAccount]:
        docs = self.accounts_collection.where(
            filter=FieldFilter("is_active", "==", True)
        ).stream()
        result = []
        async for doc in docs:
            result.append(BillingAccount(**doc.to_dict()))
        return result

    async def check_quota(self, account_id: str) -> Tuple[bool, str]:
        account = await self.get_account(account_id)
        if not account:
            return False, "Account not found"

        if not account.is_active:
            return False, "Account inactive"

        if account.usage.daily_tokens >= account.daily_token_limit:
            return False, "Daily token quota exceeded"

        if account.usage.monthly_cost >= account.monthly_cost_limit:
            return False, "Monthly cost quota exceeded"

        return True, ""
