from datetime import datetime, timezone
from typing import List, Optional, Tuple

from google.cloud import firestore
from google.cloud.firestore import FieldFilter

from ..domain.billing import (
    DEFAULT_DAILY_COST_LIMIT,
    BillingAccount,
    UsageIncrement,
)
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

    async def increment_account_usage(
        self, account_id: str, tokens: int, cost: float
    ) -> UsageIncrement:
        """Increment usage; report the resulting daily-spend position.

        Concurrent specialist executions all land here for the same account document.
        A read-modify-write transaction made them abort each other and exhaust Firestore's
        5 attempts (usage silently lost, prod log audit C-04), so the hot path is lock-free:
        server-side `Increment`s with no transaction. A transaction is kept only for the rare
        increment that has to rotate the daily or monthly window — that decision, and the
        snapshot of the day that ended, must be made once.

        The return value exists so a caller can raise a budget alert without a second read.
        On the lock-free path it comes from the server's own post-increment value, so
        concurrent callers see distinct, ordered `before`/`after` intervals and exactly one
        of them crosses the limit.
        """
        doc_ref = self.accounts_collection.document(account_id)
        now = datetime.now(timezone.utc)

        snapshot = await doc_ref.get()
        if not snapshot.exists:
            raise ValueError(f"Account {account_id} not found")
        data = snapshot.to_dict()
        usage = data.get("usage", {})

        daily_rotates, monthly_rotates = self._rotations_due(usage, now)
        if daily_rotates or monthly_rotates:
            return await self._increment_with_rotation(doc_ref, account_id, tokens, cost, now)

        updates = {
            "usage.total_tokens": firestore.Increment(tokens),
            "usage.total_cost": firestore.Increment(cost),
            "usage.total_requests": firestore.Increment(1),
            "usage.daily_tokens": firestore.Increment(tokens),
            "usage.daily_cost": firestore.Increment(cost),
            "usage.monthly_tokens": firestore.Increment(tokens),
            "usage.monthly_cost": firestore.Increment(cost),
        }
        write_result = await doc_ref.update(updates)

        daily_cost_after = self._applied_value(write_result, updates, "usage.daily_cost")
        if daily_cost_after is None:
            # The write succeeded; only the alert's accuracy degrades.
            logger.warning(
                "usage increment for %s returned no usable transform results; "
                "estimating the daily position from the pre-read", account_id,
            )
            daily_cost_after = usage.get("daily_cost", 0.0) + cost

        return UsageIncrement(
            daily_cost_before=daily_cost_after - cost,
            daily_cost_after=daily_cost_after,
            daily_cost_limit=data.get("daily_cost_limit", DEFAULT_DAILY_COST_LIMIT),
        )

    @staticmethod
    def _rotations_due(usage: dict, now: datetime) -> Tuple[bool, bool]:
        daily_reset_at = usage.get("daily_reset_at")
        monthly_reset_at = usage.get("monthly_reset_at")
        daily = daily_reset_at is None or now.date() != daily_reset_at.date()
        monthly = monthly_reset_at is None or (now.year, now.month) != (
            monthly_reset_at.year, monthly_reset_at.month
        )
        return daily, monthly

    @staticmethod
    def _applied_value(write_result, updates: dict, field_path: str) -> Optional[float]:
        """The server's value of `field_path` after its Increment.

        `transform_results` come back in the alphabetical order of the transformed field
        paths (probed against Firestore 2026-10-07: insertion order is NOT used).
        """
        results = list(getattr(write_result, "transform_results", None) or [])
        paths = sorted(updates)
        if len(results) != len(paths):
            return None
        value = results[paths.index(field_path)]
        return float(value.double_value or value.integer_value)

    async def _increment_with_rotation(
        self, doc_ref, account_id: str, tokens: int, cost: float, now: datetime
    ) -> UsageIncrement:
        @firestore.async_transactional
        async def _transaction(transaction) -> UsageIncrement:
            snapshot = await doc_ref.get(transaction=transaction)
            if not snapshot.exists:
                raise ValueError(f"Account {account_id} not found")

            data = snapshot.to_dict()
            usage = data.get("usage", {})
            daily_reset_at = usage.get("daily_reset_at")
            daily_needs_reset, monthly_needs_reset = self._rotations_due(usage, now)

            updates = {
                "usage.total_tokens": firestore.Increment(tokens),
                "usage.total_cost": firestore.Increment(cost),
                "usage.total_requests": firestore.Increment(1),
            }

            # Daily spend before/after this increment. On a rotation the day starts
            # fresh, so "before" is 0 regardless of the counter's leftover value.
            daily_cost_before = 0.0 if daily_needs_reset else usage.get("daily_cost", 0.0)

            if daily_needs_reset:
                # Snapshot the day that just ended before resetting. Stamp its
                # calendar date so a clock-driven report can tell whether this
                # snapshot actually belongs to "yesterday" (see
                # AccountUsageStats.usage_for_date).
                prev_date = daily_reset_at.date().isoformat() if daily_reset_at else None
                updates.update({
                    "usage.prev_daily_tokens": usage.get("daily_tokens", 0),
                    "usage.prev_daily_cost": usage.get("daily_cost", 0.0),
                    "usage.prev_daily_date": prev_date,
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

            return UsageIncrement(
                daily_cost_before=daily_cost_before,
                daily_cost_after=daily_cost_before + cost,
                daily_cost_limit=data.get("daily_cost_limit", DEFAULT_DAILY_COST_LIMIT),
            )

        transaction = self.db_client.transaction()
        return await _transaction(transaction)

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
